"""Manager-scoped offers and own-source choices for conserved intentions.

Cards are signed evidence on the existing native follow-up task. A task or a
manager action alone never supplies purchase authority: only the delivery
reader's actual SENT receipt and authenticated customer source can do that.
"""
from copy import deepcopy
from dataclasses import dataclass
from datetime import timedelta
import hmac
import json
import re
import uuid

from django.db import transaction
from django.utils import timezone

from management.services import ig_assisted_commerce as core

CARD_KEY = "assisted_partial_card"
CHOICE_KEY = "assisted_partial_choice"
PARTIAL_REASON = "assisted_partial_offer"
CONTINUATION_REASON = "assisted_continuation_offer"
SHIPPING_REVIEW_REASON = "assisted_shipping_review"
_ISSUER = object()

REASONS = {
    "approved_decision_missing": "Спочатку перевірте та затвердьте умови товарів.",
    "partial_terms_required": "Окремо збережіть і затвердьте максимальні витрати на доставку та ціну готової частини.",
    "partial_scope_required": "Ця дія доступна для кошика з готовими та відкладеними позиціями.",
    "coverage_changed": "Склад наміру змінився. Оновіть картку перед наступною дією.",
    "choice_required": "Очікуємо власного вибору клієнта в доставленій картці.",
    "customer_waiting_together": "Клієнт обрав чекати всі позиції. Часткове оформлення не погоджено.",
    "card_expired": "Строк цієї пропозиції завершився. Потрібна нова перевірка умов.",
    "provider_outcome_unknown_no_retry": "Доставку потрібно перевірити. Повторне надсилання заблоковано.",
    "continuation_not_ready": "Продовження доступне після підтвердженого замовлення першої частини.",
    "request_missing": "Поточну перевірку товарів ще не створено.",
    "offer_already_issued": "Цю картку вже підготовлено. Перевірте її доставку та вибір клієнта; повторне надсилання не створюється.",
    "selected_catalog_price_changed": "Ціна готових позицій каталогу змінилася. Повторно перевірте та затвердьте умови перед надсиланням.",
}


def reason_label(reason):
    return REASONS.get(reason, "Дія зараз недоступна. Перевірте поточний запит, джерела та умови.") if reason else ""


def _scopes():
    from management.services import ig_assisted_scopes
    return ig_assisted_scopes


def _signed(kind, body):
    key = next(iter(core._keys()))
    body = json.loads(core._json(body))
    return {"version": 1, "key_id": key, "hmac": core._sign(kind, body, key), "manifest": body}


def _verified(kind, value):
    if not isinstance(value, dict) or set(value) != {"version", "key_id", "hmac", "manifest"} or value["version"] != 1:
        return None
    expected = core._sign(kind, value["manifest"], value["key_id"])
    return deepcopy(value["manifest"]) if expected and hmac.compare_digest(expected, str(value["hmac"])) else None


def _rows(client_id, request_id, decision_id=None):
    Request, Decision, _Execution, _Image = core._models()
    request = Request.objects.filter(pk=core._uuid(request_id), client_id=client_id).first()
    if not request or not core._verified(request, "request"):
        core._fail("request_missing")
    decision = (Decision.objects.filter(pk=core._uuid(decision_id), request=request).first()
        if decision_id is not None else Decision.objects.filter(request=request).order_by("-version").first())
    if decision is not None and not core._verified(decision, "decision"):
        core._fail("decision_unverified")
    return request, decision


def _coverage(request, *, now, lock=False):
    value = _scopes().intention_coverage(request.client_id, request.intention_id, now=now, lock=lock)
    if value.get("ready") is not True:
        core._fail(value.get("reason") or "coverage_unverified")
    return value


def _scope_quote(decision):
    return deepcopy(((decision.envelope["snapshot"].get("scoped_quotes") or {}).get("selected"))) if decision else None


def _card_language(client, settings_obj, data, *, now):
    """Display language comes from an actual owned sealed customer source."""
    from management.models import IgCustomerTurnRevision
    from management.services.ig_purpose_sources import latest_customer_source, owned_source
    from management.services.ig_reply_language import resolve_own_source_reply_language
    family = data["reply_language"]["template_family"]
    source = latest_customer_source(client, settings_obj, now=now)
    revision = (IgCustomerTurnRevision.objects.filter(client=client, sources__message=source).order_by("-pk").first()
        if source else None)
    owned, _reason = owned_source(revision, source, settings_obj, require_head=False) if revision else (None, "")
    if owned:
        _revision, row, proof = owned
        if row.quick_reply_payload:
            # A short physical button caption is language evidence only after
            # its own sealed source and the exact signed SENT card validate.
            # Historical validation avoids re-entering this current-card gate.
            # A continuation response may belong to the preceding card; only
            # choice authority, rather than this language hint, pins task ID.
            from management.services.ig_assisted_partial_delivery import (
                AssistedPartialSourceError, validate_card_choice_source,
            )
            try:
                native = validate_card_choice_source(row, _revision, settings_obj, now=now,
                    require_current=False)
            except AssistedPartialSourceError:
                native = None
            if native is not None and native.card["proof"]["settings_id"] == settings_obj.pk:
                locale = native.card["proof"]["locale"]
                if locale in {"uk", "ru", "en"}:
                    return locale
        resolved = resolve_own_source_reply_language(sources=[{"message_id": row.pk, "role": "user", "text": row.text,
            "provider_created_at": row.provider_created_at.isoformat(), "source_digest": proof["digest"]}],
            profile_language=family, reset_floor=proof["reset_floor"], watermark_message_id=row.pk, watermark_event_at=now)
        family = resolved.template_family
    return family if family in {"uk", "ru", "en"} else "en"


