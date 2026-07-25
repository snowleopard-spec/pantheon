# Deploy to Droplet

## 1. What this runbook is for

Reach for this doc when you need to spin up the mini-dex pipeline on a fresh
DigitalOcean droplet from scratch: cold box, no venv, no data. It captures
the exact commands, the order they go in, and the traps we hit the first
time. Do **not** use it for adding features locally, adjusting bucket
weights downstream, or debugging the scoring prompt — those belong in the
spec and the local dev loop, not in an ops runbook.

## 2. Prerequisites

- DigitalOcean account with an SSH key already registered on the account.
- Anthropic API key, rotated recently. Never reuse an exposed key — assume
  any key that ever lived in plaintext on disk (including the original
  `API Keys.md`) is compromised.
- SEC user agent string. Format: `"any name <email>"`. SEC just wants a
  contact; any real-looking name and email works.
- Repo cloneable at `https://github.com/snowleopard-spec/pantheon.git`.
- Local copy of `.env` (or the values memorised) and, optionally, a recent
  DB snapshot (`data/minidex.db.*.bak`) to shortcut stages 1-2.

## 3. Droplet sizing

The workload is dominated by Stage 4 (embedding with `BAAI/bge-large-en-v1.5`,
which wants ~2.5 GB RAM resident) and Stage 3 (SEC EDGAR fetches, CPU-light
but wall-time-bound by rate limits).

| Tier | Spec | Verdict |
|---|---|---|
| Minimum | 4 GB / 2 vCPU / 80 GB | Works, but headroom on the embedding model is thin — risk of thrashing if anything else is on the box. |
| Recommended | 8 GB / 2 vCPU / 160 GB | What we ran the full universe on. Comfortable, no swap needed. |
| Overkill | 16 GB / 4 vCPU / 200 GB | Only worth it if you're running multiple pipelines concurrently. |

If you swap `embedding_model` to `BAAI/bge-small-en-v1.5` in `config.json`
(and drop `similarity_threshold` to ~0.45 to compensate for the weaker model),
even a 2 GB droplet handles the full run.

## 4. Bootstrap sequence

SSH in as `root`. Then run:

```bash
apt-get update && apt-get install -y tmux git sqlite3 python3-venv
curl -LsSf https://astral.sh/uv/install.sh | sh && source $HOME/.local/bin/env
git clone https://github.com/snowleopard-spec/pantheon.git && cd pantheon
```

Ubuntu's base image ships with `git` and `python3` but **not** the `sqlite3`
CLI. Python's stdlib `sqlite3` module is always available and the pipeline
uses it, but the command-line tool (needed for `.backup` snapshots and
ad-hoc inspection) is a separate apt package.

Bring secrets. From your Mac, either scp the existing file:

```bash
scp .env root@<droplet-ip>:/root/pantheon/.env
```

or create it on the droplet directly:

```bash
cat > /root/pantheon/.env <<EOF
ANTHROPIC_API_KEY=sk-ant-...
SEC_USER_AGENT=Your Name <you@example.com>
EOF
```

Then lock it down:

```bash
chmod 600 /root/pantheon/.env
```

Optional but recommended: transfer a recent DB snapshot to skip
stages 1-2 and reuse any already-fetched filings:

```bash
scp data/minidex.db.<latest>.bak root@<droplet-ip>:/root/pantheon/data/minidex.db
```

Finally, sync the Python environment:

```bash
uv sync
```

This takes 2-4 minutes on first run — most of it is downloading PyTorch.

## 5. Running the pipeline

Everything runs inside a single detached tmux session so an SSH drop
doesn't kill hours of work.

```bash
tmux new -s pantheon           # first time
tmux attach -t pantheon        # to reconnect later
```

Detach with `Ctrl+B` then `D`. The session keeps running.

**The single biggest gotcha:** every long-running command must be prefixed
with `PYTHONUNBUFFERED=1` when its output is piped through `tee` or
captured by tmux. Python defaults to line-buffered stdout on a TTY but
switches to fully-buffered when stdout is a pipe. Without
`PYTHONUNBUFFERED=1`, the log file sits empty for hours even while the
process is doing real work, and you have no way to see progress.

Sequence:

```bash
mkdir -p logs

# Stage 1-2: cheap, ~15 min combined. Skip if you scp'd a DB snapshot.
uv run minidex init
uv run minidex universe
uv run minidex filter

# Stage 3: 3-8 hrs depending on droplet CPU and how many pilot rows
# you preloaded via the DB snapshot.
PYTHONUNBUFFERED=1 uv run minidex fetch 2>&1 | tee logs/fetch.log

# Stage 4: 15-30 min. Downloads BGE model (~1.3 GB) on first run.
PYTHONUNBUFFERED=1 uv run minidex shortlist 2>&1 | tee logs/shortlist.log

# Stage 3.5 (optional but ~$3-5 well spent for full-universe runs):
# LLM segment refinement for filings whose XBRL segments are thin.
PYTHONUNBUFFERED=1 uv run minidex fetch-segments-llm submit --yes 2>&1 | tee logs/llm_seg_submit.log
# Wait for the batch to end (see polling snippet below), then:
PYTHONUNBUFFERED=1 uv run minidex fetch-segments-llm poll 2>&1 | tee logs/llm_seg_poll.log

# Stage 5: paid! ~$12-30 depending on candidate count.
PYTHONUNBUFFERED=1 uv run minidex score submit --yes 2>&1 | tee logs/score_submit.log
# Wait for the batch to end, then:
PYTHONUNBUFFERED=1 uv run minidex score poll 2>&1 | tee logs/score_poll.log

# Stage 6-7: seconds.
uv run minidex qc
uv run minidex build --asof YYYY-MM-DD
```

