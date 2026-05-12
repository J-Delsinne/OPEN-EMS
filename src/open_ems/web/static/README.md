# OPEN-EMS static assets

These files are served at `/static/*` and committed to the repository.
No CDN, no build step, no Node.js — per UX spec §"Local-first constraint"
(no CDN for any asset) and the Story 10.1 / 9-X zero-new-Python-dependency
contract.

## Files

| File | Purpose | Source / version |
|---|---|---|
| `open-ems.css` | Hand-authored CSS bundle with design tokens (Story 10.1 AC19 Path B). | n/a (in-repo) |
| `alpine.min.js` | Alpine.js 3.x — client-side optimistic state machine for the EV override button (Story 10.2 AC15). | https://unpkg.com/alpinejs@3.13.10/dist/cdn.min.js · sha256: `fb9b146b7fbd1bbf251fb3ef464f2e7c5d33a4a83aeb0fcf21e92ca6a9558c4b` |

## Updating Alpine.js

1. Download the pinned version:
   ```sh
   curl -fsSL https://unpkg.com/alpinejs@<version>/dist/cdn.min.js -o src/open_ems/web/static/alpine.min.js
   ```
2. Recompute the SHA256 and update this README:
   ```sh
   python -c "import hashlib; print(hashlib.sha256(open('src/open_ems/web/static/alpine.min.js', 'rb').read()).hexdigest())"
   ```
3. Verify the `evOverride` factory in `templates/dashboard.html` still
   parses (`x-data`, `x-on:click`, `x-bind:disabled` are 3.x stable APIs).
