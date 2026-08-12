# Rebuilding the product demo

`site/axonllm-demo.mp4` and `site/axonllm-demo.vtt` — the film the landing page's
ribbon opens — are committed, and this is what produces them.

They are committed for the same reason the narration MP3s are (see
`scripts/build_narration_audio.sh`): `site/` is uploaded to S3 verbatim with no
build step, and a customer demo that needs an AWS credential, a running gateway
and twenty minutes of ffmpeg to make a picture is a demo that fails in a meeting
room. What is *not* committed is the intermediate output — tens of thousands of
PNG frames and the 1080p master run to gigabytes, and nothing downstream reads
them.

## Prerequisites

- AWS credentials that can call Polly (`aws sts get-caller-identity`)
- `ffmpeg` and `ffprobe`
- `uv pip install websocket-client boto3` (or run these scripts under `uv run --with websocket-client --with boto3`)
- Google Chrome at the path in `record.py`
- A gateway serving **seeded demo data**:
  `AXON_LOAD_DEMO_DATA=true uv run python serve_dashboard.py`

That last one matters more than it looks. The narration names the numbers on the
pages being filmed — 66 requests, $1.26, 5 of 51 unpriced mappings — so filming a
gateway with a different seed, or none, produces a film whose voice-over
contradicts the screen it is describing.

## The four stages

Run in order, from the repo root:

```sh
python3 scripts/demo/synthesize.py     # Polly -> per-scene MP3s, measures each
python3 scripts/demo/record.py         # headless Chrome -> PNG frame sequences
python3 scripts/demo/encode.py         # -> 1080p master, then site/…-demo.mp4
python3 scripts/demo/make_captions.py  # -> site/…-demo.vtt
```

Separate commands rather than one script because `record.py` is the slow stage
(it screenshots every frame over the DevTools protocol) and re-encoding is
routine, so being able to re-run stage 3 alone is worth the two extra
invocations. They pass state through `narration.json` and `paths.WORK`.

`narration.json` is the single source of what is said, in what order, and for how
long. Stage 1 writes each measured MP3 duration back into it; stages 2–4 all read
those numbers, which is why picture, voice and captions cannot drift — every one
of them is derived from the same measured file lengths rather than timed by hand.

Intermediates go under the system temp dir. Set `AXON_DEMO_WORK` to keep them
somewhere durable while iterating, and `AXON_DEMO_BASE_URL` if the gateway is not
on `127.0.0.1:8000`.

## Editing the narration

Edit the `ssml` and `text` of a scene in `narration.json`, then re-run all four
stages — a changed sentence changes that scene's duration, which changes its
frame budget, which changes the cue timings. Committing an edited script without
re-recording leaves the film saying the old words.

`text` is the caption-friendly plain version of `ssml`; `make_captions.py` reads
`text`, so a figure corrected in one and not the other ships a subtitle that
disagrees with the audio.

The engine is `neural`, not the `generative` used by the architecture and tour
narrations: `<emphasis>` raises `InvalidSsmlException` on neural, so phrasing here
is carried by `<break>` and sentence shape instead. Switching this to generative
means re-testing every scene's SSML.
