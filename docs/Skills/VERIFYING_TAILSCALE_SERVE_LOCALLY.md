# Verifying `tailscale serve` on a box that also runs Caddy (the self-curl trap)

**TL;DR:** you cannot verify `tailscale serve` by curling the tailnet URL from
the serving machine itself if anything else (Caddy, nginx) listens on `*:443`.
The self-curl lands on that listener, not on Tailscale — a false negative.
Verify from *any other* tailnet device instead.

## Symptom

Serve is configured and healthy:

```
$ tailscale serve status
https://unicorn-hunt.macaw-dominant.ts.net (tailnet only)
|-- / path  /srv/pantheon
```

…but curling it from the same machine fails the TLS handshake:

```
$ curl https://unicorn-hunt.macaw-dominant.ts.net/
curl: (35) OpenSSL ... tlsv1 alert internal error
```

Nothing appears in `journalctl -u tailscaled` at handshake time — tailscaled
never even sees the connection. The cert is provisioned fine
(`/var/lib/tailscale/certs/` has the `.crt`/`.key`).

## Why

Where a connection to the node's own tailscale IP (here `100.79.67.34:443`)
ends up depends on where it *originates*:

- **From another tailnet device:** packets arrive over WireGuard. tailscaled
  inspects inbound peer traffic and diverts serve ports into its embedded
  netstack *before* the host network stack sees them. Serve answers; Caddy
  never knows.
- **From the machine itself:** locally-originated traffic to a local IP
  short-circuits through the host stack — no WireGuard, no tailscaled
  interception. It is delivered to whatever process holds the port, and
  Caddy's `*:443` wildcard listener wins. Caddy has no certificate for the
  `*.ts.net` SNI, so it aborts the handshake with `alert internal error`.

So the self-curl doesn't test Tailscale at all — it tests Caddy's reaction to
an unknown SNI.

## How we proved it

Point the same IP:port at a hostname Caddy *does* know, and Caddy answers:

```
$ curl -sk --resolve api.unicornpunk.org:443:100.79.67.34 \
    https://api.unicornpunk.org/ -o /dev/null -w "%{http_code}\n"
404        # Caddy's api.unicornpunk.org vhost responded — same IP, same port
```

Same socket, two SNIs, two different outcomes → the local 443 is Caddy's.

## How to actually verify

1. **From any other tailnet device** (phone, laptop): open or curl the
   `https://<host>.<tailnet>.ts.net/` URL. This exercises the real path
   (WireGuard → tailscaled netstack → serve).
2. Machine-side checks that *are* meaningful on the serving box:
   - `tailscale serve status` — config present, says "(tailnet only)".
   - `tailscale funnel status` — confirm nothing is funneled (public).
   - `journalctl -u tailscaled | grep cert` — `got cert` confirms the ACME
     provisioning worked.
   - `ss -tlnp` — confirm serve added **no** new host-stack listener (it
     lives inside tailscaled), and nothing new is bound to `0.0.0.0`.

## Notes

- This is not a conflict: Caddy on `*:443` and `tailscale serve` on the same
  port coexist indefinitely, because they see disjoint traffic (public
  internet vs. tailnet peers).
- A box with *nothing* on `*:443` doesn't have this trap — there the loopback
  connection reaches tailscaled's local handler and self-curl works, which is
  why generic Tailscale docs suggest it as a check.
- First HTTPS request after enabling serve can take ~15 s while the Let's
  Encrypt cert provisions (DNS-01 via the Tailscale control plane). That
  failure mode is transient and *does* log `cert(...)` lines in the journal —
  distinguishable from the silent Caddy case.
