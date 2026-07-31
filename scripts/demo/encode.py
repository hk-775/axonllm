#!/usr/bin/env python3
"""Stage 3 of 4: assemble the frames and narration into the shipping MP4.

    python3 scripts/demo/encode.py

Per scene: encode the PNG sequence at a frame rate that makes the video exactly
as long as its MP3, mux the audio, then concat. Setting the rate per scene rather
than trimming means picture and voice cannot drift -- the last frame lands on the
last word by construction.

Two encodes, not one. The master is 1080p from the 1440x810 capture (both 16:9,
so a clean upscale with no letterboxing, and h264/yuv420p gets the even
dimensions it requires -- an odd height fails with "Could not open encoder before
EOF"). The master is then downscaled to 720p, and that is what ships.

720p at crf 25 because this plays in a dialog on a landing page, not full-screen:
the 1080p master is 13MB, and a customer clicking Watch the demo over hotel wifi
waits for it. The same content at 720p/crf25 is 5.8MB and, at the size the dialog
actually renders, indistinguishable. Both numbers are measured -- crf 24 is 6.2MB
and crf 26 is 5.5MB with visible mush on the dashboard's small table text, which
is the one thing in this film a viewer needs to be able to read.
"""
from __future__ import annotations

import json
import shutil
import subprocess

from paths import AUDIO, CLIPS, FRAMES, MASTER, NARRATION, VIDEO

# Shared by both passes so the master and the shipping file cannot drift apart in
# anything but resolution and quality.
AUDIO_ARGS = ["-c:a", "aac", "-ar", "48000", "-ac", "2"]
X264 = ["-c:v", "libx264", "-preset", "slow", "-r", "30"]


def run(cmd: list[str]) -> None:
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode:
        raise SystemExit(f"FAILED: {' '.join(cmd[:9])}...\n{r.stderr[-1500:]}")


def probe(path) -> str:
    return subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries",
         "format=duration,size:stream=codec_name,width,height,r_frame_rate",
         "-of", "default=nw=1", str(path)],
        capture_output=True, text=True, check=True,
    ).stdout


def main() -> None:
    scenes = json.loads(NARRATION.read_text())["scenes"]
    shutil.rmtree(CLIPS, ignore_errors=True)
    CLIPS.mkdir(parents=True)

    parts = []
    for s in scenes:
        sid, secs = s["id"], s["duration"]
        d = FRAMES / sid
        n = len(list(d.glob("*.png")))
        if not n:
            raise SystemExit(f"{sid}: no frames -- run record.py first")
        mp3 = AUDIO / f"{sid}.mp3"
        if not mp3.exists():
            raise SystemExit(f"{sid}: no audio -- run synthesize.py first")
        fps = n / secs  # exact, so the picture ends when the sentence does
        clip = CLIPS / f"{sid}.mp4"
        run([
            "ffmpeg", "-y", "-loglevel", "error",
            "-framerate", f"{fps:.6f}", "-i", str(d / "%05d.png"),
            "-i", str(mp3),
            "-vf", "scale=1920:1080:flags=lanczos,format=yuv420p",
            *X264, "-crf", "20", *AUDIO_ARGS, "-b:a", "160k",
            "-shortest", "-movflags", "+faststart", str(clip),
        ])
        parts.append(clip)
        print(f"  {sid:<16} {n:>4}f @ {fps:5.2f}fps = {secs:6.2f}s")

    lst = CLIPS / "list.txt"
    lst.write_text("".join(f"file '{p}'\n" for p in parts))
    # Re-encode on concat rather than -c copy: the clips share a codec and
    # geometry, but concatenating AAC streams by copy leaves a click at each
    # boundary where the priming samples meet.
    run([
        "ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0",
        "-i", str(lst), *X264, "-crf", "20", *AUDIO_ARGS, "-b:a", "160k",
        "-movflags", "+faststart", str(MASTER),
    ])
    print(f"\nmaster {MASTER}\n{probe(MASTER)}")

    # From the master rather than the clips: one concat, encoded once, so the
    # scene boundaries in the shipping file are the ones that were checked.
    VIDEO.parent.mkdir(parents=True, exist_ok=True)
    run([
        "ffmpeg", "-y", "-loglevel", "error", "-i", str(MASTER),
        "-vf", "scale=1280:720:flags=lanczos",
        *X264, "-crf", "25", *AUDIO_ARGS, "-b:a", "128k",
        "-movflags", "+faststart", str(VIDEO),
    ])
    print(f"shipping {VIDEO}\n{probe(VIDEO)}")
    print("==> now run make_captions.py, then commit site/axonllm-demo.{mp4,vtt}")


if __name__ == "__main__":
    main()
