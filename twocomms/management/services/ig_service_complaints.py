"""Customer-reported service concerns; never order, payment or refund authority.

Readers perform bounded SELECTs only. Durable ownership belongs to the revision
manager-case writer; a neutral later message or a manager explanation cannot
silently resolve that exact source's service debt.
"""
from __future__ import annotations

from datetime import timedelta
import re

from django.utils import timezone

SCHEMA = "service-complaint.v1"
TASK_REASON = "revision_case:service_complaint"
HOLD_REASON = "service_complaint_open"
MAX_TEXT = 8000
MAX_SOURCES = 32
MAX_TASKS = 20
MAX_RECENT_SOURCES = 32
FRESH_SOURCE_MAX_AGE = timedelta(hours=24)

_DELIVERY = re.compile(r"\b(?:достав\w*|пересил\w*|shipping|delivery|postage|courier)\b", re.I)
_QUOTED_FEE = re.compile(r"(?:ви|вы|you).{0,35}(?:казал\w*|сказал\w*|писал\w*|написал\w*|обіця\w*|обеща\w*|said|quoted|promised|told)|(?:казал\w*|сказал\w*|писал\w*|обіця\w*|обеща\w*|quoted|promised).{0,30}\d", re.I)
_REPORTED_DELIVERY_COST = re.compile(r"(?:достав\w*|shipping|delivery|postage).{0,35}(?:коштувал\w*|коштує|стоил\w*|стоила|cost|was|would\s+be|expected).{0,12}\d", re.I)
_CHARGED = re.compile(r"\b(?:вз[яі]л\w*|взя\w*|заплати\w*|сплати\w*|оплати\w*|стягнул\w*|виявил\w*|оказал\w*|charged|paid)\b", re.I)
_NUMBER = re.compile(r"(?<!\w)\d{1,5}(?:[.,]\d{1,2})?(?!\d)")
_FEE_DISSATISFACTION = re.compile(r"переплат\w*|дорожч\w*|дороже|завищ\w*|завыш\w*|overcharg\w*|more\s+than|unexpected\s+(?:fee|charge|cost)|не\s+(?:та|так[аийе]+)\s+(?:цін\w*|цен\w*|варт\w*)", re.I)
_DEFECT = re.compile(r"\b(?:брак\w*|дефект\w*|пошкод\w*|поврежд\w*|порван\w*|дірк\w*|дырк\w*|damaged|defective|broken|torn|hole)\b", re.I)
_OWN_ITEM = re.compile(r"\b(?:отрима\w*|получи\w*|прийш\w*|приш\w*|arrived|received|товар\w*|замовлен\w*|заказ\w*|посилк\w*|посылк\w*|order|parcel|package|футболк\w*|худі|худи|shirt|hoodie|item)\b", re.I)
_MISSING = re.compile(r"(?:посилк\w*|посылк\w*|замовлен\w*|заказ\w*|товар\w*).{0,20}не\s+(?:прийш\w*|приш\w*|достав\w*|отрим\w*|получ\w*)|не\s+(?:отрим\w*|получ\w*).{0,20}(?:посилк\w*|посылк\w*|замовлен\w*|заказ\w*|товар\w*)|(?:order|parcel|package).{0,25}(?:hasn't|has\s+not|didn't|did\s+not|never).{0,15}(?:arriv\w*|come|deliver\w*)|(?:haven't|have\s+not|didn't|did\s+not)\s+receiv\w*.{0,15}(?:order|parcel|package)", re.I)
_DISSATISFACTION = re.compile(r"\b(?:я\s+)?(?:незадоволен\w*|недоволен\w*|недовольн\w*|розчарован\w*|разочарован\w*)\b|(?:i(?:['’]m|\s+am)\s+(?:disappointed|unhappy|dissatisfied))|(?:перше|первое|first).{0,15}(?:враженн\w*|впечатлен\w*|impression).{0,25}(?:негатив\w*|поган\w*|плох\w*|не\s+(?:дуже\s+|очень\s+)?(?:гарн\w*|хорош\w*)|bad|negative|not\s+(?:very\s+)?good)", re.I)
_WRONG_ITEM = re.compile(r"(?:отрима\w*|получи\w*|прийш\w*|приш\w*).{0,25}(?:не\s+т[аоеий]+|інш\w*|друг\w*)|(?:received|arrived|sent\s+me).{0,25}(?:wrong|different)\s+(?:item|size|shirt|hoodie|order)|(?:розмір|размер).{0,12}не\s+(?:підійш\w*|подош\w*)", re.I)
_NOT_COMPLAINT = re.compile(r"(?:це|это)\s+не\s+(?:скарга|жалоба|претензія|претензия)|не\s+(?:скарж\w*|жалу\w*)|(?:i(?:['’]m|\s+am)\s+)?not\s+complaining|(?:this\s+is\s+)?not\s+a\s+complaint", re.I)
_NO_ISSUES = re.compile(r"(?:немає|нема|нет)\s+(?:жодних\s+|никаких\s+)?(?:проблем|скарг|жалоб|претенз)|(?:no|without)\s+(?:problems?|issues?|complaints?|defects?|damage)|(?:не\s+(?:пошкоджен\w*|поврежден\w*|бракован\w*))|(?:not\s+(?:damaged|defective|broken|disappointed|unhappy))", re.I)
_HYPOTHETICAL = re.compile(r"\b(?:якщо|если|if|умови|условия|політика|политика|policy)\b", re.I)
_UNCERTAIN = re.compile(r"^(?:is|are|could|can|will|would|should|чи|ли)\b|^(?:це|это)\s+(?:брак|дефект)\s*$", re.I)
_ACTUAL_PAST = re.compile(r"\b(?:arrived|received|delivered|charged|paid|отрима(?:в|ла|ли)|получи(?:л|ла|ли)|прийш(?:ов|ла|ло|ли)|приш(?:ел|ёл|ла|ло|ли)|заплати\w*|сплати\w*|взяли)\b", re.I)
_ADDRESSED_SERVICE = re.compile(r"^(?:(?:can|could|would)\s+you\s+(?:please\s+)?(?:check|help|look|investigate)|чи\s+можете\s+(?:перевірити|допомогти)|можете\s+(?:проверить|помочь))\b", re.I)
_SERVICE_TEAM = re.compile(r"\b(?:обслуговуван\w*|обслуживан\w*|сервіс\w*|сервис\w*|your\s+(?:team|support|service)|twocomms)\b", re.I)
_UNRELATED_CONTEXT = re.compile(r"\b(?:погод\w*|weather|ваканс\w*|job|employment|interview|співбесід\w*|собеседован\w*)\b", re.I)
_PRE_SALE_QUESTION = re.compile(r"\b(?:do|can|could|would)\s+you\s+(?:sell|offer|have)\b|(?:ви|вы)\s+(?:продає\w*|прода[её]\w*|має\w*).{0,30}(?:брак\w*|дефект\w*|пошкод\w*|поврежд\w*)", re.I)
_REPORTED = re.compile(r"^(?:(?:мій\s+|моя\s+|мой\s+)?(?:друг|подруга)|(?:my\s+|a\s+|our\s+)?friend|he\s+said|she\s+said|quote|цитата|переслан\w*)\b", re.I)


