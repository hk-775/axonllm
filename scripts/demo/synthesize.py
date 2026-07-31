#!/usr/bin/env python3
"""Stage 1 of 4: synthesize the narration with Polly, measuring each track.

    python3 scripts/demo/synthesize.py

Each scene's audio length is what the recorder budgets frames against, so the
duration has to come from the file rather than an estimate: a scene padded to a
guessed length either freezes at the end or cuts the sentence off. ffprobe reads
it back from the MP3 after synthesis and this writes it into narration.json, so
record.py, encode.py and make_captions.py all work from one measured number.

That write is why this stage runs even when the MP3s already exist: the
durations in the committed JSON belong to the audio in WORK, and a rebuild that
skipped straight to recording would budget frames against the previous take.
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import boto3

from paths import AUDIO as OUT
from paths import NARRATION as SCRIPT


def probe(path: Path) -> float:
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nw=1:nk=1", str(path)],
        capture_output=True, text=True, check=True,
    )
    return round(float(r.stdout.strip()), 2)


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    doc = json.loads(SCRIPT.read_text())
    polly = boto3.client("polly", region_name=os.environ.get("AWS_REGION", "us-east-1"))

    total = 0.0
    for s in doc["scenes"]:
        mp3 = OUT / f"{s['id']}.mp3"
        r = polly.synthesize_speech(
            Text=s["ssml"], TextType="ssml", OutputFormat="mp3",
            VoiceId=doc["voice"], Engine=doc["engine"],
        )
        mp3.write_bytes(r["AudioStream"].read())
        s["duration"] = probe(mp3)
        total += s["duration"]
        print(f"  {s['id']:<16} {s['duration']:>6.2f}s  {mp3.stat().st_size//1024}KB")

    doc["total_duration"] = round(total, 2)
    SCRIPT.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n")
    print(f"\n  total {total:.2f}s across {len(doc['scenes'])} scenes")


if __name__ == "__main__":
    main()
