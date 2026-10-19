"""One finite rate-cap acknowledgement; every capped source remains owed."""
from django.db import connection, transaction
from django.utils import timezone

from management.models import (GeminiRequest, IgBotNotification, IgClient,
    IgCustomerTurnRevision, IgFollowUpTask, IgRevisionDeliveryEffect, InstagramBotSettings)
from management.services.ig_revision_input import RevisionInputDecision
from management.services.ig_revision_outbox import PublicationBinding, _digest, pre_winner_readiness
from management.services.ig_revision_authority import check_fact_bindings, check_offer_bindings


KEY = "rate_handoff"
MODE = "local_rate_limit"
REASON = "revision_case:rate_limit_handoff"
PAUSE_CODE = "rate_limit_handoff"
VERSION = "revision-rate-handoff-v1"
RECENT_SCOPE_LIMIT = 100


def _case_key(client_id, window):
    return f"ig-rate-handoff:{client_id}:{window['anchor_revision_id']}"


def rate_handoff_scope_valid(revision):
    """Exact per-revision ownership, independent of any delivered answer."""
    receipt = (revision.action_receipts or {}).get(KEY) or {}
    source_ids = list(revision.sources.order_by("ordinal", "id").values_list("message_id", flat=True))
    decision = (revision.action_receipts or {}).get("input_decision") or {}
    if (receipt.get("version") != VERSION or receipt.get("revision_id") != revision.pk
        or receipt.get("snapshot_digest") != revision.snapshot_digest
        or receipt.get("source_message_ids") != source_ids or not source_ids
        or receipt.get("input_digest") != _digest(decision)
        or decision.get("origin") != "rate_handoff" or decision.get("reason") != "rate_limited"
        or decision.get("snapshot_digest") != revision.snapshot_digest
        or decision.get("source_message_ids") != source_ids
        or receipt.get("digest") != _digest({k: v for k, v in receipt.items() if k != "digest"})):
        return False
    window = decision.get("rate_window") or {}
    if (receipt.get("window") != window or not window.get("anchor_revision_id")
        or receipt.get("window_digest") != _digest(window)):
        return False
    task = IgFollowUpTask.objects.filter(pk=receipt.get("task_id"), client_id=revision.client_id,
        kind="manager_task", reason=REASON, event_key=_case_key(revision.client_id, window)).first()
    return bool(task and (task.event_payload or {}).get("rate_window") == window
        and (task.event_payload or {}).get("rate_window_digest") == receipt["window_digest"]
        and (task.manager_context or {}).get("owner") == "manager")


def park_rate_handoff(revision):
    """Caller holds client/revision locks; no generic case or notification."""
    if not rate_handoff_scope_valid(revision):
        raise ValueError("rate_handoff_scope_invalid")
    scope = revision.action_receipts[KEY]
    revision.action_receipts = {**revision.action_receipts, "response_debt":
        (revision.action_receipts or {}).get("response_debt", {
            "version": 1, "task_id": scope["task_id"], "owner": "manager",
            "disposition": "manager_reply", "source_message_ids": scope["source_message_ids"]})}
    revision.recovery_state, revision.recovery_code = "manual", PAUSE_CODE
    revision.recovery_due_at = None
    revision.claim_token, revision.claimed_at, revision.lease_until = "", None, None
    revision.save(update_fields=["action_receipts", "recovery_state", "recovery_code",
        "recovery_due_at", "claim_token", "claimed_at", "lease_until", "updated_at"])


