# Admin Order Operations Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add two state-aware operation controls to each custom-admin order card while reusing the existing Nova Poshta and warehouse action pages without changing Telegram.

**Architecture:** Add small staff-only redirect views in a dedicated storefront admin-actions module. The views resolve the order's current TTN/write-off state at click time and redirect to the existing signed Nova Poshta or warehouse URL. Prefetch write-off state for the order list and render a compact responsive action row in the existing order-card partial.

**Tech Stack:** Django 5, Django TestCase/client, existing `NovaPoshtaDocumentService` action links, existing `warehouse.services.order_links`, server-rendered Django templates, existing admin CSS/JS.

---

### Task 1: Add failing redirect and access tests

**Files:**
- Create: `twocomms/storefront/tests/test_admin_order_operations.py`
- Read: `twocomms/storefront/views/order_actions.py`, `twocomms/warehouse/services/order_links.py`

**Step 1: Write the failing tests**

Cover one behavior per test:

- anonymous users are redirected by both new admin routes;
- staff TTN route redirects to the canonical signed create URL when no API
  document is attached;
- staff TTN route redirects to the canonical scoped delete URL when an API
  document is attached;
- staff warehouse route creates one pending write-off request on first click
  and reuses it on the next click;
- staff warehouse route redirects to the cancel-sale URL after completion;
- completed/cancelled order states do not expose a create-TTN redirect.

Patch the URL builders and assert exact arguments/redirect targets so the
tests prove that Telegram's routes are reused rather than duplicated.

**Step 2: Run tests to verify they fail**

Run:

```bash
DEBUG=1 SECRET_KEY=local-test-key python twocomms/manage.py test storefront.tests.test_admin_order_operations
```

Expected: import/URL reverse failures because the admin routes and views do
not exist yet.

### Task 2: Implement staff-only action redirects

**Files:**
- Create: `twocomms/storefront/views/admin_order_actions.py`
- Modify: `twocomms/storefront/urls.py`
- Modify: `twocomms/storefront/views/__init__.py` only if the URL loader needs
  an export (prefer the module-view pattern already used in `urls.py`)

**Step 1: Implement the minimal redirect views**

- Decorate both views with `staff_member_required` and `require_GET`.
- Load the order with `get_object_or_404`.
- For TTN, select create/delete based on `nova_poshta_document_ref`; include
  the document ref as the delete token scope. If the current state is blocked
  by the canonical action page, redirect back to `admin_panel?section=orders`
  with a warning rather than exposing a dead action.
- For warehouse, use `get_completed_write_off`; otherwise call
  `build_storage_writeoff_url` only at click time. Redirect to the exact
  canonical URL returned by the existing service.
- Use `messages.warning` for invalid/stale state and preserve the order id in
  the return URL.

**Step 2: Add narrow URL patterns**

Add `admin-panel/orders/<int:order_id>/nova-poshta/` and
`admin-panel/orders/<int:order_id>/warehouse-action/` names under the existing
admin-panel routes.

**Step 3: Run the focused tests**

```bash
DEBUG=1 SECRET_KEY=local-test-key python twocomms/manage.py test storefront.tests.test_admin_order_operations
```

Expected: all redirect/access tests pass.

### Task 3: Add failing admin-card state/render tests

**Files:**
- Modify: `twocomms/storefront/tests/test_admin_order_operations.py`
- Read: `twocomms/storefront/views/admin.py`
- Read: `twocomms/twocomms_django_theme/templates/partials/admin_orders_section.html`

**Step 1: Write tests for rendered card states**

As a logged-in staff user, request `reverse('admin_panel') + '?section=orders'`
with orders covering:

- no TTN and no completed write-off: create-TTN and write-off controls;
- API TTN: tracking value plus unlink control, no create-TTN control;
- manual tracking only: tracking value and no API unlink control;
- completed write-off: cancel-sale control instead of write-off;
- cancelled order: no carrier/warehouse operation controls.

Assert stable `data-*` hooks and URL names rather than brittle visual details.

**Step 2: Run the tests to verify they fail**

```bash
DEBUG=1 SECRET_KEY=local-test-key python twocomms/manage.py test storefront.tests.test_admin_order_operations
```

Expected: the response lacks the new controls and context values.

### Task 4: Implement efficient context and compact controls

**Files:**
- Modify: `twocomms/storefront/views/admin.py` around `_build_orders_context`
- Modify: `twocomms/twocomms_django_theme/templates/partials/admin_orders_section.html`

**Step 1: Extend order context without N+1 queries**

Prefetch `warehouse_write_off_requests` with only the fields needed for
pending/completed state. Attach a small state object/flags to each rendered
order. Do not call `build_storage_writeoff_url` while rendering.

**Step 2: Add the operation row**

Use the current scoped admin palette and existing icon/button conventions:

- create button links to the staff TTN redirect;
- API-created TTN displays the tracking number and unlink button;
- storage button links to the staff warehouse redirect and changes label when
  a completed write-off exists;
- `target="_blank"` and `rel="noopener"` preserve the order list;
- hide the row's invalid actions for cancelled/done states and preserve all
  existing card JS hooks.

**Step 3: Run the render tests**

```bash
DEBUG=1 SECRET_KEY=local-test-key python twocomms/manage.py test storefront.tests.test_admin_order_operations
```

Expected: all redirect and render tests pass.

### Task 5: Regression verification and cleanup

**Files:**
- Modify only files already listed if cleanup is required.

**Step 1: Run focused suites**

```bash
DEBUG=1 SECRET_KEY=local-test-key python twocomms/manage.py test \
  storefront.tests.test_admin_order_operations \
  storefront.tests.test_feed_admin \
  storefront.tests.test_nova_poshta_delivery \
  storefront.tests.test_telegram_order_status_actions \
  warehouse.tests.test_sale_flow \
  warehouse.tests.test_write_off
```

**Step 2: Run project checks**

```bash
DEBUG=1 SECRET_KEY=local-test-key python twocomms/manage.py check
git diff --check
```

**Step 3: Inspect the diff**

Confirm no Telegram source/template changed and no migration was generated.
Use a browser/staff test or Django test client to verify the order card at
mobile and desktop widths, then exercise redirect destinations with mocked
Nova Poshta/warehouse services only.

**Step 4: Commit implementation**

```bash
git add twocomms/storefront/views/admin_order_actions.py \
  twocomms/storefront/views/admin.py \
  twocomms/storefront/urls.py \
  twocomms/twocomms_django_theme/templates/partials/admin_orders_section.html \
  twocomms/storefront/tests/test_admin_order_operations.py
git commit -m "feat(admin): add order TTN and warehouse actions"
```

After the commit, push the branch, deploy using the user's server command, and
verify the deployed SHA, admin page, and both redirect actions without making a
live stock mutation or carrier API test call.
