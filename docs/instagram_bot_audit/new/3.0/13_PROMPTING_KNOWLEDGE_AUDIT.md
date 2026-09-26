# A4 · Prompting, knowledge sources, brand voice

**Auditor:** root session, 27.09.2026 · HEAD `c69925d56`

---

## 1. Prompt stack (actual assembly order)

`_assemble_system_instruction()` (`services/instagram_bot.py:8597–8689`) → `ig_policy_compiler.compile_policy()`.

**Order:** `immutable authority (server)` → `published core (DB system_prompt + payment protocol + truth boundaries + live directives)` → `verified dynamic facts (automation, client_state, checkout_readiness, shown_products, funnel_journal, objection_lifecycle, catalog, quick_links)` → `playbooks (instruction publication selection)` → `knowledge (approved_public_facts)` → `customer data (memory, conversation, match, media, turn)`.

Budget: `IG_BOT_POLICY_BUDGET_CHARS` default 48 000 chars. **Mandatory blocks overflow = readiness error** (the reply is refused, not silently truncated) — correct design. Optional blocks are dropped whole with a reason in `omissions`.

**Snapshot isolation:** `prompt_snapshot()` (context manager) dedupes repeated DB reads inside one assembly (was 29 queries, several repeated). It explicitly notes the cache scope is *the assembly*, not the turn — because a payment webhook could land mid-turn and stale `payment_link_allowed` would create a second invoice. **That reasoning is correct and must be preserved.**

---

## 2. THE BRAND.MD MISMATCH (owner's R-30)

### Facts verified in code

| Claim | Reality |
|---|---|
| `bot_knowledge/brand.md:3–5` says "Цей файл читає бот… бот підхопить зміни автоматично (без рестарту)" | **False.** `bot_knowledge.py:1–7` docstring: *"The repository Markdown directory is retained as an archive for editorial review. It is deliberately not read into provider payloads"*. `read_knowledge_manifest()` reads only `approved_public_facts`. |
| brand.md is the "source of truth about the brand" | The provider sees `approved_public_facts.APPROVED_PUBLIC_FACTS_VERSION = "2026-09-07.b02.8.1"` instead — a small typed set, last meaningful edit **07.09**. |
| Editing brand.md changes bot behaviour | Only if someone separately updates `approved_public_facts.py` in code and deploys. |

