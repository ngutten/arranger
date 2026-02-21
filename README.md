# arranger
MIDI sequencer/arranger in Python

This is software intended for composing and rendering out songs as well as writing MIDI files meant to be used elsewhere. It has a Python frontend that uses either fluidsynth for rendering, or if you also build the included audio engine server (C++) can support a more complex node-based synthesis and filtering approach. 

Features:
- Write sequences of notes as 'patterns' which can be arranged, overlapped, and transposed.
- Beat grid editor for beat tracks
- Onion skin mode that shows what notes are playing at the same time as a pattern, so you can easily write counterpoint and harmony
- Record patterns from a MIDI device, with recording that syncs up to the first note played
- Render to MP3 (with ffmpeg), WAV, or MIDI
- Live editing, including of the synthesis graph
- Preliminary node-based synthesis for more complex rendering.

How to run (release):

- Put soundfont files in the instruments/ directory and off you go!

### Building from source

For the audio server backend, the prerequisites for building can be installed (e.g. with apt):

`
            sudo apt-get install -y \
            build-essential cmake pkg-config \
            python3-dev \
            libportaudio2 portaudio19-dev \
            libfluidsynth-dev \
            libsndfile1-dev \
            ffmpeg \
            wget \
            librsvg2-bin
`

Some of these overlap with things the Python frontend wants anyhow (portaudio, ffmpeg, fluidsynth).

- Run ./build.sh in the root directory of the project to build the audio server and plugins
- Run the project by running `pything main.py` from the project's root directory.

Note: This code was heavily developed using Claude Sonnet 4.5 and Opus 4.6, see DEVELOPMENT_NOTES.md for details.