def _reported_fee_discrepancy(value):
    """Compare customer-reported numbers solely to recognize their concern."""
    quoted = _QUOTED_FEE.search(value)
    delivery_cost = _REPORTED_DELIVERY_COST.search(value)
    if not quoted and not delivery_cost:
        return False
    anchor = quoted.start() if quoted else delivery_cost.start()
    expected = _NUMBER.search(value, anchor, min(len(value), anchor + 100))
    charged = _CHARGED.search(value, expected.end() if expected else anchor)
    actual = _NUMBER.search(value, charged.end(), min(len(value), charged.end() + 60)) if charged else None
    if expected is None or actual is None:
        return False
    low = float(expected.group().replace(",", "."))
    amount = float(actual.group().replace(",", "."))
    # A quoted range includes its upper endpoint; this is not an overcharge.
    tail = value[expected.end():actual.start()]
    range_match = re.match(r"\s*(?:[-–—]|до|to)\s*(\d{1,5}(?:[.,]\d{1,2})?)", tail)
    high = float(range_match.group(1).replace(",", ".")) if range_match else low
    return not min(low, high) <= amount <= max(low, high)


def classify_service_complaint(text):
    """Recognize own actual concerns, abstaining on ambiguous policy questions."""
    if not isinstance(text, str) or not text.strip() or len(text) > MAX_TEXT:
        return {}
    value = text.casefold().replace("’", "'")
    value = re.sub(r"(?m)^\s*>[^\n]*(?:\n|$)", " ", value)
    value = re.sub(r'''«[^»]*»|“[^”]*”|"[^"\n]*"|(?<!\w)'(?:[^'\n]|(?<=\w)'(?=\w))*'(?!\w)''', " ", value)
    value = re.sub(r"https?://\S+", " ", value).strip()
    if not value or value.startswith(('"', "«", "“", ">")):
        return {}
    # Split punctuation only outside decimal numbers. Reported/hypothetical
    # clauses cannot erase a separate, actual own concern in the same message.
    clauses = [part.strip() for part in re.split(r"(?<!\d)[.,]|[.,](?!\d)|[!?;\n]+|\b(?:але|но|but)\b", value) if part.strip()]
    own_clauses = [part for part in clauses if not _HYPOTHETICAL.search(part)
        and not _NO_ISSUES.search(part) and not _NOT_COMPLAINT.search(part)
        and not _PRE_SALE_QUESTION.search(part)
        and not _REPORTED.match(part) and not _UNRELATED_CONTEXT.search(part)]
    own_past = any(_ACTUAL_PAST.search(part) for part in own_clauses)
    actual = " ".join(part for part in own_clauses if not _UNCERTAIN.search(part)
        or _ACTUAL_PAST.search(part) or (_ADDRESSED_SERVICE.search(part) and own_past))
    if not actual:
        return {}
    fee = bool(_DELIVERY.search(actual) and (
        _reported_fee_discrepancy(actual) or _FEE_DISSATISFACTION.search(actual)))
    service = bool(_MISSING.search(actual) or (_DISSATISFACTION.search(actual)
        and (_OWN_ITEM.search(actual) or _DELIVERY.search(actual) or _SERVICE_TEAM.search(actual)))
        or (_OWN_ITEM.search(actual) and (_DEFECT.search(actual) or _WRONG_ITEM.search(actual))))
    if not fee and not service:
        return {}
    return {"schema": SCHEMA, "kind": "delivery_fee_dispute" if fee else "service_complaint",
            "customer_reported": True, "monetary_authority": False}


