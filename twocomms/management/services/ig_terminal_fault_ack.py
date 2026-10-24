"""One source-bound acknowledgement of a newly recorded zero-provider fault.

This path spends no generation budget and never turns an old manual case into
new send authority. A candidate must be recorded by the live fault producer.
"""
from datetime import timedelta
import secrets

from django.db import connection, transaction
from django.db.models import Q
from django.utils import timezone

from management.models import (
    GeminiRequest, GeminiRequestAttempt, IgBotNotification, IgClient,
    IgCustomerTurnRevision, IgFollowUpTask, InstagramBotSettings,
)
from management.services.ig_revision_input import RevisionInputDecision
from management.services.ig_revision_outbox import PublicationBinding, _digest


MODE = "local_terminal_fault"
CANDIDATE_KEY = "terminal_fault_ack_candidate"
ATTEMPT_KEY = "terminal_fault_ack_attempt"
LIFETIME = timedelta(seconds=45)


def _time(value):
    try:
        result = timezone.datetime.fromisoformat(value)
        return result if timezone.is_aware(result) else None
    except (TypeError, ValueError):
        return None


def _material(value):
    return {key: item for key, item in value.items() if key != "digest"}


def _fault_context(revision, *, now, allow_own_effect=False):
    """Require exact native fault/case/admission and absence of all model work."""
    from management.services.ig_response_debt import DEBT_REASON, reply_debt_notification_key
    from management.services.ig_revision_burst_budget import economic_root, economic_family
    from management.services.ig_revision_outbox import revision_sources_current, revision_has_newer_source
    from management.services.ig_ingress_source_proof import newer_manager_sources
    from management.services.ig_revision_outbox import _revision_namespace
    from management.services.ig_revision_finalization_faults import finalization_fault_blocks_execution

    receipts = revision.action_receipts or {}
    admission = receipts.get("generation_admission") or {}
    sources = (revision.bundle_snapshot or {}).get("sources") or []
    if not isinstance(admission, dict) or not isinstance(sources, list) or any(not isinstance(row, dict) for row in sources):
        return None
    ids = [row.get("message_id") for row in sources]
    pub = admission.get("publication") or {}
    if (any(type(pk) is not int or pk <= 0 for pk in ids)
        or not isinstance(pub, dict) or set(pub) != {"id", "version", "hash"}
        or type(pub["id"]) is not int or type(pub["version"]) is not int
        or not isinstance(pub["hash"], str) or len(pub["hash"]) != 64
        or admission.get("schema_version") != 1
        or type(admission.get("settings_id")) is not int
        or type(admission.get("settings_permission_epoch")) is not int):
        return None
    if (revision.active_slot != 1 or revision.state != "claimed"
        or revision.recovery_state != "manual" or revision.recovery_code != "local_preprovider_fault"
        or not sources or _digest(revision.bundle_snapshot) != revision.snapshot_digest
        or finalization_fault_blocks_execution(revision)
        or not revision_sources_current(revision) or revision_has_newer_source(revision)
        or newer_manager_sources(revision.client_id, max(ids), namespace=_revision_namespace(revision)).exists()
        or not isinstance(admission, dict) or admission.get("digest") != _digest(_material(admission))
        or admission.get("revision_id") != revision.pk
        or admission.get("logical_turn_id") != f"ig-revision:{revision.pk}"
        or admission.get("snapshot_digest") != revision.snapshot_digest):
        return None
    task = IgFollowUpTask.objects.filter(
        event_key=f"ig-revision-debt:{revision.pk}", client_id=revision.client_id,
        kind="manager_task", reason=DEBT_REASON,
    ).exclude(status__in=("completed", "cancelled")).first()
    context = task.manager_context if task and isinstance(task.manager_context, dict) else {}
    proof = context.get("local_preprovider_fault") or {}
    debt = receipts.get("response_debt") or {}
    if (not isinstance(proof, dict) or proof.get("schema") != "revision-local-preprovider-fault.v1"
        or proof.get("digest") != _digest(_material(proof))
        or proof.get("revision_id") != revision.pk or proof.get("snapshot_digest") != revision.snapshot_digest
        or proof.get("source_message_ids") != ids or proof.get("code") != "local_preprovider_fault"
        or proof.get("stage") not in {"revision_execution", "generation_preparation"}
        or proof.get("provider_graph_present") is not False
        or proof.get("provider_graph_scope") != "current_revision"
        or proof.get("automatic_http_retry") is not False
        or context.get("owner") != "manager" or context.get("revision_id") != revision.pk
        or context.get("operator_review") or context.get("reply_resolution")
        or not task or (task.event_payload or {}).get("revision_id") != revision.pk
        or (task.event_payload or {}).get("source_message_ids") != ids
        or debt.get("task_id") != task.pk or debt.get("owner") != "manager"
        or debt.get("source_message_ids") != ids):
        return None
    root = economic_root(revision)
    family = economic_family(root) if root else []
    if not family or revision.pk not in {row.pk for row in family}:
        return None
    turns = [f"ig-revision:{row.pk}" for row in family]
    family_sources = {item.get("message_id") for row in family
        for item in (row.bundle_snapshot or {}).get("sources", ())}
    if (any(row.generation_proposal or row.generation_proposal_digest for row in family)
        or GeminiRequest.objects.filter(Q(logical_turn_id__in=turns) | Q(source_execution_key__in=turns)).exists()
        or GeminiRequestAttempt.objects.filter(Q(logical_turn_id__in=turns)
            | Q(client_id=revision.client_id, source_message_id__in=family_sources, lane="live")).exists()):
        return None
    own = receipts.get("technical_holding") or {}
    for row in family:
        effects = list(row.delivery_effects.all())
        if not effects:
            continue
        if (not allow_own_effect or row.pk != revision.pk or len(effects) != 1
            or own.get("acknowledgement_mode") != MODE
            or effects[0].purpose != "technical_holding"
            or effects[0].state not in {"planned", "claimed", "provider_started", "sent"}
            or (effects[0].payload.get("message") or {}).get("text") != own.get("reply_text")):
            return None
    notification = IgBotNotification.objects.filter(
        client_id=revision.client_id, dedupe_key=reply_debt_notification_key(revision, ids),
        payload__manager_task_id=task.pk, payload__revision_id=revision.pk,
        payload__source_message_ids=ids, payload__snapshot_digest=revision.snapshot_digest,
    ).first()
    return (task, notification, proof, admission) if notification else None


