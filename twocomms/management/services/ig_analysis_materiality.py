"""Shadow-only materiality ledger and canonical CRM snapshot selection."""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from datetime import timedelta

from django.conf import settings
from django.db import IntegrityError, OperationalError, transaction
from django.db.models import CharField, Subquery, Value
from django.utils import timezone

from management.models import (
    IgAnalysisMaterialityEvent,
    IgClient,
    IgConversationAnalysisJob,
    IgConversationAnalysisSnapshot,
    IgCustomerTurn,
    InstagramBotMessage,
)


QUIET_SECONDS = 90
MAX_STALENESS_SECONDS = 10 * 60
RESET_FLOOR_ANNOTATION = "materiality_reset_after_message_id"


@dataclass(frozen=True, slots=True)
class MaterialityClaimCursor:
    digest: str = ""
    event_highwater: int = 0
    authority_digest: str = ""
    artifact_digest: str = ""


def materiality_mode() -> str:
    value = str(
        getattr(settings, "IG_ANALYSIS_MATERIALITY_MODE", "off") or "off"
    ).strip().casefold()
    return value if value in {"off", "shadow"} else "off"


def selector_mode() -> str:
    # Э3.4: IG_ANALYSIS_FRESHNESS_SELECTOR замінює старий
    # IG_ANALYSIS_CURRENT_SELECTOR_MODE. Новий флаг читається як bool, а не
    # як режим-рядок, і за замовчуванням увімкнений.
    freshness = getattr(settings, "IG_ANALYSIS_FRESHNESS_SELECTOR", True)
    if isinstance(freshness, str):
        freshness = freshness.strip().lower() in {"1", "true", "yes", "on"}
    else:
        freshness = bool(freshness)
    if freshness:
        return "enforce"
    # Fallback на старий режим, якщо новий флаг явно вимкнений.
    value = str(
        getattr(settings, "IG_ANALYSIS_CURRENT_SELECTOR_MODE", "legacy")
        or "legacy"
    ).strip().casefold()
    return value if value in {"legacy", "enforce"} else "legacy"


def selector_enforced() -> bool:
    # The read gate cannot activate without the shadow ledger that supplies
    # its freshness cursor. In particular, materiality=off always preserves
    # the exact legacy operational behavior even if an env value is stale.
    return materiality_mode() == "shadow" and selector_mode() == "enforce"


def _sha(payload) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _validated_digest(value) -> str:
    """Accept only a caller-produced, content-free immutable revision digest.

    Silently hashing an arbitrary string here is unsafe: a caller could pass
    customer text and turn the ledger into a stable, dictionary-attackable PII
    index. Adapters must digest immutable identifiers/revisions instead.
    """
    digest = str(value or "").strip().casefold()
    if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
        raise ValueError("event_digest must be a 64-character hexadecimal digest")
    return digest


def _validated_optional_digest(value, *, field_name: str) -> str:
    if not value:
        return ""
    try:
        return _validated_digest(value)
    except ValueError as exc:
        raise ValueError(f"{field_name} must be content-free SHA-256 identity") from exc


