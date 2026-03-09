#!/usr/bin/env python3
"""
Train a DDSP (Differentiable Digital Signal Processing) decoder and export
to ONNX for the Arranger DDSP synthesizer plugin.

The model learns to map (f0, loudness) -> (harmonic_amplitudes, noise_magnitudes,
amplitude) from mono audio files.  At inference time the plugin feeds live MIDI
pitch/velocity and the decoder returns DSP parameters every ~10ms.

Usage
-----
    # Train on a directory of .wav files:
    python scripts/train_ddsp.py --audio_dir /path/to/wavs --output_dir models/my_ddsp

    # Resume from checkpoint:
    python scripts/train_ddsp.py --audio_dir /path/to/wavs --output_dir models/my_ddsp \
        --resume models/my_ddsp/checkpoint.pt

    # Quick test with synthetic data (no audio files needed):
    python scripts/train_ddsp.py --synthetic --output_dir models/test_ddsp --epochs 50

Output (in --output_dir):
    decoder.onnx   - ONNX model for the plugin
    config.json    - model config (num_harmonics, num_noise_bands, etc.)
    checkpoint.pt  - PyTorch checkpoint for resuming training

Dependencies (NOT required by the main app):
    pip install -r scripts/requirements-ddsp.txt
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
from pathlib import Path

import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.utils.data import Dataset, DataLoader
from tqdm import tqdm

# ---------------------------------------------------------------------------
# Feature extraction (f0 + loudness from audio)
# ---------------------------------------------------------------------------

def extract_f0(audio: np.ndarray, sr: int, frame_rate: int) -> np.ndarray:
    """Extract f0 contour using librosa's pyin."""
    import librosa
    hop = sr // frame_rate
    f0, voiced, _ = librosa.pyin(
        audio, fmin=50.0, fmax=2000.0, sr=sr, hop_length=hop,
        fill_na=0.0,
    )
    return f0.astype(np.float32)


def extract_loudness(audio: np.ndarray, sr: int, frame_rate: int) -> np.ndarray:
    """Extract A-weighted loudness in dB, normalised to roughly [-1, 0]."""
    import librosa
    hop = sr // frame_rate
    S = np.abs(librosa.stft(audio, n_fft=2048, hop_length=hop))
    # A-weighting approximation: just use power dB for simplicity
    power = np.mean(S ** 2, axis=0)
    db = librosa.power_to_db(power, ref=1.0, top_db=120.0)
    # Normalise to roughly [-1, 0]
    db = (db - db.max()) / 120.0
    return db.astype(np.float32)


