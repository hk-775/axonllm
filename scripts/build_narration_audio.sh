#!/usr/bin/env bash
#
# Synthesize a narration track set with Amazon Polly.
#
#   ./scripts/build_narration_audio.sh [architecture|tour]
#
# Two narrations share this script, because they differ only in which JSON is
# the source and where the MP3s land:
#
#   architecture  site/narration/            — the three diagrams on the site
#   tour          src/gateway/admin/static/tour/ — the dashboard's guided demo
#
# The chosen JSON is the single source of its script. This writes one MP3 per
# track next to it and records the measured duration back into the JSON, so the
# player shows a real progress bar instead of guessing.
#
# The MP3s are committed, not built on demand. site/ is uploaded to S3 verbatim
# (see site/infra/stack.py) with no build step, the dashboard serves static/
# straight off the checkout, and a customer demo that needs an AWS credential
# and a network round-trip to make sound is a demo that fails in a meeting room.
# Re-run this after editing the narration, and commit the results together.
set -euo pipefail

cd "$(dirname "$0")/.."

case "${1:-architecture}" in
    architecture)
        SRC="site/narration/architecture-narration.json"
        OUT_DIR="site/narration"
        ;;
    tour)
        SRC="src/gateway/admin/static/tour/tour-narration.json"
        OUT_DIR="src/gateway/admin/static/tour"
        ;;
    *)
        echo "usage: $0 [architecture|tour]" >&2
        exit 2
        ;;
esac

if [[ ! -f "$SRC" ]]; then
    echo "error: $SRC not found" >&2
    exit 1
fi

for tool in aws python3; do
    command -v "$tool" >/dev/null 2>&1 || { echo "error: $tool not found" >&2; exit 1; }
done

if ! aws sts get-caller-identity >/dev/null 2>&1; then
    echo "error: no usable AWS credentials — Polly needs them to synthesize." >&2
    exit 1
fi

# ffprobe measures the real duration. Without it the page would need a guess,
# and a progress bar that disagrees with the audio is worse than none.
HAVE_FFPROBE=1
command -v ffprobe >/dev/null 2>&1 || HAVE_FFPROBE=0
[[ "$HAVE_FFPROBE" == 1 ]] || echo "warning: ffprobe not found — durations will not be updated" >&2

VOICE=$(python3 -c "import json;print(json.load(open('$SRC'))['voice'])")
ENGINE=$(python3 -c "import json;print(json.load(open('$SRC'))['engine'])")
RATE=$(python3 -c "import json;print(json.load(open('$SRC'))['sample_rate'])")
IDS=$(python3 -c "import json;print(' '.join(t['id'] for t in json.load(open('$SRC'))['tracks']))")

echo "==> voice $VOICE, engine $ENGINE, ${RATE}Hz"

for id in $IDS; do
    out="$OUT_DIR/${id}.mp3"
    # Read the SSML via python rather than a shell variable: it contains quotes
    # and angle brackets, and word-splitting would corrupt it silently.
    python3 - "$SRC" "$id" > /tmp/axon-ssml.xml <<'PY'
import json, sys
src, want = sys.argv[1], sys.argv[2]
track = next(t for t in json.load(open(src))["tracks"] if t["id"] == want)
sys.stdout.write(track["ssml"])
PY

    echo "==> $id -> $out"
    # --text-type ssml so the <break> pacing is honoured. Generative engine:
    # Matthew reads noticeably less like a screen reader on it than on neural,
    # which matters when the audience is a customer rather than a test.
    aws polly synthesize-speech \
        --text-type ssml \
        --text "file:///tmp/axon-ssml.xml" \
        --voice-id "$VOICE" \
        --engine "$ENGINE" \
        --output-format mp3 \
        --sample-rate "$RATE" \
        "$out" >/dev/null

    if [[ ! -s "$out" ]]; then
        echo "error: $out is empty — synthesis failed" >&2
        exit 1
    fi

    if [[ "$HAVE_FFPROBE" == 1 ]]; then
        dur=$(ffprobe -v error -show_entries format=duration \
                      -of default=noprint_wrappers=1:nokey=1 "$out")
        python3 - "$SRC" "$id" "$dur" <<'PY'
import json, sys
src, want, dur = sys.argv[1], sys.argv[2], float(sys.argv[3])
data = json.load(open(src))
for t in data["tracks"]:
        if t["id"] == want:
            t["duration"] = round(dur, 2)
with open(src, "w") as f:
    json.dump(data, f, indent=2, ensure_ascii=False)
    f.write("\n")
PY
        printf '    %.1fs\n' "$dur"
    fi
done

rm -f /tmp/axon-ssml.xml

echo
echo "==> done. Commit $OUT_DIR/*.mp3 with the JSON."
