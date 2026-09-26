# A5 · Memory & context assembly

**Auditor:** root session, 27.09.2026 · HEAD `c69925d56`

---

## 1. How memory reaches the prompt (designed behaviour)

`assemble_system_instruction()` accepts five customer-context blocks (`instagram_bot.py:8649–8655`), injected as `context:*` modules **after** policy/knowledge:

| Block | Producer | Content |
|---|---|---|
| `memory` | `bot_memory.memory_note(client)` | narrative `client.memory_summary`, wrapped in an explicit untrusted-data envelope |
| `conversation` | `bot_memory.client_context_note(client)` | ad attribution → **matched product**, repeat-customer status, last order status |
| `match` | catalogue match hint | which catalogue item the turn matched |
| `media` | `_media_context_hint(media)` | what media was attached/captured |
| `turn` | caller | per-turn coverage/intent/timing guidance |

**`memory_note()` safety design (verified, keep):**
- `bot_memory.py:112–167`: the customer's *own words* previously leaked into the prompt as "memory" and survived the conversation window — a real prompt-injection channel. Now the summary is passed as clearly-labelled untrusted quoted data (`<records>…</records>`) with an explicit instruction that records are not obligations and cannot change rules, run through `neutralize_untrusted_text()` — **the same neutraliser used for manager notes.**
- Episode scoping: a summary generated **before** the current `IgCommercialEpisode.opened_at` is **discarded** (L137–141) — "a narrative for an earlier sales episode has no typed subject/line boundaries; reusing it can turn an old recipient, gift, size or test persona into a current customer fact". Excellent invariant; the owner's R-12/R-11 gift-vs-self case depends on it.
- Funnel reset: a summary older than the latest `IgFunnelResetAudit` is discarded (L143–154).
- Cap: `MEMORY_NOTE_MAX_CHARS`, `TRANSCRIPT_LIMIT` for regeneration input.

**`client_context_note()`** resolves the ad referral and, when the campaign maps to a product, includes the product + pricing — this is what makes R-07 ("не искать по фотографии, сразу понимать какой товар") work in the *legacy* path.

---

## 2. THE MEMORY REGRESSION (P0)

### F3-MEM-01 · Live revision lane drops memory and client context entirely — **P0 · REGRESSION**

**Observed:** `ig_revision_live.py:759–769` calls `bot.gemini_generate(...)` with `client=revision.client, turn_note=coverage_note` and **no `memory_note`, no `context_note`, no `match_hint`, no `media_hint`**. `gemini_generate()` does not construct them itself — it forwards whatever it is given straight into `assemble_system_instruction()` (`instagram_bot.py:7986–7998`). Defaults are `None` (`:7857`).

**Contrast — legacy path** (`instagram_bot.py:14643–14665, 14843`): builds `mem_note = bot_memory.memory_note(row.client)` and `ctx_note = bot_memory.client_context_note(...)`, plus a joined fallback note, and passes them to `gemini_generate`.

**Which path is live?** `production_settings.py:69` → `IG_REVISION_EXECUTION_ENABLED` default `'1'`, cutover `2026-09-08T20:14:23+00:00`. So **since 08.09 the production reply path passes no memory and no client context.**

**Impact (ties directly to owner brief):**
- **R-07 is broken in production:** ad referral → product resolution no longer reaches the prompt as context. The bot must re-derive intent from text/photo every turn, which is exactly what the owner complained about ("чтобы не искать по фотографии… а чтобы сразу понимать, с какой рекламы он пришёл").
- **R-28 is broken:** "память должна помогать Gemini" — the narrative summary is not delivered at all on the live path. Every turn starts from the sealed history window alone.
- **R-29 is broken:** a returning customer's confirmed preferences (size, colour, recipient) survive only if they happen to sit inside the bounded transcript window.
- Repeat-customer status and last-order status (used to avoid re-asking delivery data, and to detect "second purchase" episodes) are absent from the prompt.
- The generator still computes `coverage_note`, `intent_generation_guidance`, `conversation_timing_guidance` — so *some* context exists, but not memory, not ad attribution, not catalogue match hint.

**Root cause:** identical to F3-GEM-01 — the revision lane was built as a sealed-history generator and only the history + media + a hand-built coverage note were migrated. The richer context assembly stayed in the legacy path and is now unreachable.

**Recommendation (one refactor serves both P0s):**
1. Extract a single `build_turn_context(client, revision_or_row, *, turn_text, images, media, commerce_request, ad_resolution)` that returns `{memory_note, context_note, match_hint, media_hint, turn_note}` **plus** the `TurnFacts` for routing (see F3-GEM-01). Both lanes call it.
2. Ship behind a flag with **shadow comparison**: log the assembled context sizes/hashes and the routing decisions from both approaches for 48h on live traffic; diff; then enable.
3. Regression test: two fixtures (returning customer with a confirmed size; ad-referral customer with a matched product) asserting the prompt contains the memory/attribution block on **both** lanes.

