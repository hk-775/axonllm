#!/usr/bin/env python3
"""Stage 4 of 4: build the WebVTT caption track from the narration JSON.

    python3 scripts/demo/make_captions.py

Cue boundaries are the scene boundaries, which are the measured MP3 durations —
the same numbers the encoder used to set each scene's frame rate. So a cue cannot
drift from the voice: both are derived from the same file lengths rather than
timed by hand.

One cue per scene would put 40-odd words on screen at once, so each scene's text
is split into sentences and the scene's span is divided between them in
proportion to their length. Speech rate is near enough constant within a scene
for that to track the voice closely.
"""
from __future__ import annotations

import json
import re
from paths import CAPTIONS as OUT
from paths import NARRATION as SRC


def stamp(t: float) -> str:
    h, rem = divmod(t, 3600)
    m, s = divmod(rem, 60)
    return f"{int(h):02d}:{int(m):02d}:{s:06.3f}"


def sentences(text: str) -> list[str]:
    # Split after . ! ? followed by a space and a capital. Not on every period:
    # "$1.26", "1.4 seconds" and "MIT-0" all contain one.
    parts = re.split(r"(?<=[.!?])\s+(?=[A-Z])", text.strip())
    return [p.strip() for p in parts if p.strip()]


def main() -> None:
    scenes = json.loads(SRC.read_text())["scenes"]
    lines = ["WEBVTT", ""]
    t = 0.0
    n = 0
    for sc in scenes:
        span = sc["duration"]
        sents = sentences(sc["text"])
        total_chars = sum(len(s) for s in sents) or 1
        start = t
        for i, sent in enumerate(sents):
            share = span * len(sent) / total_chars
            end = start + share
            if i == len(sents) - 1:
                end = t + span  # absorb rounding so cues tile the scene exactly
            n += 1
            lines += [f"{n}", f"{stamp(start)} --> {stamp(end)}", sent, ""]
            start = end
        t += span
    OUT.write_text("\n".join(lines))
    print(f"{OUT}: {n} cues over {t:.2f}s")


if __name__ == "__main__":
    main()
