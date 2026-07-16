# PROD-005 — Redirect fallback makes 404 handling depend on MariaDB

**Priority:** P1
**State:** fixed `59d1ad1f` (2026-07-16)
**Owner:** middleware/SEO

## Symptom

During a DB outage, an unknown URL, typo, scanner probe or missing icon can become a 500 even if the 404 template itself is safe.

## Cause

`SubdomainRedirectFallbackMiddleware` in `twocomms/twocomms/middleware.py:227-241` applies Django's `RedirectFallbackMiddleware` to every non-DTF host. On a 404, Django resolves the current `Site` and queries `django.contrib.redirects.Redirect`.

The upstream middleware handles `Redirect.DoesNotExist`, not `OperationalError`/`InterfaceError`. With the DB unavailable, local reproduction produces:

```text
SubdomainRedirectFallbackMiddleware
-> RedirectFallbackMiddleware.process_response
-> Site.objects.get_current()
-> OperationalError
```

Thus an outage expands from DB-dependent business pages to otherwise cheap 404 paths and creates more alerts/work.

## Implementation plan

1. Write tests for an unknown path with healthy DB, failed DB and failed cache on each host.
2. Limit redirect lookup to the main storefront hosts that actually use the redirect table.
3. Skip known scanner, static/media, icon and service paths before any DB lookup.
4. Catch narrowly defined DB connection errors and fail open to the original 404 response. Do not hide programming/query errors.
5. Consider publishing a bounded redirect map to cache/edge if redirect availability must survive DB outages.
6. Preserve `APPEND_SLASH`, intentional 301/410 behavior and locale routing in regression tests.

## Acceptance criteria

- DB unavailable plus unknown URL returns the original DB-free 404, not 500.
- Existing redirect rows still produce their intended status/location when DB is healthy.
- Error logs identify fallback bypass/failure without Telegram flooding.
- No additional DB query occurs for excluded hosts and paths.

## Resolution

`SubdomainRedirectFallbackMiddleware` now skips every routed subdomain and
limits redirect-table behavior to the primary storefront. Narrow `DatabaseError`
fail-open handling preserves the original 404 during an unavailable redirect
lookup. Focused production error/middleware tests passed 5/5; the implementation
commit is `59d1ad1f`.
