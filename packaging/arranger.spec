# -*- mode: python ; coding: utf-8 -*-
#
# PyInstaller spec for Arranger (onedir bundle).
#
# Usage:
#   pyinstaller packaging/arranger.spec
#
# Expects these build outputs to already exist (produced by CMake):
#   standalone/arranger_engine*.so  (Linux) or arranger_engine*.pyd (Windows)
#   plugins/arranger_plugin_*.so    (Linux) or arranger_plugin_*.dll (Windows)
#
# The bundle preserves the directory layout that binding_engine.py relies on
# for plugin discovery:
#   _MEIPASS/standalone/core/binding_engine.py   ← __file__
#   _MEIPASS/plugins/arranger_plugin_*            ← 3 levels up / "plugins"

import glob
import os
import sys
from pathlib import Path

# Project root is one level above this spec file (packaging/)
ROOT = Path(SPECPATH).parent

IS_WINDOWS = sys.platform == 'win32'

# ---------------------------------------------------------------------------
# Locate build outputs
# ---------------------------------------------------------------------------

# arranger_engine extension module (built by CMake into standalone/)
_engine_patterns = (
    list((ROOT / 'standalone').glob('arranger_engine*.pyd')) +   # Windows
    list((ROOT / 'standalone').glob('arranger_engine*.so'))       # Linux/macOS
)
if not _engine_patterns:
    raise RuntimeError(
        "arranger_engine extension not found in standalone/. "
        "Run CMake with -DENABLE_PYTHON_BINDINGS=ON first."
    )

# Dynamic plugin libraries (built by CMake into plugins/)
_plugin_ext = '*.dll' if IS_WINDOWS else '*.so'
_plugin_files = list((ROOT / 'plugins').glob(_plugin_ext))
if not _plugin_files:
    raise RuntimeError(
        f"No plugin libraries found in plugins/. "
        f"Run CMake build first."
    )

# Optional bundled ffmpeg binary
_ffmpeg_dir = ROOT / 'packaging' / 'ffmpeg'
_ffmpeg_files = list(_ffmpeg_dir.glob('ffmpeg*')) if _ffmpeg_dir.is_dir() else []

# ---------------------------------------------------------------------------
# Binaries and data
# ---------------------------------------------------------------------------

# Plugins: placed at _MEIPASS/plugins/
# binding_engine.py resolves:  __file__ → 3 parents up → _MEIPASS → / "plugins"
_plugin_binaries = [(str(p), 'plugins') for p in _plugin_files]

# arranger_engine extension: placed at _MEIPASS/standalone/
# (PyInstaller auto-discovers it via the import but we add it explicitly
# to ensure it's included even if static analysis misses the relative import)
_engine_binaries = [(str(p), 'standalone') for p in _engine_patterns]

_all_binaries = _engine_binaries + _plugin_binaries

_datas = [
    # Default project state loaded at startup
    (str(ROOT / 'defaults'), 'defaults'),
]

# Bundle ffmpeg if it was downloaded during the build
if _ffmpeg_files:
    _datas += [(str(f), '.') for f in _ffmpeg_files]

# ---------------------------------------------------------------------------
# Hidden imports
# PySide6 and scipy use many sub-modules that PyInstaller's static analysis
# may not discover through import scanning.
# ---------------------------------------------------------------------------
_hidden = [
    # PySide6 modules used by the app
    'PySide6.QtCore',
    'PySide6.QtGui',
    'PySide6.QtWidgets',
    'PySide6.QtOpenGL',
    'PySide6.QtOpenGLWidgets',
    'PySide6.QtMultimedia',
    'PySide6.QtPrintSupport',
    # scipy internals
    'scipy.signal',
    'scipy.signal.windows',
    'scipy._lib.messagestream',
    'scipy.special._ufuncs_cxx',
    # rtmidi
    'rtmidi',
    '_rtmidi',
    # sounddevice / PortAudio
    'sounddevice',
    '_sounddevice',
    # pyfluidsynth
    'fluidsynth',
    # numpy
    'numpy.core._multiarray_umath',
    'numpy.core._multiarray_tests',
]

a = Analysis(
    [str(ROOT / 'main.py')],
    pathex=[str(ROOT)],
    binaries=_all_binaries,
    datas=_datas,
    hiddenimports=_hidden,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        'tkinter',
        '_tkinter',
        'matplotlib',
        'PIL',
        'IPython',
        'jupyter',
        'notebook',
    ],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='arranger',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=not IS_WINDOWS,   # UPX can corrupt MinGW-built Windows PE; disable on Windows
    console=False,        # No terminal window; set to True for debugging
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    # Windows: embed an application manifest requesting DPI awareness
    uac_admin=False,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=not IS_WINDOWS,   # UPX can corrupt MinGW-built Windows PE; disable on Windows
    upx_exclude=[
        # Don't compress Qt libraries — UPX can corrupt them
        'Qt6*.dll',
        'Qt6*.so*',
        'PySide6/*.dll',
        'PySide6/*.so*',
        'arranger_engine*',
        'arranger_plugin_*',
    ],
    name='arranger',
)