def _prior_ack_material(revision, task, *, lock=False, allow_planned=False):
    """Bounded physical proof, including older scopes beyond the UI's tail."""
    from management.services.ig_revision_outbox import _revision_namespace

    rows = IgCustomerTurnRevision.objects.filter(client_id=revision.client_id, pk__lt=revision.pk,
        action_receipts__rate_handoff__task_id=task.pk).order_by("pk")
    rows = list((rows.select_for_update() if lock else rows)[:RECENT_SCOPE_LIMIT + 1])
    if not rows or len(rows) > RECENT_SCOPE_LIMIT:
        return None
    scope = revision.action_receipts[KEY]
    namespace = _revision_namespace(revision)
    if not namespace or any(not rate_handoff_scope_valid(row) or row.active_slot == 1
        or row.action_receipts[KEY]["window_digest"] != scope["window_digest"]
        or _revision_namespace(row) != namespace for row in rows):
        return None
    effects = IgRevisionDeliveryEffect.objects.filter(revision_id__in=[row.pk for row in rows]).order_by("pk")
    effects = list((effects.select_for_update() if lock else effects)[:RECENT_SCOPE_LIMIT + 1])
    allowed = {"cancelled", "superseded"} | ({"planned"} if allow_planned else set())
    if len(effects) > RECENT_SCOPE_LIMIT or any(effect.state not in allowed
        or effect.provider_started_at or effect.provider_message_id or effect.claim_token or effect.lease_until
        or effect.purpose != "technical_holding" or effect.provider_namespace != namespace
        or effect.recipient_igsid != revision.client.igsid for effect in effects):
        return None
    return {"revisions": [{"revision_id": row.pk, "snapshot_digest": row.snapshot_digest,
        "scope_digest": row.action_receipts[KEY]["digest"]} for row in rows],
        "effects": [{"effect_id": effect.pk, "revision_id": effect.revision_id,
            "payload_digest": effect.payload_digest, "state": effect.state,
            "failure_code": effect.failure_code} for effect in effects], "namespace": namespace}


def _claim_undispatched_ack(revision, task, client, automation_lease_token, *, now):
    """Move ownership only after the old physical plans are fenced and aborted."""
    if (not automation_lease_token or client.automation_lease_token != automation_lease_token
        or not client.automation_lease_until or client.automation_lease_until <= now):
        return None
    material = _prior_ack_material(revision, task, lock=True, allow_planned=True)
    if material is None:
        return None
    prior_ids = [row["revision_id"] for row in material["revisions"]]
    previous_owner = (task.manager_context or {}).get("ack_owner_revision_id", task.event_payload.get("revision_id"))
    if previous_owner not in prior_ids:
        return None
    if IgCustomerTurnRevision.objects.filter(pk__in=prior_ids, lease_until__gt=now).exists():
        return None
    # The client, prior revisions and effects are locked. Only a pristine
    # PLANNED part can pass this cancellation CAS; CLAIMED always blocks.
    for effect in material["effects"]:
        if effect["state"] == "planned":
            changed = IgRevisionDeliveryEffect.objects.filter(pk=effect["effect_id"], state="planned",
                provider_started_at__isnull=True, provider_message_id="", claim_token="", lease_until__isnull=True).update(
                state="cancelled", failure_code="rate_ack_owner_superseded", terminal_at=now, updated_at=now)
            if changed != 1:
                raise ValueError("rate_ack_cancellation_race")
    IgCustomerTurnRevision.objects.filter(pk__in=prior_ids).update(
        claim_token="", claimed_at=None, lease_until=None, updated_at=now)
    material = _prior_ack_material(revision, task, lock=True)
    if material is None:
        raise ValueError("rate_ack_zero_dispatch_proof_changed")
    material["digest"] = _digest(material)
    task.manager_context = {**(task.manager_context or {}), "ack_owner_revision_id": revision.pk}
    task.save(update_fields=["manager_context", "updated_at"])
    return material


def rate_ack_valid(revision, receipt):
    if (receipt.get("acknowledgement_mode") != MODE or not rate_handoff_scope_valid(revision)
        or receipt.get("rate_handoff_digest") != revision.action_receipts[KEY]["digest"]
        or receipt.get("source_message_ids") != revision.action_receipts[KEY]["source_message_ids"]
        or receipt.get("task_id") != revision.action_receipts[KEY]["task_id"]
        or GeminiRequest.objects.filter(logical_turn_id=f"ig-revision:{revision.pk}").exists()):
        return False
    task = IgFollowUpTask.objects.filter(pk=receipt["task_id"], client_id=revision.client_id,
        reason=REASON).exclude(status__in=("completed", "cancelled")).first()
    if not task:
        return False
    initial_revision_id = (task.event_payload or {}).get("revision_id")
    owner = (task.manager_context or {}).get("ack_owner_revision_id", initial_revision_id)
    if owner != revision.pk:
        return False
    if revision.pk != initial_revision_id:
        proof = receipt.get("zero_dispatch_predecessors") or {}
        material = _prior_ack_material(revision, task)
        if (material is None or proof.get("digest") != _digest({k: v for k, v in proof.items() if k != "digest"})
            or {k: v for k, v in proof.items() if k != "digest"} != material):
            return False
    decision = revision.action_receipts["input_decision"]
    notification = IgBotNotification.objects.filter(pk=receipt.get("notification_id"),
            client_id=revision.client_id, dedupe_key=f"ig-rate-handoff:{task.pk}",
            payload__manager_task_id=task.pk, payload__revision_id=initial_revision_id).first()
    return bool(receipt.get("publication") == decision.get("publication")
        and receipt.get("settings_permission_epoch") == decision.get("settings_permission_epoch")
        and notification and notification.payload.get("source_message_ids") == task.event_payload.get("source_message_ids"))


