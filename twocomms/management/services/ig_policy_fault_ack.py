"""A finite customer acknowledgement of a proved local, zero-dispatch fault.

This owner admits no answer about business terms. The substantive question
stays with an exact source-bound manager case and the ordinary holding ledger.
"""
from django.db import connection, transaction
from django.utils import timezone

from management.models import (
    GeminiRequest, IgBotNotification, IgClient, IgCustomerTurnRevision,
    IgFollowUpTask, InstagramBotSettings,
)
from management.services.ig_revision_input import RevisionInputDecision
from management.services.ig_revision_outbox import PublicationBinding, _digest, pre_winner_readiness
from management.services.ig_revision_authority import check_fact_bindings, check_offer_bindings


MODE = "local_policy_fault"


def technical_acknowledgement(language):
    """A recorded execution case makes no claim that a human received it."""
    return {
        "uk": "Дякую за звернення. Зафіксувала ваш запит для перевірки командою. Якщо є додаткові деталі або запитання, напишіть їх тут.",
        "ru": "Спасибо за обращение. Зафиксировала ваш запрос для проверки командой. Если есть дополнительные детали или вопросы, напишите их здесь.",
        "en": "Thank you for your message. I have recorded your request for our team to review. Please add any further details or questions here.",
        "hi": "आपके संदेश के लिए धन्यवाद। मैंने आपका अनुरोध हमारी टीम की समीक्षा के लिए दर्ज कर लिया है। अन्य जानकारी या प्रश्न यहाँ लिखें।",
    }.get(language, "")


def business_acknowledgement(language, *, business_cooperation=None):
    brief = business_cooperation if isinstance(business_cooperation, dict) else {}
    subtypes = set(brief.get("subtypes") or ())
    if subtypes and subtypes <= {"partnership"} and not brief.get("shop_launch"):
        return {
            "uk": "Дякую за звернення щодо співпраці. Зафіксувала його для розгляду командою. Який формат співпраці вас цікавить?",
            "ru": "Спасибо за обращение по сотрудничеству. Зафиксировала его для рассмотрения командой. Какой формат сотрудничества вас интересует?",
            "en": "Thank you for your cooperation enquiry. I have recorded it for our team to review. What kind of cooperation are you interested in?",
            "hi": "सहयोग के बारे में संपर्क करने के लिए धन्यवाद। मैंने आपका अनुरोध हमारी टीम की समीक्षा के लिए दर्ज कर लिया है। आप किस तरह के सहयोग में रुचि रखते हैं?",
        }[language]
    if brief.get("shop_launch") and not set(brief.get("requested_topics") or ()) - {"resale_channel"}:
        return {
            "uk": "Дякую за звернення щодо магазину та співпраці. Зафіксувала його для розгляду командою. Напишіть, будь ласка, свої запитання тут.",
            "ru": "Спасибо за обращение по магазину и сотрудничеству. Зафиксировала его для рассмотрения командой. Напишите, пожалуйста, свои вопросы здесь.",
            "en": "Thank you for your enquiry about opening a shop and working with us. I have recorded it for our team to review. Please send your questions here.",
            "hi": "दुकान शुरू करने और हमारे साथ काम करने के बारे में संपर्क करने के लिए धन्यवाद। मैंने आपका अनुरोध हमारी टीम की समीक्षा के लिए दर्ज कर लिया है। कृपया अपने प्रश्न यहाँ लिखें।",
        }[language]
    return {
        "uk": "Дякую, що розглядаєте співпрацю з нами. Зафіксувала ваші запитання про партнерство для перевірки командою. Якщо є додаткові деталі або запитання, напишіть їх тут.",
        "ru": "Спасибо, что рассматриваете сотрудничество с нами. Зафиксировала ваши вопросы о партнёрстве для проверки командой. Если есть дополнительные детали или вопросы, напишите их здесь.",
        "en": "Thank you for considering a partnership with us. I have recorded your partnership questions for our team to review. Please add any further details or questions here.",
        "hi": "हमारे साथ साझेदारी पर विचार करने के लिए धन्यवाद। मैंने आपके साझेदारी के प्रश्न हमारी टीम की समीक्षा के लिए दर्ज कर लिए हैं। अन्य जानकारी या प्रश्न यहाँ लिखें।",
    }[language]


