# PROD-013 — Telegram webhook accepts unauthenticated POST when secret is unset

**Priority:** P0 security
**State:** confirmed/open in effective production environment
**Owner:** operations plus accounts webhook code

## Evidence

Current production emitted the explicit warning that `TELEGRAM_BOT_WEBHOOK_SECRET` is not set and unauthenticated POSTs are accepted.

`twocomms/accounts/telegram_views.py:11-39` behaves as follows:

- secret set: compare request header;
- secret missing: log warning and continue processing;
- mismatch: print part of the received secret and return HTTP 200 with a rejection marker.

The fail-open branch was intentionally transitional, but production never completed the server-side setup. An attacker can submit forged updates, exercise account-linking/bot logic, and inject PII-like payloads into logs. Telegram authenticity cannot be inferred from source IP alone.

## Remediation sequence

1. Generate a strong independent webhook secret in the approved secret store. Do not reuse the bot token or SSH/application secret.
2. Register the webhook with Telegram using `secret_token`; coordinate with PROD-012 token rotation.
3. Verify legitimate deliveries include the expected header.
4. Change application behavior to fail closed when the secret is absent in production.
5. Reject mismatches without printing any received-secret prefix or request body.
6. Add a startup/deploy check that production cannot become ready with an unset secret.
7. Rate limit and metric rejected webhook requests using safe counts only.

## Tests

- absent header, wrong header and empty configured secret are rejected before JSON/business processing;
- correct header succeeds;
- comparison remains constant-time;
- no secret fragment/body appears in logs;
- staging/development override, if needed, is explicit and cannot affect production.

## Acceptance criteria

- Telegram reports the registered secret-backed webhook.
- Production readiness fails if the environment value is absent.
- Forged POST produces no side effect and no sensitive log record.