def record_materiality_event(
    *,
    client_id: int,
    event_kind: str,
    event_digest: str,
    source_role: str,
    relevant_at=None,
    episode_id: int | None = None,
    line_id: str = "",
    customer_turn_id: int | None = None,
    source_message_id: int | None = None,
    authority_digest: str = "",
    artifact_revision: int = 0,
    artifact_digest: str = "",
    authority_immediate: bool = False,
):
    """Append one PII-free event and advance only shadow job fields.

    The hot path is one job SELECT, one event INSERT and one conditional job
    UPDATE. Transaction control statements are not application queries.
    """
    if materiality_mode() != "shadow" or not client_id:
        return None
    relevant_at = relevant_at or timezone.now()
    digest = _validated_digest(event_digest)
    authority_identity = _validated_optional_digest(
        authority_digest,
        field_name="authority_digest",
    )
    artifact_identity = _validated_optional_digest(
        artifact_digest,
        field_name="artifact_digest",
    )
    kind = str(event_kind or "")[:32]
    event_key = f"materiality:{client_id}:{kind}:{digest}"[:160]
    for attempt in range(3):
        try:
            with transaction.atomic():
                job = (
                    IgConversationAnalysisJob.objects.select_for_update()
                    .filter(client_id=client_id)
                    .annotate(
                        _source_message_role=(
                            Subquery(
                                InstagramBotMessage.objects.filter(
                                    pk=source_message_id,
                                    client_id=client_id,
                                ).values("role")[:1]
                            )
                            if source_message_id
                            else Value("", output_field=CharField())
                        )
                    )
                    .only(
                        "id", "first_unanalysed_at", "last_relevant_at",
                        "materiality_event_highwater", "materiality_episode_id",
                        "materiality_line_id", "authority_digest",
                        "artifact_digest",
                    )
                    .first()
                )
                if job is None:
                    return None
                source_role_value = str(source_role or "system")[:16]
                if kind == IgAnalysisMaterialityEvent.Kind.CUSTOMER_TURN and (
                    source_role_value != IgAnalysisMaterialityEvent.SourceRole.USER
                    or job._source_message_role != InstagramBotMessage.Role.USER
                ):
                    return None
                if source_role_value == IgAnalysisMaterialityEvent.SourceRole.MANAGER and (
                    job._source_message_role != InstagramBotMessage.Role.MANAGER
                ):
                    return None
                event = IgAnalysisMaterialityEvent.objects.create(
                    client_id=client_id,
                    episode_id=episode_id,
                    line_id=str(line_id or "")[:96],
                    customer_turn_id=customer_turn_id,
                    source_message_id=source_message_id,
                    source_role=source_role_value,
                    event_kind=kind,
                    event_key=event_key,
                    event_digest=digest,
                    authority_digest=authority_identity,
                    artifact_revision=max(0, int(artifact_revision or 0)),
                    artifact_digest=artifact_identity,
                    relevant_at=relevant_at,
                )
                first_unanalysed_at = min(
                    value
                    for value in (job.first_unanalysed_at, relevant_at)
                    if value is not None
                )
                last_relevant_at = max(
                    value
                    for value in (job.last_relevant_at, relevant_at)
                    if value is not None
                )
                materiality_due_at = (
                    relevant_at
                    if authority_immediate
                    else min(
                        last_relevant_at + timedelta(seconds=QUIET_SECONDS),
                        first_unanalysed_at
                        + timedelta(seconds=MAX_STALENESS_SECONDS),
                    )
                )
                materiality_digest = _sha({
                    "event_id": event.pk,
                    "digest": digest,
                })
                effective_episode_id = episode_id or job.materiality_episode_id
                effective_line_id = str(
                    line_id or job.materiality_line_id or ""
                )[:96]
                effective_authority_digest = str(
                    authority_identity or job.authority_digest or ""
                )[:64]
                effective_artifact_digest = str(
                    artifact_identity or job.artifact_digest or ""
                )[:64]
                updated = IgConversationAnalysisJob.objects.filter(
                    pk=job.pk,
                    materiality_event_highwater__lt=event.pk,
                ).update(
                    materiality_episode_id=effective_episode_id,
                    materiality_line_id=effective_line_id,
                    first_unanalysed_at=first_unanalysed_at,
                    last_relevant_at=last_relevant_at,
                    materiality_due_at=materiality_due_at,
                    materiality_event_highwater=event.pk,
                    materiality_digest=materiality_digest,
                    authority_digest=effective_authority_digest,
                    artifact_digest=effective_artifact_digest,
                    updated_at=timezone.now(),
                )
                return event if updated else None
        except IntegrityError:
            return None
        except OperationalError:
            if attempt >= 2:
                raise
            time.sleep(0.01 * (attempt + 1))
    return None


def _artifact_schema_revision(artifact) -> int:
    try:
        return max(0, int((artifact or {}).get("schema_version") or 0))
    except (AttributeError, TypeError, ValueError):
        return 0


def _turn_artifact_identity(rows) -> tuple[int, str]:
    revision = 0
    revisions = []
    for row in rows:
        artifact = (
            row.turn_intelligence_artifact
            if isinstance(row.turn_intelligence_artifact, dict)
            else {}
        )
        schema_revision = _artifact_schema_revision(artifact)
        revision = max(revision, schema_revision)
        if artifact:
            # The artifact is immutable on the source row. Its row identity
            # plus schema revision is sufficient; media/content hashes are
            # intentionally excluded from the materiality ledger.
            revisions.append({
                "source_message_id": row.pk,
                "schema_revision": schema_revision,
            })
    return revision, (_sha({"artifacts": revisions}) if revisions else "")


