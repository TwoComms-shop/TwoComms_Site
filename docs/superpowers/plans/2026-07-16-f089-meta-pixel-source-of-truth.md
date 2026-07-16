# F-089 Meta Pixel Source Of Truth Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make browser Pixel, storefront CAPI, and management IG CAPI use one resolved Meta Pixel ID in every environment.

**Architecture:** Resolve the existing `META_PIXEL_ID`/legacy `FACEBOOK_PIXEL_ID` environment inputs once in Django settings, expose `FACEBOOK_PIXEL_ID` only as a compatibility alias, and make all runtime consumers read the canonical `META_PIXEL_ID`. Remove the context processor's second hardcoded fallback so configuration cannot silently diverge after settings load.

**Tech Stack:** Django 5.2 settings, context processors, Django `SimpleTestCase`, Facebook CAPI service.

---

### Task 1: Add configuration contract regressions

**Files:**
- Create: `twocomms/storefront/tests/test_meta_pixel_configuration.py`

- [x] **Step 1: Write failing tests for the settings alias and all runtime consumers**

```python
from types import SimpleNamespace

from django.conf import settings
from django.test import SimpleTestCase, override_settings

from management.services.ig_meta_events import _has_capi_env
from orders.facebook_conversions_service import FacebookConversionsService
from storefront.context_processors import analytics_settings


class MetaPixelConfigurationTests(SimpleTestCase):
    def test_legacy_settings_name_is_an_alias_of_canonical_pixel_id(self):
        self.assertEqual(settings.FACEBOOK_PIXEL_ID, settings.META_PIXEL_ID)

    @override_settings(META_PIXEL_ID="", FACEBOOK_PIXEL_ID="legacy-value")
    def test_context_processor_does_not_add_a_second_fallback(self):
        self.assertEqual(analytics_settings(SimpleNamespace())["META_PIXEL_ID"], "")

    @override_settings(
        META_PIXEL_ID="canonical-value",
        FACEBOOK_PIXEL_ID="",
        FACEBOOK_CONVERSIONS_API_TOKEN="",
    )
    def test_storefront_capi_reads_canonical_pixel_id(self):
        self.assertEqual(FacebookConversionsService().pixel_id, "canonical-value")

    @override_settings(
        META_PIXEL_ID="canonical-value",
        FACEBOOK_PIXEL_ID="",
        FACEBOOK_CONVERSIONS_API_TOKEN="token",
    )
    def test_ig_capi_gate_reads_canonical_pixel_id(self):
        self.assertTrue(_has_capi_env())
```

- [x] **Step 2: Run the focused test and verify RED**

Run: `cd twocomms && python manage.py test storefront.tests.test_meta_pixel_configuration -v 2`

Expected: failures prove the current settings values diverge, the context processor masks empty settings, and both CAPI paths read the legacy value.

### Task 2: Unify the runtime configuration

**Files:**
- Modify: `twocomms/twocomms/settings.py`
- Modify: `twocomms/storefront/context_processors.py`
- Modify: `twocomms/orders/facebook_conversions_service.py`
- Modify: `twocomms/management/services/ig_meta_events.py`

- [x] **Step 1: Make the legacy Django setting an alias**

Keep the existing environment precedence and production fallback, then assign:

```python
FACEBOOK_PIXEL_ID = META_PIXEL_ID
```

Remove the later reassignment from `FACEBOOK_PIXEL_ID` directly to the environment.

- [x] **Step 2: Make every consumer read only the canonical setting**

Use `settings.META_PIXEL_ID` in the context processor, storefront CAPI service, and IG CAPI gate. Do not add consumer-local fallbacks.

- [x] **Step 3: Run focused and related tests**

Run: `cd twocomms && python manage.py test storefront.tests.test_meta_pixel_configuration storefront.tests.test_analytics_loader storefront.tests.test_analytics_tracking management.tests_ig_sales_automation -v 2`

Expected: all tests pass with zero failures.

### Task 3: Deploy and close the finding

**Files:**
- Modify: `docs/qa/AUDIT_FINDINGS_2026-07-09.md`
- Modify: `docs/qa/README.md`
- Modify: `docs/superpowers/plans/2026-07-16-f089-meta-pixel-source-of-truth.md`

- [x] **Step 1: Verify, commit, and push the runtime slice**

Run the focused tests, Django system check, secret scan, and inspect the exact diff. Commit only F-089 files, fetch/rebase if `origin/main` advanced, then push `main`.

- [x] **Step 2: Deploy and restart Passenger**

Pull `main` on the server, run the focused server tests and `manage.py check`, then touch `tmp/restart.txt`.

- [x] **Step 3: Prove production equality without printing secrets or IDs**

Verify booleans for `META_PIXEL_ID` set/numeric, `FACEBOOK_PIXEL_ID` set/numeric, equality, CAPI token presence, and confirm live HTML contains the canonical ID consistently.

- [ ] **Step 4: Mark F-089 `[x]` and deploy the documentation checkpoint**

Record the runtime commit and fresh production evidence in every F-089 summary/detail location. Commit, push, pull on the server, and verify the production HEAD.
