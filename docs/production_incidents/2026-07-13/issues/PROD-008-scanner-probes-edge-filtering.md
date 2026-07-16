# PROD-008 — Scanner probes reach Passenger and pollute incident handling

**Priority:** P2 hardening
**State:** confirmed/open
**Owner:** web server/hosting, with safe application fallback

## Classification

- `/.env` requests search for exposed configuration.
- `/wp-login.php`, `/wp-includes/wlwmanifest.xml` and GravitySMTP paths are WordPress vulnerability probes.
- This application is Django; these are not missing features and must not be implemented.

Across accessible logs, top false 5xx noise includes 252×500 for `wlwmanifest.xml`, 61×500 plus one 503 for `/.env`, and repeated WP/GravitySMTP probes. No 200 response for `.env` was found, so there is no evidence the file was served.

## Why it becomes an application incident

The root `.htaccess` includes rules for `.env`/`.git`, yet different attempts receive 400/403/404/500. That suggests inconsistent vhost document roots/rule application or requests reaching Passenger after edge routing. Once in Django, PROD-004/005 can turn the intended denial/404 into 500 and PROD-010 spends the same Telegram budget as a checkout failure.

## Implementation plan

1. Inventory the effective document root and `.htaccess` inheritance for every vhost.
2. Reject before Passenger:
   - `.env*`, `.git*`, VCS/config/backups and common encoded/case variants;
   - known WordPress-only endpoints on this Django deployment.
3. Keep the response small, non-reflective and free of stack details.
4. Rate limit abusive sources at the edge; do not put unbounded IP lists in application code.
5. If cPanel/LiteSpeed cannot apply a common rule, request hosting support.
6. Keep a cheap DB-free 404 as defense in depth.

## Acceptance criteria

- External probes against every host return consistent 403/404, never 200/500.
- They do not start a Passenger request, hit DB, or emit Telegram alerts.
- Encoded/case variants are covered without blocking legitimate storefront paths.
- `DisallowedHost` probes for fake `mail.*` hosts remain rejected; do not add them to `ALLOWED_HOSTS` merely to silence logs.