def extract_harmonic_amplitudes(
    audio: np.ndarray, f0: np.ndarray, sr: int, frame_rate: int,
    n_harmonics: int,
) -> np.ndarray:
    """
    Extract ground-truth harmonic amplitudes via short-time DFT at harmonic
    frequencies.  Returns shape (n_frames, n_harmonics), linear amplitude.
    """
    hop = sr // frame_rate
    n_frames = len(f0)
    n_fft = 2048
    amps = np.zeros((n_frames, n_harmonics), dtype=np.float32)

    for t in range(n_frames):
        if f0[t] < 20.0:
            continue
        centre = t * hop
        start = max(0, centre - n_fft // 2)
        end = min(len(audio), start + n_fft)
        seg = np.zeros(n_fft, dtype=np.float32)
        actual = audio[start:end]
        seg[:len(actual)] = actual
        seg *= np.hanning(n_fft).astype(np.float32)
        spec = np.fft.rfft(seg)
        freqs = np.fft.rfftfreq(n_fft, 1.0 / sr)

        for h in range(n_harmonics):
            hf = f0[t] * (h + 1)
            if hf > sr / 2:
                break
            # Find nearest bin
            idx = int(round(hf / (sr / n_fft)))
            if 0 <= idx < len(spec):
                amps[t, h] = np.abs(spec[idx]) / (n_fft / 2)

    return amps


def extract_noise_magnitudes(
    audio: np.ndarray, sr: int, frame_rate: int, n_noise_bands: int,
) -> np.ndarray:
    """
    Extract noise magnitude envelope in mel-spaced bands.
    Returns shape (n_frames, n_noise_bands), linear magnitude.
    """
    import librosa
    hop = sr // frame_rate
    S = np.abs(librosa.stft(audio, n_fft=2048, hop_length=hop))
    # Create mel filterbank and apply
    mel_fb = librosa.filters.mel(sr=sr, n_fft=2048, n_mels=n_noise_bands)
    mel_S = mel_fb @ S  # (n_noise_bands, n_frames)
    # Normalise per frame
    mel_S = mel_S.T.astype(np.float32)  # (n_frames, n_noise_bands)
    # Mild compression
    mel_S = np.sqrt(mel_S + 1e-8)
    return mel_S


# ---------------------------------------------------------------------------
# Dataset
# ---------------------------------------------------------------------------

class AudioDataset(Dataset):
    """Loads audio files, extracts features, returns fixed-length segments."""

    def __init__(
        self, audio_dir: str | None, sr: int, frame_rate: int,
        n_harmonics: int, n_noise_bands: int,
        segment_frames: int = 200, synthetic: bool = False,
    ):
        self.sr = sr
        self.frame_rate = frame_rate
        self.n_harmonics = n_harmonics
        self.n_noise_bands = n_noise_bands
        self.segment_frames = segment_frames
        self.segments: list[dict[str, np.ndarray]] = []

        if synthetic:
            self._generate_synthetic(n_segments=500)
        else:
            self._load_audio_dir(audio_dir)

    def _load_audio_dir(self, audio_dir: str | None):
        if audio_dir is None:
            raise ValueError("--audio_dir is required when not using --synthetic")
        import librosa
        audio_dir = Path(audio_dir)
        files = sorted(
            p for p in audio_dir.rglob("*")
            if p.suffix.lower() in (".wav", ".flac", ".ogg", ".mp3", ".aiff", ".aif")
        )
        if not files:
            raise FileNotFoundError(f"No audio files in {audio_dir}")

        print(f"Found {len(files)} audio file(s)")
        for path in tqdm(files, desc="Extracting features"):
            try:
                audio, _ = librosa.load(str(path), sr=self.sr, mono=True)
            except Exception as e:
                print(f"  Skipping {path.name}: {e}")
                continue
            if len(audio) < self.sr:
                continue

            f0 = extract_f0(audio, self.sr, self.frame_rate)
            loudness = extract_loudness(audio, self.sr, self.frame_rate)
            harm = extract_harmonic_amplitudes(
                audio, f0, self.sr, self.frame_rate, self.n_harmonics)
            noise = extract_noise_magnitudes(
                audio, self.sr, self.frame_rate, self.n_noise_bands)

            n = min(len(f0), len(loudness), harm.shape[0], noise.shape[0])
            f0, loudness, harm, noise = f0[:n], loudness[:n], harm[:n], noise[:n]

            # Overall amplitude envelope
            amp = np.sqrt(np.sum(harm ** 2, axis=1, keepdims=False) + 1e-8)

            # Normalise harmonic amps to distribution (sum=1 per frame)
            harm_sum = harm.sum(axis=1, keepdims=True)
            harm_dist = np.where(harm_sum > 1e-8, harm / harm_sum, 0.0).astype(np.float32)

            # Chop into fixed-length segments
            for start in range(0, n - self.segment_frames, self.segment_frames // 2):
                end = start + self.segment_frames
                self.segments.append({
                    "f0": f0[start:end],
                    "loudness": loudness[start:end],
                    "harmonic_dist": harm_dist[start:end],
                    "noise_mags": noise[start:end],
                    "amplitude": amp[start:end],
                })

        print(f"Extracted {len(self.segments)} training segments")

    def _generate_synthetic(self, n_segments: int = 500):
        """Generate synthetic training data for testing the pipeline."""
        print(f"Generating {n_segments} synthetic segments...")
        rng = np.random.default_rng(42)
        for _ in range(n_segments):
            n = self.segment_frames
            # Random f0 contour with vibrato
            base_f0 = rng.uniform(100, 800)
            vibrato = 5.0 * np.sin(2 * np.pi * 5.5 * np.arange(n) / self.frame_rate)
            f0 = np.full(n, base_f0, dtype=np.float32) + vibrato.astype(np.float32)

            loudness = rng.uniform(-0.5, 0.0, size=n).astype(np.float32)
            amp = (10.0 ** (loudness * 2)).astype(np.float32)  # rough dB->linear

            # Harmonic distribution: decaying with some randomness
            harm_dist = np.zeros((n, self.n_harmonics), dtype=np.float32)
            decay = np.exp(-np.arange(self.n_harmonics) * rng.uniform(0.02, 0.15))
            for t in range(n):
                h = decay * (1.0 + 0.1 * rng.standard_normal(self.n_harmonics))
                h = np.maximum(h, 0.0)
                s = h.sum()
                harm_dist[t] = (h / s) if s > 0 else 0.0

            noise_mags = rng.uniform(0.0, 0.05, size=(n, self.n_noise_bands)).astype(
                np.float32)

            self.segments.append({
                "f0": f0,
                "loudness": loudness,
                "harmonic_dist": harm_dist.astype(np.float32),
                "noise_mags": noise_mags,
                "amplitude": amp,
            })

    def __len__(self):
        return len(self.segments)

    def __getitem__(self, idx):
        seg = self.segments[idx]
        return {k: torch.from_numpy(v) for k, v in seg.items()}


# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------

class DDSPDecoder(nn.Module):
    """
    Maps (f0, loudness) -> (harmonic_amplitudes, noise_magnitudes, amplitude).

    Architecture: GRU backbone with MLP heads.  Small enough for realtime
    frame-by-frame inference (~1-2ms per frame on CPU).
    """

    def __init__(
        self, n_harmonics: int = 60, n_noise_bands: int = 65,
        hidden_size: int = 256, n_layers: int = 1, z_dim: int = 0,
    ):
        super().__init__()
        self.n_harmonics = n_harmonics
        self.n_noise_bands = n_noise_bands
        self.z_dim = z_dim

        input_dim = 2 + z_dim  # f0 + loudness + optional z
        self.input_mlp = nn.Sequential(
            nn.Linear(input_dim, hidden_size),
            nn.LayerNorm(hidden_size),
            nn.ReLU(),
            nn.Linear(hidden_size, hidden_size),
            nn.LayerNorm(hidden_size),
            nn.ReLU(),
        )
        self.gru = nn.GRU(hidden_size, hidden_size, num_layers=n_layers, batch_first=True)
        self.harmonic_head = nn.Sequential(
            nn.Linear(hidden_size, hidden_size),
            nn.ReLU(),
            nn.Linear(hidden_size, n_harmonics),
        )
        self.noise_head = nn.Sequential(
            nn.Linear(hidden_size, hidden_size // 2),
            nn.ReLU(),
            nn.Linear(hidden_size // 2, n_noise_bands),
        )
        self.amp_head = nn.Sequential(
            nn.Linear(hidden_size, hidden_size // 2),
            nn.ReLU(),
            nn.Linear(hidden_size // 2, 1),
        )

    def forward(
        self, f0: torch.Tensor, loudness: torch.Tensor,
        z: torch.Tensor | None = None,
    ) -> tuple[torch.Tensor, torch.Tensor, torch.Tensor]:
        """
        Args:
            f0:       (B, T) or (B, T, 1) — fundamental frequency in Hz
            loudness: (B, T) or (B, T, 1) — normalised loudness
            z:        (B, T, Z) or None    — optional latent code

        Returns:
            harmonic_amplitudes: (B, T, n_harmonics)  — softmax distribution
            noise_magnitudes:    (B, T, n_noise_bands) — sigmoid [0,1]
            amplitude:           (B, T, 1)             — exp, positive
        """
        if f0.dim() == 2:
            f0 = f0.unsqueeze(-1)
        if loudness.dim() == 2:
            loudness = loudness.unsqueeze(-1)

        # Normalise f0 to log scale centred around A4
        f0_norm = torch.log2(f0.clamp(min=20.0) / 440.0)

        x = torch.cat([f0_norm, loudness], dim=-1)
        if z is not None:
            if z.dim() == 2:
                z = z.unsqueeze(1).expand(-1, x.shape[1], -1)
            x = torch.cat([x, z], dim=-1)

        x = self.input_mlp(x)
        x, _ = self.gru(x)

        harm = F.softmax(self.harmonic_head(x), dim=-1)
        noise = torch.sigmoid(self.noise_head(x)) * 0.1  # scale down noise
        amp = torch.exp(self.amp_head(x))  # positive amplitude

        return harm, noise, amp


class DDSPDecoderSingleFrame(nn.Module):
    """
    Wrapper that accepts single-frame inputs (B=1, T=1) and returns
    squeezed outputs matching the plugin's expected tensor shapes.

    This is the model exported to ONNX.
    """

    def __init__(self, decoder: DDSPDecoder):
        super().__init__()
        self.decoder = decoder
        # Pre-allocate hidden state as a buffer so it persists across calls
        self.register_buffer(
            "h0",
            torch.zeros(decoder.gru.num_layers, 1, decoder.gru.hidden_size),
        )

    def forward(
        self, f0: torch.Tensor, loudness: torch.Tensor,
        z: torch.Tensor | None = None,
    ) -> tuple[torch.Tensor, torch.Tensor, torch.Tensor]:
        """
        Args:
            f0:       (1, 1)
            loudness: (1, 1)
            z:        (1, Z_DIM) or absent

        Returns:
            harmonic_amplitudes: (1, N_HARMONICS)
            noise_magnitudes:    (1, N_NOISE_BANDS)
            amplitude:           (1, 1)
        """
        harm, noise, amp = self.decoder(
            f0.unsqueeze(0) if f0.dim() == 2 else f0,
            loudness.unsqueeze(0) if loudness.dim() == 2 else loudness,
            z,
        )
        # Squeeze time dimension: (1, 1, C) -> (1, C)
        return harm.squeeze(1), noise.squeeze(1), amp.squeeze(1)


# ---------------------------------------------------------------------------
# Loss
# ---------------------------------------------------------------------------

def spectral_loss(
    pred_harm: torch.Tensor, pred_noise: torch.Tensor, pred_amp: torch.Tensor,
    gt_harm: torch.Tensor, gt_noise: torch.Tensor, gt_amp: torch.Tensor,
) -> torch.Tensor:
    """
    Combined loss:
      - KL divergence on harmonic distribution
      - L1 on noise magnitudes
      - L1 on log amplitude
    """
    # Harmonic distribution loss (modified cross-entropy)
    eps = 1e-7
    harm_loss = -torch.sum(gt_harm * torch.log(pred_harm + eps), dim=-1).mean()

    # Noise magnitude loss
    noise_loss = F.l1_loss(pred_noise, gt_noise)

    # Amplitude loss (in log domain)
    amp_loss = F.l1_loss(
        torch.log(pred_amp.clamp(min=eps)),
        torch.log(gt_amp.clamp(min=eps).unsqueeze(-1)),
    )

    return harm_loss + 10.0 * noise_loss + amp_loss


# ---------------------------------------------------------------------------
# Training loop
# ---------------------------------------------------------------------------

def train(args: argparse.Namespace):
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    print(f"Device: {device}")

    # Dataset
    dataset = AudioDataset(
        audio_dir=args.audio_dir,
        sr=args.sample_rate,
        frame_rate=args.frame_rate,
        n_harmonics=args.n_harmonics,
        n_noise_bands=args.n_noise_bands,
        segment_frames=args.segment_frames,
        synthetic=args.synthetic,
    )
    if len(dataset) == 0:
        print("ERROR: No training data. Provide --audio_dir with audio files or use --synthetic.")
        sys.exit(1)

    loader = DataLoader(
        dataset, batch_size=args.batch_size, shuffle=True,
        num_workers=min(4, os.cpu_count() or 1), pin_memory=(device.type == "cuda"),
    )

    # Model
    model = DDSPDecoder(
        n_harmonics=args.n_harmonics,
        n_noise_bands=args.n_noise_bands,
        hidden_size=args.hidden_size,
        n_layers=args.n_layers,
        z_dim=args.z_dim,
    ).to(device)

    n_params = sum(p.numel() for p in model.parameters())
    print(f"Model: {n_params:,} parameters")

    optimiser = torch.optim.Adam(model.parameters(), lr=args.lr)
    scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(optimiser, T_max=args.epochs)

    # Resume
    start_epoch = 0
    if args.resume and Path(args.resume).exists():
        ckpt = torch.load(args.resume, map_location=device, weights_only=False)
        model.load_state_dict(ckpt["model"])
        optimiser.load_state_dict(ckpt["optimiser"])
        start_epoch = ckpt.get("epoch", 0) + 1
        print(f"Resumed from epoch {start_epoch}")

    # Train
    os.makedirs(args.output_dir, exist_ok=True)
    best_loss = float("inf")

    for epoch in range(start_epoch, args.epochs):
        model.train()
        total_loss = 0.0
        n_batches = 0

        pbar = tqdm(loader, desc=f"Epoch {epoch+1}/{args.epochs}", leave=False)
        for batch in pbar:
            f0 = batch["f0"].to(device)
            loudness = batch["loudness"].to(device)
            gt_harm = batch["harmonic_dist"].to(device)
            gt_noise = batch["noise_mags"].to(device)
            gt_amp = batch["amplitude"].to(device)

            pred_harm, pred_noise, pred_amp = model(f0, loudness)
            loss = spectral_loss(pred_harm, pred_noise, pred_amp,
                                 gt_harm, gt_noise, gt_amp)

            optimiser.zero_grad()
            loss.backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
            optimiser.step()

            total_loss += loss.item()
            n_batches += 1
            pbar.set_postfix(loss=f"{loss.item():.4f}")

        scheduler.step()
        avg_loss = total_loss / max(n_batches, 1)
        print(f"Epoch {epoch+1}/{args.epochs}  loss={avg_loss:.4f}  lr={scheduler.get_last_lr()[0]:.2e}")

        # Checkpoint
        ckpt = {
            "epoch": epoch,
            "model": model.state_dict(),
            "optimiser": optimiser.state_dict(),
            "args": vars(args),
        }
        torch.save(ckpt, os.path.join(args.output_dir, "checkpoint.pt"))

        if avg_loss < best_loss:
            best_loss = avg_loss
            torch.save(ckpt, os.path.join(args.output_dir, "best.pt"))

    print(f"Training complete. Best loss: {best_loss:.4f}")

    # Export
    export_onnx(model, args)


# ---------------------------------------------------------------------------
# ONNX export
# ---------------------------------------------------------------------------

def export_onnx(model: DDSPDecoder, args: argparse.Namespace):
    """Export the decoder to ONNX with the tensor interface the plugin expects."""
    import onnx

    model.eval()
    model.cpu()

    wrapper = DDSPDecoderSingleFrame(model)
    wrapper.eval()

    # Dummy inputs matching plugin's inference call
    f0 = torch.tensor([[440.0]])        # (1, 1)
    loudness = torch.tensor([[0.5]])    # (1, 1)

    input_names = ["f0", "loudness"]
    dynamic_axes = {}
    dummy_inputs = (f0, loudness)

    if args.z_dim > 0:
        z = torch.zeros(1, args.z_dim)  # (1, Z_DIM)
        dummy_inputs = (f0, loudness, z)
        input_names.append("z")

    output_names = ["harmonic_amplitudes", "noise_magnitudes", "amplitude"]

    onnx_path = os.path.join(args.output_dir, "decoder.onnx")

    # Use dynamo=False to force the legacy TorchScript-based exporter,
    # which doesn't require the onnxscript package.
    export_kwargs: dict = dict(
        input_names=input_names,
        output_names=output_names,
        dynamic_axes=dynamic_axes,
        opset_version=17,
        do_constant_folding=True,
    )

    import inspect
    sig = inspect.signature(torch.onnx.export)
    if "dynamo" in sig.parameters:
        export_kwargs["dynamo"] = False

    torch.onnx.export(wrapper, dummy_inputs, onnx_path, **export_kwargs)

    # Validate
    onnx_model = onnx.load(onnx_path)
    onnx.checker.check_model(onnx_model)

    # Write config.json
    config = {
        "sample_rate": args.sample_rate,
        "frame_rate": args.frame_rate,
        "num_harmonics": args.n_harmonics,
        "num_noise_bands": args.n_noise_bands,
        "z_dim": args.z_dim,
        "hidden_size": args.hidden_size,
        "n_layers": args.n_layers,
    }
    config_path = os.path.join(args.output_dir, "config.json")
    with open(config_path, "w") as f:
        json.dump(config, f, indent=2)

    size_mb = os.path.getsize(onnx_path) / (1024 * 1024)
    print(f"Exported: {onnx_path} ({size_mb:.1f} MB)")
    print(f"Config:   {config_path}")
    print(f"\nTo use in Arranger: set the DDSP node's model_dir to '{os.path.abspath(args.output_dir)}'")


# ---------------------------------------------------------------------------
# Export-only mode (from existing checkpoint)
# ---------------------------------------------------------------------------

def export_only(args: argparse.Namespace):
    """Load a checkpoint and export to ONNX without training."""
    ckpt_path = args.resume
    if not ckpt_path or not Path(ckpt_path).exists():
        print("ERROR: --resume path required for --export_only")
        sys.exit(1)

    ckpt = torch.load(ckpt_path, map_location="cpu", weights_only=False)
    saved_args = ckpt.get("args", {})

    # Use saved args as defaults, allow CLI overrides
    for k, v in saved_args.items():
        if not hasattr(args, k) or getattr(args, k) is None:
            setattr(args, k, v)

    model = DDSPDecoder(
        n_harmonics=args.n_harmonics,
        n_noise_bands=args.n_noise_bands,
        hidden_size=args.hidden_size,
        n_layers=args.n_layers,
        z_dim=args.z_dim,
    )
    model.load_state_dict(ckpt["model"])
    print(f"Loaded checkpoint from {ckpt_path}")

    os.makedirs(args.output_dir, exist_ok=True)
    export_onnx(model, args)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    p = argparse.ArgumentParser(
        description="Train a DDSP decoder and export to ONNX for the Arranger plugin.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )

    # Data
    p.add_argument("--audio_dir", type=str, default=None,
                    help="Directory of audio files (.wav/.flac/.ogg/.mp3) to train on.")
    p.add_argument("--synthetic", action="store_true",
                    help="Use synthetic training data (for testing the pipeline).")
    p.add_argument("--sample_rate", type=int, default=16000,
                    help="Audio sample rate for feature extraction.")
    p.add_argument("--frame_rate", type=int, default=100,
                    help="Feature frames per second (inference rate in plugin).")
    p.add_argument("--segment_frames", type=int, default=200,
                    help="Training segment length in frames (2s at 100fps).")

    # Model
    p.add_argument("--n_harmonics", type=int, default=60,
                    help="Number of harmonic partials.")
    p.add_argument("--n_noise_bands", type=int, default=65,
                    help="Number of noise filter bands.")
    p.add_argument("--hidden_size", type=int, default=256,
                    help="GRU + MLP hidden size.")
    p.add_argument("--n_layers", type=int, default=1,
                    help="Number of GRU layers.")
    p.add_argument("--z_dim", type=int, default=0,
                    help="Latent z dimension (0 = no z input).")

    # Training
    p.add_argument("--epochs", type=int, default=200)
    p.add_argument("--batch_size", type=int, default=32)
    p.add_argument("--lr", type=float, default=1e-3)
    p.add_argument("--resume", type=str, default=None,
                    help="Path to checkpoint.pt to resume from.")

    # Output
    p.add_argument("--output_dir", type=str, required=True,
                    help="Directory for decoder.onnx, config.json, checkpoint.pt.")
    p.add_argument("--export_only", action="store_true",
                    help="Skip training, just export from --resume checkpoint.")

    args = p.parse_args()

    if args.export_only:
        export_only(args)
    else:
        train(args)


if __name__ == "__main__":
    main()
