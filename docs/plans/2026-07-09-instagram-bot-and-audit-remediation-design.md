# Instagram Bot and Audit Remediation Design

**Date:** 2026-07-09

## Goal

Fix every confirmed item in `docs/qa/AUDIT_FINDINGS_2026-07-09.md` safely, beginning with the management Instagram bot. Every independently verifiable slice is committed, pushed, deployed to production, and checked before the next slice starts.

## Constraints

- Production MySQL and runtime state are authoritative; verification runs through SSH on the production host.
- The production checkout has unrelated untracked diagnostics. Deployment uses `git pull --ff-only` and never removes them.
- No finding is checked off until its automated and live acceptance checks pass.
- Meta Advanced Access and a recipient's Meta eligibility are external prerequisites. The product can expose and route those states correctly, but cannot grant Meta permissions.
- A tracked deployment credential is removed before the first push.

## Delivery approach

One large rewrite would mix migrations, Graph API behavior, CRM actions, and dashboard changes, making a production regression hard to isolate. Cosmetic-only changes would leave the operational failures intact. The selected approach is a sequence of focused releases with a production gate after each one.

1. Remove the tracked deployment credential (F-093).
2. Persist a per-client IG delivery state and distinguish operational delivery blocks from sales transfers (IG-005/007/013/014, F-097).
3. Add explicit transfer-to-manager behavior (IG-003, F-098).
4. Repair Hide/Unhide/Lost interactions and Ukrainian CRM feedback (IG-001/002/009/010/011, F-095).
5. Localize and strengthen the compact dashboard (IG-004, F-096).
6. Handle reactions and echo resilience (IG-006/008).
7. Repair storefront attribution/session propagation, payment conversion/dispatch, and pixel P0 before opening the ads gate.
8. Close security, operations, SEO, cart, routing, and hygiene findings in root-cause groups.

## Acceptance policy

Each release is scoped, reviewed, pushed, pulled with `--ff-only`, tested through SSH, migrated/built/restarted when needed, and checked live. The audit checkbox is flipped only after the corresponding acceptance succeeds. External Meta permissions remain documented and unchecked until Meta itself accepts delivery.