def _fresh_partial(request, decision, *, actor, now, read_unit=None):
    if not decision:
        core._fail("approved_decision_missing")
    if read_unit is not None:
        unit = _scopes().require_selection_read_unit(read_unit, request, now=now)
        cap = unit.decision(request, decision, actor=actor, now=now)
    else:
        cap = core.load_verified_assisted_decision(request.pk, decision.pk, actor=actor,
            expected_request_digest=request.envelope_hmac, expected_decision_digest=decision.envelope_hmac, now=now)
    quote = _scope_quote(decision)
    if not quote or not quote.get("shipping_terms") or not quote.get("terms_digest"):
        core._fail("partial_terms_required")
    if quote.get("scope") != "partial" or not quote.get("selected_line_ids"):
        core._fail("partial_scope_required")
    if _scopes().build_scoped_quotes(request, cap.lines, quote["shipping_terms"], read_unit=read_unit, now=now)["selected"] != quote:
        core._fail("selected_catalog_price_changed")
    coverage = unit.coverage(request, now=now) if read_unit is not None else _coverage(request, now=now)
    if not quote.get("deferred_line_ids"):
        history = unit.history(request, now=now) if read_unit is not None else _scopes().original_intention_history(request, now=now)
        if (history.get("ready") is not True or history.get("root_request_id") == str(request.pk)
            or not coverage.get("settled_line_ids")
            or set(quote["selected_line_ids"]) != set(coverage.get("deferred_line_ids", []))):
            core._fail("partial_scope_required")
    blocked = set(coverage.get("settled_line_ids", [])) | set(coverage.get("inflight_line_ids", [])) | set(coverage.get("cancelled_line_ids", []))
    if set(quote["selected_line_ids"]) & blocked:
        core._fail("coverage_changed")
    return cap, quote, coverage


COPY = {
    "uk": {"heading": "Готову частину можна оформити окремо", "ready": "Зараз", "later": "Пізніше",
        "goods": "Товари зараз", "fee": "Оплата доставки перевізнику окремо: до", "merchant": "Доставку оплачує TwoComms; для вас",
        "together": "Доставка всіх разом", "partial": "Оформити готові", "wait": "Дочекатися всіх", "resume": "Продовжити",
        "notice": "Після оформлення відправимо готові позиції. Точний день отримання залежить від перевізника.",
        "maximum": "Це погоджені максимуми для вас. Фактичну вартість доставки визначає перевізник.",
        "continuation": "Залишилися неоплачені позиції. Повернути їх до перевірки? Готовність, ціну й доставку підтвердимо окремо."},
    "ru": {"heading": "Готовую часть можно оформить отдельно", "ready": "Сейчас", "later": "Позже",
        "goods": "Товары сейчас", "fee": "Оплата доставки перевозчику отдельно: до", "merchant": "Доставку оплачивает TwoComms; для вас",
        "together": "Доставка всех вместе", "partial": "Оформить доступное", "wait": "Подождать все", "resume": "Продолжить",
        "notice": "После оформления отправим готовые позиции. Точный день получения зависит от перевозчика.",
        "maximum": "Это согласованные максимумы для вас. Фактическую стоимость доставки определяет перевозчик.",
        "continuation": "Остались неоплаченные позиции. Вернуть их к проверке? Готовность, цену и доставку подтвердим отдельно."},
    "en": {"heading": "You can order the ready items separately", "ready": "Now", "later": "Later",
        "goods": "Goods due now", "fee": "Delivery paid separately to the carrier: up to", "merchant": "TwoComms pays delivery; you pay",
        "together": "Delivery of all items together", "partial": "Order ready items", "wait": "Wait for all items", "resume": "Continue",
        "notice": "After checkout, we’ll send the ready items. The exact arrival date depends on the carrier.",
        "maximum": "These are your agreed maximum charges. The carrier determines the actual delivery cost.",
        "continuation": "Some items remain unpaid. Return them for review? Readiness, price and delivery will be confirmed separately."},
}


def _payload(task_id, action, manifest, key_id):
    code = {"partial": "p", "together": "t", "resume": "r"}[action]
    body = f"{task_id}:{code}"
    mac = core._sign("partial_button", {"body": body, "card_digest": core._digest(manifest)}, key_id)[:16]
    return f"AC1:{body}:{mac}"


def _template_copy(manifest):
    """Run the server template once, before signing this newly issued card."""
    copy = COPY.get(manifest["locale"], COPY["uk"])
    quote = manifest.get("quote") or {}
    if manifest["kind"] == "continuation":
        text = copy["continuation"] + "\n" + "\n".join(manifest["residual_labels"])
        buttons = [{"action": "resume", "title": copy["resume"]}]
    else:
        remainder = quote.get("delivery_scope") == "later" and not quote["deferred_line_ids"]
        headings = {"uk": "Готова решта для окремого оформлення", "ru": "Готовый остаток для отдельного оформления",
            "en": "The remaining ready items can be ordered separately"}
        parts = [headings.get(manifest["locale"], headings["uk"]) if remainder else copy["heading"]]
        for phase, ids in (("ready", quote["selected_line_ids"]), ("later", quote["deferred_line_ids"])):
            if ids:
                parts.append(copy[phase] + ": " + "; ".join(manifest["labels"][line_id] for line_id in ids))
        parts.append(f"{copy['goods']}: {quote['total']} UAH.")
        for phase in (("later",) if remainder else ("now", "later", "together")):
            term_scope = quote["delivery_scope"] if phase == "now" else phase
            term = quote["shipping_terms"][term_scope]
            shipment = {"uk": "Ця відправка", "ru": "Эта отправка", "en": "This shipment"}
            title = shipment.get(manifest["locale"], shipment["uk"]) if remainder else copy["together"] if phase == "together" else copy["ready" if phase == "now" else "later"]
            payer = copy["fee"] if term["payer"] == "customer" else copy["merchant"]
            parts.append(f"{title} — {payer} {term['customer_maximum']} UAH.")
        if any(quote["shipping_terms"][scope]["payer"] == "customer" for scope in
                (("later",) if remainder else ("now", "later", "together"))):
            parts.append(copy["maximum"])
        parts.append(copy["notice"])
        text = "\n".join(parts)
        buttons = [{"action": action, "title": copy["partial"] if action == "partial" else copy["wait"]}
            for action in (("partial",) if remainder else ("partial", "together"))]
    return {"text": text, "buttons": buttons}


