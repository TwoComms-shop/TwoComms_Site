# F-010 Production Debug Routes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove debug, development, and analytics-test routes from the production URL surface while retaining them for explicit `DEBUG=True` development environments.

**Architecture:** Keep the existing view implementations unchanged, but register their URL patterns only when Django settings have `DEBUG=True`. Add a regression matrix that exercises the literal UK, RU, and EN paths with the production-like `test_settings` configuration (`DEBUG=False`), then verify the same behavior on the deployed server.

**Tech Stack:** Django URL routing, Django `TestCase` client, `test_settings`, production HTTP checks.

---

### Task 1: Add the production URL-surface regression

**Files:**
- Modify: `twocomms/storefront/tests/test_seo_regressions.py`
- Test: `twocomms/storefront/tests/test_seo_regressions.py`

- [x] **Step 1: Replace the login-redirect expectation with a failing 404 matrix**

```python
def test_debug_and_dev_routes_are_absent_when_debug_is_false(self):
    self.assertFalse(settings.DEBUG)
    internal_paths = (
        "debug/media/",
        "debug/media-page/",
        "debug/product-images/",
        "dev/grant-admin/",
        "test-analytics/",
        "test-pricelist/",
        "wholesale/debug-invoices/",
    )
    locale_prefixes = ("", "ru/", "en/")

    for locale_prefix in locale_prefixes:
        for internal_path in internal_paths:
            url = f"/{locale_prefix}{internal_path}"
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url, follow=False).status_code, 404)
```

- [x] **Step 2: Run the focused test to verify RED**

Run: `cd twocomms && python manage.py test storefront.tests.test_seo_regressions.PublicUrlIndexationSeoRegressionTests.test_debug_and_dev_routes_are_absent_when_debug_is_false --settings=test_settings -v 2`

Expected: FAIL because the current unconditional routes return login redirects instead of 404.

### Task 2: Restrict route registration to development

**Files:**
- Modify: `twocomms/storefront/urls.py`
- Test: `twocomms/storefront/tests/test_seo_regressions.py`

- [x] **Step 1: Import Django settings in the URL module**

```python
from django.conf import settings
```

- [x] **Step 2: Remove the seven internal paths from the unconditional list and append them only under DEBUG**

```python
if settings.DEBUG:
    urlpatterns += [
        path("debug/media/", views.debug_media, name="debug_media"),
        path("debug/media-page/", views.debug_media_page, name="debug_media_page"),
        path("debug/product-images/", views.debug_product_images, name="debug_product_images"),
        path("dev/grant-admin/", views.dev_grant_admin, name="dev_grant_admin"),
        path("test-analytics/", views.test_analytics_events, name="test_analytics"),
        path("test-pricelist/", _legacy_view("test_pricelist"), name="test_wholesale_prices"),
        path("wholesale/debug-invoices/", _legacy_view("debug_invoices"), name="debug_invoices"),
    ]
```

- [x] **Step 3: Run focused RED-to-GREEN verification**

Run the Task 1 command again.

Expected: PASS for all 21 UK/RU/EN path combinations.

- [x] **Step 4: Run the complete regression module and Django deployment checks**

Run: `cd twocomms && python manage.py test storefront.tests.test_seo_regressions --settings=test_settings -v 1`

Run: `cd twocomms && python manage.py check --deploy --settings=twocomms.production_settings`

Expected: the F-010 regression remains green; deployment check introduces no
new errors. Any pre-existing failures or warnings must be reported rather than
hidden or folded into this finding.

Current local evidence: the focused class passes 7/7 and Django check/compileall
pass. The broader SEO module is 71/75 because four pre-existing Schema.org
assertions also fail in isolated runs. The deployment check exits 0 with 15
pre-existing warnings when supplied a non-production check-only secret; the
real production configuration remains a server-stage check.

- [x] **Step 5: Commit, push, deploy, and verify production paths**

Commit the plan, regression test, and URL change. Push `main`, pull with `--ff-only` on the server, restart Passenger, run the focused test on the server, and verify the 21 live routes return 404 without redirects.

### Task 3: Close the audit finding with deployed evidence

**Files:**
- Modify: `docs/qa/AUDIT_FINDINGS_2026-07-09.md`
- Modify: `docs/qa/PRE_ADS_MASTER_AUDIT_CHECKLIST.md`
- Modify: `TWOCOMMS_A_TO_B/technical/twocomms_global_audit.md`
- Modify the directly related technical audit report if it contains the stale F-010 claim.

- [x] **Step 1: Change only current F-010 status records to `[x]`, `FIXED`, and `DONE`**

Record the implementation commit, server test count, and live UK/RU/EN 404 matrix. Preserve the original dated reproduction table as historical evidence and append a post-fix note.

- [x] **Step 2: Verify the documentation diff**

Run: `git diff --check`

Run: `rg -n "F-010" docs/qa/AUDIT_FINDINGS_2026-07-09.md docs/qa/PRE_ADS_MASTER_AUDIT_CHECKLIST.md TWOCOMMS_A_TO_B/technical/*.md`

Expected: no current `[ ]`, `OPEN`, or `YES` status remains for F-010; historical reproduction text remains labeled by date/context.

- [ ] **Step 3: Commit, push, deploy, and verify server documentation**

Commit only the audit-document changes, push `main`, deploy with `git pull --ff-only`, and verify server HEAD plus the F-010 `[x]/FIXED/DONE` entries.
