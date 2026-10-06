"""Pure, bounded context over an already captured customer turn.

Capture adapters own database reads, source ownership and the final dispatch
fence. This module cannot discover a newer message, refresh a memory head,
bootstrap a session or perform any provider/business effect. ``legacy`` and
``shadow`` callers keep their existing request; ``unified`` callers may use
the returned modules. All modes compute the same proposed facts.
"""
from __future__ import annotations

from dataclasses import dataclass, fields
from datetime import datetime
import hashlib
import json
import re
from types import MappingProxyType, SimpleNamespace
from typing import Mapping

from management.services.gemini_routing import TurnFacts, classify_live_turn


VERSION = "ig-turn-intelligence.v1"
MAX_SOURCES = 32
MAX_HISTORY = 12
MAX_SOURCE_CHARS = 64_000
MAX_MEDIA_PARTS = 64
MAX_CONTEXT_CHARS = 12_000
MAX_MEMORY_SOURCES = 60
SCOPE_KEYS = ("client_id", "source_namespace", "reset_id", "reset_floor",
              "erasure_epoch", "episode_id", "line_id", "recipient_id")
_CODE = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_QUOTES = re.compile(r'«[^»]*»|“[^”]*”|"[^"\n]*"|‘[^’]*’|(?<!\w)\x27[^\x27\n]+\x27(?!\w)')
_REPORTED = re.compile(r"^(?:це\s+цитата|цитата|у\s+(?:рекламі|статті)|в\s+(?:рекламе|статье)|"
    r"(?:друг|подруга|він|вона|он|она)\s+(?:сказ\w*|напис\w*)|"
    r"(?:мені|мне)\s+(?:сказали|написали)|quote|(?:my\s+friend|he|she|they)\s+(?:said|wrote))\b", re.I)
_HISTORICAL = re.compile(r"\b(?:раніше|раньше|колись|торік|минулого|вчора|вчера|previously|used\s+to|last\s+time)\b", re.I)
_NEGATION = re.compile(r"\b(?:не|ні|not|never|no\s+longer|don['’]t|isn['’]t|wasn['’]t)\s+(?:\w+\s+){0,2}$", re.I)
_OBJECTIONS = (
    ("price", re.compile(r"\b(?:дорог\w*|задорого|expensive|too\s+much|не\s+по\s+(?:кишен\w*|карман\w*))\b", re.I)),
    ("cheaper_elsewhere", re.compile(r"\b(?:дешевш\w*|дешевле|cheaper\s+elsewhere)\b", re.I)),
    ("prepayment_trust", re.compile(r"\b(?:не\s+довір\w*|не\s+довер\w*|боюс\w*|страшно|шахра\w*|мошенн\w*)\b.{0,80}\b(?:передоплат\w*|предоплат\w*|оплат\w*)\b", re.I)),
    ("size_risk", re.compile(r"\b(?:боюс\w*|не\s+впевнен\w*|не\s+уверен\w*|раптом|вдруг|не\s+знаю)\b.{0,70}\b(?:підійд\w*|подойд\w*|розмір\w*|размер\w*|сяде|сядет)\b", re.I)),
    ("defect_risk", re.compile(r"\b(?:боюс\w*|страшно|раптом|вдруг|а\s+якщо|а\s+если)\b.{0,80}\b(?:брак\w*|дефект\w*|пошкодж\w*|поврежд\w*)\b", re.I)),
    ("delivery_time", re.compile(r"\b(?:(?:занадто|надто|слишком)\s+(?:довго|долго)|не\s+встиг\w*|не\s+успе\w*)\b", re.I)),
    ("print_quality", re.compile(r"\b(?:принт\w*|друк\w*|печат\w*)\b.{0,90}\b(?:потріск\w*|тріск\w*|треск\w*|зліз\w*|слез\w*|зітр\w*|стир\w*)\b", re.I)),
    ("out_of_stock", re.compile(r"\b(?:немає|нет|відсутн\w*|отсутств\w*)\b.{0,60}\b(?:розмір\w*|размер\w*|кольор\w*|цвет\w*|варіант\w*|вариант\w*)\b", re.I)),
    ("payday", re.compile(r"\b(?:після|после)\s+(?:зарплат\w*|авансу?)\b", re.I)),
    ("thinking", re.compile(r"\b(?:подумаю|подумаємо|подумаем|поміркую|ще\s+подума\w*)\b", re.I)),
)