def _render(manifest, key_id):
    """Reproduce the signed physical copy without today's marketing template.

    Payloads bind the complete frozen manifest; storing only action/title
    pairs avoids a circular payload -> manifest -> payload digest.
    """
    from management.services.ig_message_templates import QuickReply
    frozen = manifest.get("rendered_copy")
    if (manifest.get("locale") not in {"uk", "ru", "en"} or not isinstance(frozen, dict)
        or set(frozen) != {"text", "buttons"} or not isinstance(frozen["text"], str)
        or not isinstance(frozen["buttons"], list) or not 1 <= len(frozen["buttons"]) <= 2):
        core._fail("card_rendered_copy_invalid")
    quote = manifest.get("quote") or {}
    expected = (["resume"] if manifest["kind"] == "continuation" else ["partial"]
        if quote.get("delivery_scope") == "later" and not quote["deferred_line_ids"] else ["partial", "together"])
    if any(not isinstance(button, dict) or set(button) != {"action", "title"} for button in frozen["buttons"]):
        core._fail("card_rendered_copy_invalid")
    if [button["action"] for button in frozen["buttons"]] != expected:
        core._fail("card_rendered_copy_invalid")
    for button in frozen["buttons"]:
        title = button["title"]
        if (not isinstance(title, str) or not 1 <= len(title) <= 20 or title.strip() != title
            or any(ord(character) < 32 for character in title)):
            core._fail("card_rendered_copy_invalid")
    text = frozen["text"]
    if len(text.encode()) > 16000:
        core._fail("offer_body_too_large")
    from management.services.ig_delivery_plan import build_delivery_plan
    if not text or not build_delivery_plan(text).complete:
        core._fail("offer_body_too_large")
    actions = {button["action"]: _payload(manifest["task_id"], button["action"], manifest, key_id)
        for button in frozen["buttons"]}
    buttons = tuple(QuickReply(button["title"], actions[button["action"]]) for button in frozen["buttons"])
    return text, buttons, actions


def card_for_task(task, settings_obj=None, *, now=None, require_current=True, read_unit=None):
    from management.models import IgFollowUpTask, IgClient
    now = core._now(now)
    actual = IgFollowUpTask.objects.filter(pk=getattr(task, "pk", None)).first()
    card = (actual.manager_context or {}).get(CARD_KEY) if actual else None
    manifest = _verified("partial_card", card)
    if not manifest:
        return {"ready": False, "reason": "card_unverified"}
    try:
        origin = PARTIAL_REASON if manifest["kind"] == "partial" else CONTINUATION_REASON
        if (actual.pk != manifest["task_id"] or actual.client_id != manifest["client_id"]
            or actual.reason != origin or actual.kind != "manager_task" or actual.trigger != "event"
            or actual.event_key != "assisted-card:" + manifest["operation_id"]
            or actual.event_payload != {"origin": origin, "operation_id": manifest["operation_id"]}):
            core._fail("card_binding_changed")
        request, decision = _rows(actual.client_id, manifest["request_id"], manifest.get("decision_id"))
        if request.envelope_hmac != manifest["request_digest"] or (decision and decision.envelope_hmac != manifest.get("decision_digest")):
            core._fail("card_binding_changed")
        client = IgClient.objects.get(pk=actual.client_id)
        if client.privacy_erasure_started_at or client.hidden_at:
            core._fail("client_unavailable")
        if require_current:
            if now >= core._date(manifest["expires_at"]):
                core._fail("card_expired")
            actual_settings, _epochs = core._permission(client, manifest["settings_id"], expected_settings_epoch=manifest["settings_epoch"],
                expected_client_epoch=manifest["client_epoch"])
            actor = core._actor(manifest["actor_id"])
            if _card_language(client, actual_settings, request.envelope["snapshot"], now=now) != manifest["locale"]:
                core._fail("card_language_changed")
            if manifest["kind"] == "partial":
                _cap, quote, coverage = _fresh_partial(request, decision, actor=actor, now=now, read_unit=read_unit)
                if quote != manifest["quote"]:
                    core._fail("terms_changed")
            else:
                coverage = _coverage(request, now=now)
            if coverage["coverage_digest"] != manifest["coverage_digest"]:
                core._fail("coverage_changed")
        text, buttons, actions = _render(manifest, card["key_id"])
        if actual.message_text != text:
            core._fail("card_body_changed")
        proof = {**manifest, "card_digest": core._digest(manifest),
            "body_digest": core._digest({"text": text, "buttons": [button.__dict__ for button in buttons]})}
        return {"ready": True, "reason": "", "text": text, "buttons": buttons, "actions": actions, "proof": proof}
    except (core.AssistedDecisionError, KeyError, TypeError, ValueError) as exc:
        return {"ready": False, "reason": getattr(exc, "reason", "card_unverified")}


def parse_card_payload(raw):
    from management.models import IgFollowUpTask
    match = re.fullmatch(r"AC1:([1-9]\d{0,19}):(p|t|r):([a-f0-9]{16})", str(raw or ""))
    if not match:
        return None
    task = IgFollowUpTask.objects.filter(pk=int(match[1])).first()
    value = card_for_task(task, require_current=False)
    action = {"p": "partial", "t": "together", "r": "resume"}[match[2]]
    expected = value.get("actions", {}).get(action)
    return (task.pk, action) if expected and hmac.compare_digest(raw, expected) else None


