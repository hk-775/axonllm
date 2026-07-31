#!/usr/bin/env python3
"""Where the demo build keeps its inputs and its scratch space.

The narration is committed (it is the script, and it carries the measured
durations every later stage budgets against). Everything else the build produces
-- tens of thousands of PNG frames, the per-scene MP3s, the per-scene clips, the
1080p master -- is intermediate and stays out of the tree: the frames alone run
to gigabytes, and the only two artefacts anyone needs afterwards are the 720p
MP4 and the VTT, which land in site/ and are committed.

WORK defaults under the system temp dir rather than the repo so a build cannot
leave the working tree dirty. Override it with AXON_DEMO_WORK to keep the
intermediates somewhere durable while iterating -- the stages are separate
commands precisely so a slow record can be re-encoded without re-running.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

# The narration script: the single source of what is said, in what order, and
# -- once synthesize.py has run -- for how long.
NARRATION = Path(__file__).resolve().parent / "narration.json"

WORK = Path(os.environ.get("AXON_DEMO_WORK") or Path(tempfile.gettempdir()) / "axon-demo")
AUDIO = WORK / "audio"
FRAMES = WORK / "frames"
CLIPS = WORK / "clips"
MASTER = WORK / "master-1080p.mp4"

# The two committed outputs. site/ is uploaded to S3 verbatim (see
# site/infra/stack.py) and the gateway serves the same files off the checkout,
# so both deployments hand out one identical asset.
VIDEO = ROOT / "site" / "axonllm-demo.mp4"
CAPTIONS = ROOT / "site" / "axonllm-demo.vtt"

# The gateway the recorder films. Must be serving seeded demo data: the
# narration quotes the numbers on these pages, so an empty gateway records a
# film that contradicts its own voice-over.
BASE_URL = os.environ.get("AXON_DEMO_BASE_URL", "http://127.0.0.1:8000")