def record_completed_customer_turn(turn_or_id):
    if materiality_mode() != "shadow":
        return None
    turn_id = getattr(turn_or_id, "pk", turn_or_id)
    turn = (
        IgCustomerTurn.objects.select_related("client", "episode")
        .filter(pk=turn_id, claim_state=IgCustomerTurn.ClaimState.PROCESSED)
        .first()
    )
    if turn is None:
        return None
    rows = [
        membership.message
        for membership in turn.turn_messages.select_related("message")
        .filter(role=InstagramBotMessage.Role.USER)
        .order_by("ordinal", "id")
    ]
    meaningful = []
    from management.services.bot_sales_classifier import is_reaction_only

    for row in rows:
        text = str(row.text or "").strip()
        has_media = bool(row.attachments or row.attachment_media)
        has_action = bool(row.quick_reply_payload)
        if not has_media and not has_action and (not text or is_reaction_only(text)):
            continue
        meaningful.append(row)
    if not meaningful:
        return None
    artifact_revision, artifact_digest = _turn_artifact_identity(meaningful)
    event_digest = _sha({
        "event_kind": IgAnalysisMaterialityEvent.Kind.CUSTOMER_TURN,
        "client_id": turn.client_id,
        "episode_id": turn.episode_id,
        "customer_turn_id": turn.pk,
        "source_message_ids": [row.pk for row in meaningful],
        "artifact_schema_revisions": [
            {
                "source_message_id": row.pk,
                "schema_revision": _artifact_schema_revision(
                    row.turn_intelligence_artifact
                ),
            }
            for row in meaningful
            if isinstance(row.turn_intelligence_artifact, dict)
            and row.turn_intelligence_artifact
        ],
    })
    latest = meaningful[-1]
    relevant_at = latest.provider_created_at or latest.created_at or timezone.now()
    return record_materiality_event(
        client_id=turn.client_id,
        episode_id=turn.episode_id,
        customer_turn_id=turn.pk,
        source_message_id=latest.pk,
        source_role=IgAnalysisMaterialityEvent.SourceRole.USER,
        event_kind=IgAnalysisMaterialityEvent.Kind.CUSTOMER_TURN,
        event_digest=event_digest,
        relevant_at=relevant_at,
        artifact_revision=artifact_revision,
        artifact_digest=artifact_digest,
    )


def record_authority_materiality(
    *,
    client: IgClient,
    job: IgConversationAnalysisJob | None,
    trigger: str,
    source_message_id: int | None = None,
    now=None,
):
    if materiality_mode() != "shadow" or job is None:
        return None
    kind = (
        IgAnalysisMaterialityEvent.Kind.ORDER_TRUTH
        if "order" in str(trigger or "")
        else IgAnalysisMaterialityEvent.Kind.PAYMENT_TRUTH
    )
    episode_id = client.current_commercial_episode_id
    # Job revision is the stable identity of one external truth transition:
    # exact retries retain it, while A -> B -> A increments it twice and must
    # append a new event for the returning A state.
    authority_identity = _sha({
        "event_kind": kind,
        "client_id": client.pk,
        "episode_id": episode_id,
        "job_revision": int(job.revision or 0),
        "source_message_id": int(source_message_id or 0),
    })
    return record_materiality_event(
        client_id=client.pk,
        episode_id=episode_id,
        source_message_id=source_message_id,
        source_role=IgAnalysisMaterialityEvent.SourceRole.AUTHORITY,
        event_kind=kind,
        event_digest=authority_identity,
        authority_digest=authority_identity,
        relevant_at=now or timezone.now(),
        authority_immediate=True,
    )


def _legacy_snapshot(client_id: int, *, include_manager: bool):
    from management.services.ig_funnel_reset import latest_reset_after_message_id

    reset_after = latest_reset_after_message_id(client_id)
    queryset = IgConversationAnalysisSnapshot.objects.filter(client_id=client_id)
    if reset_after:
        queryset = queryset.filter(last_analyzed_message_id__gt=reset_after)
    if not include_manager:
        queryset = queryset.exclude(
            interaction_type=(
                IgConversationAnalysisSnapshot.InteractionType.MANAGER_OBSERVATION
            )
        )
    snapshot = queryset.order_by("-id").first()
    if snapshot is not None:
        from management.services.ig_funnel_reset import analysis_snapshot_is_after_reset

        if not analysis_snapshot_is_after_reset(client_id, snapshot):
            return None
    return snapshot


