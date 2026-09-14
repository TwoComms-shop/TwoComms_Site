"""Explicit changed-source economics links, separate from reply authority.

The source-transfer receipt proves what is owed. This paired receipt only
shares a frozen provider budget. It never copies snapshot or reply authority.
All graph creation, admission and settlement serialize on the original
economic root's final source before locking a child source or graph.
"""
from django.db import connection
from django.db.models import Q

from management.models import GeminiRequest, IgCustomerTurnRevision, InstagramBotMessage

IN_KEY = "burst_budget_in"
OUT_KEY = "burst_budget_out"
VERSION = "revision-burst-budget-v1"


def _refs(revision):
    return [{"message_id": row.message_id, "source_digest": row.source_digest}
            for row in revision.sources.order_by("ordinal", "id")]


def _economic_view(head, *, include_family=False):
    """Bulk immutable evidence for this read only; never cache across a mutex."""
    from management.models import IgTurnRevisionSource
    from management.services.ig_revision_provider_execution import _root, _manifest, _digest, MAX_LINEAGE_ROWS
    from management.services.ig_revision_source_coverage import validate_source_transfer

    current = IgCustomerTurnRevision.objects.filter(pk=head.pk).first() if head else None
    if current is None:
        return None, []
    receipts = current.action_receipts or {}
    hint = receipts.get(IN_KEY) or receipts.get("provider_execution_reference") or {}
    root_id = hint.get("root_revision_id")
    if root_id is not None:
        root = IgCustomerTurnRevision.objects.filter(pk=root_id, client_id=current.client_id).first()
    else:
        root = _root(current) if current.origin in {"auto_refresh", "outage_recovery"} else current
    if root is None or root.origin not in {"inbound", "manual_resume"}:
        return None, []
    manifest = _manifest(root)
    if (root.action_receipts or {}).get("provider_execution_manifest") and (
        not manifest or _digest(root.bundle_snapshot) != root.snapshot_digest
        or manifest.get("source_message_ids") != [item.get("message_id")
            for item in (root.bundle_snapshot or {}).get("sources", [])]
    ):
        return None, []
    # Membership remains parent-descendant based: another manual resume can
    # legitimately share a sealed snapshot without sharing this budget.
    family, pending = [root], [root.pk]
    while pending:
        children = list(IgCustomerTurnRevision.objects.filter(parent_id__in=pending).filter(
            Q(origin__in=("auto_refresh", "outage_recovery")) | Q(action_receipts__has_key=IN_KEY)
        ).order_by("pk"))
        if len(family) + len(children) > MAX_LINEAGE_ROWS:
            return None, []
        family.extend(children)
        pending = [row.pk for row in children]
    if current.pk not in {row.pk for row in family}:
        return None, []
    rows = {row.pk: row for row in family}
    # Source-transfer clock roots can precede the provider's economic root.
    clock_ids = {(row.action_receipts.get("source_transfer_in") or {}).get("root_revision_id") for row in family}
    missing = {pk for pk in clock_ids if type(pk) is int and pk not in rows}
    if missing:
        rows.update({row.pk: row for row in IgCustomerTurnRevision.objects.filter(pk__in=missing, client_id=root.client_id)})
    sources = {}
    for source in IgTurnRevisionSource.objects.filter(revision_id__in=rows).order_by("ordinal", "id"):
        sources.setdefault(source.revision_id, []).append(source)
    memo, visiting = {}, set()
    source_ids = manifest.get("source_message_ids") or []

    def resolve(row):
        if row.pk in memo:
            return memo[row.pk]
        if row.pk in visiting or len(visiting) >= MAX_LINEAGE_ROWS:
            return False
        visiting.add(row.pk)
        valid = False
        if row.client_id == root.client_id:
            receipt = (row.action_receipts or {}).get(IN_KEY)
            previous = rows.get(row.parent_id)
            if row.pk == root.pk and receipt is None:
                valid = True
            elif row.origin in {"auto_refresh", "outage_recovery"}:
                valid = bool(previous and row.snapshot_digest == previous.snapshot_digest
                             and row.permission_epoch == previous.permission_epoch and resolve(previous))
            elif row.origin == "inbound" and isinstance(receipt, dict) and previous:
                refs = [{"message_id": item.message_id, "source_digest": item.source_digest}
                        for item in sources.get(row.pk, ())]
                valid = bool(
                    source_ids and receipt.get("version") == VERSION
                    and receipt.get("digest") == _digest({key: value for key, value in receipt.items() if key != "digest"})
                    and (previous.action_receipts or {}).get(OUT_KEY) == receipt
                    and receipt.get("predecessor_revision_id") == previous.pk
                    and receipt.get("successor_revision_id") == row.pk
                    and receipt.get("client_id") == row.client_id
                    and receipt.get("permission_epoch") == row.permission_epoch
                    and receipt.get("predecessor_snapshot_digest") == previous.snapshot_digest
                    and receipt.get("successor_sources") == refs and refs
                    and receipt.get("root_revision_id") == root.pk
                    and receipt.get("manifest_digest") == manifest.get("digest")
                    and receipt.get("anchor_source_id") == source_ids[-1]
                    and refs[-1]["message_id"] > source_ids[-1]
                    and receipt.get("settings_id") == manifest.get("settings_id")
                    and receipt.get("settings_permission_epoch") == manifest.get("settings_permission_epoch")
                    and receipt.get("horizon_at") == manifest.get("horizon_at")
                    and validate_source_transfer(row, revision_rows=rows, sources_by_revision=sources)
                    and resolve(previous)
                )
                if valid and row.snapshot_digest:
                    snapshot = row.bundle_snapshot or {}
                    valid = (_digest(snapshot) == row.snapshot_digest
                             and [{"message_id": item.get("message_id"), "source_digest": item.get("source_digest")}
                                  for item in snapshot.get("sources", [])] == refs)
        visiting.remove(row.pk)
        memo[row.pk] = valid
        return valid

    if not resolve(rows[current.pk]):
        return None, []
    if include_family:
        for row in family:
            # A corrupt child reference must not hide its spent graph.
            outgoing = (row.action_receipts or {}).get(OUT_KEY)
            if (not resolve(row) or (outgoing and outgoing.get("successor_revision_id") not in rows)):
                return None, []
    return root, family


