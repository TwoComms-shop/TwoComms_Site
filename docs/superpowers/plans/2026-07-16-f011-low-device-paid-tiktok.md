# F-011 Low-Device Paid TikTok Bootstrap Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Preserve TikTok PageView attribution for low-end/save-data visitors arriving from paid campaigns without loading TikTok for low-end organic traffic.

**Architecture:** Keep the existing deferred analytics architecture for organic traffic and the immediate paid-landing loader injection in `base.html`. Detect the same click-ID/UTM marker inside `analytics-loader.js`, invoke its idempotent pixel initializer immediately for paid landings, exempt only paid landings from the low-device TikTok suppression, and bump the static query version so deployed HTML cannot reuse the old loader.

**Tech Stack:** Vanilla JavaScript, Django `SimpleTestCase`, Node syntax check, Playwright network interception, Django static files.

---

### Task 1: Add the paid low-device regression

**Files:**
- Modify: `twocomms/storefront/tests/test_analytics_loader.py`
- Test: `twocomms/storefront/tests/test_analytics_loader.py`

- [x] **Step 1: Reuse a loader-source helper and assert the paid exception**

```python
class AnalyticsLoaderRegressionTests(SimpleTestCase):
    @staticmethod
    def _loader_source():
        loader_path = (
            Path(__file__).resolve().parents[2]
            / "twocomms_django_theme"
            / "static"
            / "js"
            / "analytics-loader.js"
        )
        return loader_path.read_text(encoding="utf-8")

    def test_paid_landing_loads_tiktok_on_low_device(self):
        source = self._loader_source()

        self.assertIn(
            "var isPaidLanding = /[?&](gclid|fbclid|ttclid|wbraid|gbraid|msclkid|utm_source|utm_medium|utm_campaign)=/i.test(win.location.search);",
            source,
        )
        self.assertIn("if (!isLowDevice || isPaidLanding) {", source)
        self.assertIn(
            "if (isPaidLanding) {\n    initializePixelsDeferred();\n  } else {",
            source,
        )
```

- [x] **Step 2: Run the focused test to verify RED**

Run: `cd twocomms && python manage.py test storefront.tests.test_analytics_loader --settings=test_settings -v 2`

Expected: FAIL because the loader currently uses only `if (!isLowDevice)`.

### Task 2: Restore paid attribution without regressing organic low-device performance

**Files:**
- Modify: `twocomms/twocomms_django_theme/static/js/analytics-loader.js`
- Modify: `twocomms/twocomms_django_theme/templates/base.html`
- Test: `twocomms/storefront/tests/test_analytics_loader.py`

- [x] **Step 1: Detect paid query markers next to the device class**

```javascript
var deviceClass = (doc.documentElement.dataset.deviceClass || '').toLowerCase();
var isLowDevice = deviceClass === 'low';
var isPaidLanding = /[?&](gclid|fbclid|ttclid|wbraid|gbraid|msclkid|utm_source|utm_medium|utm_campaign)=/i.test(win.location.search);
```

- [x] **Step 2: Exempt paid landings from only the TikTok low-device guard**

```javascript
if (!isLowDevice || isPaidLanding) {
  loadTikTokPixel();
}
```

Do not change the GA4, Clarity, interaction, idle-delay, consent, or event-buffer behavior.

- [x] **Step 3: Invoke the idempotent initializer immediately for paid landings**

```javascript
if (isPaidLanding) {
  initializePixelsDeferred();
} else {
  // Existing interaction listeners and idle fallback stay here.
}
```

This must bypass interaction and idle waiting for all paid-marker landings. Do
not merely inject the loader file earlier; the initializer itself must run.

- [x] **Step 4: Bump the loader query version in the base template**

```javascript
var src = "{% static 'js/analytics-loader.js' %}?v=9";
```

- [x] **Step 5: Run GREEN, syntax, and frozen-idle browser verification**

Run: `cd twocomms && python manage.py test storefront.tests.test_analytics_loader --settings=test_settings -v 2`

Run: `node --check twocomms/twocomms_django_theme/static/js/analytics-loader.js`

Run a local Playwright harness that replaces `requestIdleCallback` with a
non-calling stub before loading the JavaScript and intercepts
`analytics.tiktok.com`. Expected: paid-low and paid-normal each request one SDK
script immediately; organic-low requests none; one interaction after paid init
does not produce a duplicate request.

- [x] **Step 6: Commit, push, deploy, and verify server assets**

Commit the plan, test, JavaScript and template only. Push `main`, pull with `--ff-only`, run the focused server suite, run `collectstatic --noinput`, restart Passenger, and verify live HTML references `v=9` and live/local loader hashes match.

### Task 3: Verify browser behavior and close the finding

**Files:**
- Modify: `docs/qa/AUDIT_FINDINGS_2026-07-09.md`
- Modify: `docs/qa/PRE_ADS_MASTER_AUDIT_CHECKLIST.md`
- Modify: `TWOCOMMS_A_TO_B/technical/audit_report_section2_analytics.md`
- Modify: `TWOCOMMS_A_TO_B/technical/twocomms_global_audit.md`
- Modify: `docs/superpowers/plans/2026-07-16-f011-low-device-paid-tiktok.md`

- [x] **Step 1: Run intercepted Playwright production checks**

Before navigation, intercept `analytics.tiktok.com` so the browser records but does not execute the third-party SDK. Emulate a low device before page scripts run, freeze `requestIdleCallback`, then verify:

```text
paid + low device: events.js request observed
organic + low device: no events.js request before interaction/idle window
paid + normal device: events.js request observed
```

Also verify `window.ttq.load` exists for both paid cases and no duplicate SDK request occurs after one interaction.

- [x] **Step 2: Reclassify and close F-011 with exact evidence**

Record that missing inline `ttq.load` was a false-positive assumption because the external deferred loader owns bootstrap. Close the actual paid low-device residual only after server tests and the intercepted browser matrix pass. Use `[x]/FIXED/DONE`; preserve the dated raw-HTML observation as historical evidence.

- [ ] **Step 3: Commit, push, deploy, and verify documentation**

Commit only the audit-document changes, push `main`, deploy with `git pull --ff-only`, and verify server HEAD plus all current F-011 status entries.
