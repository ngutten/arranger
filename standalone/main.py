#!/usr/bin/env python3
"""Music Arranger - Standalone Desktop Application.

A pattern-based MIDI sequencer with piano roll editor, beat grid editor,
and arrangement timeline. Built with tkinter.

Usage:
    python -m standalone.main [--instruments DIR]
"""

import argparse
import sys
import tkinter as tk
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description='Music Arranger - Standalone')
    parser.add_argument('--instruments', type=str, default=None,
                        help='Path to instruments directory containing .sf2 files')
    args = parser.parse_args()

    instruments_dir = args.instruments
    if instruments_dir is None:
        # Default: instruments/ directory next to the project root
        instruments_dir = str(Path(__file__).parent.parent / 'instruments')

    root = tk.Tk()

    # Import here to avoid circular imports
    from .app import App
    app = App(root, instruments_dir=instruments_dir)

    root.mainloop()


if __name__ == '__main__':
    main()
