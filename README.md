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

How to run:

- Put soundfont files in the instruments/ directory
- If using the built-in fluidsynth bindings, just run `python main.py` from the `standalone/` sub-directory.
- If you want to use the audio engine, currently you should build it and run `./audio_server` from `audio_server/build`. Eventually this will be automatically detected and performed by the Python interface, possibly implemented as a library with Python bindings.

Requirements (beyond requirements.txt):

For using soundfonts you'll want to install fluidsynth (`apt-get install fluidsynth`)
For rendering mp3 files you'll want to install ffmpeg (`apt-get install ffmpeg`)

System libraries for basic fluidsynth synthesizer: fluidsynth, ffmpeg, libfluidsynth3, and portaudio19-dev

For the audio server backend, there are additional prerequisites (still in flux, will update when that's stable)

Note: This code was heavily developed using Claude Sonnet 4.5 and Opus 4.6, see DEVELOPMENT_NOTES.md for details.