def terminal_fault_ack_current(revision, *, now=None):
    """Narrow lifetime authority; the ordinary outbox still checks the window."""
    now = now or timezone.now()
    candidate = (revision.action_receipts or {}).get(CANDIDATE_KEY) or {}
    if not isinstance(candidate, dict):
        return False
    deadline, admitted = _time(candidate.get("deadline_at")), _time(candidate.get("recorded_at"))
    return bool(revision.active_slot == 1 and revision.state == "claimed"
        and revision.recovery_state == "manual" and revision.recovery_code == "local_preprovider_fault"
        and candidate.get("schema") == "terminal-fault-ack.v1"
        and candidate.get("digest") == _digest(_material(candidate))
        and candidate.get("revision_id") == revision.pk
        and candidate.get("snapshot_digest") == revision.snapshot_digest
        and deadline and admitted and admitted <= now < deadline <= admitted + LIFETIME)


def terminal_fault_ack_valid(revision, receipt, *, now=None):
    from management.services.ig_policy_fault_ack import technical_acknowledgement
    from management.services.ig_revision_outbox import pre_winner_readiness
    from management.services.ig_revision_authority import check_fact_bindings, check_offer_bindings

    now = now or timezone.now()
    if not terminal_fault_ack_current(revision, now=now):
        return False
    settings = InstagramBotSettings.objects.filter(pk=receipt.get("settings_id")).first()
    client = IgClient.objects.filter(pk=revision.client_id).first()
    if not _permission_current(revision, client, settings):
        return False
    context = _fault_context(revision, now=now, allow_own_effect=True)
    if context is None:
        return False
    task, notification, proof, admission = context
    candidate = revision.action_receipts[CANDIDATE_KEY]
    observed, deadline = _time(proof.get("observed_at")), _time(candidate.get("deadline_at"))
    valid = bool(receipt.get("acknowledgement_mode") == MODE
        and observed and deadline and observed <= now < deadline <= observed + LIFETIME
        and receipt.get("fault_digest") == proof["digest"] == candidate.get("fault_digest")
        and receipt.get("generation_admission_digest") == admission["digest"] == candidate.get("generation_admission_digest")
        and receipt.get("candidate_digest") == candidate["digest"]
        and receipt.get("task_id") == task.pk and receipt.get("notification_id") == notification.pk
        and receipt.get("source_message_ids") == proof["source_message_ids"]
        and receipt.get("publication") == admission.get("publication")
        and receipt.get("settings_id") == admission.get("settings_id")
        and receipt.get("settings_permission_epoch") == admission.get("settings_permission_epoch")
        and receipt.get("reply_text") == technical_acknowledgement(receipt.get("language")))
    if not valid:
        return False
    pub = receipt["publication"]
    authority = receipt.get("authority") or {}
    return pre_winner_readiness(revision.pk, revision.claim_token, settings_id=receipt["settings_id"],
        settings_permission_epoch=receipt["settings_permission_epoch"],
        publication=PublicationBinding(pub["id"], pub["version"], pub["hash"]),
        fact_bindings=authority.get("fact_bindings") or [], offer_bindings=authority.get("offer_bindings") or [],
        fact_checker=check_fact_bindings, offer_checker=check_offer_bindings, now=now,
        allow_terminal_fault_ack=True).ready


