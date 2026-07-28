#!/bin/bash
# droplet_refresh.sh — daily Pantheon refresh on the droplet (the cron entry point).
#
# 1. Pull incremental Polygon prices. A failure here is logged loudly but does
#    not abort: per spec D6 the report is still re-rendered from cached prices,
#    so a Polygon outage degrades to "yesterday's data" instead of a dead page.
# 2. Re-render the bucket-returns HTML report.
# 3. Publish it atomically to /srv/pantheon/index.html (tailscale serve's root).
#
# The render is fully self-contained HTML (inline CSS/JS, no assets), so the
# publish step is a single file.

set -u

REPO="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO" || exit 1
PY="$REPO/.venv/bin/python"
DEST="/srv/pantheon/index.html"

echo "=== pantheon refresh start $(date -u +%FT%TZ) ==="

"$PY" scripts/pull_prices.py \
    || echo "warn: price pull failed (exit $?); re-rendering from cached prices"

if ! "$PY" scripts/bucket_returns.py; then
    echo "error: bucket_returns render failed (exit $?); keeping previous $DEST" >&2
    exit 1
fi

# Write to a temp file on the same filesystem, then rename: a reader mid-refresh
# never sees a half-written page.
install -m 644 outputs/bucket_returns.html "$DEST.tmp" && mv "$DEST.tmp" "$DEST" \
    || { echo "error: publish to $DEST failed" >&2; exit 1; }

echo "=== pantheon refresh done $(date -u +%FT%TZ) ==="
