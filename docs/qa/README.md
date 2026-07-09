# QA audit docs — main site `twocomms.shop`

Large pre-ads / pre-scale audit pack. **No code fixes** during checklist walks.

## Files

| File | Role |
|------|------|
| [`PRE_ADS_MASTER_AUDIT_CHECKLIST.md`](./PRE_ADS_MASTER_AUDIT_CHECKLIST.md) | **v2** walkable mega-checklist (IDs: SEO-*, CRO-*, UTM-*, CART-*, PIX-*, TECH-*, …) |
| [`AUDIT_FINDINGS_2026-07-09.md`](./AUDIT_FINDINGS_2026-07-09.md) | Pass A/B findings F-001…F-086 (prod truth) |
| [`PLAN_VS_FINDINGS_2026-07-09.md`](./PLAN_VS_FINDINGS_2026-07-09.md) | **Re-verify** `TWOCOMMS_A_TO_B/technical/IMPLEMENTATION_PLAN.md` DONE/OPEN vs prod + F-* (includes REOPEN) |
| [`AUDIT_FINDINGS_TEMPLATE.md`](./AUDIT_FINDINGS_TEMPLATE.md) | Copy → `AUDIT_FINDINGS_YYYY-MM-DD.md` |
| (optional) `AUDIT_PROGRESS_YYYY-MM-DD.md` | Checkbox status log if you prefer not to edit master |

## Four pillars (v2)

1. **SEO + GEO** — every public page matrix + molecular titles/canonical/sitemap/404  
2. **CRO / funnel** — ads → land → PDP → ATC → mini-cart → cart → checkout → purchase; drop-offs from Dispatcher/`UserAction`  
3. **UTM + Pixel** — middleware, order attribution, Admin **Диспетчер**, ATC/Purchase dual-channel  
4. **TECH** — scripts, cart path, logs, Telegram alert classes, favicon/PWA “broken icon” class  

## Workflow

```text
Pass A  walk checklist on PRODUCTION
Pass B  write findings MD
Pass C  independent confirm
Pass D  fix only CONFIRMED (separate PR)
```

## Rules

1. Production MySQL + live HTTP = truth (local is not).  
2. Never commit secrets (SSH, DB, API tokens, `.env`).  
3. Mark every item: `PASS` / `FAIL` / `WARN` / `N/A` / `BLOCKED` / `SKIP`.  
4. Ads gate: **BLOCKED** if any open P0 FAIL.

## Code entry points

- Funnel: `storefront/utm_tracking.py`, `models.UserAction`  
- Dispatcher: `storefront/views/admin.py` → `_build_dispatcher_context`  
- UTM rules: `twocomms/docs/UTM_GOVERNANCE.md`  
- Cart: `storefront/views/cart.py`, `static/js/modules/cart.js`  
- Pixel: `twocomms_django_theme/templates/base.html`, `order_success.html`  

## Related historical docs (reference only)

- `docs/seo/*`  
- `twocomms/_audit_seo.md`  
- `TRACKING_QA_CHECKLIST_2025.md`  
- `README_UTM_DISPATCHER.md`  