def current_analysis_snapshot(
    client_or_id,
    *,
    include_manager: bool = False,
    candidates=None,
):
    """Return one current snapshot or ``None``; never project stale intent."""
    client = client_or_id if hasattr(client_or_id, "pk") else None
    client_id = getattr(client, "pk", client_or_id)
    if not client_id:
        return None
    if not selector_enforced():
        if candidates is not None:
            from management.services.ig_funnel_reset import latest_reset_after_message_id

            reset_after = latest_reset_after_message_id(client_id)
            for snapshot in candidates:
                if snapshot.client_id != client_id or (
                    reset_after and int(snapshot.last_analyzed_message_id or 0) <= reset_after
                ):
                    continue
                from management.services.ig_funnel_reset import analysis_snapshot_is_after_reset

                if not analysis_snapshot_is_after_reset(client_id, snapshot):
                    continue
                if include_manager or snapshot.interaction_type != (
                    IgConversationAnalysisSnapshot.InteractionType.MANAGER_OBSERVATION
                ):
                    return snapshot
            return None
        return _legacy_snapshot(client_id, include_manager=include_manager)
    if client is None:
        client = (
            IgClient.objects.select_related("current_commercial_episode")
            .filter(pk=client_id)
            .first()
        )
    if client is None:
        return None
    try:
        job = client.analysis_job
    except IgConversationAnalysisJob.DoesNotExist:
        return None
    if (
        job.status != IgConversationAnalysisJob.Status.DONE
        or not job.materiality_digest
        or job.analyzed_materiality_digest != job.materiality_digest
        or int(job.analyzed_materiality_event_highwater or 0)
        < int(job.materiality_event_highwater or 0)
        or int(job.analyzed_watermark_message_id or 0) <= 0
    ):
        return None
    from management.services.ig_funnel_reset import current_message_floor

    floor = current_message_floor(client)
    required_state_fingerprint = str(job.required_state_fingerprint or "")

    def is_current(snapshot) -> bool:
        if not include_manager and snapshot.interaction_type == (
            IgConversationAnalysisSnapshot.InteractionType.MANAGER_OBSERVATION
        ):
            return False
        # Э3.5 / NEW-ANALYSIS-002. Роль автора доказу перестає бути довідковим
        # полем і стає межею: історичний знімок (до `ROLE_BOUNDARY_POLICY_VERSION`)
        # міг отримати customer-facing enum на доказі менеджера. Один enum-чек
        # не захищає від цього, тому read-шлях перевіряє покриття ролей ще раз.
        from management.services import ig_analysis_roles as analysis_roles

        if snapshot.interaction_type != (
            IgConversationAnalysisSnapshot.InteractionType.MANAGER_OBSERVATION
        ) and not analysis_roles.snapshot_carries_customer_intent(snapshot):
            # Enum один не є доказом наміру клієнта. Це також fail-closed на
            # випадок, якщо модель помилково маркувала manager-only доказ як
            # high_intent/product_interest: вони не проходять і не потрапляють
            # у probability, follow-up і CTA-проєкцію.
            return False
        if int(snapshot.last_analyzed_message_id or 0) < floor:
            return False
        if int(snapshot.last_analyzed_message_id or 0) != int(
            job.analyzed_watermark_message_id or 0
        ):
            return False
        if snapshot.commercial_episode_id != client.current_commercial_episode_id:
            return False
        if (
            required_state_fingerprint
            and snapshot.required_state_fingerprint != required_state_fingerprint
        ):
            return False
        return True

    if candidates is not None:
        return next((item for item in candidates if is_current(item)), None)
    queryset = IgConversationAnalysisSnapshot.objects.filter(
        client_id=client.pk,
        last_analyzed_message_id=job.analyzed_watermark_message_id,
        commercial_episode_id=client.current_commercial_episode_id,
    )
    if not include_manager:
        queryset = queryset.exclude(
            interaction_type=(
                IgConversationAnalysisSnapshot.InteractionType.MANAGER_OBSERVATION
            )
        )
    if required_state_fingerprint:
        queryset = queryset.filter(
            required_state_fingerprint=required_state_fingerprint
        )
    snapshot = queryset.order_by("-id").first()
    return snapshot if snapshot and is_current(snapshot) else None


def claimed_materiality_cursor(job: IgConversationAnalysisJob) -> MaterialityClaimCursor:
    """Read the immutable materiality cursor copied by the atomic Job claim."""
    return MaterialityClaimCursor(
        digest=str(job.claimed_materiality_digest or "")[:64],
        event_highwater=max(
            0,
            int(job.claimed_materiality_event_highwater or 0),
        ),
        authority_digest=str(job.claimed_authority_digest or "")[:64],
        artifact_digest=str(job.claimed_artifact_digest or "")[:64],
    )


def mark_job_materiality_analyzed(
    job: IgConversationAnalysisJob,
    *,
    watermark: int,
    claimed_revision: int,
    cursor: MaterialityClaimCursor,
) -> list[str]:
    """Advance only the cursor captured by this claim, never newer evidence."""
    if (
        materiality_mode() != "shadow"
        or not cursor.digest
        or int(cursor.event_highwater or 0) <= 0
        or int(job.watermark_message_id or 0) != int(watermark or 0)
        or int(job.revision or 0) != int(claimed_revision or 0)
    ):
        return []
    job.analyzed_materiality_digest = cursor.digest
    job.analyzed_materiality_event_highwater = cursor.event_highwater
    fields = [
        "analyzed_materiality_digest",
        "analyzed_materiality_event_highwater",
    ]
    if (
        str(job.materiality_digest or "") == cursor.digest
        and int(job.materiality_event_highwater or 0) == cursor.event_highwater
        and str(job.authority_digest or "") == cursor.authority_digest
        and str(job.artifact_digest or "") == cursor.artifact_digest
    ):
        job.first_unanalysed_at = None
        fields.append("first_unanalysed_at")
    return fields
