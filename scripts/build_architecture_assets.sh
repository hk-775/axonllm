#!/usr/bin/env bash
#
# Regenerate the architecture assets the marketing site serves.
#
#   ./scripts/build_architecture_assets.sh
#
# docs/architecture.drawio is the single source. Everything this writes into
# site/ is derived from it and committed anyway, because site/ is uploaded to S3
# verbatim (see site/infra/stack.py) and there is no build step in the deploy.
#
# Run this after editing the diagram, and commit the results together.
set -euo pipefail

cd "$(dirname "$0")/.."

SRC="docs/architecture.drawio"

if [[ ! -f "$SRC" ]]; then
    echo "error: $SRC not found" >&2
    exit 1
fi

if ! command -v drawio >/dev/null 2>&1; then
    echo "error: drawio CLI not found." >&2
    echo "  brew install --cask drawio    # then reopen your shell" >&2
    exit 1
fi

# Page order matches the <diagram> order in the .drawio, which is also the tab
# order on the page. -p is 1-based; -p 0 is silently the same as -p 1.
PAGES=(1:infrastructure 2:pipeline 3:components)

for entry in "${PAGES[@]}"; do
    num="${entry%%:*}"
    name="${entry##*:}"
    out="site/architecture-${name}.svg"
    echo "==> page $num -> $out"
    # --embed-svg-fonts false keeps each file ~60-80KB instead of ~1MB; the page
    # already loads Inter from Google Fonts, so the embedded copy is dead weight.
    # --svg-theme light pins the export to the light palette the site uses.
    drawio -x -f svg -p "$num" \
        --embed-svg-fonts false \
        --svg-theme light \
        -o "$out" "$SRC" >/dev/null

    if ! grep -q "<svg" "$out"; then
        echo "error: $out has no <svg> root — export failed" >&2
        exit 1
    fi
done

# The download link on the page offers the editable original.
echo "==> $SRC -> site/architecture.drawio"
cp "$SRC" site/architecture.drawio

# The dashboard's /admin/architecture route reads this one, so it tracks page 1
# too rather than drifting behind the site.
echo "==> page 1 -> docs/architecture.svg (dashboard route)"
cp site/architecture-infrastructure.svg docs/architecture.svg

echo
echo "==> done. Commit site/architecture-*.svg, site/architecture.drawio and"
echo "    docs/architecture.svg along with the .drawio change."
