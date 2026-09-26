# A3 · Gemini routing, model selection, quota management

**Auditor:** root session, 27.09.2026 · **Prod SSH:** read-only (HEAD `c69925d56`)

---

## 1. Policy architecture (as of 26.09)

**Policy version:** `gemini-routing-v2.1` (`services/gemini_routing.py:22`)  
**Authority snapshot:** `ig-authority-v1`  
**Four task classes:**

| Task class | Chain | Deadline | Triggers | Lane |
|---|---|---|---|---|
| `NO_MODEL` | (empty) | 0 ms | deterministic_action (e.g. "authoritative_reply", explicit backend truth) | live |
| `ORDINARY_LIVE` | 3.5-flash-lite → 3.5-flash → 3.6-flash | 35 s | backend facts complete, no media, no complexity signals | live |
| `COMPLEX_LIVE` | **3.8-flash** → 3.7-flash → 3.6-flash | 45 s | image/audio, ambiguous catalog, objections, product switch, fit, custom, conflict, referral, comparison | live |
| `DURABLE_ANALYSIS` | 3.6-flash → 3.5-flash → 3.5-flash-lite (escalation: 3.8 → 3.7) | 75 s | background CRM intelligence, typed Fact/Head extraction | analysis |

**Routing mode:** adaptive (chain fallback on quota/5xx) unless pinned. Pin overrides the chain but preserves task class and deadline.

**Classification path (legacy):** `live_routing_decision()` (`instagram_bot.py:7127–7243`) builds rich `TurnFacts`:
- `objection_present` — lexical detection via `ig_objections.detect_objection_types()` + open `client.primary_objection` (L7197–7211)
- `product_or_recipient_switch` — branch switch flag
- `personalized_fit_required` — fit/size decision
- `custom_print_brief` — custom DTF request
- `conflicting_intent` — mixed signals
- `ambiguous_ad_referral` — unresolved ad/referral with no product
- `comparison_required` — catalog comparison
- `unresolved_catalog_candidates` — >1 ambiguous match

This legacy classifier is called by the **old generator path** at `instagram_bot.py:13631, 14352, 14374, 14451`.

**Classification path (live revision):** `ig_revision_live.py:706–716` builds **minimal** `TurnFacts`:
- `has_image` — any image part
- `has_audio` — any audio part
- `reasoning_task_hint` — "media_analysis" if media present, else empty

**Nothing else.** No objection, product, fit, custom, or conflict flags. The revision path cannot route to COMPLEX based on business logic — only on media presence.

---

## 2. CRITICAL FINDING — Revision lane bypasses rich routing (P0)

### F3-GEM-01 · Live revision lane uses image/audio-only routing, ignoring objections and product complexity — **P0 · REGRESSION**

**Observed:** The live revision lane (`ig_revision_live.py:709`) is the production reply path since `IG_REVISION_EXECUTION_CUTOVER_AT = 2026-09-08T20:14:23+00:00` (`production_settings.py:70`). It passes only `has_image`, `has_audio`, and a hint to `classify_live_turn()`. Every other complexity signal — objections, product switches, fit decisions, custom prints, conflicts, ambiguous referrals, comparisons — is **never set**.

**Impact:**
- A customer says "Це дорого" (objection) with no image → routes to ORDINARY (3.5 Lite/Flash), not COMPLEX (3.8/3.7).
- A customer switches from hoodie to t-shirt mid-conversation → ORDINARY, not COMPLEX.
- A customer asks for a custom print with no attachment → ORDINARY, not COMPLEX.
- Owner's R-26 requirement ("3.8 для фото") is met for images, but the broader D082-G design intent ("3.8 для складних продажних і продуктових ситуацій") is **completely bypassed**.

**Root cause:** The revision lane was designed for immutable generation (one sealed snapshot → one reply) and the rich routing logic was never migrated from the legacy `live_routing_decision()` builder. The legacy path still exists and is correct, but it is **unreachable** when `IG_REVISION_EXECUTION_ENABLED=1`.

**Evidence:**
- `ig_revision_live.py:710`: `TurnFacts(has_image=has_image, has_audio=has_audio, reasoning_task_hint="media_analysis" if ...)` — 4 fields only
- `instagram_bot.py:7227`: `TurnFacts(deterministic_action=..., has_image=..., has_audio=..., unresolved_catalog_candidates=..., personalized_fit_required=..., product_or_recipient_switch=..., custom_print_brief=..., conflicting_intent=..., ambiguous_ad_referral=..., comparison_required=..., objection_present=..., commercial_risk=..., reasoning_task_hint=...)` — 12 fields, fully populated
- No code path from revision → legacy classifier exists; the two are parallel and the revision lane does not call `live_routing_decision()`.