def _lock(revision_id, settings_id):
    identity = IgCustomerTurnRevision.objects.filter(pk=revision_id).values("client_id").first()
    if identity is None:
        return None, None, None
    settings = InstagramBotSettings.objects.select_for_update().select_related("active_instruction_publication").filter(pk=settings_id).first()
    client = IgClient.objects.select_for_update().filter(pk=identity["client_id"]).first()
    revision = IgCustomerTurnRevision.objects.select_for_update().filter(pk=revision_id).first()
    if revision:
        revision.client = client
    return settings, client, revision


def _permission_current(revision, client, settings):
    from management.services.ig_revision_input import classify_sealed_input
    from management.services.ig_revision_outbox import _active_opt_out, _revision_namespace
    from management.services.instagram_bot import ingress_provider_namespace

    return bool(client and settings and settings.is_enabled and settings.ai_enabled
        and client.reply_permission_epoch == revision.permission_epoch
        and not (client.hidden_at or client.is_blocked or client.bot_paused or client.manager_takeover
            or client.privacy_erasure_started_at or revision.erasure_started_at_snapshot
            or client.stage == IgClient.Stage.SPAM or _active_opt_out(client))
        and ingress_provider_namespace(settings) == _revision_namespace(revision)
        and classify_sealed_input(revision.bundle_snapshot, client, settings)[0] == "generate")


def record_terminal_fault_ack_candidate(revision_id, *, settings_id=1, now=None):
    """Live fault-producer hook; old manual cases are never scanned/admitted."""
    now = now or timezone.now()
    with transaction.atomic():
        settings, client, revision = _lock(revision_id, settings_id)
        if revision is None or not _permission_current(revision, client, settings):
            return RevisionInputDecision(reason="terminal_ack_permission_unavailable")
        existing = (revision.action_receipts or {}).get(CANDIDATE_KEY)
        if existing:
            valid = terminal_fault_ack_current(revision, now=now) and _fault_context(revision, now=now) is not None
            return RevisionInputDecision(valid,
                "technical_holding", "terminal_ack_candidate_existing", existing, True)
        context = _fault_context(revision, now=now)
        if context is None:
            return RevisionInputDecision(reason="terminal_ack_fault_unproved")
        _task, _notification, proof, admission = context
        observed = _time(proof.get("observed_at"))
        if observed is None or not observed <= now < observed + LIFETIME:
            return RevisionInputDecision(reason="terminal_ack_fault_not_new")
        from management.services.ig_revision_outbox import _normal_reply_window_deadline
        window = _normal_reply_window_deadline(revision)
        if window is None or window <= now:
            return RevisionInputDecision(reason="reply_window_closed")
        candidate = {"schema": "terminal-fault-ack.v1", "revision_id": revision.pk,
            "snapshot_digest": revision.snapshot_digest, "fault_digest": proof["digest"],
            "generation_admission_digest": admission["digest"], "recorded_at": now.isoformat(),
            "deadline_at": min(now + LIFETIME, observed + LIFETIME, window).isoformat()}
        candidate["digest"] = _digest(candidate)
        revision.action_receipts = {**revision.action_receipts, CANDIDATE_KEY: candidate}
        revision.save(update_fields=["action_receipts", "updated_at"])
        return RevisionInputDecision(True, "technical_holding", "terminal_ack_candidate_recorded", candidate)