def record_rate_handoff(revision_id, token, *, settings_id, automation_lease_token=""):
    """No model budget, failed graph or customer completion is invented."""
    if connection.in_atomic_block:
        return RevisionInputDecision(reason="caller_transaction_active")
    identity = IgCustomerTurnRevision.objects.filter(pk=revision_id).values("client_id").first()
    if not identity:
        return RevisionInputDecision(reason="revision_missing")
    with transaction.atomic():
        settings = InstagramBotSettings.objects.select_for_update().filter(pk=settings_id).first()
        client = IgClient.objects.select_for_update().filter(pk=identity["client_id"]).first()
        revision = IgCustomerTurnRevision.objects.select_for_update().filter(pk=revision_id).first()
        if not settings or not client or not revision:
            return RevisionInputDecision(reason="rate_handoff_identity_missing")
        revision.client = client
        decision = (revision.action_receipts or {}).get("input_decision") or {}
        if (decision.get("origin") != "rate_handoff" or decision.get("reason") != "rate_limited"
            or decision.get("snapshot_digest") != revision.snapshot_digest
            or not decision.get("rate_window") or revision.generation_proposal_digest
            or GeminiRequest.objects.filter(logical_turn_id=f"ig-revision:{revision.pk}").exists()):
            return RevisionInputDecision(reason="rate_handoff_not_admitted")
        pub, authority = decision["publication"], decision["authority"]
        readiness = pre_winner_readiness(revision.pk, token, settings_id=settings_id,
            settings_permission_epoch=decision["settings_permission_epoch"],
            publication=PublicationBinding(pub["id"], pub["version"], pub["hash"]),
            fact_bindings=authority["fact_bindings"], fact_checker=check_fact_bindings,
            offer_checker=check_offer_bindings)
        if not readiness.ready:
            return RevisionInputDecision(reason=readiness.reasons[0])
        existing = (revision.action_receipts or {}).get(KEY)
        if existing:
            holding = (revision.action_receipts or {}).get("technical_holding")
            return RevisionInputDecision(rate_handoff_scope_valid(revision),
                "technical_holding" if holding else "rate_handoff_wait", "rate_handoff_replayed",
                holding or existing, True)
        window = decision["rate_window"]
        source_ids = decision["source_message_ids"]
        now = timezone.now()
        task, first = IgFollowUpTask.objects.get_or_create(event_key=_case_key(client.pk, window), defaults={
            "client": client, "kind": "manager_task", "reason": REASON, "due_at": now,
            "status": "skipped", "skip_reason": "rate_limit_human_reply_required",
            "manager_approval_status": "pending", "manager_approval_requested_at": now,
            "trigger": "event", "event_occurred_at": now, "policy_started_at": now,
            "policy_version": VERSION, "message_text": "Потрібна змістовна відповідь клієнту. Автоматичний ліміт вичерпано; нові запитання збережено у зверненні.",
            "event_payload": {"revision_id": revision.pk, "source_message_ids": source_ids,
                "rate_window": window, "rate_window_digest": _digest(window), "snapshot_digest": revision.snapshot_digest},
            "manager_context": {"owner": "manager", "case_kind": "rate_limit_handoff",
                "initial_revision_id": revision.pk, "initial_source_message_ids": source_ids,
                "ack_owner_revision_id": revision.pk,
                "substantive_obligation": "open_manager_reply", "recent_scopes": [], "scope_count": 0}})
        task = IgFollowUpTask.objects.select_for_update().get(pk=task.pk)
        context = dict(task.manager_context or {})
        scope = {"revision_id": revision.pk, "source_message_ids": source_ids,
            "snapshot_digest": revision.snapshot_digest, "manager_informed": False,
            "questions": [str(row.get("text") or "")[:1000] for row in revision.bundle_snapshot.get("sources", ())]}
        context["recent_scopes"] = [*(context.get("recent_scopes") or []), scope][-RECENT_SCOPE_LIMIT:]
        context["scope_count"] = int(context.get("scope_count") or 0) + 1
        context["latest_revision_id"] = revision.pk
        context["latest_source_message_ids"] = source_ids
        context["latest_questions"] = scope["questions"]
        task.manager_context = context
        if task.status in {"completed", "cancelled"}:
            task.status, task.manager_approval_status = "skipped", "pending"
        task.save(update_fields=["manager_context", "status", "manager_approval_status", "updated_at"])
        from management.services.instagram_bot import notify_manager
        from management.services.ig_alerts import client_admin_url

        key = f"ig-rate-handoff:{task.pk}"
        if first:
            notify_manager(task.message_text + "\n" + client_admin_url(client.pk), dedupe_key=key,
                event_type="sender_rate_limited", client=client, deliver_immediately=False, raise_on_error=True,
                metadata={"manager_task_id": task.pk, "revision_id": revision.pk,
                    "source_message_ids": source_ids, "snapshot_digest": revision.snapshot_digest,
                    "case_kind": "rate_limit_handoff", "customer_reply_confirmed": False})
        notification = IgBotNotification.objects.get(dedupe_key=key, client=client)
        receipt = {"version": VERSION, "revision_id": revision.pk, "snapshot_digest": revision.snapshot_digest,
            "source_message_ids": source_ids, "task_id": task.pk, "notification_id": notification.pk,
            "window": window, "window_digest": _digest(window), "input_digest": _digest(decision), "initial_scope": first,
            "substantive_obligation": "open_manager_reply", "recorded_at": now.isoformat()}
        receipt["digest"] = _digest(receipt)
        revision.action_receipts = {**revision.action_receipts, KEY: receipt}
        predecessor_proof = None
        if not first:
            predecessor_proof = _claim_undispatched_ack(revision, task, client,
                automation_lease_token, now=now)
        if not first and predecessor_proof is None:
            revision.save(update_fields=["action_receipts", "updated_at"])
            park_rate_handoff(revision)
            return RevisionInputDecision(True, "rate_handoff_wait", "rate_handoff_appended", receipt)
        from management.services.ig_reply_language import resolve_own_source_reply_language
        from management.services.ig_revision_holding import PURPOSE, VERSION as HOLDING_VERSION

        language = resolve_own_source_reply_language(sources=revision.bundle_snapshot.get("sources") or [], profile_language=client.language)
        text = {"uk": "Дякую, зафіксувала ваші запитання для перевірки командою. Якщо є додаткові деталі, напишіть їх тут.",
            "ru": "Спасибо, зафиксировала ваши вопросы для проверки командой. Если есть дополнительные детали, напишите их здесь.",
            "en": "Thank you. I have recorded your questions for our team to review. Please add any further details here.",
            "hi": "धन्यवाद। मैंने आपके प्रश्न हमारी टीम की समीक्षा के लिए दर्ज किए हैं। कृपया अन्य जानकारी यहाँ लिखें।"}.get(language.template_family)
        if not text:
            revision.save(update_fields=["action_receipts", "updated_at"])
            park_rate_handoff(revision)
            return RevisionInputDecision(True, "rate_handoff_wait", "rate_ack_language_unavailable", receipt)
        holding = {"version": HOLDING_VERSION, "origin": PURPOSE, "purpose": PURPOSE,
            "acknowledgement_mode": MODE, "acknowledgement_only": True,
            "snapshot_digest": revision.snapshot_digest, "source_message_ids": source_ids,
            "task_id": task.pk, "notification_id": notification.pk, "rate_handoff_digest": receipt["digest"],
            "settings_id": settings_id, "settings_permission_epoch": decision["settings_permission_epoch"],
            "publication": pub, "authority": authority, "reply_text": text, "reply_digest": _digest(text),
            "language": language.template_family, "reply_language": language.as_dict(),
            "recorded_at": now.isoformat(), "substantive_obligation": "open_manager_reply", "reply_mode": "manager_handoff"}
        if predecessor_proof is not None:
            holding["zero_dispatch_predecessors"] = predecessor_proof
            holding["manager_notice_scope"] = "initial_case_only"
        revision.action_receipts = {**revision.action_receipts, "technical_holding": holding}
        revision.save(update_fields=["action_receipts", "updated_at"])
        return RevisionInputDecision(True, PURPOSE, "rate_ack_admitted", holding)