def _proof_valid(revision, proof):
    from management.services.ig_revision_recovery import (
        LOCAL_POLICY_READINESS_CODES, recovery_lineage_for_authority,
    )
    admission = (revision.action_receipts or {}).get("generation_admission") or {}
    material = {key: value for key, value in proof.items() if key != "digest"}
    admission_material = {key: value for key, value in admission.items() if key != "digest"}
    if (proof.get("schema") != "revision-policy-readiness.v1"
        or proof.get("digest") != _digest(material)
        or proof.get("code") not in LOCAL_POLICY_READINESS_CODES
        or proof.get("revision_id") != revision.pk
        or proof.get("logical_turn_id") != f"ig-revision:{revision.pk}"
        or proof.get("snapshot_digest") != revision.snapshot_digest
        or proof.get("source_message_ids") != [row["message_id"] for row in revision.bundle_snapshot.get("sources", ())]
        or proof.get("generation_admission_digest") != admission.get("digest")
        or admission.get("digest") != _digest(admission_material)
        or proof.get("settings_id") != admission.get("settings_id")
        or proof.get("publication") != admission.get("publication")
        or proof.get("provider_dispatched") is not False
        or revision.generation_proposal_digest or revision.generation_proposal):
        return False
    lineage, reason = recovery_lineage_for_authority(revision)
    return bool(not reason and lineage and not GeminiRequest.objects.filter(
        logical_turn_id__in=[f"ig-revision:{row.pk}" for row in lineage],
    ).exists() and not any(row.delivery_effects.exists() for row in lineage if row.pk != revision.pk))


def policy_fault_ack_valid(revision, receipt):
    """Recheck the precise local fault and still-open promised business case."""
    proof = (revision.action_receipts or {}).get("policy_readiness_failure") or {}
    from management.services.ig_revision_recovery import wait_notification_receipts
    if any(row["notification"] for row in wait_notification_receipts(revision, exclude_revision_id=revision.pk)):
        return False
    if (receipt.get("acknowledgement_mode") != MODE or not _proof_valid(revision, proof)
        or receipt.get("policy_failure_digest") != proof.get("digest")
        or receipt.get("source_message_ids") != proof.get("source_message_ids")
        or receipt.get("publication") != proof.get("publication")
        or receipt.get("settings_id") != proof.get("settings_id")
        or receipt.get("settings_permission_epoch") != proof.get("settings_permission_epoch")):
        return False
    task = IgFollowUpTask.objects.filter(
        pk=receipt.get("task_id"), client_id=revision.client_id,
        kind=IgFollowUpTask.Kind.MANAGER_TASK,
        reason="revision_case:" + str(receipt.get("case_kind") or ""),
        event_key=f"ig-policy-fault-case:{revision.client_id}:{revision.pk}",
    ).exclude(status__in=(IgFollowUpTask.Status.COMPLETED, IgFollowUpTask.Status.CANCELLED)).first()
    if not task:
        return False
    context = task.manager_context or {}
    if (context.get("snapshot_digest") != revision.snapshot_digest
        or context.get("source_message_ids") != proof["source_message_ids"]):
        return False
    notification = IgBotNotification.objects.filter(
        pk=receipt.get("notification_id"), client_id=revision.client_id,
        dedupe_key=f"ig-policy-fault-case:{task.pk}",
    ).first()
    payload = notification.payload if notification and isinstance(notification.payload, dict) else {}
    return bool(payload.get("revision_id") == revision.pk and payload.get("manager_task_id") == task.pk
        and payload.get("snapshot_digest") == revision.snapshot_digest
        and payload.get("source_message_ids") == proof["source_message_ids"])