def _issue(client_id, *, actor, request_id, expected_request_digest, expected_coverage_digest,
        operation_id, decision_id=None, expected_decision_digest=None, expected_terms_digest=None, kind="partial", now=None):
    from management.models import IgClient, IgFollowUpTask
    from management.services.ig_commercial_episodes import commercial_episode_client_lock
    now, actor, operation_id = core._now(now), core._actor(getattr(actor, "pk", None)), core._uuid(operation_id)
    with commercial_episode_client_lock(client_id), transaction.atomic():
        actor = core._actor(actor.pk)
        client = IgClient.objects.select_for_update().get(pk=client_id)
        request, decision = _rows(client_id, request_id, decision_id)
        operation = {"kind": kind, "client_id": client_id, "actor_id": actor.pk, "request_id": str(request.pk),
            "request_digest": expected_request_digest, "decision_id": str(decision.pk) if decision else None,
            "decision_digest": expected_decision_digest if kind == "partial" else decision.envelope_hmac if decision else None,
            "coverage_digest": expected_coverage_digest,
            "terms_digest": expected_terms_digest, "operation_id": str(operation_id)}
        prior = IgFollowUpTask.objects.filter(event_key="assisted-card:" + str(operation_id)).first()
        if prior:
            manifest = _verified("partial_card", (prior.manager_context or {}).get(CARD_KEY))
            if not manifest or manifest["operation_input_digest"] != core._digest(operation):
                core._fail("operation_conflict")
            return prior, False
        for uncertain in IgFollowUpTask.objects.select_for_update().filter(client_id=client_id,
                reason__in=[PARTIAL_REASON, CONTINUATION_REASON], status="ambiguous").order_by("-pk")[:40]:
            previous = _verified("partial_card", (uncertain.manager_context or {}).get(CARD_KEY))
            if previous and previous["intention_id"] == str(request.intention_id):
                core._fail("provider_outcome_unknown_no_retry")
        for existing in IgFollowUpTask.objects.select_for_update().filter(client_id=client_id,
                reason=PARTIAL_REASON if kind == "partial" else CONTINUATION_REASON).order_by("-pk")[:40]:
            previous = _verified("partial_card", (existing.manager_context or {}).get(CARD_KEY))
            if (previous and previous["request_id"] == str(request.pk)
                    and previous["decision_id"] == operation["decision_id"]
                    and previous["coverage_digest"] == expected_coverage_digest
                    and previous["terms_digest"] == expected_terms_digest
                    and existing.status not in {"cancelled", "skipped"}):
                core._fail("offer_already_issued")
        if client.privacy_erasure_started_at or client.hidden_at or request.envelope_hmac != expected_request_digest:
            core._fail("request_source_or_owner_changed")
        if kind == "partial":
            if not decision or decision.envelope_hmac != expected_decision_digest:
                core._fail("approved_decision_changed")
            cap, quote, coverage = _fresh_partial(request, decision, actor=actor, now=now)
            if quote["terms_digest"] != expected_terms_digest:
                core._fail("terms_changed")
            original_ids = {entry["current_line_id"]: entry["line_id"] for entry in _scopes().request_line_map(request)}
            labels = {original_ids[line.line_id]: " · ".join(str(value) for value in (line.title, line.color, line.size, f"×{line.quantity}") if value)
                for line in cap.lines}
        else:
            coverage = _coverage(request, now=now)
            if (not any(row["request_id"] == str(request.pk) and row["state"] == "settled" for row in coverage.get("executions", []))
                or coverage.get("inflight_line_ids") or not coverage.get("deferred_line_ids")
                or core._models()[0].objects.filter(client_id=client_id, intention_id=request.intention_id).order_by("-version").first().pk != request.pk):
                core._fail("continuation_not_ready")
            quote, labels = None, {}
        if coverage["coverage_digest"] != expected_coverage_digest:
            core._fail("coverage_changed")
        data = request.envelope["snapshot"]
        from management.services.ig_commerce_projection import capture_current_selection_lines
        capture = capture_current_selection_lines(client.pk, now=now)
        if capture.get("status") != "captured":
            core._fail("source_not_selected")
        settings_obj, epochs = core._permission(client, data["settings_id"],
            expected_settings_epoch=data["settings_permission_epoch"], expected_client_epoch=request.client_permission_epoch)
        origin = PARTIAL_REASON if kind == "partial" else CONTINUATION_REASON
        task = IgFollowUpTask.objects.create(client=client, kind="manager_task", reason=origin,
            due_at=now, status="pending", trigger="event", event_key="assisted-card:" + str(operation_id),
            event_occurred_at=now, policy_started_at=now, event_payload={"origin": origin, "operation_id": str(operation_id)})
        history = _scopes().original_intention_history(request, now=now)
        if history.get("ready") is not True:
            core._fail(history.get("reason") or "intention_history_unverified")
        root = core._models()[0].objects.get(pk=history["root_request_id"])
        root_lines = root.envelope["snapshot"]["lines"]
        residual_labels = [" · ".join(str(value) for value in (row.get("draft_title"),
            row["source_choices"].get("color"), row["source_choices"].get("size")) if value)
            for row in root_lines if row["line_id"] in coverage.get("deferred_line_ids", [])]
        manifest = {"task_id": task.pk, "kind": kind, **operation, "operation_input_digest": core._digest(operation),
            "intention_id": str(request.intention_id), "source_namespace": request.source_namespace,
            "source_head": deepcopy(data["source_head"]), "source_id": request.source_id,
            "owner_scope": {"scope": deepcopy(capture["scope"]), "session_id": capture.get("session_id"),
                "generation": capture.get("generation"), "selection_revision": capture.get("selection_revision"),
                "capture_digest": capture["capture_digest"]},
            "settings_id": settings_obj.pk, "settings_epoch": epochs.settings_epoch, "client_epoch": epochs.client_epoch,
            "locale": _card_language(client, settings_obj, data, now=now), "issued_at": now.isoformat(),
            "expires_at": min(now + timedelta(hours=12), decision.expires_at).isoformat() if kind == "partial" else (now + timedelta(hours=12)).isoformat(),
            "quote": quote, "labels": labels, "residual": coverage.get("deferred_line_ids", []), "residual_labels": residual_labels}
        manifest["rendered_copy"] = _template_copy(manifest)
        signed = _signed("partial_card", manifest)
        text, _buttons, _actions = _render(signed["manifest"], signed["key_id"])
        task.manager_context, task.message_text = {CARD_KEY: signed}, text
        task.save(update_fields=["manager_context", "message_text", "updated_at"])
        return task, True


def issue_partial_offer(client_id, **kwargs):
    return _issue(client_id, kind="partial", **kwargs)


def issue_continuation_offer(client_id, **kwargs):
    return _issue(client_id, kind="continuation", **kwargs)


@dataclass(frozen=True)
class VerifiedAssistedChoice:
    request_id: object
    decision_id: object
    client_id: int
    task_id: int
    revision_id: int
    choice_source_id: int
    purchase_source_id: int
    terms_digest: str
    card_digest: str
    choice_proof: dict
    purchase_proof: dict
    _issuer: object

    def __post_init__(self):
        if self._issuer is not _ISSUER:
            raise ValueError("native_partial_choice_required")


def revalidate_native_card_choice(native_choice, *, now=None, require_current=True):
    from management.services.ig_assisted_partial_delivery import revalidate_native_card_choice as native_reader
    return native_reader(native_choice, now=now, require_current=require_current)


def _choice_cap(task, body):
    manifest = _verified("partial_card", (task.manager_context or {}).get(CARD_KEY))
    if not manifest or body["action"] != "partial":
        core._fail("choice_required")
    return VerifiedAssistedChoice(core._uuid(manifest["request_id"]), core._uuid(manifest["decision_id"]),
        task.client_id, task.pk, body["revision_id"], body["source_id"], body["source_id"],
        manifest["terms_digest"], core._digest(manifest), deepcopy(body), deepcopy(body["source_proof"]), _ISSUER)