**Impact — this is the most serious prompting finding:** the owner has been told (by the file's own header, and by older docs) that editing brand.md changes the bot. It does not. Every business rule the owner has refined since 07.09 that lives only in brand.md is invisible to the model. Examples currently in brand.md but **not** in the provider facts (need verification per fact, but by structure): the exact prepayment explanation wording, exchange/return rules, FAQ phrasing, "colab/opt questions are not retail support", and the tone paragraph.

### F3-PRM-01 · brand.md is dead weight and actively misleading — P1 · CODE + DOC
- **Recommendation (choose one, explicitly):**
  **(a)** Make brand.md a real source: compile it into the published policy on deploy (hash it, version it, show it in the admin as the current published text), or
  **(b)** Delete/repurpose the file: header rewritten to "editorial archive, not read by the bot — to change behaviour edit the published instructions in the admin", and move every rule that *should* apply into `approved_public_facts.py` or the DB publication.
- I recommend **(a) for brand.md's factual sections + (b)'s honest header**, because the owner thinks in Markdown and it is where the brand knowledge naturally lives; but any rule that shapes money/stock/consent must stay in the typed facts where it can be validated.
- **Owner link:** R-30 ("стоит ли боту обращаться к этому файлу всё время") — answer: he *should not* rely on it today, and the file should stop claiming otherwise.

---

## 3. What the model actually receives about the brand

`approved_public_facts.py` (version `2026-09-07.b02.8.1`), supported languages uk/ru/en. Verified entries (uk):

- `brand` — two meanings of the name, shaka sign, founder link
- `assistant` — honest Solomiya identity (matches D040/Q089)
- `current_tshirt_bases` — regular 190 g/m² 95% cotton 5% elastane; oversize 210–220 g/m², with the directive "name these only for the matching confirmed base from the catalogue"
- `unconfirmed_bases` — **explicit honesty rule:** hoodies have fleece but composition/density are *not publicly confirmed*; longsleeve needs confirmation
- `dispatch_days` — dynamic from `ig_core_policy.ORDINARY_DISPATCH_WINDOW_DAYS`

**Assessment:** small but high quality. The "unconfirmed → say so" pattern is exactly the honesty behaviour the owner wants, and it generalises well. The set is, however, *tiny* relative to the business: no prepayment rule (200 UAH + COD), no free-delivery threshold (3000 after discounts), no custom-print rules, no collaboration intake rules, no points/ZSU wording, no size-chart policy beyond a link placeholder.

**Where those rules live instead:** the DB publication `system_prompt` (core, 5175 chars in prod per D085 §5) + `knowledge_base` (empty in prod) + instruction modules (20 public modules) + the playbooks. So the rules do exist — but they are only editable through the admin publication flow and the *content* was authored before the recent owner decisions.

### F3-PRM-02 · Published core predates several owner decisions (B02.13) — P1 · CODE/DOC
- **Observed:** D085 §5/P1: DB core == `0ac24f65c` body; canonical after `1728ccc2f` is 89 chars longer (adds the ban on setting `paid/order_created/done` from model/analysis). Both labelled `2026-09-23.core.v3`.
- **Impact:** the newest safety line is not in production, and version labels lie.
- **Recommendation:** hash-addressed publication (publication id already has a hash — surface it in the UI next to the version), plus a diff view between the DB core and the repo canonical at every deploy, plus a "publication is behind repo" warning in the Overview diagnostics.

---

## 4. Tone, persona, language

- Persona: **Соломія**, virtual assistant; on direct question answer honestly (D040, Q089) — implemented via the `assistant` fact and presumably the core prompt.
- Language mirroring: `knowledge_language` from `client.language`, fallback `uk`, allowed uk/ru/en (`instagram_bot.py:8620–8622`). Tone paragraph exists **only in brand.md** (i.e. not in the prompt) — the tone the model actually follows comes from the DB core. **Verify what the core says about tone; that is the only tone that matters.**
- Owner complaint R-02 ("Здравствуйте/До свидания" curtness): cannot be diagnosed from code alone — needs the prod prompt snapshot (see §6) or a conversation sample (blocked, see A9).

### F3-PRM-03 · Tone/language policy fragmented across 3 places, only 1 live — P2 · DESIGN
- **Observed:** tone rules in `brand.md` (not used), some guidance in the DB core (used), the apology policy in `ig_apology_policy.py` (used), and the fallback text in `ig_message_templates.py`.
- **Recommendation:** one "voice" module in the published policy with 3–5 few-shot examples (bad → good) per language, versioned, and a UI preview. Few-shot examples outperform abstract instruction for small models (R-42).

---

## 5. Playbook / instruction selection

`active_instruction_selection()` selects modules by tags with a char budget; D085 §5 found a synthetic selection run chose 10 of 20 modules (3461 chars) with `budget_exhausted` dropping 10, and that tag `payment` selects module 4 while `collaboration`/`ugc` select nothing.

### F3-PRM-04 · Instruction selection is budget-truncated and tag-fragile — P1 · CODE
- **Impact:** exactly the modules needed for collaboration or UGC may be omitted on the turn that needs them. D085 already flagged "cannot conclude all creator instructions are absent" — but the synthetic run *did* show `collaboration`/`ugc` tags selecting no module, which suggests either wrong tags on the modules or wrong tags in the selector input.
- **Recommendation:** (a) log every turn's `instruction_selection` (chosen/omitted IDs + reason) into the decision log (see 80_…), so this becomes observable in production rather than inferred; (b) make selection mandatory for a small number of always-on modules (identity, safety, money boundaries) and budget-bound only for the rest; (c) unit-test that each owner-critical scenario (collaboration, custom, UGC, objection) selects at least its module.
- **Risk:** raising budget increases token cost on a free tier. Mitigate with per-scenario modules instead of one big always-on core.

---

## 6. Prod evidence needed (bounded, read-only, no PII)

Not yet collected — do these next (each is a small shell probe, no customer text):
1. `InstagramBotSettings.system_prompt` length + sha256 + first/last 200 chars (business text, safe to store in the audit as a snapshot file).
2. `knowledge_base` length.
3. Active `ig_policy_publication` row: id, version, compiler version, hash, module count, draft revision.
4. `BotInstruction` rows: id, title, tags, length, active flag.
5. Compare DB core hash against repo canonical (`ig_core_policy.CORE_POLICY_SHA256`).

Store as `3.0/evidence/prod_prompt_snapshot_2026-09-27.md`.

---

## 7. Recommendations summary (feeds 90_ plan)

| # | Action | Priority |
|---|---|---|
| 1 | Fix the brand.md lie: either wire it in or relabel it; move live rules to typed facts | P1 |
| 2 | Publish the missing 89-char safety line (B02.13) with hash verification and a UI hash display | P1 |
| 3 | Add a "publication behind repo" diagnostic to Overview | P2 |
| 4 | Log instruction selection per turn; test each owner scenario selects its module | P1 |
| 5 | Introduce a versioned "voice" module with few-shot examples per language | P2 |
| 6 | Expand approved facts to cover: prepayment, free-delivery threshold, custom rules, collaboration intake, points/ZSU, size policy | P1 |

## 8. Owner questions
- **Q-PRM-01:** Do you want brand.md to remain the place you edit brand knowledge (we make it live), or should it become a read-only archive with everything moved into the admin instructions?
- **Q-PRM-02:** Which single sentence should the bot always use when asked whether it is a bot? (D040 says the current answer is loved — confirm it should be frozen.)
