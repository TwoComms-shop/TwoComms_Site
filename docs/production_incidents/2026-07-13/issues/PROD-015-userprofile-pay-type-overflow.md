# PROD-015 — UserProfile payment value exceeds database column

**Priority:** P1
**State:** fixed `ed4aeb8d` (2026-07-16)
**Owner:** accounts/storefront data model

## Symptom

Five logical save attempts in current stderr end with:

```text
django.db.utils.DataError: (1406, "Data too long for column 'pay_type' at row 1")
```

The exception is caught in cart profile save. Autosave returns HTTP 200 with `{ok:false, reason:error}` and non-AJAX shows a generic message, so it may not appear as Telegram 500 even though user data is not saved.

## Exact mismatch

- `twocomms/accounts/models.py:20-23`: `UserProfile.pay_type` has `max_length=10` and legacy choices `full`/`partial`.
- `twocomms/storefront/views/cart.py:497-506` tries to call a normalization helper from package `storefront.views`.
- That package no longer exposes the helper, so fallback accepts `online_full` or `prepay_200`.
- `online_full` is 11 characters and cannot fit the 10-character column.
- Order model already uses canonical values and a longer field, so profile/order contracts have drifted.

## Implementation plan

1. Define one canonical payment enum/normalizer shared by profile, checkout and Order.
2. Audit production distinct values/counts without exporting user rows.
3. Add a migration to widen/alter the profile field and map legacy values deliberately.
4. Replace dynamic import/fallback with the shared normalizer.
5. Validate at form/API boundary and never write an unsupported value.
6. Improve autosave error semantics so schema/programming failure is observable without exposing details.

## Tests

- legacy `full`/`partial` migrate to intended canonical values;
- `online_full`, `prepay_200` and any supported COD value save and round-trip;
- invalid value is rejected before DB write;
- cart autosave and checkout use the same normalization;
- migration rollback/data compatibility is explicitly tested or documented.

## Acceptance criteria

- No code 1406 for `pay_type` under old sessions/profiles and new submissions.
- Profile and Order display/business logic agree for every allowed value.

## Resolution

`UserProfile.pay_type` now shares the canonical payment contract with checkout
and orders, uses a 20-character column, accepts legacy inputs only through the
normalizer, and rejects unsupported boundary values. Migration `accounts.0028`
widened the production column and mapped 55 `full` rows to `online_full` and
four `partial` rows to `prepay_200`; the existing `prepay_200` row remained
unchanged. A private id/pay_type rollback snapshot was stored server-side before
the migration. Production now contains 55 `online_full` and five `prepay_200`
profiles, with no non-canonical values.

Local and production focused suites passed 130/130. Production `manage.py
check`, HEAD parity and storefront/cart smoke checks also passed.