def terminal_fault_ack_revision_ids(*, limit=1):
    from django.db.models import F

    return list(IgCustomerTurnRevision.objects.filter(
        active_slot=1, state="claimed", recovery_state="manual", recovery_code="local_preprovider_fault",
        action_receipts__has_key=CANDIDATE_KEY,
        action_receipts__terminal_fault_ack_candidate__deadline_at__gt=timezone.now().isoformat(),
        client__hidden_at__isnull=True, client__is_blocked=False, client__bot_paused=False,
        client__manager_takeover=False, client__privacy_erasure_started_at__isnull=True,
        client__reply_permission_epoch=F("permission_epoch"),
    ).exclude(client__stage=IgClient.Stage.SPAM).exclude(
        Q(client__opted_out_at__isnull=False) & (Q(client__opted_in_at__isnull=True)
            | Q(client__opted_out_at__gt=F("client__opted_in_at"))),
    ).exclude(action_receipts__has_key=ATTEMPT_KEY).order_by("pk").values_list("pk", flat=True)[:max(0, min(int(limit), 5))])


def claim_terminal_fault_ack(revision_id, *, settings_id=1, automation_lease_token, now=None):
    """One fixed-lifetime acknowledgement claim; never reopen generation."""
    from management.services.ig_revision_holding import PURPOSE, RECEIPT_KEY, VERSION, resolve_revision_reply_language
    from management.services.ig_revision_outbox import pre_winner_readiness
    from management.services.ig_revision_authority import check_fact_bindings, check_offer_bindings
    from management.services.ig_policy_fault_ack import technical_acknowledgement

    now = now or timezone.now()
    with transaction.atomic():
        settings, client, revision = _lock(revision_id, settings_id)
        if (revision is None or not _permission_current(revision, client, settings)
            or not automation_lease_token or client.automation_lease_token != automation_lease_token
            or not client.automation_lease_until or client.automation_lease_until <= now
            or (revision.action_receipts or {}).get(ATTEMPT_KEY)
            or revision.claim_token or (revision.lease_until and revision.lease_until > now)
            or not terminal_fault_ack_current(revision, now=now)):
            return None, ""
        context = _fault_context(revision, now=now)
        if context is None:
            revision.action_receipts = {**revision.action_receipts, ATTEMPT_KEY: {
                "outcome": "denied", "reason": "terminal_ack_fault_changed", "at": now.isoformat()}}
            revision.save(update_fields=["action_receipts", "updated_at"])
            return None, ""
        task, notification, proof, admission = context
        language = resolve_revision_reply_language(revision, profile_language=client.language)
        text = technical_acknowledgement(language.template_family)
        if not text:
            revision.action_receipts = {**revision.action_receipts, ATTEMPT_KEY: {
                "outcome": "denied", "reason": "terminal_ack_language_unavailable", "at": now.isoformat()}}
            revision.save(update_fields=["action_receipts", "updated_at"])
            return None, ""
        candidate = revision.action_receipts[CANDIDATE_KEY]
        # Ordinary expired-claim recovery must never treat an acknowledgement
        # execution lease as a new generation owner after a worker crash.
        token = "debt:terminal-ack:" + secrets.token_hex(16)
        revision.claim_token, revision.claimed_at, revision.lease_until = token, now, _time(candidate["deadline_at"])
        revision.save(update_fields=["claim_token", "claimed_at", "lease_until", "updated_at"])
        pub = admission["publication"]
        ready = pre_winner_readiness(revision.pk, token, settings_id=settings_id,
            settings_permission_epoch=admission["settings_permission_epoch"],
            publication=PublicationBinding(pub["id"], pub["version"], pub["hash"]),
            fact_bindings=admission.get("fact_bindings") or [], offer_bindings=admission.get("offer_bindings") or [],
            fact_checker=check_fact_bindings, offer_checker=check_offer_bindings, now=now,
            allow_terminal_fault_ack=True)
        if not ready.ready:
            revision.claim_token, revision.claimed_at, revision.lease_until = "", None, None
            revision.action_receipts = {**revision.action_receipts, ATTEMPT_KEY: {
                "outcome": "denied", "reason": ready.reasons[0], "at": now.isoformat()}}
            revision.save(update_fields=["claim_token", "claimed_at", "lease_until", "action_receipts", "updated_at"])
            return None, ""
        receipt = {"version": VERSION, "origin": PURPOSE, "purpose": PURPOSE,
            "acknowledgement_mode": MODE, "acknowledgement_only": True,
            "snapshot_digest": revision.snapshot_digest, "source_message_ids": proof["source_message_ids"],
            "fault_digest": proof["digest"], "generation_admission_digest": admission["digest"],
            "candidate_digest": candidate["digest"], "task_id": task.pk, "notification_id": notification.pk,
            "settings_id": settings_id, "settings_permission_epoch": admission["settings_permission_epoch"],
            "publication": pub, "authority": {"allowed_actions": [],
                "fact_bindings": admission.get("fact_bindings") or [], "offer_bindings": admission.get("offer_bindings") or []},
            "reply_text": text, "reply_digest": _digest(text), "language": language.template_family,
            "reply_language": language.as_dict(), "recorded_at": now.isoformat(),
            "substantive_obligation": "open_manager_reply", "reply_mode": "manager_handoff"}
        receipt["authority"]["authority_digest"] = _digest(sorted(
            (*receipt["authority"]["fact_bindings"], *receipt["authority"]["offer_bindings"]), key=lambda row: row["claim"]))
        revision.action_receipts = {**revision.action_receipts, RECEIPT_KEY: receipt,
            ATTEMPT_KEY: {"candidate_digest": candidate["digest"], "claimed_at": now.isoformat()}}
        revision.save(update_fields=["action_receipts", "updated_at"])
        return revision, token