def _scope(client_id, *, using="default"):
    from management.models import IgClient, IgFunnelResetAudit, InstagramBotSettings
    from management.services.instagram_bot import ingress_provider_namespace
    if type(client_id) is not int or client_id <= 0:
        return None
    client = IgClient.objects.using(using).filter(pk=client_id, hidden_at__isnull=True,
        privacy_erasure_started_at__isnull=True, is_blocked=False).first()
    settings = InstagramBotSettings.objects.using(using).order_by("pk").first()
    namespace = ingress_provider_namespace(settings) if settings else ""
    if client is None or not namespace:
        return None
    boundary = IgFunnelResetAudit.objects.using(using).filter(client_id=client.pk).order_by("-pk").values_list("reset_after_message_id", flat=True).first()
    return client, namespace, int(boundary or 0) + 1


def _own_source(row, client, namespace, floor, *, now):
    return bool(row.client_id == client.pk and row.role == "user" and row.sender_id == client.igsid
        and row.source in {"webhook", "poll"} and row.mid and row.provider_namespace == namespace
        and row.pk >= floor and row.status != "failed" and row.provider_created_at is not None
        and not timezone.is_naive(row.provider_created_at) and row.provider_created_at <= now)


def _revision_proof(revision, scope, rows, *, now):
    """Pure validation over one already-read scope and bounded source graph."""
    from management.services.ig_turn_revisions import _digest, _source_payload
    try:
        snapshots = (revision.bundle_snapshot or {}).get("sources") or []
        if (scope is None or not 1 <= len(snapshots) <= MAX_SOURCES
                or not revision.snapshot_digest or _digest(revision.bundle_snapshot) != revision.snapshot_digest
                or getattr(revision, "erasure_started_at_snapshot", None)):
            return {}
        client, namespace, floor = scope
        if revision.client_id != client.pk:
            return {}
        if len(rows) != len(snapshots) or len(rows) != revision.source_count:
            return {}
        refs, kind = [], "service_complaint"
        for row, captured in zip(rows, snapshots, strict=True):
            message = row.message
            if (not _own_source(message, client, namespace, floor, now=now)
                    or captured.get("revision_source_id") != row.pk
                    or captured.get("message_id") != row.message_id
                    or captured.get("source_digest") != row.source_digest
                    or captured.get("text") != row.text or captured.get("role") != "user"
                    or captured.get("source_namespace") != namespace
                    or _source_payload(message, previous=row, ordinal=row.ordinal)["source_digest"] != row.source_digest):
                return {}
            complaint = classify_service_complaint(captured.get("text"))
            if complaint:
                refs.append({"message_id": message.pk, "source_digest": row.source_digest})
                if complaint["kind"] == "delivery_fee_dispute":
                    kind = "delivery_fee_dispute"
        if not refs:
            return {}
        return {"schema": SCHEMA, "kind": kind, "customer_reported": True, "monetary_authority": False,
            "client_id": client.pk, "revision_id": revision.pk, "reset_floor": floor,
            "snapshot_digest": revision.snapshot_digest, "source_refs": refs}
    except (AttributeError, TypeError, ValueError):
        return {}