def economic_root(head):
    return _economic_view(head)[0]


def economic_family(root):
    return _economic_view(root, include_family=True)[1]


def economic_graphs(root, family):
    """Each graph retains its own exact child source/execution identity."""
    identities = Q(pk__in=[])
    for row in family:
        sources = (row.bundle_snapshot or {}).get("sources") or []
        if sources:
            identities |= Q(source_execution_key=f"ig-revision:{row.pk}", source_message_id=sources[-1]["message_id"])
    return GeminiRequest.objects.filter(identities, client_id=root.client_id, lane="live").order_by("pk")


def lock_economic_source(revision_id):
    """No client/revision writes under this mutex, including late settlement."""
    if not connection.in_atomic_block:
        raise ValueError("economic_source_lock_not_atomic")
    revision = IgCustomerTurnRevision.objects.filter(pk=revision_id).first()
    root = economic_root(revision) if revision else None
    if root is None:
        return False
    sources = (root.bundle_snapshot or {}).get("sources") or []
    if not sources:
        return False
    return InstagramBotMessage.objects.select_for_update().filter(
        pk=sources[-1]["message_id"], client_id=root.client_id,
    ).only("pk").first() is not None


def transfer_budget_binding(previous):
    from management.services.ig_revision_provider_execution import _manifest
    from management.models import InstagramBotSettings

    root = economic_root(previous)
    manifest = _manifest(root) if root else {}
    if not manifest or not lock_economic_source(previous.pk):
        return {}
    settings = InstagramBotSettings.objects.select_related("active_instruction_publication").filter(
        pk=manifest["settings_id"], is_enabled=True, ai_enabled=True,
        reply_permission_epoch=manifest["settings_permission_epoch"],
    ).first()
    publication = manifest.get("instruction_publication") or {}
    if (settings is None or publication != {
        "id": settings.active_instruction_publication_id,
        "version": getattr(settings.active_instruction_publication, "version", None),
        "hash": getattr(settings.active_instruction_publication, "snapshot_hash", ""),
    }):
        return {}
    return {"root_revision_id": root.pk, "manifest_digest": manifest["digest"],
            "anchor_source_id": manifest["source_message_ids"][-1],
            "settings_id": manifest["settings_id"],
            "settings_permission_epoch": manifest["settings_permission_epoch"],
            "horizon_at": manifest["horizon_at"]}


def record_budget_transfer(previous, successor, binding):
    from management.services.ig_revision_provider_execution import _digest, REFERENCE_KEY

    receipt = {"version": VERSION, **binding, "client_id": previous.client_id,
               "permission_epoch": previous.permission_epoch,
               "predecessor_revision_id": previous.pk, "successor_revision_id": successor.pk,
               "predecessor_snapshot_digest": previous.snapshot_digest,
               "successor_sources": _refs(successor)}
    receipt["digest"] = _digest(receipt)
    if OUT_KEY in (previous.action_receipts or {}) or IN_KEY in (successor.action_receipts or {}):
        raise ValueError("burst_budget_already_transferred")
    previous.action_receipts = {**previous.action_receipts, OUT_KEY: receipt}
    successor.action_receipts = {**successor.action_receipts, IN_KEY: receipt,
                               REFERENCE_KEY: {key: binding[key] for key in ("root_revision_id", "manifest_digest")}}
    previous.save(update_fields=["action_receipts", "updated_at"])
    successor.save(update_fields=["action_receipts", "updated_at"])
    if economic_root(successor) is None:
        raise ValueError("burst_budget_receipt_invalid")
