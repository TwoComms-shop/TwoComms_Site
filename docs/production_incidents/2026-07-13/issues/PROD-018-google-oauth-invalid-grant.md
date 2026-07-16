# PROD-018 — Google OAuth token exchange returns `invalid_grant`

**Priority:** P2
**State:** repeated but not yet uniquely attributed
**Owner:** authentication/social-auth

## Evidence

Retained logs contain six `requests.exceptions.HTTPError` responses with Google token endpoint HTTP 400/`invalid_grant`. Access-log callbacks to `/oauth/complete/google-oauth2/` return 302, including repeated callbacks within the same second/minute. Because application logs have no timestamp and OAuth query values must not be copied, exact one-to-one correlation is currently impossible.

## Likely causes to distinguish

- authorization code submitted more than once due double callback/reload/back navigation;
- expired code;
- redirect URI mismatch across host/scheme;
- state/session cookie loss;
- server clock skew.

The log does not prove which cause applies. A 302 may be the configured login-error redirect, not successful authentication.

## Investigation/implementation plan

1. Add a safe OAuth attempt correlation ID and outcome category without logging code, token, email or raw query.
2. Record callback host/scheme, state-present boolean, code-present boolean, provider error category and redirect target.
3. Verify server clock/NTP and exact registered redirect URIs for all supported hosts.
4. Prevent duplicate processing of one callback in session/cache with a short-lived one-time marker.
5. Treat expired/replayed user-flow errors as an expected login failure with clear retry UI, not an unbounded ERROR traceback.
6. Test state/cookie behavior across locale redirects and HTTP-to-HTTPS/www redirects.

## Acceptance criteria

- One user login creates one token exchange.
- Double callback/back/reload is idempotently handled and gives a safe retry path.
- Metrics distinguish expiry, replay, state failure and configuration mismatch.
- No authorization code/token enters logs, URLs in tickets or analytics.