def revision_service_complaint(revision):
    """Validate actual sealed sources; model labels confer no authority."""
    from management.models import IgCustomerTurnRevision
    if not isinstance(revision, IgCustomerTurnRevision):
        return {}
    scope = _scope(revision.client_id, using=getattr(getattr(revision, "_state", None), "db", None) or "default")
    rows = list(revision.sources.select_related("message").order_by("ordinal", "id")[:MAX_SOURCES + 1])
    return _revision_proof(revision, scope, rows, now=timezone.now())


def _task_capture(task):
    context = task.manager_context if isinstance(task.manager_context, dict) else {}
    captured = context.get("service_complaint")
    if (not isinstance(captured, dict) or captured.get("client_id") != task.client_id
            or type(captured.get("revision_id")) is not int or captured["revision_id"] <= 0):
        return {}
    return captured


def _receipt_owns_task(task, revision, notifications):
    """Mutable task context alone cannot release someone else's source debt."""
    receipt = (revision.action_receipts or {}).get("manager_handoff") or {}
    if not isinstance(receipt, dict):
        return False
    from management.services.ig_turn_revisions import _digest
    try:
        proposal_valid = bool(revision.generation_proposal_digest
            and _digest(revision.generation_proposal) == revision.generation_proposal_digest)
    except (TypeError, ValueError):
        return False
    notification = notifications.get(receipt.get("notification_id"))
    payload = notification.payload if notification and isinstance(notification.payload, dict) else {}
    return bool(task.event_key == f"ig-revision-case:{task.client_id}:{revision.pk}:service_complaint_review"
        and receipt.get("task_id") == task.pk and receipt.get("case_kind") == "service_complaint_review"
        and receipt.get("snapshot_digest") == revision.snapshot_digest
        and proposal_valid
        and receipt.get("generation_proposal_digest") == revision.generation_proposal_digest
        and notification and notification.client_id == task.client_id
        and notification.event_type == "escalation" and notification.dedupe_key == f"ig-revision-case:{task.pk}"
        and payload.get("revision_id") == revision.pk and payload.get("manager_task_id") == task.pk
        and payload.get("case_kind") == "service_complaint_review")


