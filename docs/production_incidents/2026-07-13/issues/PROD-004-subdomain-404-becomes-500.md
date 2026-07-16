# PROD-004 — `management` and `fin` turn ordinary 404 into 500

**Priority:** P0
**State:** fixed `013f6f9d` (2026-07-16)
**Owner:** Django routing/templates

## Failure chain

```text
unknown path on management or fin
-> Django default handler404
-> templates/404.html
-> extends storefront base.html
-> {% url 'blog_rss' %}
-> blog_rss is absent from that subdomain URLconf
-> NoReverseMatch inside the 404 handler
-> Django converts the intended 404 to bare HTTP 500
```

The final log often contains only `Internal Server Error: /path`, without the original exception or incident code. This matches the Telegram alerts for robots, WordPress probes, favicon, Apple aliases and missing media.

## Code evidence

- `twocomms/twocomms/middleware.py:191-221` selects a URLconf from the host.
- `twocomms/twocomms/urls_management.py` and `urls_fin.py` define neither `handler404` nor `handler500`.
- `urls_storage.py:13-14` and `urls_dtf.py:13-14` show working subdomain-specific handlers.
- `twocomms/twocomms_django_theme/templates/404.html:1` extends the common base.
- `twocomms/twocomms_django_theme/templates/base.html:182` reverses `blog_rss` unconditionally.
- `blog_rss` exists only in the main `twocomms/urls.py` URLconf.

## Reproduction

With the DB-backed redirect middleware isolated, an unknown path returns 404 on main/storage/DTF and 500 on management/fin. With exception propagation enabled, the original exception is `NoReverseMatch` for `blog_rss`. The same result reproduces for `/robots.txt`, `/wp-login.php`, favicon/Apple paths and the missing category icon.

## Production support

- Across retained access logs, harmless/scanner paths alternate among 301/400/404/500.
- Main `/robots.txt` worked 63 times on 13 July, while two requests returned 500, supporting a host-specific failure.
- The category-icon event has a management referer.
- The mixed access log does not include `Host`; do not assert a particular host for every bare alert without new structured logging.

## Implementation plan

1. Add failing host-matrix tests for unknown paths under production-like `DEBUG=False`.
2. Create minimal autonomous 404 and 500 views/templates for management and fin.
3. Register handlers directly in both URLconfs.
4. Error templates must not inherit storefront base, reverse optional routes, query DB/cache, or rely on ordinary context processors.
5. Attach one incident/request ID to a genuine 500 without logging a second ERROR.
6. Test rendering while DB and cache are unavailable.

## Acceptance criteria

- Unknown paths on main, management, fin, storage and DTF return deliberate 404.
- `/robots.txt`, WP probes, favicon, Apple aliases and missing media never become 500 because of error rendering.
- A 404 does not emit Telegram ERROR.
- A genuine subdomain exception returns the minimal 500 page and one correlated incident.

## Related

PROD-005 explains the independent DB-backed 404 failure that can affect non-DTF hosts even after this template bug is fixed.

## Resolution

Management and finance now register autonomous 404/500 handlers. The plain-text
500 fallback is resilient even if the normal error template fails and exposes a
trace ID in both the body and `X-Error-Trace-ID`. Focused error and redirect
middleware tests passed 5/5 on production at `569626c3`; the implementation
commit is `013f6f9d`.
