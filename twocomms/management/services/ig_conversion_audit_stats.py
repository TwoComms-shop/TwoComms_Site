"""Bounded episode observations; never payment authority or causal uplift.

Existing immutable analysis snapshots are the structured log. This read model
does not write jobs, memory, customer state or provider requests. Counts apply
to the explicitly selected episode page, not a representative sample/census.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace
from django.db.models import BigIntegerField, Count, Exists, F, OuterRef, Q, Subquery, Value
from django.db.models.functions import Coalesce
from django.utils import timezone

from management.models import (
    IgCommercialEpisode, IgCommercialEpisodeEvent, IgConversationAnalysisSnapshot,
    IgDeal, IgFunnelDropOff, IgFunnelResetAudit, IgPaymentConfirmationReview,
    IgOrderAssignment, IgOrderAttribution, IgRevisionDeliveryEffect, IgTurnRevisionSource,
    InstagramBotMessage, InstagramBotSettings,
)
from management.services.bot_payment_truth import (
    CONFIRMED_ORDER_PAYMENT_STATUSES, manager_confirmed_review_q, verified_payment_q,
)


SCHEMA_VERSION = "ig-conversion-audit-stats.v1"
MAX_DAYS = 31
MAX_PAGE = 50
QUERY_CAP = 14
CAUSE_LABELS = {
    "price": "Ціна", "relevance": "Відповідність відповіді", "product": "Товар",
    "size_fit": "Розмір або посадка", "delivery_deadline": "Доставка або термін",
    "payment_friction": "Труднощі оплати", "response_delay": "Очікування відповіді",
    "handoff": "Передача менеджеру", "unknown": "Причину не встановлено",
}
COMMERCIAL_INTERACTIONS = frozenset({
    "product_interest", "size_fit_question", "custom_print", "price_objection",
    "high_intent", "payment_pending",
})


def intent_assessment(snapshot):
    commercial = bool(snapshot and snapshot.interaction_type in COMMERCIAL_INTERACTIONS)
    return {"kind": "ai_intent_assessment", "calibration_status": "uncalibrated",
        "value": str(snapshot.purchase_probability) if commercial else None,
        "is_conversion_forecast": False}


def conversion_audit_payload(snapshot, client, *, fresh, state=None, load=True):
    """Use the existing protected freshness envelope; stale data stays absent."""
    raw = getattr(snapshot, "conversion_audit", None) if snapshot else None
    status = state or ("current" if fresh else "stale")
    data = {}
    if not snapshot or not raw:
        status = "missing"
    elif fresh and not load:
        status = "not_loaded"
    elif fresh:
        from management.services.ig_analysis_reply_review import public_conversion_audit

        data = public_conversion_audit(snapshot, client, fresh=fresh)
        if not data:
            status = "unobservable"
        else:
            data = {key: data[key] for key in ("schema_version", "motivation", "readiness_evidence",
                "nonconversion_reasons", "_response_review") if key in data}
    return {"state": status, "fresh": bool(fresh and data), "source_fresh": bool(fresh),
        "episode_id": getattr(snapshot, "commercial_episode_id", None),
        "snapshot_id": getattr(snapshot, "pk", None),
        "watermark_message_id": int(getattr(snapshot, "last_analyzed_message_id", 0) or 0),
        "analyzed_at": snapshot.analyzed_at.isoformat() if snapshot else None,
        "data": data, "intent_assessment": intent_assessment(snapshot if fresh and data else None)}


def _window(since, until, now):
    for value in (since, until, now):
        if value is not None and (not isinstance(value, datetime) or timezone.is_naive(value)):
            raise ValueError("window_invalid")
    end = min(until or now, now)
    if since is not None and since >= end:
        raise ValueError("window_invalid")
    start = max(since or end - timedelta(days=MAX_DAYS), end - timedelta(days=MAX_DAYS))
    return start, end, since is None or start != since


def _references(audit):
    result = set()
    for field in ("motivation", "readiness_evidence", "nonconversion_reasons"):
        items = audit.get(field) if isinstance(audit.get(field), list) else []
        for item in items[:4]:
            if not isinstance(item, dict):
                continue
            if field == "readiness_evidence" and type(item.get("message_id")) is int:
                result.add(item["message_id"])
            for name in ("evidence", "counterevidence"):
                refs = item.get(name) if isinstance(item.get(name), list) else []
                for ref in refs[:4]:
                    if isinstance(ref, dict) and type(ref.get("message_id")) is int:
                        result.add(ref["message_id"])
    review = audit.get("_response_review")
    if isinstance(review, dict):
        for row in (review.get("diagnostics") if isinstance(review.get("diagnostics"), list) else [])[:8]:
            if isinstance(row, dict) and isinstance(row.get("source_message_ids"), list):
                result.update(pk for pk in row["source_message_ids"][:32] if type(pk) is int)
    return result


def _owned_message(message, episode, namespace, floor, ceiling):
    from management.services.ig_ingress_source_proof import is_current_customer_source

    return bool(message and message.client_id == episode.client_id and message.role == "user"
        and message.sender_id == episode.client.igsid and message.provider_namespace == namespace
        and floor <= message.pk < ceiling and message.status != "failed"
        and is_current_customer_source(message, namespace=namespace))


def _audit_status(snapshot, episode, messages, namespace, effects):
    if not snapshot or not snapshot.conversion_audit:
        return "missing", {}
    value = snapshot.conversion_audit
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        return "invalid", {}
    scope = value.get("_source_scope")
    if not isinstance(scope, dict):
        return "unobservable", {}
    if any(type(scope.get(key)) is not int for key in ("client_id", "episode_id",
        "watermark_message_id", "reset_floor", "client_permission_epoch")):
        return "unobservable", {}
    digest = scope.get("response_review_digest")
    if not isinstance(digest, str) or len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
        return "unobservable", {}
    watermark = int(snapshot.last_analyzed_message_id or 0)
    floor = max(int(episode.reset_floor), int(episode.opened_watermark_message_id or 0))
    expected = {"client_id": episode.client_id, "source_namespace": namespace,
        "episode_id": episode.pk, "watermark_message_id": watermark,
        "reset_floor": floor, "client_permission_epoch": int(episode.client.reply_permission_epoch)}
    if (not namespace or any(scope.get(key) != value for key, value in expected.items())
        or snapshot.client_id != episode.client_id or snapshot.commercial_episode_id != episode.pk
        or watermark < floor or watermark >= episode.next_floor
        or messages.get(watermark) is None
        or messages[watermark].client_id != episode.client_id
        or messages[watermark].sender_id != episode.client.igsid
        or messages[watermark].role not in {"user", "manager"}
        or messages[watermark].provider_namespace != namespace):
        return "unobservable", {}
    source = messages[watermark]
    if episode.latest_source_event_at is not None and (
        source.provider_created_at or source.created_at, watermark
    ) < (episode.latest_source_event_at, episode.latest_admitted_source_id):
        return "stale", {}
    refs = _references(value)
    if any(not _owned_message(messages.get(pk), episode, namespace, floor, min(watermark + 1, episode.next_floor))
        for pk in refs):
        return "unobservable", {}
    # Reuse the deterministic normalizer at the read boundary. No free-form
    # claim or changed quote may turn a legacy JSON write into a reported loss.
    from management.services.ig_conversion_audit import normalize_conversion_audit

    by_id = {pk: {"role": "user", "text": messages[pk].text or "", "evidence_eligible": True}
        for pk in refs}
    review = _verified_review(snapshot, episode, messages, effects)
    recorded = value.get("_response_review") or {}
    if isinstance(recorded, dict) and recorded.get("coverage") == "sent_receipts_observed":
        diagnostics = recorded.get("diagnostics")
        if not isinstance(diagnostics, list) or any(not isinstance(item, dict)
            or not isinstance(item.get("ref"), str) for item in diagnostics):
            return "unobservable", {}
        expected_refs = {item["ref"] for item in diagnostics}
        if not expected_refs or expected_refs != set(review["diagnostic_refs"]):
            return "unobservable", {}
    normalized = normalize_conversion_audit(value, by_id, response_review_context=review)
    return ("current", normalized) if normalized else ("invalid", {})


def _verified_review(snapshot, episode, messages, effects):
    """Rebuild private diagnostic proof from actual native effects in one batch."""
    value = snapshot.conversion_audit
    scope = value.get("_source_scope") or {}
    review = value.get("_response_review") or {}
    diagnostics = review.get("diagnostics") if isinstance(review, dict) else None
    result = {"schema_version": 1, "client_id": episode.client_id, "episode_id": episode.pk,
        "namespace": scope.get("source_namespace"), "reset_floor": scope.get("reset_floor"),
        "watermark_message_id": snapshot.last_analyzed_message_id,
        "client_permission_epoch": episode.client.reply_permission_epoch,
        "digest": scope.get("response_review_digest"), "coverage": "reply_not_yet_observable",
        "diagnostic_refs": [], "diagnostics": []}
    for item in (diagnostics if isinstance(diagnostics, list) else [])[:8]:
        if not isinstance(item, dict) or type(item.get("effect_id")) is not int:
            continue
        effect = effects.get(item["effect_id"])
        sources = item.get("source_message_ids")
        if (not effect or not isinstance(sources, list) or not 1 <= len(sources) <= 32
            or sources != getattr(effect, "_stats_source_message_ids", None)
            or any(type(pk) is not int or pk not in messages for pk in sources)
            or effect.revision.client_id != episode.client_id
            or effect.revision.turn.episode_id != episode.pk
            or effect.revision.permission_epoch != episode.client.reply_permission_epoch
            or effect.revision.erasure_started_at_snapshot is not None
            or effect.source_message_id not in sources or effect.revision_id != item.get("revision_id")
            or effect.state != "sent" or effect.purpose != "normal_reply" or effect.actor != "bot"
            or effect.recipient_igsid != episode.client.igsid
            or not effect.provider_http_status or not 200 <= effect.provider_http_status < 300
            or not getattr(effect, "_stats_whole_proof", False)
            or effect.provider_namespace != scope.get("source_namespace")
            or not effect.provider_message_id or item.get("ref") != f"revision_effect:{effect.pk}"
            or not effect.terminal_at or effect.terminal_at.isoformat() != item.get("sent_at")):
            continue
        payload = effect.payload if isinstance(effect.payload, dict) else {}
        message = payload.get("message") if isinstance(payload.get("message"), dict) else {}
        latest_source = max((messages[pk] for pk in sources),
            key=lambda row: (row.provider_created_at or row.created_at, row.pk))
        source_time = latest_source.provider_created_at or latest_source.created_at
        elapsed = (effect.terminal_at - source_time).total_seconds()
        result["diagnostics"].append({"ref": item["ref"], "kind": "sent_reply", "state": "sent",
            "delivery_state": "provider_accepted", "source_message_ids": sources,
            "revision_id": effect.revision_id, "effect_id": effect.pk,
            "provider_message_id": effect.provider_message_id,
            "sent_at": effect.terminal_at.isoformat(), "source_event_at": source_time.isoformat(),
            "time_basis": "provider_event_to_provider_accept" if latest_source.provider_created_at else "local_ingest_to_provider_accept",
            "response_seconds": round(elapsed, 3) if elapsed >= 0 else None,
            "media_observed": isinstance(message.get("attachment"), dict),
            "media_kind": str(effect.kind)[:32], "text": str(message.get("text") or "")[:1500]})
        result["diagnostic_refs"].append(item["ref"])
    if result["diagnostics"]:
        result["coverage"] = "sent_receipts_observed"
    return result


def _receipt_latencies(snapshot, episode, messages, effects):
    """One observed first receipt per native revision; no nearest-send guesses."""
    latencies = {}
    for item in _verified_review(snapshot, episode, messages, effects)["diagnostics"]:
        effect = effects[item["effect_id"]]
        sources = item["source_message_ids"]
        source_time = max(messages[pk].provider_created_at or messages[pk].created_at for pk in sources)
        seconds = (effect.terminal_at - source_time).total_seconds()
        if seconds >= 0:
            latencies[effect.revision_id] = min(seconds, latencies.get(effect.revision_id, seconds))
    return latencies


def build_conversion_audit_stats(*, since=None, until=None, before_episode_id=None,
                                limit=MAX_PAGE, observed_at=None):
    now = observed_at or timezone.now()
    start, end, bounded = _window(since, until, now)
    if type(limit) is not int or not 1 <= limit <= MAX_PAGE:
        raise ValueError("page_limit_invalid")
    if before_episode_id is not None and (type(before_episode_id) is not int or not 0 < before_episode_id < 2**63):
        raise ValueError("cursor_invalid")
    settings_row = InstagramBotSettings.objects.filter(pk=1).first()
    from management.services.instagram_bot import ingress_provider_namespace

    namespace = ingress_provider_namespace(settings_row) if settings_row else ""
    inquiries = IgCommercialEpisodeEvent.objects.filter(episode_id=OuterRef("pk"),
        event_type="commercial_inquiry_observed",
        evidence__commercial_inquiry_v1__classification__in=("retail", "custom"))
    cohort = IgCommercialEpisode.objects.filter(opened_at__gte=start, opened_at__lt=end,
        client__pk__isnull=False, client__privacy_erasure_started_at__isnull=True).filter(Exists(inquiries))
    population = cohort.count()
    snapshots = IgConversationAnalysisSnapshot.objects.filter(commercial_episode_id=OuterRef("pk"),
        client_id=OuterRef("client_id")).order_by("-pk")
    next_episode = IgCommercialEpisode.objects.filter(client_id=OuterRef("client_id"),
        sequence__gt=OuterRef("sequence"), opened_watermark_message_id__gt=0).order_by("sequence", "pk")
    reset = IgFunnelResetAudit.objects.filter(client_id=OuterRef("client_id")).order_by("-pk")
    page = cohort.annotate(audit_snapshot_id=Subquery(snapshots.values("pk")[:1]),
        inquiry_event_id=Subquery(inquiries.order_by("pk").values("pk")[:1]),
        reset_floor=Coalesce(Subquery(reset.values("reset_after_message_id")[:1]),
            Value(0), output_field=BigIntegerField()) + Value(1),
        next_floor=Coalesce(Subquery(next_episode.values("opened_watermark_message_id")[:1]),
            Value(2**63 - 1), output_field=BigIntegerField()))
    from management.services.bot_conversation_analysis import _analysis_admitted_sources

    # Reuse the analyzer's customer/manager admission policy with correlated
    # client fields. Provider event order, rather than insertion PK, determines
    # freshness when a delayed webhook arrives after the analyzed event.
    client_scope = SimpleNamespace(pk=OuterRef("client_id"), igsid=OuterRef("client__igsid"))
    latest_source = _analysis_admitted_sources(client_scope, namespace=namespace).filter(
        pk__gte=OuterRef("reset_floor"), pk__lt=OuterRef("next_floor"),
    ).filter(pk__gte=OuterRef("opened_watermark_message_id")).annotate(
        event_at=Coalesce("provider_created_at", "created_at"),
    ).order_by("-event_at", "-pk")
    page = page.annotate(
        latest_admitted_source_id=Subquery(latest_source.values("pk")[:1]),
        latest_source_event_at=Subquery(latest_source.values("event_at")[:1]),
    )
    if before_episode_id is not None:
        page = page.filter(pk__lt=before_episode_id)
    episodes = list(page.select_related("client", "intended_order").order_by("-pk")[:limit + 1])
    has_more, episodes = len(episodes) > limit, episodes[:limit]
    episode_ids = [episode.pk for episode in episodes]
    audits = {row.pk: row for row in IgConversationAnalysisSnapshot.objects.filter(
        pk__in=[episode.audit_snapshot_id for episode in episodes if episode.audit_snapshot_id])}
    source_ids = set()
    for snapshot in audits.values():
        source_ids.add(snapshot.last_analyzed_message_id)
        if isinstance(snapshot.conversion_audit, dict):
            source_ids.update(_references(snapshot.conversion_audit))
    messages = {row.pk: row for row in InstagramBotMessage.objects.filter(pk__in=source_ids).select_related("client")}
    effect_ids = set()
    for snapshot in audits.values():
        value = snapshot.conversion_audit
        review = value.get("_response_review") if isinstance(value, dict) else None
        for row in (review.get("diagnostics") if isinstance(review, dict) and isinstance(review.get("diagnostics"), list) else [])[:8]:
            if isinstance(row, dict) and type(row.get("effect_id")) is int:
                effect_ids.add(row["effect_id"])
    effect_counts = IgRevisionDeliveryEffect.objects.filter(revision_id=OuterRef("revision_id")).order_by(
        ).values("revision_id").annotate(total=Count("pk")).values("total")[:1]
    source_counts = IgTurnRevisionSource.objects.filter(revision_id=OuterRef("revision_id")).order_by(
        ).values("revision_id").annotate(total=Count("pk")).values("total")[:1]
    effects = {row.pk: row for row in IgRevisionDeliveryEffect.objects.filter(pk__in=effect_ids)
        .annotate(proof_effect_count=Subquery(effect_counts), proof_source_count=Subquery(source_counts))
        .select_related("revision__client", "revision__turn")} if effect_ids else {}
    revisions = {row.revision_id: row.revision for row in effects.values()
        if row.proof_effect_count and row.proof_effect_count <= 32
        and row.proof_source_count == row.revision.source_count and row.proof_source_count <= 32}
    all_effects = {}
    for row in IgRevisionDeliveryEffect.objects.filter(revision_id__in=revisions).order_by("revision_id", "order_index", "pk"):
        all_effects.setdefault(row.revision_id, []).append(row)
    source_rows = {}
    for row in IgTurnRevisionSource.objects.filter(revision_id__in=revisions).select_related(
        "message__client").order_by("revision_id", "ordinal", "pk"):
        source_rows.setdefault(row.revision_id, []).append(row)
    from management.services.ig_revision_execution import _whole_sent_proof
    from management.services.ig_revision_outbox import revision_sources_current

    proofs = {pk: revision_sources_current(revision, source_rows=source_rows.get(pk, []))
        and _whole_sent_proof(revision, all_effects.get(pk, []), source_rows=source_rows.get(pk, []))[0]
        for pk, revision in revisions.items()}
    for effect in effects.values():
        effect._stats_whole_proof = proofs.get(effect.revision_id, False)
        effect._stats_source_message_ids = [row.message_id for row in source_rows.get(effect.revision_id, [])]
    paid_deals = set(IgDeal.objects.filter(verified_payment_q()).filter(
        commercial_episode__pk__in=episode_ids, client_id=F("commercial_episode__client_id"))
        .values_list("commercial_episode__pk", flat=True)) if episodes else set()
    paid_reviews = set(IgPaymentConfirmationReview.objects.filter(manager_confirmed_review_q()).filter(
        commercial_episode__pk__in=episode_ids, client_id=F("commercial_episode__client_id"))
        .values_list("commercial_episode__pk", flat=True)) if episodes else set()
    order_ids = [episode.intended_order_id for episode in episodes if episode.intended_order_id]
    # The same four durable link kinds used by confirmed_purchase_units, in
    # one bounded UNION rather than a per-client money-reader loop.
    linked_orders = set(IgOrderAssignment.objects.filter(order_id__in=order_ids, unassigned_at__isnull=True)
        .order_by().values_list("client_id", "order_id").union(
            IgOrderAttribution.objects.filter(order_id__in=order_ids).order_by().values_list("client_id", "order_id"),
            IgDeal.objects.filter(order_id__in=order_ids).order_by().values_list("client_id", "order_id"),
            IgPaymentConfirmationReview.objects.filter(order_id__in=order_ids).order_by().values_list("client_id", "order_id")).order_by()) if order_ids else set()
    drops = IgFunnelDropOff.objects.filter(episode_id__in=episode_ids).aggregate(total=Count("pk"),
        silence=Count("pk", filter=Q(kind="silence")),
        recovered=Count("pk", filter=Q(recovered_at__isnull=False))) if episodes else {"total": 0, "silence": 0, "recovered": 0}
    counts = {key: 0 for key in ("page_episodes", "observed_audits", "missing_audit", "stale_audit",
        "unobservable_audit", "invalid_audit", "paid_observed", "without_confirmed_purchase")}
    counts.update(qualifying_episodes=population, page_episodes=len(episodes))
    causes = {}
    latency_by_revision = {}
    for episode in episodes:
        snapshot = audits.get(episode.audit_snapshot_id)
        status, audit = _audit_status(snapshot, episode, messages, namespace, effects)
        if status == "current":
            counts["observed_audits"] += 1
            latency_by_revision.update(_receipt_latencies(snapshot, episode, messages, effects))
        else:
            counts[status + "_audit"] += 1
        paid = bool(episode.pk in paid_deals or episode.pk in paid_reviews or (episode.intended_order_id
            and (episode.client_id, episode.intended_order_id) in linked_orders
            and episode.intended_order.payment_status in CONFIRMED_ORDER_PAYMENT_STATUSES))
        counts["paid_observed" if paid else "without_confirmed_purchase"] += 1
        outcome = "confirmed_purchase" if paid else "no_confirmed_purchase"
        seen = set()
        for item in audit.get("nonconversion_reasons", []):
            code, basis = item.get("code"), item.get("basis")
            if code not in CAUSE_LABELS or code == "unknown" or basis not in {"reported_by_customer", "hypothesis"}:
                continue
            if (code, basis) in seen:
                continue
            seen.add((code, basis))
            row = causes.setdefault((code, outcome), {"code": code, "label": CAUSE_LABELS[code],
                "outcome": outcome, "customer_reported_count": 0, "possible_count": 0})
            row["customer_reported_count" if basis == "reported_by_customer" else "possible_count"] += 1
    # Recheck the privacy/epoch/reset scope after the bounded read. The report
    # fails closed if erasure, reset or ownership changes mid-read.
    check = list(cohort.filter(pk__in=episode_ids).annotate(reset_floor=Coalesce(
        Subquery(reset.values("reset_after_message_id")[:1]), Value(0), output_field=BigIntegerField()) + Value(1)
        ).values("pk", "client_id", "client__reply_permission_epoch", "reset_floor")) if episodes else []
    stable = {row["pk"]: row for row in check}
    if any(episode.pk not in stable or stable[episode.pk]["client_id"] != episode.client_id
        or stable[episode.pk]["client__reply_permission_epoch"] != episode.client.reply_permission_epoch
        or stable[episode.pk]["reset_floor"] != episode.reset_floor for episode in episodes):
        return {"schema_version": SCHEMA_VERSION, "status": "unavailable", "reason": "read_scope_changed"}
    return {"schema_version": SCHEMA_VERSION, "status": "available", "observed_at": now.isoformat(),
        "scope": {"since": start.isoformat(), "until_exclusive": end.isoformat(),
            "time_field": "IgCommercialEpisode.opened_at", "maximum_days": MAX_DAYS,
            "window_bounded": bounded, "includes_hidden_clients": True,
            "privacy_erasure_excluded": True, "unit": "qualifying_commercial_episodes"},
        "counts": counts, "causes": sorted(causes.values(), key=lambda row: (row["outcome"], row["code"])),
        "observed_facts": {"recorded_dropoffs": drops["total"],
            "silence_dropoffs": drops["silence"],
            "recovered_dropoffs": drops["recovered"],
            "reply_latency_seconds": round(sum(latency_by_revision.values()) / len(latency_by_revision), 3) if latency_by_revision else None,
            "reply_latency_sample": len(latency_by_revision),
            "reply_latency_status": "observed" if latency_by_revision else "not_observed", "silence_is_cause": False},
        "pagination": {"limit": limit, "maximum": MAX_PAGE, "has_more": has_more,
            "next_before_episode_id": episodes[-1].pk if episodes and has_more else None,
            "selection": "episode_id_descending_page_not_representative_sample"},
        "limits": {"provider_calls": 0, "query_cap": QUERY_CAP},
        "interpretation": {"cause_unit": "episodes_with_current_owned_audit_on_this_page",
            "conversion_forecast": None, "causal_uplift": None, "population_extrapolation": False,
            "confirmed_purchase_basis": "canonical_provider_or_manager_or_linked_paid_order",
            "missing_analysis_is_loss": False, "silence_is_loss_cause": False}}
