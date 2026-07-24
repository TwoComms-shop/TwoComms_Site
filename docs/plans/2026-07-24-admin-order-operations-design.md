# Admin Order Operations Design

Date: 2026-07-24

## Goal

Make the existing Nova Poshta and warehouse actions easy to find from each
order card in the custom staff admin. Telegram notifications and Telegram
menus are out of scope and must remain unchanged.

## User-facing behavior

Each active order card gets one compact operations row:

- When an API waybill is not attached, show `Створити ТТН` (except for orders
  whose existing action page correctly blocks creation, such as `done` and
  `cancelled`). The button opens the existing Nova Poshta form used by the
  Telegram action.
- When an API waybill is attached, show the tracking number in the delivery
  row and a `Відв'язати ТТН` action beside it. That action opens the existing
  Nova Poshta delete confirmation. After deletion, the next card render shows
  `Створити ТТН` again.
- If a tracking number exists without an API document reference, keep it as a
  manually attached TTN. Do not offer API deletion for that state because the
  carrier document cannot be safely deleted without its document reference.
- When there is no completed warehouse sale, show `Списати зі складу`. When a
  completed `WriteOffRequest` exists, show `Відмінити продаж` instead. The
  target is the existing warehouse page that includes the garment and all
  prints matched to order items.

Actions open in a new tab so the operator keeps the order list available on a
phone or desktop. The destination pages and their business behavior remain
the canonical existing pages.

## Backend shape

Add staff-protected, narrow redirect views in the storefront admin boundary:

- Nova Poshta action: load the order, select create or delete based on the
  current persisted document reference, build the existing signed action URL,
  and redirect to it. Recheck state at click time to avoid stale card markup.
- Warehouse action: load the order, select the existing cancel-sale URL when
  a completed write-off exists, otherwise call the existing write-off URL
  builder and redirect to the resulting page. Pending write-off requests are
  created only on click, never while rendering the order list.

The order context will prefetch the relevant write-off requests and expose
small state/URL values for rendering without N+1 queries. No database schema
change is required. Existing Nova Poshta token scope, deletion compensation,
warehouse stock transactions, and Telegram's best-effort post-action update
remain unchanged.

## Template/UI

Extend the existing order-card delivery/action area with a scoped operations
row that uses the current admin palette, compact icon-plus-label buttons, and
responsive wrapping. Preserve every existing JavaScript hook and payment/order
status control. The TTN number remains the primary delivery value; the unlink
action is secondary and only appears for API-created documents.

## Safety and errors

- Only `staff_member_required` users can invoke the redirect endpoints.
- The canonical action pages remain responsible for token validation, order
  state validation, API failures, stock conflicts, and user-facing errors.
- Redirect views return a normal admin-panel redirect with a concise message
  when the order is missing or the requested action is no longer valid.
- No Telegram source file or Telegram keyboard is modified.

## Verification

Add focused tests for staff access, current-state redirects, write-off request
reuse/creation, and order-card rendering for create/attached/unlink and
write-off/cancel states. Run the existing Nova Poshta, warehouse, admin-card,
and related status suites, `manage.py check`, then verify the rendered card at
desktop and mobile sizes and exercise both action links against the deployed
application without creating live stock or carrier side effects.