**Recommendation:** Refactor `live_routing_decision()` into a **pure classifier** that accepts `(client, commerce_request, ad_resolution, current_text, images, media, deterministic_action)` and returns `TurnFacts`. Call it from **both** the legacy generator and the revision lane before invoking `classify_live_turn()`. Ship with a shadow logging phase: log both minimal and rich decisions side-by-side for 48h, verify the difference in production, then cut over.

**Risk if unfixed:** Every objection, product ambiguity, and custom request is handled by the cheap/fast model instead of the reasoning model. Conversion suffers; the owner's "3.8 для складного" requirement (D082-G) is fiction in production.

**Plan link:** This is a **new gap** not covered by the 2.0 plan. B09.4 (response guard) and B02.8 (routing correctness) exist but do not name this specific regression. Add a new 3.0 item: **"B-ROUTE-01: restore rich routing in live revision lane, parity test both paths"**.

---

## 3. Quota management (v2 accounting, shadow mode)

**Contract:** `services/gemini_accounting_runtime.py`, `services/gemini_v2_accounting.py`.  
**Mode:** configured via `GEMINI_ACCOUNTING_V2_MODE` and `EFFECTIVE_FROM` in settings. Production setting not directly visible via read-only SSH (would need `settings.py` or environment dump).

**Observed in code:**
- `gemini_v2_read_model.build_routes_payload()` exposes `accounting.mode` as "off" / "shadow" / "invalid" (`:780–784`). Shadow means tracking without enforcement.
- Per-model/project/reasoning-level consumption tracked in `gemini_v2_accounting.increment_consumption()`.
- Key rotation on 429/quota exhaustion via `gemini_v2_outbound.execute_chat_with_accounting()`.
- Separate model-health probes (Gemini Health panel, `services/gemini_health.py`).

**Gap:** The accounting payload shows mode and cumulative consumption, but the **UI does not render it**. The Overview tab shows "last model used" (F3-OPS-02) instead of "current quota headroom per model". The Gemini panel (`bot.html:1355–1469`, JS `:2094–2320`) exists but is gated by `VIEW_IG_BOT_GEMINI_PERMISSION` and shows v2 routes/attempts/health — still no quota meter.

### F3-GEM-02 · Quota accounting exists but is invisible to the operator — P1 · UX

**Observed:** v2 accounting writes consumption to `IgGeminiV2AccountingConsumption` rows; `gemini_v2_read_model.build_quotas_payload()` exists (`:714–722`); API endpoint `/api/bot/gemini-v2/quotas` exists (`bot_views.py:1108`). But no UI widget renders it.

**Impact:** The owner cannot see "3.8 Flash used 12k / 15k requests today" until after exhaustion. R-25 asks for this visibility explicitly.

**Recommendation:** Add a **quota headroom card** to the Overview or Gemini tab: per model/slot (project), show `used_today / daily_limit` with a color (green >50% headroom, yellow 20–50%, red <20%). Data already exists in the API; the UI just needs a renderer. Also: alert on <10% headroom (currently only alerts on exhaustion, which is too late).

**Plan link:** B17.6 (observability), C19.3.

---

## 4. Health monitoring (gemini_health.py, separate from quota)

**Contract:** `services/gemini_health.py` tracks per-key/model liveness via probe results and attempt ledger. Data exposed via `/api/bot/gemini-health` (`bot_views.py:1097`).

**Observed:**
- Probes can be triggered manually from the Gemini panel (`bot_gemini_health_probe_api`, `bot_views.py:1160`).
- Pool rows show `health_state` (healthy/cooldown/busy/degraded), last probe timestamp, consecutive failures.
- Keys rotate on repeated 5xx or 429.

**Coverage:** This is **narrow liveness only** (can the key reach the API?), not semantic correctness (does it route to the right model? does the model answer correctly?). The owner's "я не знаю, работает ли 3.8" (R-26) is answered by "yes, the key is live" but not "yes, objections route to 3.8" (which they don't, per F3-GEM-01).

### F3-GEM-03 · Health monitoring covers connectivity, not routing correctness — P2 · DESIGN

**Recommendation:** Add a **routing audit log**: every 100th turn, log `(client_id, turn_id, task_class, model_chain[0], reason_codes, has_image, has_audio, objection_present, commercial_risk)` to a bounded ring buffer (1000 rows, in-memory or Redis). Expose it in the Gemini panel as "recent routing decisions". This lets the owner verify "objections actually hit 3.8" without reading code.

