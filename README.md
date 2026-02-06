# arranger
MIDI sequencer/arranger in Python/Flask with web-based UI

Requirements (beyond requirements.txt):

For using soundfonts you'll want to install fluidsynth (`apt-get install fluidsynth`)
For rendering mp3 files you'll want to install ffmpeg (`apt-get install ffmpeg`)

Put soundfont files in the instruments/ directory, then run the server: `python arranger.py`

Then you can use the software by navigating to port 5000 on localhost.

In principle this could be run as an external server, but I don't recommend that - it would be pretty easy to DoS by asking the server to render some huge MIDI sequence for example.

== Notes on development ==

This was written more or less entirely using Claude, partially as an experiment to see how well it would work and partially because there are some features I wanted - namely the ability to define and arrange sub-patterns easily, as well as to easily transpose them - that were missing from LMMS. The result was surprisingly good, so it felt like a waste not to share it. It probably needs a better name than 'arranger' honestly, but this is somewhat as-is.

There are some curious things that happened during the process of generating this which I'll note here for posterity or in case anyone is interested more in the development pattern than the product itself.

Opus 4.6 was used for the initial pass, making most of the program in about 23% of a session's worth of tokens. This was my second test with the Opus 4.6 model - the first being debugging another piece of code, which suggested to me that it'd be pretty inefficient. But surprisingly it got most of the way to feature-complete in a single pass. Sort of weirdly, it decided spontaneously to make this with flask rather than e.g. tkinter or PyQt5; that decision was made when it was initially checking the development environment. Since I was doing this with the web interface, it couldn't install packages, but evidently flask was available so flask is what we got. This had some consequences later on.

The only feature that wasn't present and working in the initial pass was, amusingly, and actual ability to play the thing you're working on. Saving as MIDI, MP3, etc was all fine, but no play button. I made the mistake of asking Opus 4.6 in the same context to add a play button. It totally rewrote the code and burned 63% of a session. Lesson being - its better to use a model like Sonnet for edits like that. 

Following that I made a number of smaller adjustments and tweaks using Sonnet 4.5, which were successful at the cost of only a few percent of a session. These were things like UI elements being misaligned, wanting consistent right click = delete behavior both for patterns and notes, and some stuff with the piano roll scrollbar.

After playing around with that a bit, I decided I wanted something like the separate beat editor from LMMS. This was a big feature and I was concerned that if I just asked Sonnet to do it in one go it'd have a bunch of bugs or cause the code to expand too much. So I tried a pattern using Opus 4.6 to draft and implementation plan (which ended up having 5 parts), and then stepped through this plan with Sonnet. This took a few hours and was very token efficient - between the small adjustments and this new feature, including using Opus to plan, it took about 70% of a session. The new feature did increase the size of template.html by about 600 lines all told though (given that the entire rest of the editor fit under 1000 lines, this is relatively a lot).

During all of this I had a general preference set to write short 'researcher-style' code where possible. What I got looks like it was run through an obfuscator, which is a bit of a problem for anyone who wants to actually read and extend this, but turned out to be pretty efficient in terms of Claude (which had no problem with four-letter functions and two-letter variable names). Since this repo is mostly to make this particular artifact available to others and not to act as a locus of development, I'm not correcting that at this stage. It might be interesting to maintain a parallel translated code that is expanded to be more readable and documented. From a meta perspective, I'll have to think if there's some way to allow translation back and forth between compact, AI-friendly forms and the expanded, human-friendly forms in the future.