def revalidate_verified_partial_choice(value, *, now=None, read_unit=None):
    from management.models import IgFollowUpTask, InstagramBotMessage, IgCustomerTurnRevision, InstagramBotSettings
    from management.services.ig_assisted_partial_delivery import validate_card_choice_source
    if not isinstance(value, VerifiedAssistedChoice) or value._issuer is not _ISSUER:
        core._fail("native_partial_choice_required")
    task = IgFollowUpTask.objects.filter(pk=value.task_id, client_id=value.client_id).first()
    body = _verified("partial_choice", (task.manager_context or {}).get(CHOICE_KEY)) if task else None
    if not body or body != value.choice_proof or body["action"] != "partial":
        core._fail("choice_changed")
    native = validate_card_choice_source(InstagramBotMessage.objects.get(pk=body["source_id"]),
        IgCustomerTurnRevision.objects.get(pk=body["revision_id"]), InstagramBotSettings.objects.get(pk=body["settings_id"]),
        now=now, expected_task_id=task.pk, read_unit=read_unit)
    if native.source_proof != body["source_proof"] or native.receipt != body["receipt"]:
        core._fail("choice_changed")
    cap = _choice_cap(task, body)
    if cap != value:
        core._fail("choice_changed")
    return {"request_id": str(cap.request_id), "decision_id": str(cap.decision_id),
        "client_id": cap.client_id, "task_id": cap.task_id, "revision_id": cap.revision_id, "card_digest": cap.card_digest,
        "choice_source_id": cap.choice_source_id, "purchase_source_id": cap.purchase_source_id,
        "choice_proof": deepcopy(body), "purchase_proof": deepcopy(cap.purchase_proof), "terms_digest": cap.terms_digest}


def consume_card_choice(source, revision, settings_obj, *, now=None):
    from management.models import IgFollowUpTask
    from management.services.ig_assisted_partial_delivery import validate_card_choice_source, AssistedPartialSourceError
    from management.services.ig_assisted_continuation_sources import AssistedAdoptionError
    from management.services.ig_commercial_episodes import commercial_episode_client_lock
    parsed = parse_card_payload(getattr(getattr(source, "message", source), "quick_reply_payload", ""))
    if not parsed:
        return {"handled": False, "ready": False, "reason": ""}
    try:
        with commercial_episode_client_lock(revision.client_id), transaction.atomic():
            native = validate_card_choice_source(source, revision, settings_obj, now=now, expected_task_id=parsed[0])
            task = IgFollowUpTask.objects.select_for_update().get(pk=native.task_id)
            body = {"task_id": task.pk, "action": native.action, "source_id": native.source_proof["message_id"],
                "revision_id": revision.pk, "settings_id": settings_obj.pk, "source_proof": native.source_proof,
                "receipt": native.receipt, "card_digest": native.card["proof"]["card_digest"]}
            previous = _verified("partial_choice", (task.manager_context or {}).get(CHOICE_KEY))
            if previous and previous != body:
                core._fail("choice_already_recorded")
            if not previous:
                task.manager_context = {**(task.manager_context or {}), CHOICE_KEY: _signed("partial_choice", body)}
                task.save(update_fields=["manager_context", "updated_at"])
            if native.action == "resume":
                from management.services.ig_assisted_continuation_sources import adopt_residual_source, revalidate_verified_assisted_adoption
                from management.services.ig_commerce_projection import capture_current_selection_lines
                adoption = adopt_residual_source(native, revision, settings_obj, now=now)
                parent, _decision = _rows(task.client_id, native.card["proof"]["request_id"])
                captured = capture_current_selection_lines(task.client_id, now=now)
                if (captured.get("status") != "captured" or captured.get("coverage_complete") is not True
                    or not core._hash(captured.get("capture_digest"))):
                    core._fail(captured.get("reason") or "residual_capture_incomplete")
                request, created = core.create_residual_assisted_request(adoption, settings_id=settings_obj.pk,
                    operation_id=uuid.uuid5(uuid.NAMESPACE_URL, f"assisted-residual:{adoption.transition_id}"),
                    expected_revision_digest=revision.snapshot_digest, expected_full_capture_digest=captured["capture_digest"],
                    expected_request_version=parent.version, expected_request_digest=parent.envelope_hmac, now=now)
                locale = native.card["proof"]["locale"]
                reply = {"uk": "Неоплачені позиції повернуто до перевірки. Менеджер заново підтвердить готовність, ціну й доставку.",
                    "ru": "Неоплаченные позиции возвращены к проверке. Менеджер заново подтвердит готовность, цену и доставку.",
                    "en": "The unpaid items are back for review. A manager will confirm readiness, price and delivery again."}
                return {"handled": True, "ready": True, "reason": "", "adoption": revalidate_verified_assisted_adoption(adoption, now=now),
                    "residual_request_id": str(request.pk), "residual_request_digest": request.envelope_hmac,
                    "created": created, "requires_model": False, "reply_text": reply.get(locale, reply["uk"])}
            selection = _scopes().verified_selection_from_choice(_choice_cap(task, body), now=now) if native.action == "partial" else None
            locale = native.card["proof"]["locale"]
            replies = {
                "uk": {"partial": "Вибір готової частини збережено. Далі підтвердьте товари й умови на сторінці оформлення.",
                    "together": "Домовились: чекаємо всі позиції. Часткове замовлення не оформляємо."},
                "ru": {"partial": "Выбор готовой части сохранён. Далее подтвердите товары и условия на странице оформления.",
                    "together": "Договорились: ждём все позиции. Частичный заказ не оформляем."},
                "en": {"partial": "Your ready-item choice is saved. Next, confirm the items and terms on the checkout page.",
                    "together": "Agreed: wait for all items. No partial order is being placed."},
            }
            return {"handled": True, "ready": True, "reason": "", "selection": selection,
                "choice": body, "requires_model": native.action == "partial", "reply_text": replies.get(locale, replies["uk"])[native.action]}
    except (core.AssistedDecisionError, AssistedPartialSourceError, AssistedAdoptionError) as exc:
        return {"handled": True, "ready": False, "reason": exc.reason, "reply_text": reason_label(exc.reason)}


def latest_verified_selection(client_id, *, actor=None, request_id=None, decision_id=None, now=None):
    from management.models import IgFollowUpTask
    from management.services.ig_assisted_partial_delivery import AssistedPartialSourceError
    rows = IgFollowUpTask.objects.filter(client_id=client_id, reason=PARTIAL_REASON).order_by("-pk")[:20]
    for task in rows:
        manifest = _verified("partial_card", (task.manager_context or {}).get(CARD_KEY))
        body = _verified("partial_choice", (task.manager_context or {}).get(CHOICE_KEY))
        if (manifest and body and body["action"] == "partial"
            and (request_id is None or manifest["request_id"] == str(request_id))
            and (decision_id is None or manifest["decision_id"] == str(decision_id))):
            try:
                return _scopes().verified_selection_from_choice(_choice_cap(task, body), actor=actor, now=now)
            except AssistedPartialSourceError as exc:
                # Native source failures remain denial reasons, rather than an
                # exception escaping a committed manager action's readback.
                core._fail(exc.reason)
    core._fail("choice_required")


