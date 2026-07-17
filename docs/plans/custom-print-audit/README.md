# Custom Print Visual And Functional Audit

Status: `audit in progress`

This folder is the living record for the second Custom Print quality pass. It deliberately separates evidence from implementation so every visual or functional change can be checked against a reproducible symptom.

## Scope

- Desktop: `1366x768`, `1440x900`, `1920x1080`.
- Tablet: `768x1024`, `1024x768`.
- Mobile: `320x568`, `360x800`, `390x844`, `430x932`.
- Languages: Ukrainian, Russian, English.
- Journey: hero, mode, garment, fit/fabric/color, zones, artwork, quantity/sizes, gift, contact, cart, manager handoff, draft resume.
- Contracts that must remain stable: cart payload, lead payload, moderation, pricing, Telegram verification, canonical/hreflang/schema and SEO copy.

## Working Rules

1. Reproduce the symptom and record viewport, state and selector before changing code.
2. Fix one root cause at a time; add a regression assertion before the implementation where practical.
3. Do not put customer artwork into the physical preview. The preview only communicates geometry and selected zones.
4. Every fixed or sticky surface must have a measured safe-area offset and must not cover the active control.
5. Every selected/locked/required state must be visible without relying on color alone.
6. After each batch, re-run the relevant section of this checklist and update the evidence file.

## Documents

- `findings-2026-07-17.md` — reproduced defects, root causes, evidence and severity.
- `implementation-plan.md` — ordered implementation batches, tests and acceptance criteria.

## Status Legend

- `[ ]` not checked
- `[~]` reproduced or in progress
- `[x]` fixed and verified
- `[!]` blocked or requires a product decision
