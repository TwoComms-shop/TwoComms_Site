# PROD-012 — Telegram credential and personal data leak into retained logs

**Priority:** P0 security
**State:** confirmed/open; credential must be treated as compromised
**Owner:** security/operations and Telegram integration

## Sensitive evidence handling

The raw credential, chat/user identifiers, phone numbers and webhook payloads are intentionally omitted here. Do not copy them from production logs into Git, issues, support tickets or chat.

## Confirmed exposure

- At least 11 retained stderr lines contain a Telegram bot API URL with the credential embedded.
- Current stderr contains raw webhook update payloads; at least two include contact phone data.
- `orders/telegram_notifications.py:144-195` embeds the credential in the request URL and prints caught `requests` exceptions. The exception string includes that URL.
- The same function prints configured admin/chat identifiers for every send.
- `accounts/telegram_bot.py:401-418` prints the entire webhook update and user/message metadata.
- `print()` bypasses `PIIRedactionFilter` and flows directly to Passenger stderr.
- `PIIRedactionFilter` only replaces `record.msg`/`args`; formatted `exc_info` can still contain PII/secrets.
- Large retained backups and Nova logs expand the exposure and make manual cleanup unreliable.

## Immediate response plan

1. Record the incident without the secret value or customer PII.
2. Rotate/revoke the bot token using the official Telegram control path.
3. Update production secret storage and re-register all dependent webhooks/jobs.
4. Verify old token rejection without printing either token.
5. Inventory every current, rotated, compressed and backup log containing the token fingerprint or raw payload.
6. Securely remove/redact exposed records according to the hosting filesystem/backup capabilities; ask the host about server-side backups that the account cannot access.
7. Review repository/history and process configuration for accidental copies, but do not echo environment contents.

## Code remediation

1. Replace unconditional prints with structured logger calls containing only safe outcome/category fields.
2. Never include credentials in logged URLs. Sanitize `requests` exception/request objects before formatting.
3. Log webhook update type and safe internal correlation ID, never raw body/contact/text.
4. Implement a redacting formatter that sanitizes the final formatted exception as well as message arguments.
5. Explicitly redact authorization headers, query/body tokens, Telegram URLs, phone/email, chat/user IDs and OAuth codes.
6. Restrict log file permissions and retention.

## Tests

Create synthetic sentinel values shaped like a token, phone, email, chat ID and OAuth code. Trigger:

- successful and failed Telegram request;
- exception with sensitive URL in `exc_info`;
- webhook with contact/message payload;
- rotation/archive.

Assert the sent alert, current log and every rotated file contain none of the sentinels.

## Completion criteria

- Old credential is rejected and new credential works.
- All accessible retained copies are removed/redacted, and host backup scope is documented.
- Automated secret/PII scan is clean.
- No raw Telegram payload or identifier appears during end-to-end webhook/send tests.

## Related

PROD-013 must be completed during token/webhook re-registration.