def execute_terminal_fault_ack(revision_id, token, settings_row):
    """Use the canonical one-text outbox without compiling a response plan."""
    from management.services import instagram_bot as bot
    from management.services.ig_revision_holding import holding_receipt_valid
    from management.services.ig_revision_live import RevisionLiveResult, _drain_effects
    from management.services.ig_revision_outbox import plan_revision_effects
    from management.services.ig_revision_transport import prepare_text_effects
    from management.services.ig_revision_authority import check_fact_bindings, check_offer_bindings

    if connection.in_atomic_block:
        return RevisionLiveResult(revision_id, "blocked", ("caller_transaction_active",))
    revision = IgCustomerTurnRevision.objects.select_related("client").filter(pk=revision_id, claim_token=token).first()
    if revision is None or not holding_receipt_valid(revision):
        return RevisionLiveResult(revision_id, "blocked", ("terminal_ack_no_longer_current",))
    receipt = revision.action_receipts["technical_holding"]
    prepared = prepare_text_effects(revision.client.igsid, receipt["reply_text"],
        provider_namespace=revision.bundle_snapshot["sources"][0]["source_namespace"])
    if prepared.error or len(prepared.effects) != 1:
        return RevisionLiveResult(revision_id, "blocked", (prepared.error or "terminal_ack_requires_one_text",))
    pub, authority = receipt["publication"], receipt["authority"]
    plan = plan_revision_effects(revision_id, token, source_message_id=receipt["source_message_ids"][-1],
        settings_id=receipt["settings_id"], settings_permission_epoch=receipt["settings_permission_epoch"],
        publication=PublicationBinding(pub["id"], pub["version"], pub["hash"]),
        authority_context_digest=authority["authority_digest"], effects=prepared.effects,
        fact_bindings=authority["fact_bindings"], offer_bindings=authority["offer_bindings"],
        fact_checker=check_fact_bindings, offer_checker=check_offer_bindings, purpose="technical_holding")
    if not plan.effects:
        return RevisionLiveResult(revision_id, "blocked", plan.reasons)
    access_token = bot.get_page_token(settings_row)
    if not access_token:
        return RevisionLiveResult(revision_id, "blocked", ("provider_not_configured",))
    return _drain_effects(revision, token, settings_row, access_token)