def verified_partial_choice_for_source(source_id, *, now=None):
    """Signed choice handle; the selection producer owns its fresh revalidation."""
    from management.models import IgFollowUpTask, InstagramBotMessage
    if type(source_id) is not int or source_id <= 0:
        core._fail("choice_source_invalid")
    source = InstagramBotMessage.objects.filter(pk=source_id).first()
    if not source:
        core._fail("choice_required")
    tasks = IgFollowUpTask.objects.filter(client_id=source.client_id, reason=PARTIAL_REASON,
        manager_context__assisted_partial_choice__manifest__source_id=source_id).order_by("-pk")[:2]
    candidates = []
    for task in tasks:
        body = _verified("partial_choice", (task.manager_context or {}).get(CHOICE_KEY))
        if body and body["source_id"] == source_id and body["action"] == "partial":
            candidates.append((task, body))
    if len(candidates) != 1:
        core._fail("choice_required")
    choice = _choice_cap(*candidates[0])
    return choice


def current_verified_selection(client, request, decision, *, revision=None, now=None):
    """Optional live native choice; absence never broadens the ordinary path."""
    try:
        selection = latest_verified_selection(client.pk, request_id=request.pk, decision_id=decision.pk, now=now)
        if selection.decision_id != decision.pk or (revision and selection.revision_id != revision.pk):
            return None
        return selection
    except core.AssistedDecisionError as exc:
        if exc.reason in {"choice_required", "choice_absent"}:
            return None
        raise


def selection_reply_language(value):
    """Language of the exact signed card already proved by this selection."""
    from management.models import IgFollowUpTask
    _scopes().selection_evidence(value)  # Exact native issuer; no raw dicts.
    choice = value.choice_proof
    task = IgFollowUpTask.objects.filter(pk=choice["task_id"], client_id=value.client_id).first()
    context = task.manager_context or {} if task else {}
    card = _verified("partial_card", context.get(CARD_KEY))
    accepted = _verified("partial_choice", context.get(CHOICE_KEY))
    if (not card or accepted != choice or core._digest(card) != choice["card_digest"]
        or card["request_id"] != str(value.request_id) or card["decision_id"] != str(value.decision_id)
        or card["locale"] not in {"uk", "ru", "en"}):
        core._fail("choice_changed")
    return card["locale"]


def prepare_selected(client_id, *, actor, request_id, decision_id, expected_request_digest,
        expected_decision_digest, expected_coverage_digest, expected_terms_digest, task_id,
        expected_choice_digest, operation_id, now=None):
    from management.models import IgFollowUpTask
    from management.services.ig_assisted_checkout import prepare_assisted_checkout, validate_assisted_execution_history
    from management.services.ig_commercial_episodes import commercial_episode_client_lock
    actor, now = core._actor(getattr(actor, "pk", None)), core._now(now)
    with commercial_episode_client_lock(client_id), transaction.atomic():
        request, decision = _rows(client_id, request_id, decision_id)
        if request.envelope_hmac != expected_request_digest or not decision or decision.envelope_hmac != expected_decision_digest:
            core._fail("approved_decision_changed")
        task = IgFollowUpTask.objects.select_for_update().filter(pk=task_id, client_id=client_id).first()
        body = _verified("partial_choice", (task.manager_context or {}).get(CHOICE_KEY)) if task else None
        if not body or core._digest(body) != expected_choice_digest:
            core._fail("choice_changed")
        choice = _choice_cap(task, body)
        if choice.request_id != request.pk or choice.decision_id != decision.pk or choice.terms_digest != expected_terms_digest:
            core._fail("terms_changed")
        Execution = core._models()[2]
        previous = Execution.objects.filter(operation_id=core._uuid(operation_id), client_id=client_id).first()
        if previous:
            checked = validate_assisted_execution_history(previous)
            snapshot = previous.envelope.get("snapshot") or {}
            if (checked.get("ready") is not True or previous.request_id != request.pk or previous.decision_id != decision.pk
                or snapshot.get("choice_proof") != body or snapshot.get("terms_digest") != expected_terms_digest
                or (snapshot.get("selection_evidence") or {}).get("ledger_coverage_digest") != expected_coverage_digest):
                core._fail("operation_conflict")
            selection = _scopes().verified_selection_from_evidence(request, decision, previous, historical=True)
            return prepare_assisted_checkout(client_id, actor=actor, request_id=request.pk, decision_id=decision.pk,
                expected_request_digest=expected_request_digest, expected_decision_digest=expected_decision_digest,
                operation_id=operation_id, verified_selection=selection)
        if _coverage(request, now=now)["coverage_digest"] != expected_coverage_digest:
            core._fail("coverage_changed")
        selection = _scopes().verified_selection_from_choice(choice, actor=actor, now=now)
        return prepare_assisted_checkout(client_id, actor=actor, request_id=request.pk, decision_id=decision.pk,
            expected_request_digest=request.envelope_hmac, expected_decision_digest=decision.envelope_hmac,
            operation_id=operation_id, verified_selection=selection)


