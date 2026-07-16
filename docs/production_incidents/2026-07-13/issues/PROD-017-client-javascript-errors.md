# PROD-017 — Client JavaScript errors are partly real, partly historical/noise

**Priority:** P2
**State:** one confirmed open bug, one resolved build bug, remaining Safari events need instrumentation
**Owner:** frontend/observability

## Evidence

`client_errors.log` contains 32 entries but no timestamps or build revision:

- 14 references to `initializePixelsImmediately` being undefined (nine Chromium form, five Safari form);
- 12 Safari `Attempted to assign to readonly property` events on catalog/custom-print;
- two `window.webkit.messageHandlers` undefined-object events;
- one `isStatsTab is not defined` on admin promocodes;
- one generic cross-origin `Script error`, one audit sentinel and one blank record.

## Classification

### Analytics initializer — historical/resolved, monitor

Affected reports reference collected hash `analytics-loader.3975317011e4.js`, which still contains the obsolete call. Commit `3291ac82` changed bfcache restore to the defined deferred initializer and added a source regression test; current collected hash is different and clean. Old immutable assets remain on disk and old pages/clients may report until reload. Add timestamps/build IDs before deciding whether cleanup is needed.

### `isStatsTab` — confirmed open

`twocomms/twocomms_django_theme/templates/pages/admin_promocodes.html:2637` reads `isStatsTab`, and no declaration exists in the template. This is a deterministic ReferenceError and can stop subsequent chart initialization.

### Safari readonly/messageHandlers — unproven ownership

No application reference to `window.webkit.messageHandlers` was found. These events may come from an in-app browser/third-party injected script. The readonly error lacks stack/source detail. Do not change application behavior based on the message alone.

## Implementation plan

1. Add timestamp, revision, static asset hash, page/host, navigation type and stack/source to client-error records; strip query PII.
2. Fix/guard `isStatsTab` and test promo page under each section/tab.
3. Deduplicate by fingerprint/build/session and rate limit client telemetry.
4. Reproduce Safari events on supported devices/in-app browsers with third-party scripts toggled independently.
5. Classify known browser-extension/injected-script noise separately instead of Telegram escalation.
6. Keep the analytics bfcache regression test and verify collected manifest points to the fixed file after deploy.

## Acceptance criteria

- Promo page has no undefined global and charts initialize only when applicable.
- New client records can be tied to time/build and include actionable stack data.
- No new fixed-build analytics initializer event occurs over seven days.
- Safari issues are either reproduced to owned code and fixed or explicitly filtered with evidence.
