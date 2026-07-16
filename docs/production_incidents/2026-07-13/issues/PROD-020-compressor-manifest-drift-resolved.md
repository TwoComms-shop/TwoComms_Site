# PROD-020 — django-compressor offline manifest drift (historical/resolved)

**Priority:** monitor/deploy regression
**State:** four retained cases; no recurrence after 8 July fix
**Owner:** deployment pipeline

## Historical symptom

Retained stderr contains four `compressor.exceptions.OfflineGenerationError` events on catalog/admin pages. The template compression key was absent from the deployed offline manifest, so rendering failed with 500.

## Resolution evidence

The 8 July hardening changed the deployment/error path and regenerated the offline manifest using forced compression. Current logs after that release contain no new compressor exception. This issue is therefore not an active root cause for the 13 July screenshot cluster.

## Why it stays in the checklist

The deployment is still in-place (PROD-009). A future template/CSS change can again become visible before the corresponding manifest/static release unless compression is a pre-traffic gate.

## Preservation plan

1. In the release directory, run static collection and offline compression before worker switch.
2. Validate compressor manifest presence and render representative catalog/admin templates under production settings.
3. Restart/switch workers only after validation.
4. Keep the existing runtime fallback narrowly scoped; do not use it to hide a broken release indefinitely.
5. Emit build/manifest revision in health diagnostics.

## Acceptance criteria

- Deploy smoke renders every compressed template family before traffic.
- Manifest and source revision are atomically aligned.
- A deliberately missing key fails preflight, not a customer request.
- Recurrence monitor remains zero after subsequent template releases.