def resolve_shipping_review(client_id, *, actor, order_id, expected_document_ref, expected_terms_digest, operation_id, now=None):
    """One explicit manager reconciliation of the exact native paid Order.

    The existing completed manager task holds its signed operation receipt.
    Exact replay returns that receipt without another carrier request. The
    carrier resolver retains ownership of fee verification and accepted caps.
    """
    from orders.models import Order
    from management.models import IgClient, IgFollowUpTask
    from management.services.ig_assisted_checkout import validate_assisted_order
    from management.services.ig_assisted_partial_shipping import resolve_assisted_shipping_review, AssistedShippingError
    from management.services.ig_commercial_episodes import commercial_episode_client_lock
    actor, now, operation_id = core._actor(getattr(actor, "pk", None)), core._now(now), core._uuid(operation_id)
    with commercial_episode_client_lock(client_id), transaction.atomic():
        actor = core._actor(actor.pk)
        client = IgClient.objects.select_for_update().filter(pk=client_id).first()
        if not client or client.privacy_erasure_started_at or client.hidden_at:
            core._fail("client_unavailable")
        execution = core._models()[2].objects.select_related("order").filter(client_id=client_id, order_id=order_id, scope="partial").first()
        order = Order.objects.select_for_update().filter(pk=order_id).first() if execution else None
        checked = validate_assisted_order(order, execution=execution, now=now) if order else {}
        if checked.get("ready") is not True or checked["execution_id"] != str(execution.pk):
            core._fail("intention_order_changed")
        operation = {"client_id": client_id, "order_id": order_id, "actor_id": actor.pk,
            "operation_id": str(operation_id), "expected_document_ref": expected_document_ref,
            "expected_terms_digest": expected_terms_digest}
        key = "assisted-shipping-review:" + str(operation_id)
        prior = IgFollowUpTask.objects.select_for_update().filter(event_key=key).first()
        if prior:
            receipt = _verified("shipping_review_receipt", (prior.manager_context or {}).get("assisted_shipping_review_receipt"))
            if (not receipt or prior.client_id != client_id or prior.kind != "manager_task" or prior.status != "completed"
                or prior.reason != SHIPPING_REVIEW_REASON or receipt["operation"] != operation):
                core._fail("operation_conflict")
            return {**receipt["result"], "replayed": True}
        try:
            result = resolve_assisted_shipping_review(order_id, actor=actor,
                expected_document_ref=expected_document_ref, expected_terms_digest=expected_terms_digest)
        except AssistedShippingError as exc:
            return {"ready": False, "reason": exc.reason, "message": str(exc), "order_id": order_id}
        sanitized = {"ready": result["ready"], "reason": result["reason"], "order_id": order_id,
            "operation_id": str(operation_id), "expected_document_ref": expected_document_ref,
            "expected_terms_digest": expected_terms_digest}
        IgFollowUpTask.objects.create(client=client, kind="manager_task", reason=SHIPPING_REVIEW_REASON,
            due_at=now, status="completed", trigger="event", event_key=key,
            event_occurred_at=now, policy_started_at=now, event_payload=operation,
            manager_context={"assisted_shipping_review_receipt": _signed("shipping_review_receipt", {"operation": operation, "result": sanitized})})
        return {**sanitized, "replayed": False}


def _offer_dto(task):
    card = card_for_task(task, require_current=False)
    if not card.get("ready"):
        return None
    manifest = card["proof"]
    choice = _verified("partial_choice", (task.manager_context or {}).get(CHOICE_KEY))
    from management.services.ig_assisted_partial_delivery import capture_delivered_card
    native, _reason = capture_delivered_card(task, require_current=False)
    return {"task_id": task.pk, "operation_id": manifest["operation_id"], "card_digest": manifest["card_digest"],
        "kind": manifest["kind"],
        "request_id": manifest["request_id"], "expected_request_digest": manifest["request_digest"],
        "decision_id": manifest["decision_id"], "expected_decision_digest": manifest["decision_digest"],
        "expected_coverage_digest": manifest["coverage_digest"], "expected_terms_digest": manifest["terms_digest"],
        "locale": manifest["locale"], "text": card["text"], "buttons": [button.__dict__ for button in card["buttons"]],
        "scoped_quote": {"currency": "UAH", **deepcopy(manifest["quote"])} if manifest.get("quote") else None,
        "shipping_terms": deepcopy((manifest.get("quote") or {}).get("shipping_terms")),
        "expires_at": manifest["expires_at"], "delivery": {"status": task.status if task.status != "sent" or native else "unverified",
            "receipt_verified": native is not None, "native_mid": task.provider_message_id if native else "",
            "sent_at": task.sent_at.isoformat() if native and task.sent_at else None},
        "choice": {"action": choice["action"], "source_id": choice["source_id"], "choice_digest": core._digest(choice)} if choice else None}


def _orders_dto(coverage, *, actor, now):
    from orders.models import Order
    from management.models import IgFollowUpTask
    from management.services.ig_assisted_checkout import validate_assisted_order
    from management.services.ig_assisted_partial_shipping import assisted_shipping_contract, AssistedShippingError
    actual = {order.pk: order for order in Order.objects.filter(
        pk__in=[row["order_id"] for row in coverage.get("orders", [])]).prefetch_related("items")}
    receipts = {}
    for task in IgFollowUpTask.objects.filter(client_id=coverage["client_id"], reason=SHIPPING_REVIEW_REASON,
            status="completed").order_by("-pk")[:40]:
        body = _verified("shipping_review_receipt", (task.manager_context or {}).get("assisted_shipping_review_receipt"))
        if (body and body["operation"]["actor_id"] == actor.pk
            and body["operation"]["order_id"] in actual and body["operation"]["order_id"] not in receipts):
            receipts[body["operation"]["order_id"]] = body["result"]
    result = []
    for reference in coverage.get("orders", []):
        order = actual.get(reference["order_id"])
        checked = validate_assisted_order(order, now=now) if order else {}
        if not checked.get("ready") or checked["execution_id"] != reference["execution_id"]:
            core._fail("intention_order_changed")
        row = {**deepcopy(reference), "order_number": order.order_number,
            "lines": [{"title": item.title, "quantity": item.qty} for item in order.items.all()],
            "payment_status_label": order.get_payment_status_display(),
            "shipping_status_label": order.shipment_status or ("Статус перевізника ще не отримано" if order.tracking_number else "ТТН ще не створено")}
        try:
            contract = assisted_shipping_contract(order, now=now)
            if contract:
                row.update({key: contract[key] for key in ("payer", "customer_maximum", "currency")})
                review = (order.payment_payload or {}).get("ig_assisted_shipping_review")
                if isinstance(review, dict) and review.get("state") in {"requires_review", "resolved"}:
                    row["shipping_review"] = {"state": review["state"], "reason": review.get("reason", ""),
                        "reason_label": str(AssistedShippingError(review.get("reason", ""))) if review["state"] == "requires_review" else "",
                        "tracking_number": order.tracking_number or "",
                        "can_resolve": review["state"] == "requires_review" and bool(order.tracking_number),
                        "order_id": order.pk, "expected_document_ref": str(order.nova_poshta_document_ref or ""),
                        "expected_terms_digest": contract["terms_digest"]}
                    if order.pk in receipts:
                        row["shipping_review"]["operation_receipt"] = deepcopy(receipts[order.pk])
        except AssistedShippingError:
            row["shipping_status_label"] = "Потребує звірки умов доставки"
        result.append(row)
    return result