class TurnContextError(ValueError):
    """A finite capture failure; callers must retain their existing safe path."""
    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)


def _plain(value):
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_plain(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TurnContextError("capture_value_invalid")


def capture_digest(value):
    """Same canonical JSON digest convention as existing revision captures."""
    return hashlib.sha256(json.dumps(_plain(value), ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _freeze(value):
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    return value


def _event(value):
    try:
        stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        raise TurnContextError("capture_watermark_invalid") from None
    if stamp.tzinfo is None:
        raise TurnContextError("capture_watermark_invalid")
    return stamp


def _key(value):
    try:
        return (_event(value["event_at"]), int(value["message_id"]))
    except (KeyError, TypeError, ValueError):
        raise TurnContextError("capture_watermark_invalid") from None


def _scope_matches(snapshot, boundary):
    scope = snapshot.get("scope")
    return isinstance(scope, dict) and all(key in scope and scope[key] == boundary[key] for key in SCOPE_KEYS)


def current_objections(sources):
    """Only the customer's own current clauses, with quotes/negation removed.

    This is a bounded routing signal, never a CRM update or an authority claim.
    A historical primary_objection and purchase count are deliberately absent.
    """
    found = {}
    for source in sources:
        text = _QUOTES.sub("", str(source.get("text") or ""))
        for sentence in re.split(r"[.!?;\n]+", text):
            # A first-person contrast may establish a fresh concern after a
            # reported/historical clause; commas alone cannot strip its owner.
            for clause in re.split(r"\b(?:але|но|but)\b", sentence, flags=re.I):
                clause = clause.strip(" ,:")
                if _REPORTED.search(clause):
                    continue
                for kind, pattern in _OBJECTIONS:
                    for match in pattern.finditer(clause):
                        prefix = clause[:match.start()]
                        if _NEGATION.search(prefix):
                            # A later own correction withdraws that earlier
                            # concern in this same ordered sealed bundle.
                            found.pop(kind, None)
                            continue
                        if _HISTORICAL.search(prefix):
                            continue
                        found[kind] = {"kind": kind, "source_message_id": source["message_id"]}
                        break
    # Deterministic order and bounded independent source evidence.
    return tuple(found.values())


def _referral_state(sources, referral):
    ids = {row["message_id"] for row in sources}
    present = referral.get("source_message_id") in ids and referral.get("present") is True
    exact = (present and referral.get("status") == "resolved"
             and referral.get("mapping_active") is True and referral.get("mapping_unique") is True)
    return present, exact


def build_routing_facts(*, sources, media=None, commerce=None, referral=None):
    """Shared legacy/revision feature extraction from caller-captured inputs.

    Callers own scope/integrity checks; build_turn_context performs them first.
    Sources carry message_id/text, media.parts carry mime/source_message_id and
    validated=True. No client-global objection/ad state is read here.

    Commerce keys: deterministic_action, candidate_product_ids,
    pending_clarification, exact_product_id, personalized_fit_requested,
    reset_requested, new_purchase_requested, exchange_requested,
    recipient_switch, custom_print_requested, garment_type,
    semantic_constraints (artwork/placement/print_brief), conflicting_intent,
    comparison_requested, checkout_requested, support_requested, objections.
    Objections require state=open, active_for_turn=True and a current source ID.
    Referral keys: source_message_id,present,status,mapping_active,mapping_unique.
    No purchase count, primary_objection or raw price/size keyword promotes a
    turn. This function returns facts; only the existing classifier routes them.
    """
    sources, media = _plain(sources), _plain(media or {"parts": []})
    commerce, referral = _plain(commerce or {}), _plain(referral or {})
    if not isinstance(sources, list) or not 1 <= len(sources) <= MAX_SOURCES:
        raise TurnContextError("sealed_source_count_invalid")
    try:
        ids = {row["message_id"] for row in sources}
        if len(ids) != len(sources) or sum(len(str(row.get("text") or "")) for row in sources) > MAX_SOURCE_CHARS:
            raise TurnContextError("routing_source_capture_invalid")
        parts = media.get("parts", [])
        if not isinstance(parts, list) or len(parts) > MAX_MEDIA_PARTS:
            raise TurnContextError("media_capture_overflow")
        for part in parts:
            if (not isinstance(part, dict) or part.get("source_message_id") not in ids
                or part.get("validated") is not True):
                raise TurnContextError("media_binding_invalid")
            if not str(part.get("mime") or "").startswith(("image/", "audio/", "video/")):
                raise TurnContextError("media_modality_unsupported")
        has_image = any(str(part.get("mime") or "").startswith("image/") for part in parts)
        has_audio = any(str(part.get("mime") or "").startswith("audio/") for part in parts)
        has_video = any(str(part.get("mime") or "").startswith("video/") for part in parts)
        if has_video and "has_video" not in {field.name for field in fields(TurnFacts)}:
            raise TurnContextError("video_routing_unavailable")
        own_objections = current_objections(sources)
        lifecycle = [item for item in commerce.get("objections", []) if isinstance(item, dict)
            and item.get("active_for_turn") is True and item.get("state") == "open"
            and item.get("source_message_id") in ids]
        pending = str(commerce.get("pending_clarification") or "")
        candidates = len(set(commerce.get("candidate_product_ids") or ()))
        if pending == "multiple_product_links":
            candidates = max(2, candidates)
        present, exact = _referral_state(sources, referral)
        fit = commerce.get("personalized_fit_requested") is True or pending in {"size_fit", "fit_recommendation"}
        switch = any(commerce.get(key) is True for key in ("reset_requested", "new_purchase_requested", "exchange_requested", "recipient_switch"))
        semantic = commerce.get("semantic_constraints") or {}
        custom = (commerce.get("custom_print_requested") is True or commerce.get("garment_type") in {"custom", "custom_print"}
                  or any(key in semantic for key in ("artwork", "placement", "print_brief")))
        risk = "high" if own_objections or lifecycle or any(commerce.get(key) is True for key in
            ("checkout_requested", "exchange_requested", "support_requested")) else "low"
        hint = "media_analysis" if has_image or has_audio or has_video else "size_fit_decision" if fit else "product_decision" if switch or custom or candidates > 1 else ""
        kwargs = dict(deterministic_action=str(commerce.get("deterministic_action") or ""),
            has_image=has_image, has_audio=has_audio, unresolved_catalog_candidates=candidates,
            personalized_fit_required=fit, product_or_recipient_switch=switch,
            custom_print_brief=custom, conflicting_intent=commerce.get("conflicting_intent") is True or pending in {"multiple_product_links", "new_purchase_or_exchange", "which_product"},
            ambiguous_ad_referral=present and not exact and not commerce.get("exact_product_id"),
            comparison_required=commerce.get("comparison_requested") is True,
            objection_present=bool(own_objections or lifecycle), commercial_risk=risk, reasoning_task_hint=hint)
        if "has_video" in {field.name for field in fields(TurnFacts)}:
            kwargs["has_video"] = has_video
        return TurnFacts(**kwargs)
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        if isinstance(exc, TurnContextError):
            raise
        raise TurnContextError("routing_fact_capture_invalid") from None


@dataclass(frozen=True)
class TurnContext:
    facts: TurnFacts
    decision: object
    boundary: Mapping
    components: Mapping
    context_blocks: Mapping
    source_bindings: tuple
    omissions: tuple
    metadata: Mapping
    response_plan: object = None
    # Capture adapters may attach the exact already-bounded provider history
    # with dataclasses.replace; the pure builder never reloads/reorders it.
    provider_history: tuple = ()
    captured_history: tuple = ()

    @property
    def memory_note(self):
        return self.context_blocks.get("context:memory")

    @property
    def context_note(self):
        return self.context_blocks.get("context:conversation")

    @property
    def turn_note(self):
        return "\n".join(self.context_blocks[key] for key in
            ("context:timing", "context:response_plan") if key in self.context_blocks)


def build_turn_context(*, boundary, sources, captured_at, history=(), media=None,
                       commerce=None, referral=None, memory=None, timing=None,
                       response_plan=None, routing_policy=None, components=None):
    """Build a proposal from JSON-shaped snapshots, with no implicit reads.

    Boundary: client_id, source_namespace, source_ids (sealed order),
    source_digests (canonical pre-seal identities), sealed_sources_digest,
    watermark {event_at,message_id}, reset_id/floor, erasure_epoch,
    permission_epoch, publication {id,version,hash}, episode_id, line_id,
    recipient_id. history_digests is required when history is supplied.

    Optional snapshots have ``scope`` containing SCOPE_KEYS. Commerce has
    existing structured request facts; referral has status/source_message_id
    and mapping_active/mapping_unique. Memory is the current reader verdict
    (text,reason,provenance), never an alternate historical head. Media parts
    contain source_message_id,mime,sha256,validated=True; bytes stay outside.
    Routing policy contains mode=legacy|shadow|unified and existing pin fields.
    Returned boundary/components are immutable copies for the state-card.
    """
    if not isinstance(captured_at, datetime) or captured_at.tzinfo is None:
        raise TurnContextError("captured_clock_invalid")
    boundary, sources = _plain(boundary), _plain(sources)
    policy = _plain(routing_policy or {})
    mode = policy.get("mode", "legacy")
    if mode not in {"legacy", "shadow", "unified"}:
        raise TurnContextError("context_mode_invalid")
    required = (*SCOPE_KEYS, "source_ids", "source_digests", "sealed_sources_digest",
                "watermark", "permission_epoch", "publication")
    if not isinstance(boundary, dict) or any(key not in boundary for key in required):
        raise TurnContextError("capture_boundary_incomplete")
    if boundary["erasure_epoch"]:
        raise TurnContextError("client_erasing")
    if not isinstance(sources, list) or not 1 <= len(sources) <= MAX_SOURCES:
        raise TurnContextError("sealed_source_count_invalid")
    try:
        ids = tuple(int(item["message_id"]) for item in sources)
        floor = int(boundary["reset_floor"])
        if ids != tuple(boundary["source_ids"]) or len(set(ids)) != len(ids):
            raise TurnContextError("sealed_source_window_changed")
        if any(identity < floor for identity in ids):
            raise TurnContextError("sealed_source_before_reset")
        if capture_digest(sources) != boundary["sealed_sources_digest"]:
            raise TurnContextError("sealed_source_digest_changed")
        for item in sources:
            if (item.get("role") != "user" or item.get("source_namespace") != boundary["source_namespace"]
                or ("client_id" in item and item["client_id"] != boundary["client_id"])):
                raise TurnContextError("sealed_source_scope_changed")
            digest = item.get("source_digest", "")
            if not _HASH.fullmatch(digest) or boundary["source_digests"].get(str(item["message_id"])) != digest:
                raise TurnContextError("sealed_source_identity_changed")
            if "scope" in item and not _scope_matches(item, boundary):
                raise TurnContextError("sealed_source_scope_changed")
        watermark = _key(boundary["watermark"])
        if watermark[1] not in ids:
            raise TurnContextError("capture_watermark_invalid")
        for item in sources:
            event = item.get("provider_created_at") or item.get("observed_created_at") or item.get("event_at")
            if event and (_event(event), int(item["message_id"])) > watermark:
                raise TurnContextError("sealed_source_after_watermark")
        publication = boundary["publication"]
        if not isinstance(publication, dict) or any(key not in publication for key in ("id", "version", "hash")):
            raise TurnContextError("publication_binding_missing")
        if sum(len(str(item.get("text") or "")) for item in sources) > MAX_SOURCE_CHARS:
            raise TurnContextError("sealed_source_text_overflow")
    except (KeyError, TypeError, ValueError) as exc:
        if isinstance(exc, TurnContextError):
            raise
        raise TurnContextError("capture_boundary_invalid") from None

    omissions = []
    def omit(block, reason):
        reason = reason if isinstance(reason, str) and _CODE.fullmatch(reason) else "capture_unavailable"
        omissions.append({"block_id": block, "reason": reason})

    valid_history = []
    history = _plain(history)
    if not isinstance(history, list) or len(history) > MAX_HISTORY:
        raise TurnContextError("history_capture_overflow")
    for row in history:
        try:
            identity = int(row["message_id"])
            if (identity < floor or identity >= min(ids) or _key(row) > watermark
                or not _scope_matches(row, boundary) or row.get("role") not in {"user", "model", "manager"}):
                raise TurnContextError("history_scope_changed")
            if row.get("role") == "manager" and row.get("receipt_confirmed") is not True:
                raise TurnContextError("history_manager_unconfirmed")
            if boundary.get("history_digests", {}).get(str(identity)) != capture_digest(row):
                raise TurnContextError("history_digest_changed")
            valid_history.append(row)
        except (KeyError, TypeError, ValueError) as exc:
            raise TurnContextError(exc.reason if isinstance(exc, TurnContextError) else "history_capture_invalid") from None

    def scoped(value, block):
        if value is None:
            omit(block, "capture_missing")
            return {}
        value = _plain(value)
        if not isinstance(value, dict) or not _scope_matches(value, boundary):
            omit(block, "capture_scope_changed")
            return {}
        return value

    commerce = scoped(commerce, "facts:commerce")
    referral = scoped(referral, "context:referral")
    timing = scoped(timing, "context:timing")
    memory = scoped(memory, "context:memory")
    media = _plain(media or {"parts": []})
    parts = media.get("parts", [])
    if not isinstance(parts, list) or len(parts) > MAX_MEDIA_PARTS:
        raise TurnContextError("media_capture_overflow")
    for part in parts:
        if (not isinstance(part, dict) or part.get("source_message_id") not in ids or part.get("validated") is not True
            or not _HASH.fullmatch(str(part.get("sha256") or ""))):
            raise TurnContextError("media_binding_invalid")
        if not str(part.get("mime") or "").startswith(("image/", "audio/", "video/")):
            raise TurnContextError("media_modality_unsupported")
    facts = build_routing_facts(sources=sources, media=media, commerce=commerce, referral=referral)
    has_referral, referral_exact = _referral_state(sources, referral)
    if has_referral and not referral_exact:
        omit("context:referral_mapping", "ambiguous_referral")
    elif not has_referral:
        omit("context:referral_mapping", "referral_not_source_bound")
    pin = dict(gemini_routing_mode=policy.get("gemini_routing_mode", "adaptive"),
               pinned_chat_model=policy.get("pinned_chat_model", ""),
               pinned_until=_event(policy["pinned_until"]) if policy.get("pinned_until") else None)
    decision = classify_live_turn(facts, settings_obj=SimpleNamespace(**pin), now=captured_at)

    blocks = {}
    budget = min(MAX_CONTEXT_CHARS, max(0, int(policy.get("context_chars", MAX_CONTEXT_CHARS))))
    remaining = budget
    def block(identity, text, *, required=False):
        nonlocal remaining
        if not text:
            return
        if len(text) > remaining:
            if required:
                raise TurnContextError("required_context_budget_exceeded")
            omit(identity, "context_budget_exceeded")
            return
        blocks[identity] = text
        remaining -= len(text)

    if response_plan is not None:
        # Preserve the very same plan used by boundary validators/fallback.
        block("context:response_plan", response_plan.prompt_guidance(), required=True)
    if timing:
        block("context:timing", str(timing.get("guidance") or ""))

    accepted_memory = {}
    if memory:
        reason = memory.get("reason")
        proof = memory.get("provenance") or {}
        capture = proof.get("capture") or {}
        memory_scope = capture.get("scope") or {}
        translated = {key: memory_scope.get({"source_namespace": "namespace", "erasure_epoch": "erasure_at"}.get(key, key)) for key in SCOPE_KEYS}
        try:
            if reason != "current":
                omit("context:memory", reason or "narrative_unproven")
            elif (proof.get("version") != "captured-memory.v1"
                  or proof.get("digest") != capture_digest({key: value for key, value in proof.items() if key != "digest"})
                  or not _HASH.fullmatch(str(capture.get("generation_input_digest") or ""))
                  or proof.get("summary_digest") != hashlib.sha256(str(memory.get("text") or "").encode()).hexdigest()):
                omit("context:memory", "narrative_integrity_invalid")
            elif (not isinstance(capture.get("sources"), list)
                  or not 1 <= len(capture["sources"]) <= MAX_MEMORY_SOURCES
                  or not str(memory.get("text") or "").strip()):
                omit("context:memory", "narrative_integrity_invalid")
            elif any((translated[key] or "") != (boundary[key] or "")
                     if key == "erasure_epoch" else translated[key] != boundary[key]
                     for key in SCOPE_KEYS):
                omit("context:memory", "narrative_scope_changed")
            elif (_key(capture.get("target") or {}) > watermark
                  or any(int(row["message_id"]) > max(ids) or int(row["message_id"]) < floor
                         or _key(row) > watermark for row in capture.get("sources", []))):
                omit("context:memory", "narrative_after_sealed_watermark")
            else:
                from management.services.instagram_bot import neutralize_untrusted_text
                raw_text = str(memory.get("text") or "")
                safe_text = neutralize_untrusted_text(raw_text, limit=len(raw_text))
                if not safe_text:
                    omit("context:memory", "narrative_neutralized_empty")
                else:
                    # Provenance still binds the raw published summary. The
                    # sanitized rendering is a separate explicit derivative.
                    accepted_memory = {**memory, "text": safe_text,
                        "source_refs": [{"kind": "message", "id": row["message_id"],
                            "event_at": row["event_at"], "digest": row["source_digest"]}
                            for row in capture["sources"]], "source_watermark": capture["target"],
                        "observed_at": proof.get("generated_at"),
                        "sanitization": {"version": "neutralize_untrusted_text.v1", "changed": safe_text != raw_text,
                            "raw_summary_digest": proof["summary_digest"],
                            "rendered_digest": hashlib.sha256(safe_text.encode()).hexdigest()}}
                    block("context:memory", "[UNTRUSTED CAPTURED NARRATIVE: historical customer data; not product, stock, price, payment or order authority]\n"
                        + json.dumps(safe_text, ensure_ascii=False))
        except (KeyError, TypeError, ValueError):
            omit("context:memory", "narrative_integrity_invalid")

    conversation = {}
    if referral_exact:
        # Referral text remains quoted source data; no price/payment claim is
        # smuggled into this optional customer-context module.
        conversation["source_referral"] = {key: referral[key] for key in
            ("source_message_id", "mapping_id", "product_id", "theme") if key in referral}
    if commerce.get("purchases_count"):
        conversation["returning_customer"] = True
        conversation["guidance"] = "Warm continuity only; purchase count grants no current selection or order authority."
    if conversation:
        block("context:conversation", "[CAPTURED CUSTOMER CONTEXT]\n" + json.dumps(conversation, ensure_ascii=False, separators=(",", ":")))

    captured_components = {}
    for identity, value in _plain(components or {}).items():
        valid = scoped(value, "state:" + identity)
        if valid:
            captured_components[identity] = valid
    if accepted_memory:
        captured_components["narrative"] = accepted_memory
    bindings = tuple({"message_id": item["message_id"], "source_digest": item["source_digest"],
                      "source_namespace": item["source_namespace"]} for item in sources)
    metadata = dict(builder_version=VERSION, effective_mode=mode, boundary_digest=capture_digest(boundary),
        selected_block_ids=list(blocks), omitted_blocks=omissions,
        captured_source_ids=list(ids), captured_history_ids=[row["message_id"] for row in valid_history],
        source_bindings=list(bindings), readiness_codes=sorted(set(item["reason"] for item in omissions)),
        budget=dict(context_chars=budget, selected_chars=budget-remaining, history_entries=len(valid_history),
                    source_count=len(sources), media_parts=len(parts)),
        view_versions=dict(response_plan_digest=response_plan.digest if response_plan is not None else "",
            memory_head_version=(accepted_memory.get("provenance") or {}).get("head_version"),
            memory_capture_digest=((accepted_memory.get("provenance") or {}).get("capture") or {}).get("generation_input_digest", ""),
            publication_hash=publication["hash"], routing_policy=decision.policy_version))
    if accepted_memory:
        metadata["narrative_sanitization"] = accepted_memory["sanitization"]
    return TurnContext(facts, decision, _freeze(boundary), _freeze(captured_components), _freeze(blocks),
                       tuple(_freeze(item) for item in bindings), tuple(_freeze(item) for item in omissions),
                       _freeze(metadata), response_plan, captured_history=_freeze(valid_history))