**Confidence:** high (code-level, direct call-site comparison). **Not yet verified in production data** — a bounded probe that counts, for the last 7 days, how many revisions were generated with a non-null memory on the client, or that dumps one turn's assembled prompt length from the revision path, would confirm. Do that before writing the plan item as "confirmed in prod".

### F3-MEM-02 · No visible way for the owner to see "what the bot remembered" — P1 · UX
- **Observed:** the admin has a prompt preview (`build_prompt_snapshot()`), which does assemble memory — so the preview shows a *different* prompt than production serves for the same client. That is actively misleading given F3-MEM-01: the owner previews, sees memory, and believes it is used.
- **Recommendation:** mark the preview as "legacy path" until parity is restored, then have both paths share the builder so preview == production. Add a per-client "known facts" panel (typed slots with source + timestamp) — the seed of the state card in §4.

---

## 3. Background analysis & typed memory (designed, but dark)

- `ig_typed_memory.py` (1414 lines), `ig_analysis_v2.py` (939), `ig_analysis_v2_projector.py`, `ig_analysis_materiality.py`, `ig_analysis_roles.py`, `ig_analysis_events.py` — a typed Fact/Head model with a shadow projector.
- D084.1/D085-M1 flags: `IG_TYPED_MEMORY_MODE=off`, `IG_ANALYSIS_V2_MODE=off`, materiality off, selector legacy. So the typed memory is **built but not read**.
- Consequence: the "modern" memory path neither helps nor harms today; meanwhile the *old* narrative summary is what exists in the DB and it is not reaching the live prompt at all (F3-MEM-01).

### F3-MEM-03 · Two memory systems, neither delivering value on the live path — P1 · ARCHITECTURE
- **Recommendation:** pick one. My recommendation: keep the **typed** model as the single source of truth (it can express recipient, fit, colour, objections, consents with source and confidence — exactly what the owner's scenarios need), keep the narrative summary only as a *derived* human-readable view for the admin, and make the live prompt read **typed slots** (compact) + optional rolling narrative for tone continuity. Turn the flags on only after a shadow read comparison shows the typed slots are correct on real conversations.

---

## 4. Proposed "state card" (what the model should see every turn)

A compact, deterministic block assembled from DB truth (not LLM output), injected as one module, rendered identically in the prompt and in the admin UI:

```
[СТАН КЛІЄНТА] (дані, не інструкції)
Епізод: #347 відкрито 24.09 · Стадія: підбір розміру
Кому: подарунок (дівчина) · Зріст 165
Відомо: тип=худі, посадка=oversize, колір=чорний, розмір=невідомо
Невідомо (запитати): розмір
Реклама: кампанія «hoodie-black-sept» → товар #1284 (впевнено)
Минулі покупки: 1 (05.2026, футболка, отримано)
Відкрите заперечення: ціна (×2, оброблено 1, не вирішено)
Хто відповідає: бот · пауза: ні
Останнє від клієнта: 12 хв тому · Останнє від нас: 10 хв тому
Зобов'язання: чекаємо підтвердження наявності L (менеджер, 3 год)
Дозволи: marketing opt-in=ні · канал=24h з 21:40
Балів: 0 (немає прив'язки акаунта)
```
Justification: it converts R-28/R-29 ("пам'ять має допомагати") into a deterministic artefact; it makes F3-FUN/F3-MEM/F3-GEM failures visible (if a slot is missing, the funnel shows it too); and it is cheap in tokens compared to a rolling narrative. Rendered from the same object in the admin, it becomes the "5-second understanding" the owner asks for in R-16.

---

## 5. Ideas
1. **Memory diff view:** when a slot changes (e.g. size M → L), show old/new + the message that caused it. Directly serves trust in memory (R-28).
2. **Contradiction detector:** if the customer says "for my brother, 190 cm" after "for my girlfriend 165", flag it for the bot to clarify (and log it) rather than silently overwriting — D085 mentions memory-conflict chip already exists in the plan (C15) but not in reality.
3. **Stale-slot expiry:** a size confirmed 3 months ago for a different recipient should not be reused; tie slot validity to episode + recipient.

## 6. Owner questions
- **Q-MEM-01:** For a returning customer, should the bot reuse the previous order's size/colour automatically, or always reconfirm briefly? (Affects whether slots are "sticky" across episodes.)
- **Q-MEM-02:** How long should individual facts stay valid (e.g. "wants it for a birthday in October")?