def rate_handoff_answered(revision):
    """An operator status cannot substitute for exact audited human delivery."""
    from management.models import AdminAuditLog
    from management.ig_bot_models import HumanReplyCommand
    from management.services.ig_response_debt import confirmed_human_reply_source

    if not rate_handoff_scope_valid(revision) or revision.delivery_effects.filter(
        state__in=("unknown", "provider_started", "claimed")).exists():
        return False
    scope = revision.action_receipts[KEY]
    source_ids = scope["source_message_ids"]
    audits = list(AdminAuditLog.objects.filter(action="ig_rate_handoff_source_replied",
        entity_type="IgFollowUpTask", entity_id=str(scope["task_id"]),
        after__revision_id=revision.pk).order_by("-pk")[:len(source_ids) * 2 + 1])
    covered = set()
    for audit in audits:
        proof = audit.after or {}
        source_id = proof.get("source_message_id")
        if source_id not in source_ids or source_id in covered:
            continue
        command = HumanReplyCommand.objects.select_related("client", "context_message", "reply_message").filter(
            pk=proof.get("command_id"), client_id=revision.client_id).first()
        source = confirmed_human_reply_source(command) if command else None
        if (source is not None and source.pk == source_id and proof.get("reply_confirmed") is True
            and proof.get("provider_message_ids") == command.provider_message_ids
            and proof.get("snapshot_digest") == revision.snapshot_digest):
            covered.add(source_id)
    return covered == set(source_ids)


