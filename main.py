#!/usr/bin/env python3
"""Music Arranger - Standalone Desktop Application.

A pattern-based MIDI sequencer with piano roll editor, beat grid editor,
and arrangement timeline. Built with PySide6.

Usage:
    python main.py [--instruments DIR]        # from project root
    python -m standalone.main [--instruments DIR]
"""
import argparse
import sys
from pathlib import Path

from PySide6.QtWidgets import QApplication

# Ensure the project root (this file's directory) is on sys.path so that
# `import standalone` works regardless of how the script is invoked.
_root = str(Path(__file__).resolve().parent)
if _root not in sys.path:
    sys.path.insert(0, _root)

# Set __package__ so relative imports inside standalone/ work when this
# file is run directly rather than via `python -m standalone.main`.
if not __package__:
    __package__ = "standalone"


def main():
    parser = argparse.ArgumentParser(description='Music Arranger - Standalone')
    parser.add_argument('--instruments', type=str, default=None,
                        help='Path to instruments directory containing .sf2 files')
    parser.add_argument('--debug', action='store_true',
                        help='Enable widget lifecycle debug hooks (logs to widget_debug.log)')
    parser.add_argument('--debug-verbose', action='store_true',
                        help='Verbose widget debug (logs every risky event dispatch)')
    args = parser.parse_args()

    instruments_dir = args.instruments
    if instruments_dir is None:
        # Default: instruments/ directory next to the project root
        instruments_dir = str(Path(__file__).parent / 'instruments')

    # Install debug hooks BEFORE QApplication so deleteLater patch is ready
    if args.debug or args.debug_verbose:
        from .debug_widgets import install_hooks, install_event_filter
        import standalone.debug_widgets as dw
        if args.debug_verbose:
            dw.VERBOSE = True
        install_hooks()

    app = QApplication(sys.argv)
    
    # Set application style
    app.setStyle('Fusion')

    # Install event filter now that QApp exists
    if args.debug or args.debug_verbose:
        install_event_filter()
    
    # Import here to avoid circular imports
    from .app import App
    main_window = App(instruments_dir=instruments_dir)
    main_window.show()

    sys.exit(app.exec())


if __name__ == '__main__':
    main()
