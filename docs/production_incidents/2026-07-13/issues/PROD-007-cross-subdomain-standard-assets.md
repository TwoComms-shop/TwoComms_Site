# PROD-007 — Standard assets are inconsistent across subdomains

**Priority:** P2
**State:** partial `169e6032`; cross-subdomain/edge work remains
**Owner:** web-server routing plus per-host URL policy

## Symptom

Browsers and crawlers request `/favicon.ico`, `/apple-touch-icon.png`, `/apple-touch-icon-precomposed.png` and `/robots.txt` at the host root. Main storefront has some aliases; management, fin, storage and DTF do not expose a uniform policy. Expected 200/302/404 responses sometimes become 500 through PROD-004/005 or process saturation.

## Facts

- Main `/favicon.ico` redirects to a static asset; main `/robots.txt` returns a pure `HttpResponse`.
- Apple root aliases are absent.
- Main base template advertises tracked icon sizes, but iOS NetworkingExtension still makes conventional root requests.
- Other URLconfs omit the main routes.
- Current collected static contains favicon and common SVG assets.
- `twocomms/urls.py` calls Django `static()` in production, but with `DEBUG=False` that helper does not provide production media serving. LiteSpeed/cPanel behavior must be authoritative.

## Implementation plan

1. Define robots policy for each public and service host.
2. Prefer LiteSpeed aliases/redirects for favicon and Apple icons so they never invoke Django or DB.
3. If edge configuration is unavailable, add explicit DB-free routes to every relevant URLconf.
4. Normalize `MEDIA_URL` assumptions: base settings and production differ on the leading slash, while middleware performs literal `startswith` checks.
5. Document which layer serves existing static/media and how a missing asset is handled.

## Verification and acceptance criteria

For main, management, fin, storage and DTF, verify:

- favicon;
- both Apple aliases;
- robots;
- one existing static asset;
- one existing media asset;
- one missing media asset.

Allowed outcomes are an intentional 200/302 or clean 404, never 500. Results must remain the same with DB/cache unavailable and must not emit Telegram ERROR.

## Partial resolution

The main storefront `/favicon.ico` now returns a direct cacheable 200 and the
legacy manifest alias also returns 200 (`169e6032`). The DB-free error-handler
and redirect-fallback work in PROD-004/005 prevents ordinary subdomain misses
from escalating through the known application paths. Apple aliases, uniform
management/finance/storage/DTF root assets and serving outside Django are still
open, so this finding remains `[o]` rather than closed.
