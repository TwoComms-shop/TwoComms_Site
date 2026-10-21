"""Quarantine failed SENT proofs without altering delivery or source ledgers."""
from django.utils import timezone

from management.services.ig_revision_outbox import _digest


FAULT_KEY = "finalization_proof_fault"
FAULT_VERSION = "revision-finalization-proof-fault.v1"
FAULT_CODES = frozenset({
    "delivery_plan_proof_invalid", "delivery_receipt_scope_invalid",
    "delivery_provider_receipt_duplicate", "delivery_receipt_proof_invalid",
    "delivery_parts_proof_invalid",
})
_EFFECT_FIELDS = (
    "pk", "revision_id", "source_message_id", "state", "plan_digest",
    "settings_id_snapshot", "provider_namespace", "provider_message_id",
    "recipient_igsid", "revision_snapshot_digest", "payload", "payload_digest",
    "projection_metadata", "projection_digest", "group", "part_index", "part_count",
    "order_index", "purpose", "kind", "actor", "activation_group",
    "activation_part_index", "activation_failure_code", "failure_code",
    "provider_http_status", "publication_id", "publication_version", "publication_hash",
    "settings_permission_epoch", "client_permission_epoch", "authority_context_digest",
    "fact_bindings", "offer_bindings", "provider_started_at", "terminal_at",
)


def finalization_proof_fingerprint(revision, *, effects=None):
    """Hash actual source/effect rows and every input used by whole-proof checks."""
    rows = list(effects) if effects is not None else list(
        revision.delivery_effects.order_by("order_index", "id"))
    effects_material = []
    for row in rows:
        material = {key: getattr(row, key) for key in _EFFECT_FIELDS}
        for key in ("provider_started_at", "terminal_at"):
            material[key] = material[key].isoformat() if material[key] else None
        effects_material.append(material)
    return _digest({
        "revision_id": revision.pk, "client_id": revision.client_id,
        "recipient": revision.client.igsid, "snapshot": revision.bundle_snapshot,
        "snapshot_digest": revision.snapshot_digest,
        "source_count": revision.source_count,
        "sources": list(revision.sources.order_by("ordinal", "id").values(
            "message_id", "ordinal", "source_namespace", "role", "message__client_id")),
        "effects": effects_material,
    })


def finalization_fault_blocks_execution(revision):
    """Even changed or malformed quarantine may only re-enter proof validation."""
    return _fault_task(revision) is not None


def _fault_task(revision):
    from management.models import IgFollowUpTask
    from management.services.ig_response_debt import DEBT_REASON

    return IgFollowUpTask.objects.filter(client_id=revision.client_id,
        event_key=f"ig-revision-debt:{revision.pk}", kind="manager_task", reason=DEBT_REASON,
        manager_context__has_key=FAULT_KEY).first()


def finalization_fault_disposition(revision, *, effects=None):
    """Return the exact unchanged quarantined cause, or allow proof revalidation."""
    task = _fault_task(revision)
    receipt = (task.manager_context or {}).get(FAULT_KEY) if task else None
    if not isinstance(receipt, dict):
        return ""
    material = {key: value for key, value in receipt.items() if key != "digest"}
    if (receipt.get("version") != FAULT_VERSION or receipt.get("revision_id") != revision.pk
        or receipt.get("client_id") != revision.client_id or receipt.get("code") not in FAULT_CODES
        or receipt.get("digest") != _digest(material)):
        return ""
    return receipt["code"] if receipt.get("fingerprint") == finalization_proof_fingerprint(
        revision, effects=effects) else ""


def record_finalization_proof_fault(revision, effects, *, code, now=None):
    """Caller holds client/revision/effect locks and has run the whole proof.

    A hashed chain retains prior fault identities without copying customer text.
    The existing source-bound operator task and notification remain idempotent.
    """
    from management.services.ig_response_debt import record_reply_debt

    if code not in FAULT_CODES or not any(row.state == "sent" for row in effects):
        return False
    if finalization_fault_disposition(revision, effects=effects):
        return True
    now = now or timezone.now()
    existing_task = _fault_task(revision)
    previous = (existing_task.manager_context or {}).get(FAULT_KEY) if existing_task else None
    previous = previous if isinstance(previous, dict) else {}
    history = previous.get("history") if isinstance(previous.get("history"), list) else []
    if previous.get("digest"):
        history = [*history, {key: previous.get(key) for key in
            ("digest", "fingerprint", "code", "recorded_at")}][-16:]
    source_ids = list(revision.sources.order_by("ordinal", "id").values_list("message_id", flat=True))
    receipt = {
        "version": FAULT_VERSION, "revision_id": revision.pk, "client_id": revision.client_id,
        "source_message_ids": source_ids, "snapshot_digest": revision.snapshot_digest,
        "effect_ids": [row.pk for row in effects], "code": code,
        "fingerprint": finalization_proof_fingerprint(revision, effects=effects),
        "recorded_at": now.isoformat(), "previous_digest": previous.get("digest", ""),
        "previous_fingerprint": previous.get("fingerprint", ""),
        "previous_code": previous.get("code", ""),
        "history": history,
        "disposition": "reconcile_delivery", "automatic_http_retry": False,
    }
    receipt["digest"] = _digest(receipt)
    task = record_reply_debt(revision, code, effects=effects, now=now)
    task.manager_context = {**(task.manager_context or {}), FAULT_KEY: receipt}
    task.save(update_fields=["manager_context", "updated_at"])
    revision.action_receipts = {**(revision.action_receipts or {}),
        "response_debt": (revision.action_receipts or {}).get("response_debt") or {
            "version": 1, "task_id": task.pk, "owner": "manager",
            "disposition": "reconcile_delivery", "source_message_ids": source_ids}}
    revision.recovery_state = "manual"
    revision.recovery_code = code
    revision.recovery_due_at = None
    revision.claim_token = ""
    revision.claimed_at = None
    revision.lease_until = None
    revision.save(update_fields=["action_receipts", "recovery_state", "recovery_code",
        "recovery_due_at", "claim_token", "claimed_at", "lease_until", "updated_at"])
    return True