Between each `submit` and its `poll`, the Anthropic batch has to reach a
terminal state — typically 2-10 min for a Haiku batch of this size. Poll it
with this snippet (grab the batch id from the corresponding `_submit.log`):

```python
uv run python -c "
import time, anthropic
from dotenv import load_dotenv
load_dotenv('.env')
c = anthropic.Anthropic()
while True:
    b = c.messages.batches.retrieve('<batch_id_from_submit_log>')
    print(b.processing_status, b.request_counts)
    if b.processing_status in ('ended','canceled','expired'): break
    time.sleep(30)
"
```

**Use `uv run python -c`, not bare `python3 -c`.** The `anthropic` SDK
only lives inside the uv-managed venv; the system `python3` on the
droplet does not have it. This exact bug killed our first chained
pipeline script on the first poll — `ModuleNotFoundError: No module
named 'anthropic'`. If you script the whole chain, grep the script for
`python3 ` before launching to make sure every invocation is
`uv run python`.

## 6. Snapshotting and downloading artifacts

Snapshot the DB after each expensive stage. Cheap insurance against
accidental wipes or bad re-runs:

```bash
sqlite3 data/minidex.db ".backup data/minidex.db.<stage>_full.bak"
```

Suggested stage tags: `fetch_full`, `shortlist_full`, `llmseg_full`,
`scored_full`, `built_full`.

When the full run is done, pull everything relevant back to your Mac
before destroying the droplet:

```bash
# From local:
rsync -avz --stats root@<droplet-ip>:/root/pantheon/data/raw/    /path/to/local/pantheon/data/raw/
rsync -avz --stats root@<droplet-ip>:/root/pantheon/data/*.bak   /path/to/local/pantheon/data/
rsync -avz --stats root@<droplet-ip>:/root/pantheon/outputs/     /path/to/local/pantheon/outputs/
```

Then destroy the droplet from the DO console. Raw filings and DB snapshots
are the only artefacts that cost wall time (not money) to reproduce.

## 7. Common gotchas (learned the hard way)

- **Bare `python3` doesn't have the venv packages.** Always `uv run python`
  inside chained scripts and one-liners. Failure mode:
  `ModuleNotFoundError: No module named 'anthropic'`.
- **Python stdout buffering hides progress from log files.** Always
  `PYTHONUNBUFFERED=1` in front of long-running commands whose output is
  piped through `tee` or captured by tmux. Failure mode: empty `.log`
  file for hours despite active work.
- **`sqlite3` CLI is not installed by default.** The Python `sqlite3`
  module is stdlib and always available, but the command-line tool
  needs `apt-get install sqlite3`.
- **HuggingFace unauthenticated download warning.** Harmless for a
  one-time run; the model still downloads. Set `HF_TOKEN` in `.env` if
  you're re-running or hitting rate limits.
- **HF_TOKEN warning message quotes `HF_TOKEN` but the code reads from
  environment.** No action needed unless you get rate-limited.
- **Custom_id format for Anthropic batches must match
  `^[a-zA-Z0-9_-]{1,64}$`.** The pipeline uses underscores (fine). Don't
  introduce pipes or other separators if you extend it.
- **DB-wiping test.** Running `uv run pytest` on branches predating
  commit `0a8f998` will silently wipe `data/minidex.db` because an old
  test called `.unlink()` on the real DB path. Fixed on `main`. If you
  merge or check out anything older, back up the DB first.
- **SEC rate limits.** Stage 3 respects edgartools defaults (~10 req/s).
  Don't parallelise — you'll get IP-banned.
- **The original Anthropic API key was committed at project start in
  plaintext.** Rotate AND revoke. Rotating alone leaves the old key
  live and every leaked snapshot of the repo history still contains it.

## 8. Cost expectations

Based on the pilot + full-universe runs:

- Universe + filter: free (SEC).
- Fetch (SEC): free.
- Shortlist (embedding): free after the first-time model download.
- LLM segment refinement (optional): ~$3-5 for a full-universe run.
- Scoring: ~$12-30 depending on candidate count and how tight the
  shortlist is.
- **Total per full annual refresh: ~$15-35.**

Droplet itself: an 8 GB DO box is ~$0.09/hr, so a full run round trip
(bootstrap + fetch + shortlist + score + rsync + destroy) is another
~$1-2 in compute.

## 9. When to reach for this doc vs another one

- **Deploying to a fresh droplet** → this doc.
- **Understanding the architecture** → `docs/ARCHITECTURE.html`.
- **Reviewing what's been done historically** → `docs/PROGRESS_REPORT.md`.
- **Non-technical narrative of the bootstrap** →
  `docs/DROPLET_BOOTSTRAP_EXPLAINED.md`.
- **Full pipeline spec** → `docs/MINIDEX_SPEC.md`.
