"""Dormant DB-only action boundary for a revision-backed client selection."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Mapping

from django.db import connection, transaction
from django.utils import timezone

from management.models import (
    IgClient,
    IgCustomerTurnRevision,
    InstagramBotSettings,
)
from management.services import bot_orders
from management.services.ig_revision_authority import (
    CLAIM_CATALOG_CONFIGURATION,
    RevisionAuthorityBindingSet,
    build_revision_authority_bindings,
    check_fact_bindings,
    check_offer_bindings,
)
from management.services.ig_revision_outbox import (
    PublicationBinding,
    pre_winner_readiness,
)
from management.services.instagram_bot import persist_control_selection


ACTION_CLIENT_CONFIGURATION_UPDATE = "client_configuration_update"
MAX_ACTION_RECEIPT_BYTES = 64 * 1024


@dataclass(frozen=True)
class RevisionSelectionActionResult:
    applied: bool = False
    already_applied: bool = False
    changed_fields: tuple[str, ...] = ()
    reasons: tuple[str, ...] = ()
    authority_rebuild_required: bool = False
    post_action_claims: tuple[str, ...] = ()
    post_action_authority: RevisionAuthorityBindingSet | None = None


class _ActionRollback(Exception):
    pass


def _normalize(value) -> str:
    return " ".join(str(value or "").strip().casefold().split())


def _canonical(value) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _digest(value) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _authority_projection(authority: RevisionAuthorityBindingSet) -> dict:
    return {
        "allowed_actions": list(authority.allowed_actions),
        "fact_bindings": list(authority.fact_bindings),
        "offer_bindings": list(authority.offer_bindings),
        "authority_digest": authority.authority_digest,
    }


def _authority_from_projection(value) -> RevisionAuthorityBindingSet | None:
    if not isinstance(value, Mapping):
        return None
    try:
        return RevisionAuthorityBindingSet(
            ready=True,
            allowed_actions=tuple(value["allowed_actions"]),
            fact_bindings=tuple(value["fact_bindings"]),
            offer_bindings=tuple(value["offer_bindings"]),
            authority_digest=str(value["authority_digest"]),
        )
    except (KeyError, TypeError):
        return None


def _rebuild_authority(client, authority, control, settings_obj):
    facts, offers = [], []
    for binding in (*authority.fact_bindings, *authority.offer_bindings):
        claim = str(binding.get("claim") or "")
        # Catalog selection changes here; independent URL/offer selectors keep
        # their own exact meaning instead of being overwritten by that selector.
        selector = control if claim == CLAIM_CATALOG_CONFIGURATION else binding.get("selector") or {}
        rebuilt = build_revision_authority_bindings(client, claims=(claim,), control=selector, settings_obj=settings_obj)
        if not rebuilt.ready:
            return rebuilt
        facts.extend(rebuilt.fact_bindings)
        offers.extend(rebuilt.offer_bindings)
    combined = sorted((*facts, *offers), key=lambda value: value["claim"])
    return RevisionAuthorityBindingSet(True, allowed_actions=authority.allowed_actions, fact_bindings=tuple(facts), offer_bindings=tuple(offers), authority_digest=_digest(combined))


def _catalog_binding(authority: RevisionAuthorityBindingSet) -> dict | None:
    matches = [
        dict(item)
        for item in authority.fact_bindings
        if isinstance(item, Mapping)
        and item.get("claim") == CLAIM_CATALOG_CONFIGURATION
    ]
    return matches[0] if len(matches) == 1 else None


def _bound_control(binding: Mapping) -> tuple[dict | None, str]:
    selector = binding.get("selector")
    if not isinstance(selector, Mapping):
        return None, "catalog_selector_missing"
    try:
        product_id = int(selector.get("product") or 0)
    except (TypeError, ValueError):
        product_id = 0
    if product_id <= 0:
        return None, "catalog_selector_invalid"
    control: dict[str, object] = {"product": product_id}
    if "variant" in selector:
        try:
            variant_id = int(selector.get("variant") or 0)
        except (TypeError, ValueError):
            variant_id = 0
        if variant_id <= 0:
            return None, "catalog_selector_invalid"
        control["variant"] = variant_id
    if "fit" in selector:
        fit = _normalize(selector.get("fit"))
        if not fit:
            return None, "catalog_selector_invalid"
        control["fit"] = fit
    options = selector.get("options")
    if options is not None:
        if not isinstance(options, (list, tuple)):
            return None, "catalog_selector_invalid"
        control["options"] = list(options)
    if "size" in selector:
        size = str(selector.get("size") or "").strip().upper()[:16]
        if not size:
            return None, "catalog_selector_invalid"
        control["size"] = size
    if "qty" in selector:
        try:
            qty = int(selector.get("qty") or 0)
        except (TypeError, ValueError):
            qty = 0
        if not 1 <= qty <= 50:
            return None, "catalog_selector_invalid"
        control["qty"] = qty
    return control, ""


def _selection_matches(client, control: Mapping) -> bool:
    if int(client.current_product_id or 0) != int(control["product"]):
        return False
    if "size" in control and str(client.current_size or "").upper() != control["size"]:
        return False
    if "qty" in control and int(client.current_qty or 0) != int(control["qty"]):
        return False
    state = (client.sales_context or {}).get("assisted_checkout_selection")
    state = state if isinstance(state, Mapping) else {}
    if int(state.get("product_id") or 0) != int(control["product"]):
        return not any(key in control for key in ("variant", "fit", "options"))
    if "variant" in control and int(state.get("color_variant_id") or 0) != int(control["variant"]):
        return False
    if "fit" in control and _normalize(state.get("fit_option_code")) != control["fit"]:
        return False
    if "options" in control:
        expected = {}
        for raw in control["options"]:
            if not isinstance(raw, str) or "=" not in raw:
                return False
            key, value = raw.split("=", 1)
            expected[_normalize(key)] = _normalize(value)
        actual = state.get("option_values")
        actual = actual if isinstance(actual, Mapping) else {}
        if any(_normalize(actual.get(key)) != value for key, value in expected.items()):
            return False
    return True


def apply_revision_selection_actions(
    revision_id: int,
    revision_token: str,
    *,
    source_message_id: int,
    settings_id: int,
    settings_permission_epoch: int,
    publication: PublicationBinding,
    generation_proposal_digest: str,
    authority: RevisionAuthorityBindingSet,
    fact_checker=check_fact_bindings,
    offer_checker=check_offer_bindings,
    now=None,
) -> RevisionSelectionActionResult:
    """Atomically pin and persist only the configuration bound by authority."""
    if connection.in_atomic_block:
        return RevisionSelectionActionResult(reasons=("caller_transaction_active",))
    if (
        not isinstance(authority, RevisionAuthorityBindingSet)
        or not authority.ready
        or ACTION_CLIENT_CONFIGURATION_UPDATE not in authority.allowed_actions
    ):
        return RevisionSelectionActionResult(reasons=("action_not_authorized",))
    if fact_checker is not check_fact_bindings or offer_checker is not check_offer_bindings:
        return RevisionSelectionActionResult(reasons=("authority_checker_invalid",))
    binding = _catalog_binding(authority)
    if binding is None:
        return RevisionSelectionActionResult(reasons=("catalog_binding_missing",))
    control, reason = _bound_control(binding)
    if control is None:
        return RevisionSelectionActionResult(reasons=(reason,))
    identity = IgCustomerTurnRevision.objects.filter(pk=revision_id).values(
        "client_id"
    ).first()
    if identity is None:
        return RevisionSelectionActionResult(reasons=("revision_missing",))
    now = now or timezone.now()
    try:
        with transaction.atomic():
            settings_obj = (
                InstagramBotSettings.objects.select_for_update()
                .select_related("active_instruction_publication")
                .filter(pk=settings_id)
                .first()
            )
            client = IgClient.objects.select_for_update().filter(
                pk=identity["client_id"]
            ).first()
            revision = IgCustomerTurnRevision.objects.select_for_update().filter(
                pk=revision_id,
                client_id=identity["client_id"],
            ).first()
            if revision is None or client is None or settings_obj is None:
                raise _ActionRollback("action_identity_missing")
            proposal = revision.generation_proposal
            if (
                not generation_proposal_digest
                or revision.generation_proposal_digest
                != generation_proposal_digest
                or not isinstance(proposal, Mapping)
                or _digest(proposal) != generation_proposal_digest
            ):
                raise _ActionRollback("generation_proposal_mismatch")
            source = revision.sources.select_for_update().filter(
                message_id=source_message_id,
                message__client_id=client.pk,
            ).first()
            snapshot_ids = {
                int(item.get("message_id") or 0)
                for item in (revision.bundle_snapshot or {}).get("sources", ())
                if isinstance(item, Mapping)
            }
            if source is None or int(source_message_id or 0) not in snapshot_ids:
                raise _ActionRollback("source_not_in_revision")
            proposal_source_ids = {
                int(item.get("message_id") or 0)
                for item in proposal.get("sources", ())
                if isinstance(item, Mapping)
            }
            request_id = str(
                (proposal.get("generation") or {}).get("request_id") or ""
            )[:40]
            before_projection = _authority_projection(authority)
            if proposal.get("authority") != before_projection:
                raise _ActionRollback("generation_proposal_authority_mismatch")
            input_digest = _digest({
                "action": ACTION_CLIENT_CONFIGURATION_UPDATE,
                "source_message_id": int(source_message_id),
                "generation_request_id": request_id,
                "generation_proposal_digest": generation_proposal_digest,
                "control": control,
                "before_authority": before_projection,
            })
            if not request_id or source_message_id not in proposal_source_ids:
                raise _ActionRollback("generation_proposal_source_mismatch")
            receipt = (revision.action_receipts or {}).get(
                ACTION_CLIENT_CONFIGURATION_UPDATE
            )
            if receipt is not None:
                if (
                    not isinstance(receipt, Mapping)
                    or receipt.get("source_message_id") != int(source_message_id)
                    or receipt.get("generation_request_id") != request_id
                    or receipt.get("generation_proposal_digest")
                    != generation_proposal_digest
                    or receipt.get("input_digest") != input_digest
                    or receipt.get("before_authority") != before_projection
                ):
                    raise _ActionRollback("action_receipt_mismatch")
                recorded_after = _authority_from_projection(
                    receipt.get("after_authority")
                )
                if recorded_after is None:
                    raise _ActionRollback("action_receipt_invalid")
                from management.services.ig_revision_commerce import selected_session_receipt_current

                if not selected_session_receipt_current(client, receipt.get("selection_session") or {}):
                    raise _ActionRollback("action_session_receipt_stale")
                current_after = _rebuild_authority(
                    client, recorded_after, control, settings_obj
                )
                if (
                    not current_after.ready
                    or _authority_projection(current_after)
                    != receipt.get("after_authority")
                    or not _selection_matches(client, control)
                ):
                    raise _ActionRollback("action_receipt_stale")
                replay_readiness = pre_winner_readiness(
                    revision.pk,
                    revision_token,
                    settings_id=settings_obj.pk,
                    settings_permission_epoch=settings_permission_epoch,
                    publication=publication,
                    fact_bindings=current_after.fact_bindings,
                    offer_bindings=current_after.offer_bindings,
                    fact_checker=fact_checker,
                    offer_checker=offer_checker,
                    now=now,
                )
                if not replay_readiness.ready:
                    raise _ActionRollback(
                        "readiness:" + ",".join(replay_readiness.reasons)
                    )
                return RevisionSelectionActionResult(
                    applied=True,
                    already_applied=True,
                    changed_fields=tuple(receipt.get("changed_fields") or ()),
                    post_action_claims=(CLAIM_CATALOG_CONFIGURATION,),
                    post_action_authority=current_after,
                )
            readiness = pre_winner_readiness(
                revision.pk,
                revision_token,
                settings_id=settings_obj.pk,
                settings_permission_epoch=settings_permission_epoch,
                publication=publication,
                fact_bindings=authority.fact_bindings,
                offer_bindings=authority.offer_bindings,
                fact_checker=fact_checker,
                offer_checker=offer_checker,
                now=now,
            )
            if not readiness.ready:
                raise _ActionRollback(
                    "readiness:" + ",".join(readiness.reasons)
                )
            if not bot_orders.pin_product(client, control["product"]):
                raise _ActionRollback("product_pin_failed")
            changed = tuple(persist_control_selection(
                client,
                control,
                product_id=control["product"],
                source_message_id=source_message_id,
            ))
            from management.services.ig_revision_commerce import synchronize_selected_session

            client.refresh_from_db()
            session_receipt = synchronize_selected_session(client)
            client.refresh_from_db()
            if not _selection_matches(client, control):
                raise _ActionRollback("selection_persist_failed")
            after_authority = _rebuild_authority(
                client, authority, control, settings_obj
            )
            if not after_authority.ready:
                raise _ActionRollback("post_action_authority_unavailable")
            receipt = {
                "schema_version": 1,
                "action": ACTION_CLIENT_CONFIGURATION_UPDATE,
                "source_message_id": int(source_message_id),
                "generation_request_id": request_id,
                "generation_proposal_digest": generation_proposal_digest,
                "input_digest": input_digest,
                "changed_fields": list(changed),
                "selection_session": session_receipt,
                "before_authority": before_projection,
                "after_authority": _authority_projection(after_authority),
                "recorded_at": now.isoformat(),
            }
            if len(_canonical(receipt)) > MAX_ACTION_RECEIPT_BYTES:
                raise _ActionRollback("action_receipt_too_large")
            revision.action_receipts = {
                **(revision.action_receipts or {}),
                ACTION_CLIENT_CONFIGURATION_UPDATE: receipt
            }
            revision.save(update_fields=["action_receipts", "updated_at"])
    except _ActionRollback as exc:
        return RevisionSelectionActionResult(reasons=(str(exc),))
    except Exception:
        return RevisionSelectionActionResult(reasons=("action_failed",))
    return RevisionSelectionActionResult(
        applied=True,
        changed_fields=changed,
        authority_rebuild_required=False,
        post_action_claims=(CLAIM_CATALOG_CONFIGURATION,),
        post_action_authority=after_authority,
    )


__all__ = [
    "ACTION_CLIENT_CONFIGURATION_UPDATE",
    "RevisionSelectionActionResult",
    "apply_revision_selection_actions",
]