**Plan link:** New observability item for 3.0.

---

## 5. Model pinning (temporary override)

**Observed:** `gemini_routing._apply_pin()` (`:171–190`) checks `settings_obj.pinned_until` and `pinned_chat_model`. If active, replaces the task-class chain with `(pinned_model,)` but preserves deadline and task class.

**Usage:** Owner can force a specific model (e.g. pin 3.8 for all live turns) via the settings panel. Pinned mode shown in Overview as "routing mode: pinned" (`bot.html` JS, status payload `:18155–18162`).

**Correctness:** The pin contract is sound — it overrides the chain, not the classification. A pinned 3.5-flash-lite still gets the ORDINARY deadline (35s), not COMPLEX (45s). This is correct: the pin is about cost/quota, not task semantics.

**Gap:** No UI to set the pin exists in the current Overview/Settings tabs (only via Django admin or direct DB write). The 2.0 plan does not call for one, but R-32 ("возможность переключить модель вручную") implies it.

### F3-GEM-04 · Model pinning exists but has no UI control — P2 · UX

**Recommendation:** Add a "Pin model" toggle to the Gemini panel: dropdown (all live-capable models), duration (1h / 6h / 24h / until unpinned), and a confirm button. Show the current pin state with remaining time.

**Plan link:** Optional enhancement, not in 2.0.

---

## 6. Escalation chain (analysis only)

**Observed:** `ANALYSIS_ESCALATION_CHAIN = ("gemini-3.8-flash", "gemini-3.7-flash")` (`:56`). Durable analysis starts with `ANALYSIS_CHAIN = (3.6, 3.5, 3.5-lite)` (`:51`) and escalates to 3.8/3.7 only on specific triggers (not visible in routing.py alone; likely in `ig_analysis_lane.py`).

**Correctness:** The escalation is **narrow by design** — analysis is background, not latency-critical, so it starts cheap and escalates only when the job detects insufficient structure/confidence. This is correct.

**Gap:** The escalation trigger logic is not documented in the routing policy itself. It lives in the analysis executor and is opaque to the owner.

### F3-GEM-05 · Analysis escalation trigger is opaque — P3 · DOCS

**Recommendation:** Document the escalation conditions in `gemini_routing.py` as a docstring or in the runbook. Currently: "escalation exists but we don't know when it fires" is the best answer.

**Plan link:** B17 (runbook).

---

## 7. Deadline enforcement

**Observed:** Each task class has a deadline (`NO_MODEL=0`, `ORDINARY=35s`, `COMPLEX=45s`, `ANALYSIS=75s`). Deadline passed to `gemini_generate()` as `timeout` or `deadline`.

**Actual enforcement:** Not traced in this audit pass (requires reading `gemini_generate()` and the provider transport). Assume it exists per design.

**Risk:** If the deadline is advisory only (no hard kill), a stalled 3.8 call could block the lane for >45s. The lane-health snapshot (`ig_lane_health.py`) tracks stalls, but the alert is hourly (F3-OPS-05).

### F3-GEM-06 · Deadline enforcement not verified — P3 · OPEN

**Recommendation:** Verify that `gemini_generate()` raises `TimeoutError` or equivalent at the deadline, and that the error is caught and logged. If enforcement is missing, add it (with a 5s grace buffer to allow cleanup). If present, document it.

**Plan link:** B17.5 (robustness).

---

## 8. Ideas beyond the brief

1. **Routing dry-run API:** `/api/bot/gemini-routing-preview` that accepts `(client_id, message_text, has_image, has_audio)` and returns the decision without calling Gemini. Useful for debugging "why didn't this route to 3.8".
2. **Quota forecast:** "At current usage (X requests/day), model Y will exhaust in Z hours." Data exists; need a predictor.
3. **Model A/B test mode:** route 10% of ORDINARY to 3.6 instead of 3.5-lite, compare conversion. Requires a shadow-response logger.

---

## 9. Open questions for the owner

- **Q-GEM-01:** Is the revision-lane routing gap (F3-GEM-01) why you felt "3.8 не используется"? (R-26)
- **Q-GEM-02:** Should quota alerts fire at 80% headroom, or only on exhaustion?
- **Q-GEM-03:** Do you want a UI control for model pinning, or is Django admin enough?
- **Q-GEM-04:** What % of conversations include objections without images? (This measures F3-GEM-01 impact — need to query production with privacy-safe aggregate.)
