# SPEC — Pantheon on the Droplet, Served Privately via Tailscale

**Audience:** a fresh Claude Code session. Read this whole file before doing anything.
**Author's context:** self-taught builder; explain non-obvious choices as you go. My stack conventions are described in `STACK_CONTEXT.md` (assume it is in context alongside this spec).

---

## 1. Goal

**Motivation:** Unicorn taught me the cost of a public architecture — a separate SPA on Cloudflare Pages talking to a public API meant CORS allow-lists, an exposed surface to secure, and two deploy targets to keep in sync. This project deliberately avoids all of that: pre-rendered static HTML, no API, no public hosting, private access only. It is also my **first Tailscale build, not a one-off** — I expect tailnet usage to expand over time (Tailscale SSH, adding my Mac, reaching other services like Abicus privately), so set it up cleanly as a foundation rather than a hack, and prefer choices that generalise.

Deploy the Pantheon (mini-dex) project to my existing DigitalOcean droplet so that:

1. A **daily cron** refreshes prices and regenerates the bucket-returns HTML report. **Weights stay frozen** — the scoring pipeline is *not* run on the droplet.
2. The report is written to a **static location** and served **only over my Tailscale tailnet** using `tailscale serve` — no Caddy involvement, no public exposure of any kind.
3. I can open the report in Safari on my iPhone via my tailnet URL and add it to my home screen.

## 2. Existing context

- **Droplet:** Ubuntu 24.04 LTS, 1 vCPU, ~2 GiB RAM + 2 GiB swap, hostname `unicorn-hunt`, UTC timezone. Already hosts Unicorn Hunt (`/home/smallcap-momentum`), and Birthday / Dilithium / Sonar / Bonsai under `/root/<project>/`. Caddy serves the public Unicorn Hunt API and must not be touched by this build.
- **Droplet conventions (follow them):** one directory per project; one venv per project; cron entries that `cd` into the project and invoke its interpreter explicitly; per-project log files inside the project directory; secrets in a local `.env`, never committed.
- **Existing root crontab (UTC):** 21:00 Unicorn refresh; 22:30 Birthday; 23:00 Dilithium; Fri 20:00 Sonar heartbeat. Choose a slot that avoids these.
- **Pantheon repo:** `minidex/` package, Typer CLI, uv-managed, SQLite state, config precedence `MINIDEX_*` env var > `config.json` > defaults. Relevant to this build are only the companion scripts: `scripts/pull_prices.py` (incremental Polygon daily closes) and `scripts/bucket_returns.py` (renders the trailing-window bucket-returns HTML report from the frozen weights in `outputs/`). The heavy pipeline stages (EDGAR fetch, embedding shortlist, LLM batch scoring) are **out of scope on the droplet**; M0 restructures the repo into a lean base + `scoring` extra so the droplet and Mac install different dependency profiles from the same repo and lockfile.

## 3. Resolved decisions

Do not relitigate these:

- **D1 — Serving:** `tailscale serve` only. No Caddy site block, no nginx, no new ufw openings, and absolutely no `tailscale funnel` (funnel makes things public; this project must never use it).
- **D2 — Report location:** cron's final step copies the rendered report to `/srv/pantheon/index.html`. `/srv` is currently empty on the droplet; this project claims it.
- **D3 — Project location:** `/root/pantheon` (small-project convention), cloned from my GitHub repo.
- **D4 — No scoring on the droplet, and no scoring dependencies either (hard constraint):** weights are static, produced on my Mac / rented compute. The droplet only pulls prices and re-renders returns. The heavy scoring stack — `sentence-transformers` and the `torch` it drags in, plus the LLM-batch tooling — must **never be installed on the droplet**: the box has 1 vCPU and ~2 GiB RAM shared with four production services, and torch alone costs ~1 GB of disk and can swap-thrash the box if imported. This is enabled by the dependency split in M0: the droplet installs the lean base with plain `uv sync`; only the Mac installs `--extra scoring`. If at any point the droplet install pulls torch/sentence-transformers, that is a bug in the M0 split — stop and fix the split; do not proceed by installing the heavy packages.
- **D5 — Cron time:** 22:00 UTC daily (after US market close, clear of the 21:00 / 22:30 / 23:00 jobs), logging to `/root/pantheon/logs/cron.log`.
- **D6 — Failure behaviour:** follow stack convention — fail loudly in the log; a run that pulls no new prices should still re-render the report (idempotent). Email alerting is out of scope for v1.
- **D7 — Git workflow:** all repo changes happen on a feature branch `droplet-deploy` (M0's dependency split, plus any small additions from later milestones like the report-copy step and deploy notes). Merge to `main` only after M0's verification gate passes on the Mac. The droplet clones and tracks **`main` only** — never check out the feature branch on the droplet. If post-merge fixes are needed, they go branch → verify → merge → `git pull` on the droplet.

## 4. Human actions (I do these — prompt me at the right moment, don't attempt them yourself)

**Before the build:**

- [ ] **H1.** Create a Tailscale account (choose sign-in identity: Apple/Google/GitHub/email).
- [ ] **H2.** In the Tailscale admin console: enable **MagicDNS** and **HTTPS certificates** (Settings → DNS). Note my tailnet name (`<something>.ts.net`).
- [ ] **H3.** Install the Tailscale iOS app on my iPhone and sign in to the same account.
- [ ] **H4.** Have my Polygon API key ready for the droplet `.env`.
- [ ] **H5.** Decide the source of current state: either `scp` my existing `outputs/` directory and prices SQLite DB from my Mac to the droplet, or accept a longer first `pull_prices` run from scratch. (Frozen weights in `outputs/` are **required** — the droplet cannot regenerate them.)

**During the build (Claude Code will reach points where only I can act):**

- [ ] **H6.** Run `sudo tailscale up` authentication: the command prints a URL; I open it in a browser and approve the droplet onto my tailnet.
- [ ] **H7.** Confirm in the admin console that the droplet appears as a device and MagicDNS resolves it.

**After the build:**

- [ ] **H8.** On the iPhone with Tailscale toggled on: open the served URL in Safari, verify the report renders, then Share → **Add to Home Screen**.
- [ ] **H9.** Optionally set the Tailscale iOS app to on-demand/always-on so the shortcut works without manually toggling the VPN.

## 5. Milestones

Work milestone by milestone. **Stop at the end of each one, show me the verification output, and wait for my go-ahead before continuing.**

### M0 — Dependency split (done on the **Mac**, in the Pantheon repo, before any droplet work)
Create the `droplet-deploy` branch (per D7) and restructure `pyproject.toml` on it into a lean base plus a `scoring` extra, so the two machines install different slices of the same repo:
- Audit every import in `scripts/pull_prices.py`, `scripts/bucket_returns.py`, and the `minidex` modules they touch (db, config, etc.); those dependencies form the base `[project] dependencies`. Everything used only by the fetch/shortlist/score stages — `sentence-transformers` (and its `torch`), `anthropic`, and friends — moves to `[project.optional-dependencies] scoring = [...]`.
- Guard against import leaks: if the Typer CLI or any shared module imports the heavy stages at module top level, convert those to lazy imports inside the relevant command/stage functions, with a clear "install with `uv sync --extra scoring`" error if invoked without the extra.
- Re-lock with `uv lock` (one lockfile covers both profiles) and commit.
- **Verify (this is the gate for everything after it):** in a fresh temp clone, run plain `uv sync` and confirm (a) `pull_prices` and `bucket_returns` run successfully, and (b) `torch` / `sentence-transformers` are absent from the environment. Then confirm the Mac's full profile still works with `uv sync --extra scoring`. Show me both results. **On my go-ahead, merge `droplet-deploy` into `main` and push — M1 does not start until the split is on `main`.**

### M1 — Deploy Pantheon to the droplet
- Clone the repo to `/root/pantheon` **on `main`** (per D7); install uv if absent; create the environment with **plain `uv sync` (no extras)** — pre-approved, no need to ask.
- Sanity-check the lean install per D4: confirm torch/sentence-transformers are not present and note the environment's disk size.
- Create `.env` from a template you write (`MINIDEX_*` overrides as needed, Polygon key placeholder) and prompt me for H4/H5.
- **Verify:** both scripts respond to `--help` (or a dry-run) from the droplet environment; frozen weights from `outputs/` are present and readable.

### M2 — Manual returns refresh end-to-end
- Run `pull_prices` then `bucket_returns` once, manually, watching memory (the box has ~1.3 GiB available; if the scripts approach that, stop and report rather than swap-thrash).
- **Verify:** show me the tail of the run log and confirm the HTML report was regenerated with today's data.

### M3 — Static output location
- Create `/srv/pantheon/`; add the copy step so the freshly rendered report lands at `/srv/pantheon/index.html` (plus any assets the report references — confirm whether the HTML is self-contained; if not, copy its asset directory too).
- **Verify:** `index.html` exists, is owned sensibly, and opens correctly via `python3 -m http.server` bound to localhost as a smoke test (then kill it).

### M4 — Cron
- Add the 22:00 UTC entry to root's crontab following the existing entry style exactly: `cd` into `/root/pantheon`, invoke the environment's Python explicitly, append to `logs/cron.log`.
- **Verify:** show me the new crontab line alongside the existing ones; run the exact cron command string once by hand and confirm a clean log entry.

### M5 — Tailscale install + serve
- Install Tailscale on the droplet (official install script); bring it up (**pause for H6/H7**).
- Configure `sudo tailscale serve --bg /srv/pantheon` (or the current syntax for serving a static directory — check `tailscale serve --help`; the CLI has changed across versions).
- **Verify:** `tailscale serve status` shows the HTTPS URL; `tailscale status` shows both my devices; `curl` the served URL from the droplet itself and confirm 200 + HTML.
- Confirm nothing new is exposed publicly: show `ss -tlnp` and confirm no new listener on 0.0.0.0/public interfaces attributable to this project, and that ufw was not modified.

### M6 — Phone verification + wrap-up
- Walk me through H8/H9.
- Write `/root/pantheon/DEPLOY_NOTES.md`: what was installed, the serve config and how to change it, the cron entry, how to update frozen weights (scp from Mac → where), how to tear it all down — and, since the tailnet is intended to grow, a short "extending the tailnet" section: how to add another device, and how to serve another service privately alongside this one.
- **Verify:** I confirm the report loads on the iPhone from the home-screen icon.

## 6. Acceptance criteria

- Daily cron refreshes returns and republishes `/srv/pantheon/index.html` unattended.
- Report reachable over HTTPS at the tailnet URL from my iPhone; **unreachable from the public internet** (no public listener, no funnel, no Caddy/nginx involvement).
- Frozen weights untouched by any droplet process; `torch` / `sentence-transformers` absent from the droplet environment.
- No secrets in the repo or crontab; Polygon key lives only in `/root/pantheon/.env`.
- Existing services (Unicorn Hunt API, Sonar, Birthday, Dilithium, Caddy) verifiably undisturbed.

## 7. Out of scope (v1)

- Running any scoring/embedding pipeline stage on the droplet
- Email alerting for failed runs
- Tailscale SSH (`tailscale up --ssh`) and adding my Mac to the tailnet — likely follow-up project, do not enable now
- Any native iOS app
- Headscale / self-hosted coordination