def settle_rate_handoff_source(command, source, *, now):
    """Called only after the shared native command proof, under client lock."""
    from management.models import AdminAuditLog, InstagramBotMessage

    rows = list(IgCustomerTurnRevision.objects.select_for_update().filter(
        client_id=command.client_id, sources__message_id=source.pk,
        action_receipts__rate_handoff__version=VERSION).distinct().order_by("pk")[:RECENT_SCOPE_LIMIT + 1])
    settled = 0
    for revision in rows:
        if (not rate_handoff_scope_valid(revision)
            or revision.delivery_effects.filter(state__in=("unknown", "provider_started", "claimed")).exists()
            or source.send_state in {"unknown", "sending", "ambiguous"}):
            continue
        scope = revision.action_receipts[KEY]
        task = IgFollowUpTask.objects.select_for_update().get(pk=scope["task_id"], client_id=command.client_id)
        existing = AdminAuditLog.objects.filter(action="ig_rate_handoff_source_replied",
            entity_type="IgFollowUpTask", entity_id=str(task.pk), after__revision_id=revision.pk,
            after__source_message_id=source.pk, after__command_id=command.pk).exists()
        if existing:
            continue
        resolution = {"version": 1, "revision_id": revision.pk, "source_message_id": source.pk,
            "snapshot_digest": revision.snapshot_digest, "command_id": command.pk,
            "provider_message_ids": command.provider_message_ids, "reply_confirmed": True,
            "at": now.isoformat()}
        AdminAuditLog.objects.create(actor=command.actor, actor_role="staff",
            action="ig_rate_handoff_source_replied", entity_type="IgFollowUpTask", entity_id=str(task.pk),
            after=resolution, reason="Confirmed human answer to one exact capped customer source.")
        context = dict(task.manager_context or {})
        context["scope_resolutions"] = [*(context.get("scope_resolutions") or []), resolution][-RECENT_SCOPE_LIMIT:]
        bound = list(IgCustomerTurnRevision.objects.filter(client_id=command.client_id,
            action_receipts__rate_handoff__task_id=task.pk).order_by("pk")[:RECENT_SCOPE_LIMIT + 1])
        complete_scan = len(bound) <= RECENT_SCOPE_LIMIT and len(rows) <= RECENT_SCOPE_LIMIT
        context["resolution_scan_complete"] = complete_scan
        if complete_scan and bound and all(rate_handoff_answered(row) for row in bound):
            task.status, task.skip_reason = "completed", "manager_reply_confirmed"
        task.manager_context = context
        task.save(update_fields=["manager_context", "status", "skip_reason", "updated_at"])
        InstagramBotMessage.objects.filter(pk=source.pk, client_id=command.client_id,
            status__in=("pending", "processing")).update(status="done", processed_at=now)
        settled += 1
    return settled