def _intention_lines(request, current, coverage, *, now):
    history = _scopes().original_intention_history(request, now=now)
    if history.get("ready") is not True:
        core._fail(history.get("reason") or "intention_history_unverified")
    root, original_decision = _rows(request.client_id, history["root_request_id"])
    facts = {row["line_id"]: row for row in original_decision.envelope["snapshot"]["items"]} if original_decision else {}
    current_ids = {row["current_line_id"]: row["line_id"] for row in _scopes().request_line_map(request)}
    facts.update({current_ids[item["line_id"]]: item for item in current["editable_items"]})
    titles = {row["line_id"]: row.get("draft_title", "") for row in root.envelope["snapshot"]["lines"]}
    settled, inflight = set(coverage["settled_line_ids"]), set(coverage["inflight_line_ids"])
    recipients = list(dict.fromkeys(row["recipient_id"] for row in history["original_lines"] if row["recipient_id"] != "self"))
    lines = []
    for original in history["original_lines"]:
        identity, choices = original["line_id"], original["source_choices"]
        fact = facts.get(identity, {})
        lines.append({"line_id": identity, "recipient_id": original["recipient_id"],
            "recipient_label": "Клієнт" if original["recipient_id"] == "self" else "Одержувач " + str(recipients.index(original["recipient_id"]) + 1),
            "title": fact.get("title") or titles[identity], "color": fact.get("color") or choices.get("color", ""),
            "size": fact.get("size") or choices.get("size", ""), "quantity": original["quantity"],
            "preparation": fact.get("preparation"), "dispatch": fact.get("dispatch"),
            "disposition": "settled" if identity in settled else "inflight" if identity in inflight else
                "ready" if fact.get("availability") == "ready" else "deferred"})
    return lines


def partial_intention_projection(client_id, *, actor, request_id=None, now=None):
    from management.models import IgClient
    from management.services.ig_commercial_episodes import commercial_episode_client_lock
    actor = core._actor(getattr(actor, "pk", None))
    with commercial_episode_client_lock(client_id), transaction.atomic():
        client = IgClient.objects.select_for_update().filter(pk=client_id).first()
        if not client or client.privacy_erasure_started_at or client.hidden_at:
            core._fail("client_unavailable")
        return _partial_intention_projection(client_id, actor=actor, request_id=request_id, now=now)


def _partial_intention_projection(client_id, *, actor, request_id=None, now=None):
    from management.models import IgFollowUpTask
    now, actor = core._now(now), core._actor(getattr(actor, "pk", None))
    base = {"ready": False, "reason": "request_missing", "reason_label": reason_label("request_missing"), "lines": [],
        "coverage": {}, "orders": [], "scoped_quote": None, "shipping_terms": None, "offer": None, "continuation_offer": None, "offer_history": [],
        "can_approve_terms": False, "can_issue": False, "can_prepare_selected": False, "can_resume": False, "can_prepare_remainder": False}
    try:
        current = core.operator_context(client_id, actor=actor, request_id=request_id, now=now)
        if not current.get("request"):
            return base
        request, decision = _rows(client_id, current["request"]["id"])
        coverage = _coverage(request, now=now)
        quote = _scope_quote(decision)
        lines = _intention_lines(request, current, coverage, now=now)
        base.update(ready=True, request_id=str(request.pk), request_digest=request.envelope_hmac,
            decision_id=str(decision.pk) if decision else None, decision_digest=decision.envelope_hmac if decision else "",
            intention_id=str(request.intention_id), coverage=coverage, coverage_digest=coverage["coverage_digest"], orders=_orders_dto(coverage, actor=actor, now=now),
            lines=lines, scoped_quote={"currency": "UAH", **quote} if quote else None,
            shipping_terms=(quote or {}).get("shipping_terms"), terms_digest=(quote or {}).get("terms_digest", ""),
            can_approve_terms=current.get("can_approve") is True and bool(quote))
        tasks = list(IgFollowUpTask.objects.filter(client_id=client_id, reason__in=[PARTIAL_REASON, CONTINUATION_REASON]).order_by("-pk")[:40])
        for task in tasks:
            manifest = _verified("partial_card", (task.manager_context or {}).get(CARD_KEY))
            if not manifest or manifest["intention_id"] != str(request.intention_id):
                continue
            key = "offer" if manifest["kind"] == "partial" else "continuation_offer"
            dto = _offer_dto(task)
            matches = manifest["request_id"] == str(request.pk)
            if key == "offer":
                matches = matches and decision is not None and manifest["decision_id"] == str(decision.pk) and manifest["terms_digest"] == (quote or {}).get("terms_digest")
            else:
                matches = matches and manifest["coverage_digest"] == coverage["coverage_digest"]
            if dto:
                dto["historical"] = manifest["coverage_digest"] != coverage["coverage_digest"]
            if matches and base[key] is None:
                base[key] = dto
            elif dto:
                base["offer_history"].append(dto)
        try:
            _cap, _quote, _cov = _fresh_partial(request, decision, actor=actor, now=now)
            base.update(can_issue=not base["offer"] or base["offer"]["delivery"]["status"] in {"cancelled", "skipped"}, reason="choice_required")
            selection = latest_verified_selection(client_id, actor=actor, request_id=request.pk,
                decision_id=decision.pk, now=now)
            if selection:
                base.update(can_prepare_selected=True, can_prepare_remainder=bool(request.envelope["snapshot"].get("ancestry")), reason="")
        except core.AssistedDecisionError as exc:
            base["reason"] = exc.reason
        if ((base["offer"] or {}).get("choice") or {}).get("action") == "together":
            base.update(can_prepare_selected=False, reason="customer_waiting_together")
        base["can_resume"] = bool(any(row["request_id"] == str(request.pk) and row["state"] == "settled" for row in coverage.get("executions", []))
            and coverage.get("deferred_line_ids") and not coverage.get("inflight_line_ids")
            and not base["continuation_offer"])
        if any(task.status == "ambiguous" and (manifest := _verified("partial_card", (task.manager_context or {}).get(CARD_KEY)))
                and manifest["intention_id"] == str(request.intention_id) for task in tasks):
            base.update(can_issue=False, can_resume=False, can_prepare_selected=False, can_prepare_remainder=False,
                reason="provider_outcome_unknown_no_retry")
        base["reason_label"] = reason_label(base["reason"])
        return base
    except core.AssistedDecisionError as exc:
        return {**base, "reason": exc.reason, "reason_label": reason_label(exc.reason)}
