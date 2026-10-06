"""Narrow privacy exception for customer extensions on economic evidence.

The existing deletion owner commits each client fence before invoking this
helper, then invokes it before source/client deletion. Economic rows, their
baseline lineage, and public policy metadata are deliberately preserved.
"""
from datetime import datetime

from django.db import transaction
from django.db.models import QuerySet
from django.utils import timezone


class AccountingPrivacyError(ValueError):
    """Finite errors only; never include a customer identity or manifest."""

    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _ids(values):
    if not isinstance(values, (list, tuple, set, frozenset)):
        raise AccountingPrivacyError("privacy_scope_invalid")
    if any(type(value) is not int or not 0 < value <= 2**63 - 1 for value in values):
        raise AccountingPrivacyError("privacy_scope_invalid")
    return sorted(set(values))


def scrub_client_accounting_context(client_ids, *, frozen_message_ids=(), cutoff_at=None):
    """Idempotently erase only new customer JSON extensions under owner fences.

    A caller's existing final deletion transaction may wrap this savepoint.
    Every owner fence must have been committed before that transaction began;
    this helper rechecks the persisted fences without minting or weakening one.
    Source locks precede graph/attempt locks, matching provider accounting.
    """
    from management.models import GeminiRequest, GeminiRequestAttempt, IgClient, InstagramBotMessage

    owners, frozen = _ids(client_ids), _ids(frozen_message_ids)
    if cutoff_at is not None and (not isinstance(cutoff_at, datetime) or timezone.is_naive(cutoff_at)):
        raise AccountingPrivacyError("privacy_cutoff_invalid")
    counts = {"clients": len(owners), "request_contexts": 0, "dispatch_manifests": 0}
    if not owners:
        if frozen:
            raise AccountingPrivacyError("privacy_scope_invalid")
        return counts

    with transaction.atomic():
        fenced = list(IgClient.objects.select_for_update().filter(
            pk__in=owners, privacy_erasure_started_at__isnull=False,
        ).order_by("pk").values_list("pk", flat=True))
        if fenced != owners:
            raise AccountingPrivacyError("privacy_fence_required")
        sources = InstagramBotMessage.objects.filter(client_id__in=owners)
        if cutoff_at is not None:
            sources = sources.filter(created_at__lte=cutoff_at)
        # Economic burst roots use an older source as the first mutex. Sorted
        # owned sources lock that root before its newer child source/graph.
        locked_sources = list(sources.select_for_update().order_by("pk").values_list("pk", flat=True))
        if not set(frozen) <= set(locked_sources):
            raise AccountingPrivacyError("privacy_source_scope_mismatch")

        graph_query = GeminiRequest.objects.filter(client_id__in=owners)
        if cutoff_at is not None:
            graph_query = graph_query.filter(created_at__lte=cutoff_at)
        graphs = list(graph_query.select_for_update().order_by("pk"))
        graph_ids = [graph.pk for graph in graphs]
        # Lock all attempts before changing either extension so corrupt or
        # foreign attempt ownership rolls the entire scrub back atomically.
        attempts = list(GeminiRequestAttempt.objects.select_for_update().filter(
            request_graph_id__in=graph_ids,
        ).order_by("request_graph_id", "pk"))
        graph_owners = {graph.pk: graph.client_id for graph in graphs}
        if any(attempt.client_id not in (None, graph_owners[attempt.request_graph_id]) for attempt in attempts):
            raise AccountingPrivacyError("privacy_attempt_scope_mismatch")
        for graph in graphs:
            original = graph.policy_manifest
            if not isinstance(original, dict):
                raise AccountingPrivacyError("privacy_manifest_invalid")
            if "request_context" not in original:
                continue
            public = {key: value for key, value in original.items() if key != "request_context"}
            changed = QuerySet.update(GeminiRequest.objects.filter(
                pk=graph.pk, client_id=graph.client_id, policy_manifest=original,
            ), policy_manifest=public)
            if changed != 1:
                raise AccountingPrivacyError("privacy_manifest_cas_lost")
            counts["request_contexts"] += 1
        for attempt in attempts:
            original = attempt.dispatch_manifest
            if original == {}:
                continue
            changed = QuerySet.update(GeminiRequestAttempt.objects.filter(
                pk=attempt.pk, request_graph_id=attempt.request_graph_id,
                client_id=attempt.client_id, dispatch_manifest=original,
            ), dispatch_manifest={})
            if changed != 1:
                raise AccountingPrivacyError("privacy_manifest_cas_lost")
            counts["dispatch_manifests"] += 1
    return counts