def _case_and_reply(revision, client, language):
    from management.services.ig_business_cooperation import business_cooperation_for_sources, current_cooperation_sources
    from management.services.bot_sales_classifier import extract_collaboration_brief
    from management.services.ig_revision_holding import _collaboration_holding_reply, _collaboration_holding_inputs
    from management.services.instagram_bot import notify_manager

    sources = revision.bundle_snapshot.get("sources", ())
    business = business_cooperation_for_sources(sources)
    creator = not business and any(extract_collaboration_brief(str(row.get("text") or ""))
        for row in current_cooperation_sources(sources))
    kind = "collaboration_review" if business or creator else "manager_handoff"
    if business:
        reply = business_acknowledgement(language, business_cooperation=business)
        decisions = ["business_cooperation_review", "partnership_terms", "customer_questions"]
        message = "Перевірити запит про бізнес-співпрацю та відповісти на запитання клієнта; умови не погоджено."
        topic_names = {"partner_price": "партнерські ціни", "catalog_options": "розміри й кольори",
            "start_without_stock": "старт без закупівлі партії", "delivery_terms": "умови відправлення",
            "resale_channel": "продаж через Instagram/інший канал", "photo_usage": "право використання фото"}
        topics = [topic_names[topic] for topic in business.get("requested_topics", ()) if topic in topic_names]
        if topics:
            message += " Питання: " + "; ".join(topics) + "."
    elif creator:
        evidence, results, contact = _collaboration_holding_inputs(revision)
        reply = _collaboration_holding_reply(language, evidence_present=evidence, results_present=results, contact_present=contact)
        decisions = ["portfolio_review", "collaboration_terms", "customer_questions"]
        message = "Перевірити пропозицію creator/фото-відео співпраці та запитання клієнта; умови не погоджено."
    else:
        reply = {
            "uk": "Дякую за звернення. Зафіксувала його для перевірки командою. Якщо є додаткові деталі або запитання, напишіть їх тут.",
            "ru": "Спасибо за обращение. Зафиксировала его для проверки командой. Если есть дополнительные детали или вопросы, напишите их здесь.",
            "en": "Thank you for your message. I have recorded it for our team to review. Please add any further details or questions here.",
            "hi": "आपके संदेश के लिए धन्यवाद। मैंने इसे हमारी टीम की समीक्षा के लिए दर्ज कर लिया है। अन्य जानकारी या प्रश्न यहाँ लिखें।",
        }[language]
        decisions = ["customer_questions"]
        message = "Перевірити звернення клієнта та надати змістовну відповідь."
    now = timezone.now()
    source_ids = [row["message_id"] for row in sources]
    task, _ = IgFollowUpTask.objects.get_or_create(
        event_key=f"ig-policy-fault-case:{client.pk}:{revision.pk}",
        defaults={"client": client, "due_at": now, "status": IgFollowUpTask.Status.SKIPPED,
            "kind": IgFollowUpTask.Kind.MANAGER_TASK, "reason": "revision_case:" + kind,
            "manager_approval_status": IgFollowUpTask.ManagerApprovalStatus.PENDING,
            "manager_approval_requested_at": now, "message_text": message,
            "trigger": IgFollowUpTask.Trigger.EVENT, "event_occurred_at": now,
            "skip_reason": "human_business_decision_required", "policy_started_at": now,
            "policy_version": "policy-fault-ack-v1",
            "manager_context": {"schema_version": 1, "case_kind": kind,
                "cooperation_kind": "business" if business else "creator" if creator else "customer_question",
                "business_cooperation": business, "revision_id": revision.pk,
                "snapshot_digest": revision.snapshot_digest, "source_message_ids": source_ids,
                "sources": [{"message_id": row["message_id"], "source_digest": row["source_digest"]} for row in sources],
                "required_decisions": decisions, "substantive_obligation": "open_manager_reply",
                "authority": {"terms_confirmed": False, "price_confirmed": False, "fulfillment_started": False}}},
    )
    if task.status in {task.Status.COMPLETED, task.Status.CANCELLED}:
        raise ValueError("policy_fault_case_already_closed")
    key = f"ig-policy-fault-case:{task.pk}"
    if not notify_manager(message, dedupe_key=key, event_type="escalation", client=client,
        metadata={"revision_id": revision.pk, "manager_task_id": task.pk, "case_kind": kind,
            "source_message_ids": source_ids, "snapshot_digest": revision.snapshot_digest},
        deliver_immediately=False, raise_on_error=True):
        raise ValueError("policy_fault_notification_missing")
    return task, IgBotNotification.objects.get(dedupe_key=key, client=client), reply, kind


