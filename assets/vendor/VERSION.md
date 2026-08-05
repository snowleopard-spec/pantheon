# Vendored: uPlot

- **Version:** 1.6.32 (pinned; latest 1.6.x and latest npm release as of retrieval)
- **License:** MIT (see `LICENSE-uPlot.txt`; copyright (c) 2022 Leon Sorokin)
- **Upstream:** https://github.com/leeoniya/uPlot
- **Retrieved:** 2026-08-05

## Files

| File | Source URL (pinned) | Size (bytes) | SHA-256 |
|---|---|---|---|
| `uPlot.iife.min.js` | https://cdn.jsdelivr.net/npm/uplot@1.6.32/dist/uPlot.iife.min.js | 51081 | `19c8d4c6ad88929a79f4ae49d6f7161566dfd0ba3d15cc495e974f787eb78f1f` |
| `uPlot.min.css` | https://cdn.jsdelivr.net/npm/uplot@1.6.32/dist/uPlot.min.css | 1857 | `df630c6a8d6f8eeaff264b50f73ce5b114f646ffd9a0bb74f049b0a00135fa04` |
| `LICENSE-uPlot.txt` | https://raw.githubusercontent.com/leeoniya/uPlot/1.6.32/LICENSE | 1078 | (license text, MIT) |

The JS is the IIFE build: it assigns the library to a global `var uPlot` and is
safe to inline into a `<script>` tag in the generated report (spec D2/§5 —
report must stay fully self-contained; no CDN references at render time).

To verify integrity: `shasum -a 256 uPlot.iife.min.js uPlot.min.css`
