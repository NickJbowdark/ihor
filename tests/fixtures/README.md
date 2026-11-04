# Test fixtures

## `synthetic/` -- hand-written, NOT real data
These files imitate the documented formats of each source (RSS 2.0 / Atom,
ReliefWeb API v2 JSON, GDELT DOC ArtList JSON). They were written by hand
because the development sandbox could not reach the sources. Every title
starts with `[TEST FIXTURE]` so they can never be mistaken for real events.
They contain deliberate edge cases (unsafe links, HTML in text, missing dates,
wire-agency markers, updated items).

## `recorded/` -- real responses, recorded on YOUR machine
Run, for example:

    python -m gim.collect --source un_news --record --dry-run

The raw response is saved here as `<source>__<timestamp>__<file>`. The test
suite automatically parses every recorded file (`tests/test_recorded.py`), so
a format change at the source shows up as a failing test. Recorded files
contain third-party content: they are git-ignored by default -- commit them
only if the source's licence allows it.