def record_policy_fault_ack(revision_id, token, *, settings_id):
    """Admit acknowledgement only while the proved fault still owns its claim."""
    from management.services.ig_revision_holding import VERSION, PURPOSE, RECEIPT_KEY, holding_receipt_valid
    from management.services.ig_reply_language import resolve_own_source_reply_language
    from management.services.ig_response_guard import ProviderResponseGuard
    from management.services.ig_reply_truth import ReplyTruthContext

    if connection.in_atomic_block:
        return RevisionInputDecision(reason="caller_transaction_active")
    identity = IgCustomerTurnRevision.objects.filter(pk=revision_id).values("client_id").first()
    if not identity:
        return RevisionInputDecision(reason="revision_missing")
    with transaction.atomic():
        settings = InstagramBotSettings.objects.select_for_update().select_related("active_instruction_publication").filter(pk=settings_id).first()
        client = IgClient.objects.select_for_update().filter(pk=identity["client_id"]).first()
        revision = IgCustomerTurnRevision.objects.select_for_update().filter(pk=revision_id).first()
        if not settings or not client or not revision or not settings.ai_enabled:
            return RevisionInputDecision(reason="policy_ack_identity_unavailable")
        revision.client = client
        proof = (revision.action_receipts or {}).get("policy_readiness_failure") or {}
        if (not _proof_valid(revision, proof) or proof.get("settings_id") != settings_id
            or revision.delivery_effects.exists()):
            return RevisionInputDecision(reason="policy_ack_zero_dispatch_unproved")
        pub = proof["publication"]
        admission = revision.action_receipts["generation_admission"]
        readiness = pre_winner_readiness(revision.pk, token, settings_id=settings_id,
            settings_permission_epoch=proof["settings_permission_epoch"],
            publication=PublicationBinding(pub["id"], pub["version"], pub["hash"]),
            fact_bindings=admission.get("fact_bindings") or [], offer_bindings=admission.get("offer_bindings") or [],
            fact_checker=check_fact_bindings, offer_checker=check_offer_bindings)
        if not readiness.ready:
            return RevisionInputDecision(reason=readiness.reasons[0])
        from management.services.ig_revision_recovery import wait_notification_receipts
        if any(row["notification"] for row in wait_notification_receipts(revision, exclude_revision_id=revision.pk)):
            return RevisionInputDecision(reason="holding_continuous_wait_already_notified")
        existing = (revision.action_receipts or {}).get(RECEIPT_KEY)
        if existing:
            return RevisionInputDecision(holding_receipt_valid(revision), PURPOSE, "policy_ack_already_recorded", existing, True)
        language = resolve_own_source_reply_language(sources=revision.bundle_snapshot.get("sources") or [], profile_language=client.language)
        if language.template_family not in {"uk", "ru", "en", "hi"}:
            return RevisionInputDecision(reason="policy_ack_language_unavailable")
        task, notification, text, kind = _case_and_reply(revision, client, language.template_family)
        if not ProviderResponseGuard(context_factory=lambda _control, _reply: ReplyTruthContext()).validate({"reply_text": text, "controls": []}).valid:
            raise ValueError("policy_ack_local_guard_failed")
        facts, offers = admission.get("fact_bindings") or [], admission.get("offer_bindings") or []
        receipt = {"version": VERSION, "origin": PURPOSE, "purpose": PURPOSE,
            "acknowledgement_mode": MODE, "acknowledgement_only": True,
            "snapshot_digest": revision.snapshot_digest, "source_message_ids": proof["source_message_ids"],
            "policy_failure_digest": proof["digest"], "task_id": task.pk, "notification_id": notification.pk,
            "case_kind": kind, "settings_id": settings_id, "settings_permission_epoch": proof["settings_permission_epoch"],
            "publication": pub, "authority": {"allowed_actions": [], "fact_bindings": facts,
                "offer_bindings": offers, "authority_digest": _digest(sorted((*facts, *offers), key=lambda row: row["claim"]))},
            "reply_text": text, "reply_digest": _digest(text), "language": language.template_family,
            "reply_language": language.as_dict(), "recorded_at": timezone.now().isoformat(),
            "substantive_obligation": "open_manager_reply", "reply_mode": "manager_handoff"}
        revision.action_receipts = {**revision.action_receipts, RECEIPT_KEY: receipt}
        revision.save(update_fields=["action_receipts", "updated_at"])
        return RevisionInputDecision(True, PURPOSE, "policy_fault_ack_admitted", receipt)
