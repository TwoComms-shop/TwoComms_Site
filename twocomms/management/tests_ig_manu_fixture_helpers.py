"""Native test bootstraps that preserve migration-owned policy/settings rows.

These factories retain the original fixture bodies, hashes and native source
sealing. New publications use the next real version; singleton settings are
updated through their normal ORM API instead of colliding with native seeds.
"""
import hashlib
import json
import os
from unittest.mock import patch

from django.db.models import Max
from django.utils import timezone


def next_publication_version():
    from management.models import BotPolicyPublication
    return int(BotPolicyPublication.objects.aggregate(value=Max("version"))["value"] or 0) + 1


def setup_captured_context_fixture(case):
    """Equivalent to the captured-context integration setup, with native seeds."""
    from management.models import BotInstruction, BotPolicyPublication, InstagramBotSettings
    from management.services.ig_policy_publication import load_active_policy_snapshot, snapshot_from_rows, snapshot_hash
    from management.services.ig_revision_outbox import PublicationBinding

    case.now = timezone.now()
    case.ordinal = 0
    case.instruction = BotInstruction.objects.create(title="Captured publication",
        body="Captured public playbook A: answer with verified facts.", intent_tags="global",
        is_active=True, priority=1, locale="all")
    snapshot = snapshot_from_rows(BotInstruction.objects.all().order_by("priority", "pk"))
    case.publication = BotPolicyPublication.objects.create(version=next_publication_version(),
        kind="publish", schema_version=1, snapshot=snapshot, snapshot_hash=snapshot_hash(snapshot),
        compiler_version="instruction-set-v1", instruction_count=len(snapshot["instructions"]))
    case.settings_row, _ = InstagramBotSettings.objects.update_or_create(pk=1, defaults={
        "is_enabled": True, "ai_enabled": True, "ig_user_id": "owner-1",
        "system_prompt": InstagramBotSettings._meta.get_field("system_prompt").get_default(),
        "active_instruction_publication": case.publication})
    case.publication_binding = PublicationBinding(case.publication.pk, case.publication.version,
        case.publication.snapshot_hash)
    case.publication_snapshot = load_active_policy_snapshot(case.settings_row)
    environment = patch.dict(os.environ, {"IG_PROVIDER_TRANSPORT": "instagram_login"})
    environment.start()
    case.addCleanup(environment.stop)
    timing = patch("management.services.ig_revision_conversation_context.conversation_timing_guidance",
        return_value="[CURRENT REPLY TIMING CONTEXT]\nNo unsupported delay apology.")
    timing.start()
    case.addCleanup(timing.stop)


def setup_revision_delivery_fixture(case):
    """Equivalent to the delivery bootstrap, retaining real seal/claim guards."""
    from management.models import (BotPolicyPublication, IgClient, IgCustomerTurn, IgTurnMessage,
        InstagramBotMessage, InstagramBotSettings)
    from management.services.ig_revision_outbox import PublicationBinding
    from management.services.ig_turn_revisions import (claim_revision_preparation,
        claim_sealed_revision, create_collecting_revision, seal_revision)

    snapshot = {"schema_version": 1, "instructions": []}
    digest = hashlib.sha256(json.dumps(snapshot, ensure_ascii=False, sort_keys=True,
        separators=(",", ":")).encode()).hexdigest()
    case.publication = BotPolicyPublication.objects.create(version=next_publication_version(),
        kind=BotPolicyPublication.Kind.PUBLISH, schema_version=1, snapshot=snapshot,
        snapshot_hash=digest, compiler_version="instruction-set-v1", instruction_count=0)
    case.settings, _ = InstagramBotSettings.objects.update_or_create(pk=1, defaults={
        "is_enabled": True, "reply_permission_epoch": 4, "active_instruction_publication": case.publication})
    case.client_row = IgClient.objects.create(igsid="revision-delivery-client", reply_permission_epoch=3)
    case.source = InstagramBotMessage.objects.create(client=case.client_row, sender_id=case.client_row.igsid,
        provider_namespace="instagram_login:owner-1", role=InstagramBotMessage.Role.USER,
        source="webhook", text="question", mid="revision-delivery-source", status=InstagramBotMessage.Status.PENDING)
    now = timezone.now()
    turn = IgCustomerTurn.objects.create(client=case.client_row, primary_source_message=case.source,
        window_started_at=now, window_deadline=now)
    IgTurnMessage.objects.create(turn=turn, message=case.source, ordinal=1, role=case.source.role)
    created = create_collecting_revision(turn, [case.source], now=now, bypass_quiet=True)
    case.assertIsNotNone(created.revision, created.reason)
    prepared = claim_revision_preparation(created.revision.pk, now=now)
    case.assertTrue(prepared.token, prepared.reason)
    sealed = seal_revision(created.revision.pk, prepared.token, now=now)
    case.assertTrue(sealed.sealed, sealed.reason)
    claimed = claim_sealed_revision(sealed.revision.pk, now=now)
    case.assertTrue(claimed.token, claimed.reason)
    case.revision, case.revision_token = claimed.revision, claimed.token
    case.binding = PublicationBinding(case.publication.pk, case.publication.version, case.publication.snapshot_hash)
