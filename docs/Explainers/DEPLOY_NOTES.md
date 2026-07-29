# Pantheon droplet deployment — operational notes

_Deployed 2026-07-28/29 to `unicorn-hunt` (161.35.122.12 / tailnet
`100.79.67.34`). Full build log: `docs/Context/PROGRESS_REPORT.md` §14._

## What this is

A daily cron pulls incremental Polygon prices and re-renders the bucket-returns
report, publishing it to `/srv/pantheon/index.html`. Tailscale serves that file
**privately** at:

    https://unicorn-hunt.macaw-dominant.ts.net/

Weights are frozen (`definitions/minidex_weights.csv`, tracked in git). No
scoring, no torch, no public exposure. The repo lives at `/root/pantheon`,
currently tracking the **`droplet-deploy`** branch (switch to `main` after that
branch merges: `git checkout main && git pull`).

## What was installed on the droplet

| Thing | How | Where |
|---|---|---|
| `uv` 0.11.33 | official installer | `~/.local/bin/uv` |
| repo + lean venv (122 MB, no torch) | `git clone` + plain `uv sync` | `/root/pantheon` |
| Tailscale 1.98.10 | official install script (`apt` repo) | system service `tailscaled` |
| `.env` (Polygon key only, mode 600) | written by hand | `/root/pantheon/.env` |

The venv is the **lean base profile**. Never run `uv sync --extra scoring`
here — that pulls torch/sentence-transformers (~1 GB+) onto a 1.9 GiB box
shared with four production services.

## The daily pipeline

Cron (root's crontab, 22:00 UTC — after US close, clear of the 21:00 / 22:30 /
23:00 jobs):

```
0 22 * * * cd /root/pantheon && bash scripts/droplet_refresh.sh >> /root/pantheon/logs/cron.log 2>&1
```

`scripts/droplet_refresh.sh` does: incremental price pull (a Polygon failure
warns but still re-renders from cache) → render → atomic publish
(`index.html.tmp` + `mv`) to `/srv/pantheon/index.html`. Log:
`/root/pantheon/logs/cron.log`. Run it by hand any time — it's idempotent.

## Tailscale serve — current config and how to change it

```
tailscale serve status          # shows: / -> path /srv/pantheon (tailnet only)
tailscale serve --https=443 off # stop serving (config persists otherwise, reboots included)
tailscale serve --bg /srv/pantheon   # re-enable
```

Never use `tailscale funnel` — it makes things public.

**Verifying serve works: do it from another tailnet device (phone).** A curl
from the droplet itself lands on Caddy's `*:443` and fails TLS — that is a
false negative, not an outage. Full explanation:
`docs/Skills/VERIFYING_TAILSCALE_SERVE_LOCALLY.md`.

## Updating the frozen weights

Weights are in git now — no scp involved:

1. On the Mac: run the scoring pipeline / `minidex build`, review the run in
   `outputs/<asof>/`, then promote it: copy its `minidex_weights.csv` over
   `definitions/minidex_weights.csv`, commit, push.
2. On the droplet: `cd /root/pantheon && git pull`. Next cron picks it up.
   (New tickers get their full 400-day price history fetched automatically on
   the next pull.)

The other stateful files (`data/prices.csv`, `data/minidex.db`) are
self-maintaining; `minidex.db` only feeds company names into the report and can
be refreshed by scp from the Mac if it ever drifts.

## Teardown (full removal)

```
crontab -e                                  # delete the Pantheon line
tailscale serve --https=443 off
tailscale down && apt-get remove tailscale  # only if abandoning Tailscale entirely
rm -rf /root/pantheon /srv/pantheon
```

Nothing else to undo: no Caddy/nginx/ufw changes were made, no systemd units
added.

## Extending the tailnet (this is meant to grow)

**Add a device** (e.g. the Mac): install Tailscale on it, sign in to the same
account, approve if prompted. It appears in the admin console → Machines. For
headless machines, disable key expiry (⋯ menu on the machine's detail page).

**Serve another service alongside Pantheon:** one hostname serves one site per
port, so either:

- *Path-based on this box:* `tailscale serve --bg --set-path /abicus localhost:9000`
  → `https://unicorn-hunt.macaw-dominant.ts.net/abicus` (coexists with the
  Pantheon root path).
- *Another port on this box:* `tailscale serve --bg --https=8443 localhost:9000`
  → same hostname, `:8443`.
- *Cleanest — its own machine:* every tailnet device gets its own
  `https://<name>.macaw-dominant.ts.net` URL with zero interaction with this
  one.

**Tailscale SSH** (deliberately not enabled in v1): `tailscale up --ssh` on the
droplet would let tailnet devices ssh without keys, and the public port 22
could then be closed in ufw. Worth a small dedicated session — it changes how
you administer the box.

## Debugging quick hits

- Report stale? `tail -50 /root/pantheon/logs/cron.log` — every run logs
  start/done lines with UTC timestamps. The page `<title>` shows the latest
  price date it rendered from.
- Serve down? `tailscale serve status`, then `systemctl status tailscaled`.
- Phone can't connect? Check the VPN toggle / VPN On Demand in the iOS app
  first — it is almost never the droplet.
