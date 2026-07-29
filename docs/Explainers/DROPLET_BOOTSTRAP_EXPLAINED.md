# Droplet Bootstrap, Explained

"Spinning up a droplet" is DigitalOcean's marketing phrase for creating a fresh cloud Linux server. Yours has 2 CPUs, 8 GB of memory, and 160 GB of disk — a modest but capable box, roughly the compute of a mid-range laptop but always on and reachable over the internet. The speed came from three things: DigitalOcean does most of the setup itself the moment you click "create"; the software the pipeline needs is standard (nothing exotic to compile); and most of the code was already committed to GitHub, so bringing it onto the new machine was one `git clone`.

## Step 1: Connecting to the droplet

SSH (Secure Shell) is the standard tool for opening a remote command line to a server — you type on your Mac, the characters go to the droplet, and its responses come back. The very first connection asked us to trust the server's identity fingerprint; that's the "known hosts" warning you saw scroll by, and it only appears once per new machine.

Once connected, we confirmed the basics: hostname, memory, and disk space. The RAM check was the important one. We needed to be sure this box actually had roughly 8 GB free (it did — 7.3 GB available), whereas the previous droplet only had 1.9 GB total, which is why that one struggled.

## Step 2: Installing the toolchain

Ubuntu came with git and Python already on it. We added three tools:

- **uv** — a fast Python package manager. Installed with a one-line script from its makers.
- **tmux** — a "terminal multiplexer" that keeps command-line sessions alive when you disconnect. Critical here because the pipeline runs for hours; without tmux, closing your SSH window would kill it.
- **sqlite3** — a small command-line tool for peeking into the local database file. A quality-of-life addition.

Each is a single Ubuntu package install (`apt-get install ...`) or, for uv, a one-line curl script. Together, under a minute.

## Step 3: Bringing the code

`git clone` from GitHub. Since we've been pushing every change from your Mac to `snowleopard-spec/pantheon` throughout this project, the droplet just pulls down the exact same state. One command, a few seconds, done.

## Step 4: Bringing the data

Two files needed to move from your Mac to the droplet, using scp (secure copy — SSH's file-transfer cousin):

- **`.env`** — 181 bytes. Contains the two secrets the pipeline needs: the Anthropic API key and the SEC user-agent string.
- **`data/minidex.db.scored_v16.bak`** — 3.9 MB. The pilot database. Bringing this over means the droplet doesn't have to redo stages 1 and 2 (universe pull and filtering) that we already ran locally, saving maybe 15-20 minutes. It also means stage 3's fetch will find 77 of the 1,376 filings already downloaded (the pilot names) and skip those.

## Step 5: Installing the Python dependencies

`uv sync` reads `pyproject.toml` and `uv.lock` from the repo and downloads and installs every Python package the pipeline needs. This is the biggest single time cost of the bootstrap — about 2-3 minutes — because it pulls PyTorch, a large deep-learning library needed for the embedding stage.

## Step 6: Verifying it works

Ran the test suite. 117 tests pass. That confirmed the code is functional on the new machine — the environment, the dependencies, and the database file are all wired up correctly. Confidence that when we launch the real workload, it will actually run.

## Step 7: Starting the fetch inside tmux

Wrapped the `minidex fetch` command in a tmux session called `pantheon`. tmux makes it survive disconnections — even if the SSH connection drops, the command keeps running. You can reattach later with `tmux attach -t pantheon` to watch progress.

## Why it felt fast

- No custom compilation needed — everything is standard Python packages with prebuilt binaries.
- The code was already on GitHub — no need to rebuild it, just clone.
- We already knew the exact commands to run — no exploration or debugging on the droplet itself.
- Rehearsal — we already ran the pilot version locally, so the whole workflow was proven before we touched the cloud.

## Where we are now

The pipeline is churning through EDGAR filings on the droplet. Expected wall time: 6-8 hours for the roughly 1,299 companies we haven't fetched yet. You can walk away, or check in with `ssh root@139.59.127.139` and then `tmux attach -t pantheon`.