def promotion_service_hold_reason(client, *, now=None, using=None):
    """Hold for unresolved own service debt; thanks alone never resolves it.

    At most nine SELECTs, independent of the number of selected tasks. A full
    recent-source window conservatively holds because older unowned concerns
    may lie beyond the retained span.
    """
    from management.models import (IgBotNotification, IgCustomerTurnRevision,
        IgFollowUpTask, IgTurnRevisionSource, InstagramBotMessage)
    from django.db.models import Q
    db_alias = using or getattr(getattr(client, "_state", None), "db", None) or "default"
    scope = _scope(getattr(client, "pk", client), using=db_alias)
    if scope is None:
        return ""
    client, namespace, floor = scope
    now = now or timezone.now()
    if timezone.is_naive(now):
        return HOLD_REASON
    tasks = IgFollowUpTask.objects.using(db_alias).filter(client_id=client.pk, kind="manager_task", reason=TASK_REASON)
    active = list(tasks.exclude(status__in=("completed", "cancelled")).order_by("-pk")[:MAX_TASKS + 1])
    terminal = list(tasks.filter(status__in=("completed", "cancelled")).order_by("-pk")[:MAX_TASKS + 1])
    selected = [*active, *terminal]
    revision_ids = {_task_capture(task).get("revision_id") for task in selected} - {None}
    task_ids = [task.pk for task in selected]
    revisions = {row.pk: row for row in IgCustomerTurnRevision.objects.using(db_alias).filter(
        Q(pk__in=revision_ids) | Q(action_receipts__manager_handoff__task_id__in=task_ids),
        client_id=client.pk)[:len(selected) * 2]} if selected else {}
    graphs = {pk: [] for pk in revisions}
    # The revision producer physically bounds each graph to MAX_SOURCES.
    # Read one bounded aggregate rather than issuing one query per task.
    if revisions:
        for row in IgTurnRevisionSource.objects.using(db_alias).filter(revision_id__in=revisions).select_related("message").order_by("revision_id", "ordinal", "id")[:len(revisions) * (MAX_SOURCES + 1)]:
            graphs[row.revision_id].append(row)
    receipts = {pk: (revision.action_receipts or {}).get("manager_handoff") or {} for pk, revision in revisions.items()}
    receipts = {pk: value for pk, value in receipts.items() if isinstance(value, dict)}
    notification_ids = {receipt.get("notification_id") for receipt in receipts.values()}
    notification_ids = {pk for pk in notification_ids if type(pk) is int and pk > 0}
    notifications = {row.pk: row for row in IgBotNotification.objects.using(db_alias).filter(
        pk__in=notification_ids, client_id=client.pk)} if notification_ids else {}
    proofs = {pk: _revision_proof(revision, scope, graphs[pk], now=now) for pk, revision in revisions.items()}
    owners = {receipt.get("task_id"): revisions[pk] for pk, receipt in receipts.items()}
    unresolved = False
    for task in active:
        revision = owners.get(task.pk)
        rows = graphs.get(getattr(revision, "pk", None), [])
        # An unchanged sealed source graph entirely before the current reset
        # is positively obsolete. A malformed/current graph stays held.
        if (revision and _receipt_owns_task(task, revision, notifications)
                and rows and all(row.message_id < floor for row in rows)):
            old = _revision_proof(revision, (client, namespace, 1), rows, now=now)
            if old:
                continue
        unresolved = True
    if unresolved or len(active) > MAX_TASKS:
        return HOLD_REASON
    resolved_ids = set()
    for task in terminal:
        captured = _task_capture(task)
        revision = revisions.get(captured.get("revision_id"))
        proof = proofs.get(getattr(revision, "pk", None))
        if proof and proof == captured and _receipt_owns_task(task, revision, notifications):
            resolved_ids.update(ref["message_id"] for ref in proof["source_refs"])
    recent = list(InstagramBotMessage.objects.using(db_alias).filter(client_id=client.pk, sender_id=client.igsid,
        role="user", source__in=("webhook", "poll"), provider_namespace=namespace,
        pk__gte=floor, provider_created_at__gte=now - FRESH_SOURCE_MAX_AGE,
        provider_created_at__lte=now).exclude(mid__isnull=True).exclude(mid="").exclude(status="failed").order_by("-pk")[:MAX_RECENT_SOURCES + 1])
    if len(recent) > MAX_RECENT_SOURCES:
        return HOLD_REASON
    for row in recent:
        if row.pk not in resolved_ids and _own_source(row, client, namespace, floor, now=now) and classify_service_complaint(row.text):
            return HOLD_REASON
    return ""
