# Tailscale — the steps only you can do

The Pantheon droplet build (spec §4, H1–H9) is almost entirely automatable, but
Tailscale deliberately puts a human in the loop at a few points: account
identity, device approval, and anything that happens on your phone. This doc
walks through exactly those steps, in order, with the "why" for each. Everything
not listed here (installing Tailscale on the droplet, configuring `serve`,
verifying nothing is publicly exposed) is Claude's job in M5/M6.

---

## Before the build (do these now — M5 is blocked until they're done)

### H1. Create a Tailscale account

Go to https://login.tailscale.com/start and sign up.

**The one real decision is the sign-in identity** (Apple / Google / GitHub /
email + passkey). This matters more than it looks: your tailnet is *anchored*
to this identity forever — every device you add later (droplet, Mac, iPhone)
authenticates against it, and migrating a tailnet between identities is
painful. Pick the identity you're confident you'll keep long-term and that has
strong 2FA. Since you're on an iPhone and use Apple private relay email
already, **Apple or GitHub are both sensible**; GitHub has the small advantage
that it's the same identity that owns the Pantheon repo, keeping "infrastructure
identity" in one place.

The free "Personal" plan covers this whole build (up to 3 users / 100 devices —
we need 1 user and 2–3 devices).

### H2. Enable MagicDNS and HTTPS certificates

In the admin console (https://login.tailscale.com/admin):

1. Go to **DNS** (left sidebar).
2. Note your **tailnet name** — it looks like `tail1a2b3c.ts.net` (or a fun
   two-word name on newer accounts). Write it down; the report URL will be
   `https://unicorn-hunt.<tailnet-name>/`.
3. Ensure **MagicDNS** is enabled (it's on by default for new tailnets).
   *Why:* MagicDNS is what lets devices reach each other by hostname
   (`unicorn-hunt`) instead of a raw `100.x.y.z` IP.
4. Under **HTTPS Certificates**, click **Enable HTTPS**.
   *Why:* `tailscale serve` terminates TLS with a real Let's Encrypt
   certificate for your `*.ts.net` name. Without this toggle, Safari on iOS
   would either refuse the connection or nag about an untrusted cert — and
   "Add to Home Screen" web-app behaviour works far better over clean HTTPS.
   (Enabling this publishes your tailnet name in public certificate-transparency
   logs. That reveals the *name* exists, nothing more — no access is granted.)

### H3. Install Tailscale on the iPhone

App Store → "Tailscale" (by Tailscale Inc.) → sign in **with the same identity
you chose in H1**. The app installs a VPN profile (iOS will ask you to allow
it — this is normal; it's how any on-device VPN works).

That's it for now — don't worry about settings in the app yet; H9 below covers
the toggle that matters.

### H4 / H5 — already done

The Polygon key is on the droplet (`/root/pantheon/.env`) and the price cache +
DB were copied across in M1. Nothing for you here.

---

## During the build (Claude will tell you exactly when)

### H6. Approve the droplet onto your tailnet

When Claude runs `tailscale up` on the droplet, the command can't finish by
itself — it prints a URL like:

```
To authenticate, visit:
    https://login.tailscale.com/a/1234abcd5678
```

Open that URL in any browser where you're signed in to Tailscale, and click
**Connect**. *Why a human step:* this is the security model working as
intended — possession of a server is not enough to join it to your private
network; the account owner has to bless each device.

### H7. Confirm the droplet appears

In the admin console → **Machines**, you should see `unicorn-hunt` listed as
connected, with a `100.x.y.z` address. Two small things worth doing while
you're there:

- **Disable key expiry** for the droplet (⋯ menu → *Disable key expiry*).
  *Why:* device keys expire after ~180 days by default, which is right for
  laptops but wrong for a headless server — you don't want the report silently
  dying in six months because a key lapsed.
- Confirm the machine name is `unicorn-hunt` (it's taken from the hostname).
  This name becomes the URL, so if you'd rather the report live at
  `https://pantheon.<tailnet>/`, rename the machine here (⋯ → *Edit machine
  name*) — purely cosmetic, your call. Tell Claude if you rename it.

---

## After the build (verifying on the phone)

### H8. Open the report and pin it

1. On the iPhone, open the Tailscale app and toggle the VPN **on** (top
   switch). The iPhone is now on your tailnet.
2. In Safari, open the URL Claude gives you at the end of M5 —
   `https://unicorn-hunt.<tailnet-name>/`.
3. The Pantheon report should render, dark theme and all. Tap a bucket row to
   confirm the interactive bits (expanding constituents) work.
4. Share button → **Add to Home Screen** → name it "Pantheon". You now have an
   icon that opens straight into the report.

### H9. Make the shortcut work without thinking about the VPN (optional but recommended)

By default, if the Tailscale VPN is toggled off, the home-screen icon opens to
a connection error. Two ways to fix that, in the iOS Tailscale app:

- **VPN On Demand** (Settings inside the app → *VPN On Demand*): iOS
  activates the tunnel automatically whenever something tries to reach a
  tailnet address. Best experience — tap the icon, it just works.
- Or simply leave the VPN toggled on permanently. Tailscale is a mesh —
  traffic to the internet does **not** route through it (unless you enable an
  exit node, which we aren't), so battery and speed impact is negligible.

*Why this matters:* the whole point of this build is "open the report from the
sofa in one tap". On-demand VPN is the difference between that and "one tap,
error, open Tailscale, toggle, go back".

---

## Quick reference

| Step | Where | What |
|---|---|---|
| H1 | login.tailscale.com/start | Create account; pick long-term sign-in identity |
| H2 | Admin console → DNS | Note tailnet name; MagicDNS on; **Enable HTTPS** |
| H3 | iPhone App Store | Install Tailscale, sign in with same identity |
| H6 | Browser | Open the auth URL `tailscale up` prints; click Connect |
| H7 | Admin console → Machines | Droplet visible; **disable key expiry**; (optional) rename |
| H8 | iPhone Safari | Open tailnet URL; Add to Home Screen |
| H9 | iPhone Tailscale app | Enable VPN On Demand (or leave VPN on) |

When H1–H3 are done, say so and M5 starts. The droplet-side install touches
nothing public: no new open ports, no ufw changes, no Caddy involvement —
that's all verified as part of M5.
