"""Captured, coalescing narrative producer; canonical facts retain their owners."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone as datetime_timezone
import hashlib
import json
import secrets
import re

from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.db.models.functions import Coalesce
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from management.models import IgClient, InstagramBotMessage

VERSION = "captured-memory.v1"
TIMELINE_VERSION = "captured-memory.timeline.v2"
TIMELINE_DELTA_LIMIT = 24
TIMELINE_DELTA_CHARS = 12_000
TIMELINE_SOURCE_CHARS = 2000
TIMELINE_SCAN_LIMIT = 3 * TIMELINE_DELTA_LIMIT
WINDOW = 60
MAX_TRANSCRIPT_CHARS = 24_000
DEBOUNCE_SECONDS = 3
DEBOUNCE_CAP_SECONDS = 15
CLAIM_SECONDS = 40
GENERATION_SECONDS = 30
MAX_BACKOFF_SECONDS = 900


def memory_invalidation_updates(client):
    """For an already locked reset/erasure client; typed facts stay untouched."""
    return {"memory_summary": "", "memory_updated_at": None,
        "memory_version": int(client.memory_version or 0) + 1,
        "memory_provenance": {}, "memory_producer_state": {}, "memory_dirty_at": None,
        "memory_due_at": None, "memory_claim_token": "", "memory_claim_until": None,
        "memory_claim_snapshot": {}, "memory_attempts": 0}


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _date(value):
    return value.isoformat() if value else ""


def _event_key(source):
    return (source.provider_created_at or source.created_at, source.pk)


def _watermark(source):
    event_at, message_id = _event_key(source)
    return {"event_at": _date(event_at), "message_id": message_id}


def _key(value):
    at = parse_datetime(str((value or {}).get("event_at") or ""))
    return (at or timezone.datetime.min.replace(tzinfo=timezone.get_current_timezone()),
        int((value or {}).get("message_id") or 0))


def _namespaces(rows):
    """Resolve a bounded set of exact human receipts with one batch query."""
    rows = list(rows)
    result = {row.pk: str(row.provider_namespace or "") for row in rows}
    human = {row.pk: row for row in rows if row.role == "manager" and row.source == "human_reply"}
    if not human:
        return result
    from management.models import HumanReplyCommand
    for message_id in human:
        result[message_id] = ""
    commands = list(HumanReplyCommand.objects.filter(reply_message_id__in=human,
        state=HumanReplyCommand.State.SENT, actor_id__isnull=False).order_by("reply_message_id", "pk")[:2 * len(human) + 1])
    if len(commands) > 2 * len(human):
        return result
    matches = {}
    for command in commands:
        source = human[command.reply_message_id]
        if (command.client_id != source.client_id or command.recipient_igsid != source.sender_id
            or not command.provider_namespace or not command.terminal_at
            or not command.context_message_id or not command.window_deadline
            or not command.provider_started_at or command.provider_started_at > command.window_deadline
            or command.text != source.text or source.send_state != "sent"
            or source.provider_message_id not in command.provider_message_ids):
            continue
        matches.setdefault(source.pk, []).append(command.provider_namespace)
    for message_id, namespaces in matches.items():
        if len(namespaces) == 1:
            result[message_id] = namespaces[0]
    return result


def _namespace(source):
    return _namespaces([source])[source.pk]


def _source_allowed(source):
    if source.status == "failed" or source.source in {"poll_history", "history", "import", "backfill"}:
        return False
    if source.role == "user":
        return source.source in {"webhook", "poll"}
    if source.role == "manager":
        return source.source in {"echo", "manager", "manual", "human_reply"}
    return source.role == "model" and source.status == "done" and source.send_state == "sent" and bool(source.provider_message_id)


def _source_payload(source, namespace):
    return {"message_id": source.pk, "role": source.role, "source": source.source,
        "client_id": source.client_id, "sender_id": source.sender_id,
        "namespace": namespace, "text": source.text or "", "provider_mid": source.mid or source.provider_message_id or "",
        "event_at": _date(source.provider_created_at or source.created_at),
        "reply_to": source.reply_to_provider_message_id or "", "quick_reply": source.quick_reply_payload or ""}


def _timeline_enabled():
    return bool(getattr(settings, "IG_MEMORY_TIMELINE_ENABLED", False))


def _scope(client, namespace, *, timeline=None):
    from management.models import IgCommerceSelectionSession, IgFunnelResetAudit
    reset = IgFunnelResetAudit.objects.filter(client_id=client.pk).order_by("-pk").values(
        "pk", "reset_after_message_id").first() or {}
    era = {"client_id": client.pk, "namespace": namespace,
        "reset_id": reset.get("pk"), "reset_floor": int(reset.get("reset_after_message_id") or 0) + 1,
        "erasure_at": _date(client.privacy_erasure_started_at)}
    if (_timeline_enabled() if timeline is None else timeline):
        return era
    session = IgCommerceSelectionSession.objects.filter(client_id=client.pk, open_slot=1,
        commercial_episode_id=client.current_commercial_episode_id).order_by("-generation").first()
    line = {}
    if session:
        lines = session.lines or []
        index = int(session.active_index or 0)
        line = lines[index] if 0 <= index < len(lines) and isinstance(lines[index], dict) else {}
    return {**era,
        "episode_id": client.current_commercial_episode_id,
        "line_id": str(line.get("line_id") or ""), "recipient_id": str(line.get("recipient_id") or "self")}


@dataclass(frozen=True)
class MemoryEnqueue:
    queued: bool = False
    reason: str = ""


@dataclass(frozen=True)
class MemoryClaim:
    client_id: int
    token: str
    capture: dict
    deadline_at: object


@dataclass(frozen=True)
class MemoryResult:
    published: bool = False
    reason: str = ""


@dataclass(frozen=True)
class MemoryRead:
    text: str = ""
    reason: str = ""
    provenance: dict | None = None


def enqueue_memory_source(message_id, *, now=None):
    """DB-only admission; caller may already hold settings/client/source locks."""
    now = now or timezone.now()
    identity = InstagramBotMessage.objects.filter(pk=message_id).values("client_id").first()
    if not identity or not identity["client_id"]:
        return MemoryEnqueue(reason="source_missing")
    with transaction.atomic():
        client = IgClient.objects.select_for_update().filter(pk=identity["client_id"]).first()
        source = InstagramBotMessage.objects.select_for_update().filter(pk=message_id, client=client,
            sender_id=client.igsid if client else "").first()
        if not client or not source or not _source_allowed(source):
            return MemoryEnqueue(reason="source_not_current")
        if client.privacy_erasure_started_at:
            return MemoryEnqueue(reason="client_erasing")
        if client.hidden_at:
            return MemoryEnqueue(reason="client_hidden")
        namespace = _namespace(source)
        if not namespace:
            return MemoryEnqueue(reason="source_namespace_unproven")
        scope = _scope(client, namespace)
        if source.pk < scope["reset_floor"]:
            return MemoryEnqueue(reason="source_before_reset")
        episode = client.current_commercial_episode
        if not _timeline_enabled() and episode and source.pk < int(episode.opened_watermark_message_id or 0):
            return MemoryEnqueue(reason="source_before_episode")
        state = deepcopy(client.memory_producer_state or {})
        same_scope = state.get("scope") == scope
        if same_scope and _event_key(source) <= _key(state.get("dirty")):
            return MemoryEnqueue(reason="source_already_consumed_or_historical")
        # A high inserted ID never makes an older provider event a correction.
        newer = InstagramBotMessage.objects.filter(client=client, sender_id=client.igsid, role__in=("user", "manager"),
            provider_namespace=namespace, pk__gte=scope["reset_floor"]).exclude(status="failed").exclude(
                source__in=("poll_history", "history", "import", "backfill")).annotate(
                event_time=Coalesce("provider_created_at", "created_at")).filter(
                Q(event_time__gt=_event_key(source)[0]) | Q(event_time=_event_key(source)[0], pk__gt=source.pk))
        if newer.exists():
            return MemoryEnqueue(reason="historical_event_rewind")
        if not same_scope:
            state = {"version": TIMELINE_VERSION if _timeline_enabled() else VERSION,
                "scope": scope, "consumed": {}, "last_reason": "scope_changed"}
            client.memory_claim_token = ""
            client.memory_claim_until = None
            client.memory_claim_snapshot = {}
            client.memory_dirty_at = None
            client.memory_attempts = 0
        state["dirty"] = _watermark(source)
        state["dirty_digest"] = _digest(_source_payload(source, namespace))
        client.memory_producer_state = state
        client.memory_dirty_at = client.memory_dirty_at or now
        client.memory_due_at = min(now + timedelta(seconds=DEBOUNCE_SECONDS),
            client.memory_dirty_at + timedelta(seconds=DEBOUNCE_CAP_SECONDS))
        client.save(update_fields=["memory_producer_state", "memory_dirty_at", "memory_due_at",
            "memory_claim_token", "memory_claim_until", "memory_claim_snapshot", "memory_attempts"])
        return MemoryEnqueue(True, "source_dirty")


def _capture(client, state, owner, now):
    if _timeline_enabled():
        return _capture_timeline(client, state, owner, now)
    scope = state["scope"]
    target = state["dirty"]
    rows = InstagramBotMessage.objects.filter(client=client, sender_id=client.igsid, pk__gte=scope["reset_floor"]).exclude(
        status="failed").annotate(event_time=Coalesce("provider_created_at", "created_at")).filter(
        Q(event_time__lt=_key(target)[0]) | Q(event_time=_key(target)[0], pk__lte=target["message_id"]))
    if scope["episode_id"]:
        rows = rows.filter(pk__gte=int(client.current_commercial_episode.opened_watermark_message_id or 0))
    rows = list(rows.order_by("-event_time", "-pk")[:WINDOW * 3])
    namespaces = _namespaces(rows)
    eligible = [row for row in rows if _source_allowed(row) and namespaces[row.pk] == scope["namespace"]]
    selected = list(reversed(eligible[:WINDOW]))
    sources, transcript = [], []
    budget = MAX_TRANSCRIPT_CHARS
    # Preserve the newest correction under the character cap, then restore the
    # event-time order for the model; omissions are explicit for every source.
    for row in reversed(selected):
        payload = _source_payload(row, scope["namespace"])
        text = str(row.text or "")[:min(2000, budget)]
        budget -= len(text)
        sources.append({"message_id": row.pk, "source_digest": _digest(payload), "event_at": payload["event_at"],
            "role": row.role, "chars_sent": len(text), "chars_omitted": len(row.text or "") - len(text)})
        transcript.append(f"[{payload['event_at']} #{row.pk} {row.role}] {text}")
    sources.reverse()
    transcript.reverse()
    from management.services.ig_commerce_projection import captured_selection_for
    capture = {"version": VERSION, "scope": scope, "target": target, "dirty_digest": state["dirty_digest"],
        "sources": sources, "source_interval": {"first": sources[0]["message_id"] if sources else None,
            "last": sources[-1]["message_id"] if sources else None},
        "coverage": {"window_limit": WINDOW, "scan_limit": WINDOW * 3, "sources_sent": len(sources),
            "eligible_scanned": len(eligible), "window_omitted": max(0, len(eligible) - len(selected)),
            "scan_is_bounded": True, "historical_order": "provider_event_time_then_message_id"},
        "transcript": "\n".join(transcript), "canonical_selection": captured_selection_for(client),
        "previous_head_version": int(client.memory_version or 0),
        "previous_head_digest": _digest({"summary": client.memory_summary, "provenance": client.memory_provenance}),
        "owner": {"owner_token": owner["owner_token"], "generation": owner["generation"]},
        "captured_at": _date(now), "deadline_at": _date(now + timedelta(seconds=GENERATION_SECONDS))}
    return {**capture, "digest": _digest(capture)}


def claim_memory_job(*, client_id=None, now=None):
    from management.services.ig_analysis_lane import owner_claim_admission, mutation_guard
    now = now or timezone.now()
    owner = owner_claim_admission(now=now)
    if not owner:
        return None
    with mutation_guard(owner_token=owner["owner_token"], generation=owner["generation"], now=now) as allowed:
        if not allowed:
            return None
        rows = IgClient.objects.select_for_update(skip_locked=True).filter(
            memory_due_at__lte=now, memory_dirty_at__isnull=False, privacy_erasure_started_at__isnull=True,
            hidden_at__isnull=True,
        ).filter(Q(memory_claim_until__isnull=True) | Q(memory_claim_until__lte=now))
        if client_id is not None:
            rows = rows.filter(pk=client_id)
        client = rows.order_by("memory_due_at", "pk").first()
        if client is None:
            return None
        state = client.memory_producer_state or {}
        if not state.get("scope") or state["scope"] != _scope(client, state["scope"].get("namespace", "")):
            client.memory_due_at = None
            client.memory_claim_token = ""
            client.memory_claim_until = None
            client.memory_claim_snapshot = {}
            client.memory_producer_state = {**state, "last_reason": "scope_changed"}
            client.save(update_fields=["memory_due_at", "memory_claim_token", "memory_claim_until", "memory_claim_snapshot", "memory_producer_state"])
            return None
        try:
            capture = _capture(client, state, owner, now)
        except TimelineCaptureError as exc:
            _finish(client, exc.code, now)
            return None
        if not capture["sources"]:
            return None
        token = secrets.token_hex(16)
        client.memory_claim_token = token
        client.memory_claim_until = now + timedelta(seconds=CLAIM_SECONDS)
        client.memory_claim_snapshot = capture
        client.save(update_fields=["memory_claim_token", "memory_claim_until", "memory_claim_snapshot"])
        return MemoryClaim(client.pk, token, deepcopy(capture), parse_datetime(capture["deadline_at"]))


def _source_reason(client, capture):
    namespace = capture["scope"]["namespace"]
    rows = {row.pk: row for row in InstagramBotMessage.objects.filter(
        client=client, sender_id=client.igsid, pk__in=[item["message_id"] for item in capture["sources"]])}
    namespaces = _namespaces(rows.values())
    for item in capture["sources"]:
        row = rows.get(item["message_id"])
        digest = (timeline_source_digest(row, namespace) if row is not None and capture.get("version") == TIMELINE_VERSION
            else _digest(_source_payload(row, namespace)) if row is not None else "")
        if row is None or not _source_allowed(row) or namespaces[row.pk] != namespace or digest != item["source_digest"]:
            return "source_changed"
    return ""


def _uncaptured_source_exists(client, capture):
    target = capture["target"]
    rows = list(InstagramBotMessage.objects.filter(client=client, sender_id=client.igsid,
        pk__gte=capture["scope"]["reset_floor"]).exclude(status="failed").annotate(
            event_time=Coalesce("provider_created_at", "created_at")).filter(
            Q(event_time__gt=_key(target)[0]) | Q(event_time=_key(target)[0], pk__gt=target["message_id"])).order_by("-event_time", "-pk")[:100])
    namespaces = _namespaces(rows)
    # Exhausting the scan cannot prove freshness behind excluded/foreign rows.
    return len(rows) == 100 or any(_source_allowed(row) and namespaces[row.pk] == capture["scope"]["namespace"] for row in rows)


def _claim_reason(client, claim, now):
    capture = claim.capture
    if client.privacy_erasure_started_at:
        return "client_erasing"
    if client.hidden_at:
        return "client_hidden"
    if client.memory_claim_token != claim.token or client.memory_claim_snapshot != capture:
        return "claim_replaced"
    captured_deadline = parse_datetime(str(capture.get("deadline_at") or ""))
    if captured_deadline != claim.deadline_at:
        return "capture_digest_invalid"
    if not client.memory_claim_until or client.memory_claim_until <= now or captured_deadline <= now:
        return "claim_deadline_expired"
    if capture.get("digest") != _digest({key: value for key, value in capture.items() if key != "digest"}):
        return "capture_digest_invalid"
    if capture["scope"] != _scope(client, capture["scope"]["namespace"]):
        return "scope_changed"
    state = client.memory_producer_state or {}
    if state.get("dirty") != capture["target"] or state.get("dirty_digest") != capture["dirty_digest"]:
        return "source_watermark_advanced"
    if _uncaptured_source_exists(client, capture):
        return "source_watermark_advanced"
    if client.memory_version != capture["previous_head_version"] or _digest({"summary": client.memory_summary, "provenance": client.memory_provenance}) != capture["previous_head_digest"]:
        return "head_changed"
    return _source_reason(client, capture)


def current_claim_admission(claim, *, now=None):
    """Read-only callback for the provider's final no-HTTP admission boundary."""
    from management.models import IgWorkerLaneState
    from management.services.ig_analysis_lane import LANE_KEY
    now = now or timezone.now()
    automatic_reason = _automatic_admission_reason(now)
    if automatic_reason:
        return automatic_reason
    background_reason = _background_admission_reason(now)
    if background_reason:
        return background_reason
    owner = claim.capture["owner"]
    if not IgWorkerLaneState.objects.filter(lane_key=LANE_KEY, owner_token=owner["owner_token"],
        generation=owner["generation"], lease_until__gt=now, claim_frozen=False).exists():
        return "lane_owner_changed"
    client = IgClient.objects.filter(pk=claim.client_id).first()
    return (_claim_reason(client, claim, now) or True) if client else "client_missing"


def _automatic_admission_reason(now):
    if not getattr(settings, "IG_MEMORY_GENERATION_ENABLED", False) or not getattr(settings, "IG_MEMORY_PROVIDER_ADMISSION_ACCEPTED", False):
        return "generation_disabled"
    from management.services.gemini_accounting_runtime import shadow_runtime_active, nonlive_admission_mode
    if not shadow_runtime_active(now=now) or nonlive_admission_mode() != "enforce":
        return "admission_not_enforced"
    return ""


def _background_admission_reason(now):
    from management.services.ig_maintenance import maintenance_status
    from management.services.ig_db_circuit import circuit_status
    from management.services.bot_conversation_analysis import _customer_reply_work_waiting
    from management.models import InstagramBotSettings
    if maintenance_status().get("active"):
        return "maintenance_active"
    circuit = circuit_status()
    if circuit.get("open") or circuit.get("failures"):
        return "database_circuit_open"
    if _customer_reply_work_waiting(settings_obj=InstagramBotSettings.objects.order_by("pk").first(), now=now):
        return "customer_reply_priority"
    return ""


def _finish(client, reason, now, *, success=False):
    state = deepcopy(client.memory_producer_state or {})
    state["last_reason"] = reason
    client.memory_claim_token = ""
    client.memory_claim_until = None
    client.memory_claim_snapshot = {}
    if success:
        state["consumed"] = state["dirty"]
        client.memory_dirty_at = None
        client.memory_due_at = None
        client.memory_attempts = 0
    elif reason in {"scope_changed", "client_erasing", "client_hidden"}:
        client.memory_due_at = None
    elif reason == "source_watermark_advanced":
        client.memory_attempts = 0
        client.memory_due_at = now + timedelta(seconds=DEBOUNCE_SECONDS)
    else:
        client.memory_attempts = min(10, int(client.memory_attempts or 0) + 1)
        client.memory_due_at = now + timedelta(seconds=min(MAX_BACKOFF_SECONDS, 5 * 2 ** client.memory_attempts))
    client.memory_producer_state = state
    client.save(update_fields=["memory_producer_state", "memory_claim_token", "memory_claim_until",
        "memory_claim_snapshot", "memory_dirty_at", "memory_due_at", "memory_attempts"])


def publish_memory_result(claim, summary, *, now=None):
    from management.services.ig_analysis_lane import mutation_guard
    from management.models import IgWorkerLaneState
    from management.services.ig_analysis_lane import LANE_KEY
    owner = claim.capture["owner"]

    def temporal_reason(client, checked_at):
        # The lane is already locked by mutation_guard. This read introduces no
        # new lock ordering and checks expiry after any client/source read wait.
        if not IgWorkerLaneState.objects.filter(lane_key=LANE_KEY,
            owner_token=owner["owner_token"], generation=owner["generation"],
            lease_until__gt=checked_at, claim_frozen=False).exists():
            return "lane_owner_changed"
        if not client.memory_claim_until or client.memory_claim_until <= checked_at or claim.deadline_at <= checked_at:
            return "claim_deadline_expired"
        return ""

    with mutation_guard(owner_token=owner["owner_token"], generation=owner["generation"], now=now) as allowed:
        if not allowed:
            return MemoryResult(reason="lane_owner_changed")
        client = IgClient.objects.select_for_update().filter(pk=claim.client_id).first()
        if client is None:
            return MemoryResult(reason="client_missing")
        checked_at = now if now is not None else timezone.now()
        reason = temporal_reason(client, checked_at)
        if reason != "lane_owner_changed":
            # Preserve erasure/replaced-claim precedence; an expired old worker
            # must never clear the successor's current claim.
            reason = _claim_reason(client, claim, checked_at) or reason
        if reason:
            if client.memory_claim_token == claim.token and reason not in {"claim_replaced", "lane_owner_changed"}:
                _finish(client, reason, checked_at)
            return MemoryResult(reason=reason)
        timeline = None
        if claim.capture.get("version") == TIMELINE_VERSION:
            from management.services.ig_memory_timeline import TimelineError, build_timeline, render_timeline
            try:
                timeline = build_timeline(summary, sources=claim.capture["timeline_inputs"],
                    retained_events=claim.capture["previous_events"])
                summary = render_timeline(timeline)
            except (TimelineError, KeyError, TypeError) as exc:
                reason = getattr(exc, "code", "timeline_output_invalid")
                _finish(client, reason, checked_at)
                return MemoryResult(reason=reason)
        else:
            summary = summary.strip()[:4000] if isinstance(summary, str) else ""
            if not summary:
                _finish(client, "empty_summary", checked_at)
                return MemoryResult(reason="empty_summary")
        checked_at = now if now is not None else timezone.now()
        reason = temporal_reason(client, checked_at)
        if reason:
            if reason != "lane_owner_changed":
                _finish(client, reason, checked_at)
            return MemoryResult(reason=reason)
        version = int(client.memory_version or 0) + 1
        # Persist source proofs, not another transcript or canonical-fact store.
        captured_proof = {key: value for key, value in claim.capture.items()
            if key not in {"transcript", "canonical_selection", "digest", "timeline_inputs", "previous_events"}}
        captured_proof["generation_input_digest"] = claim.capture["digest"]
        proof = {"version": claim.capture.get("version", VERSION), "head_version": version, "capture": captured_proof,
            "summary_digest": hashlib.sha256(summary.encode()).hexdigest(), "generated_at": _date(checked_at)}
        if timeline is not None:
            proof["timeline"] = timeline
        proof["digest"] = _digest(proof)
        client.memory_summary = summary
        client.memory_updated_at = checked_at
        client.memory_version = version
        client.memory_provenance = proof
        client.save(update_fields=["memory_summary", "memory_updated_at", "memory_version", "memory_provenance"])
        _finish(client, "published", checked_at, success=True)
        return MemoryResult(True, "published")


def fail_memory_claim(claim, reason="generation_failed", *, now=None):
    from management.services.ig_analysis_lane import mutation_guard
    now = now or timezone.now()
    owner = claim.capture["owner"]
    with mutation_guard(owner_token=owner["owner_token"], generation=owner["generation"], now=now) as allowed:
        if not allowed:
            return
        client = IgClient.objects.select_for_update().filter(pk=claim.client_id).first()
        if client and client.memory_claim_token == claim.token:
            _finish(client, str(reason)[:80], now)


def read_memory_summary(client):
    """One bounded read validation; old timestamp-only narrative is omitted."""
    current = IgClient.objects.filter(pk=client.pk).first()
    if current is None or not current.memory_summary.strip():
        return MemoryRead(reason="narrative_empty")
    if current.privacy_erasure_started_at:
        return MemoryRead(reason="client_erasing")
    if current.hidden_at:
        return MemoryRead(reason="client_hidden")
    proof = current.memory_provenance or {}
    if not proof or proof.get("version") != VERSION:
        return MemoryRead(reason="narrative_provenance_missing")
    if (proof.get("head_version") != current.memory_version
        or parse_datetime(str(proof.get("generated_at") or "")) != current.memory_updated_at
        or proof.get("digest") != _digest({key: value for key, value in proof.items() if key != "digest"})
        or proof.get("summary_digest") != hashlib.sha256(current.memory_summary.encode()).hexdigest()):
        return MemoryRead(reason="narrative_integrity_invalid")
    capture = proof.get("capture") or {}
    if not capture.get("scope") or capture["scope"] != _scope(current, capture["scope"].get("namespace", ""), timeline=False):
        return MemoryRead(reason="narrative_scope_changed")
    reason = _source_reason(current, capture)
    if reason:
        return MemoryRead(reason=reason)
    state = current.memory_producer_state or {}
    if state.get("dirty") != capture.get("target") or _uncaptured_source_exists(current, capture):
        return MemoryRead(reason="narrative_source_stale", provenance=deepcopy(proof))
    return MemoryRead(current.memory_summary, "current", deepcopy(proof))


def process_due_memory(*, limit=1, generate=None, admission=None, now=None):
    """Bounded background drain; reversible flags and enforced dispatch gates."""
    automatic_reason = _automatic_admission_reason(now or timezone.now())
    if automatic_reason:
        return {"claimed": 0, "published": 0, "discarded": 0, "failed": 0, "reason": automatic_reason}
    reason = _background_admission_reason(now or timezone.now())
    if reason:
        return {"claimed": 0, "published": 0, "discarded": 0, "failed": 0, "reason": reason}
    result = {"claimed": 0, "published": 0, "discarded": 0, "failed": 0, "reason": "drained"}
    for _ in range(max(0, min(int(limit), 10))):
        claim = claim_memory_job(now=now)
        if claim is None:
            break
        result["claimed"] += 1
        try:
            def final_admission():
                current = current_claim_admission(claim)
                if current is not True:
                    return current
                return admission(claim) if admission is not None else True

            allowed = final_admission()
            if allowed is not True:
                denial = allowed if isinstance(allowed, str) and re.fullmatch(r"[a-z0-9_]{1,48}", allowed) else "generation_admission_denied"
                fail_memory_claim(claim, denial)
                result["discarded"] += 1
                continue
            if generate is None:
                from management.services.bot_memory import build_summary_payload, build_timeline_payload
                from management.services.call_ai_analysis import gemini_generate_text
                payload = (build_timeline_payload(claim.capture)
                    if claim.capture.get("version") == TIMELINE_VERSION else build_summary_payload(claim.capture["transcript"]))
                output = gemini_generate_text(payload, role="management",
                    reasoning_task="memory_summary", deadline_seconds=max(0.01, (claim.deadline_at - timezone.now()).total_seconds()),
                    pre_dispatch_guard=final_admission)
            else:
                output = generate(claim)
            summary = output.get("parsed") if isinstance(output, dict) else output
            published = publish_memory_result(claim, summary)
            result["published" if published.published else "discarded"] += 1
        except Exception:
            try:
                denied = final_admission()
            except Exception:
                denied = "generation_admission_unavailable"
            if isinstance(denied, str) and re.fullmatch(r"[a-z0-9_]{1,48}", denied):
                fail_memory_claim(claim, denied)
                result["discarded"] += 1
            else:
                fail_memory_claim(claim)
                result["failed"] += 1
    return result


def reconcile_memory_sources(*, limit=25, now=None):
    """Fair bounded durable-source sweep repairs a crash after source commit."""
    from management.models import InstagramBotSettings
    bounded = max(0, min(int(limit), 100))
    if not bounded:
        return {"scanned": 0, "queued": 0}
    configuration = InstagramBotSettings.objects.order_by("pk").values("pk", "memory_reconcile_cursor").first()
    if configuration is None:
        return {"scanned": 0, "queued": 0}
    previous_cursor = configuration["memory_reconcile_cursor"]
    rows = InstagramBotMessage.objects.filter(client_id__isnull=False,
        role__in=("user", "manager"), created_at__gte=(now or timezone.now()) - timedelta(days=1),
        client__privacy_erasure_started_at__isnull=True, client__hidden_at__isnull=True).exclude(status="failed").exclude(
            source__in=("poll_history", "history", "import", "backfill"))
    ids = list(rows.filter(pk__gt=previous_cursor).order_by("pk").values_list("pk", flat=True)[:bounded])
    if not ids:
        ids = list(rows.order_by("pk").values_list("pk", flat=True)[:bounded])
    # Never hold settings while waiting for a client: restricted inbound uses
    # client→settings. Each admission commits its own client/source transaction.
    queued = sum(enqueue_memory_source(message_id, now=now).queued for message_id in ids)
    # A crash before checkpoint replays this page idempotently. A concurrent
    # sweep's winning cursor is preserved by this single atomic compare/update.
    InstagramBotSettings.objects.filter(pk=configuration["pk"],
        memory_reconcile_cursor=previous_cursor).update(memory_reconcile_cursor=ids[-1] if ids else 0)
    return {"scanned": len(ids), "queued": queued}


class TimelineCaptureError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def timeline_source_digest(row, namespace):
    """V2 binds both backend clocks; the v1 source hash stays byte-compatible."""
    return _digest({**_source_payload(row, namespace),
        "provider_created_at": _date(row.provider_created_at),
        "observed_created_at": _date(row.created_at)})


def _timeline_dto(row, namespace):
    stamp = row.provider_created_at or row.created_at
    return {"source_message_id": row.pk, "text": row.text or "",
        "event_at": stamp.astimezone(datetime_timezone.utc).isoformat(),
        "time_basis": "provider" if row.provider_created_at else "local_ingest",
        "source_digest": timeline_source_digest(row, namespace),
        "provider_created_at": row.provider_created_at.astimezone(datetime_timezone.utc).isoformat() if row.provider_created_at else None,
        "observed_created_at": row.created_at.astimezone(datetime_timezone.utc).isoformat(),
        "role": row.role, "scope": None}


def _timeline_proof_row(row, namespace, *, sent=True):
    dto = _timeline_dto(row, namespace)
    return {"message_id": row.pk, "source_digest": dto["source_digest"],
        "event_at": dto["event_at"], "time_basis": dto["time_basis"],
        "provider_created_at": dto["provider_created_at"], "observed_created_at": dto["observed_created_at"],
        "role": row.role, "scope": None,
        "chars_sent": len(dto["text"]) if sent else 0,
        "chars_omitted": 0 if sent else len(dto["text"])}


def _timeline_rows(client, scope, *, target, after=None, bootstrap=False):
    """Bounded event-order scan; incremental capture never silently drops rows."""
    bound = _key(target)
    rows = InstagramBotMessage.objects.filter(client_id=client.pk, sender_id=client.igsid,
        pk__gte=scope["reset_floor"], pk__lte=bound[1]).exclude(status="failed").annotate(
            event_time=Coalesce("provider_created_at", "created_at")).filter(
            Q(event_time__lt=bound[0]) | Q(event_time=bound[0], pk__lte=bound[1]))
    if after is not None:
        start = _key(after)
        rows = rows.filter(Q(event_time__gt=start[0]) | Q(event_time=start[0], pk__gt=start[1]))
    rows = list(rows.order_by("-event_time", "-pk")[:TIMELINE_SCAN_LIMIT + 1])
    namespaces = _namespaces(rows)
    eligible = [row for row in rows if _source_allowed(row) and namespaces[row.pk] == scope["namespace"]]
    exhausted = len(rows) > TIMELINE_SCAN_LIMIT
    if not bootstrap and (exhausted or len(eligible) > TIMELINE_DELTA_LIMIT):
        raise TimelineCaptureError("timeline_delta_count_budget")
    selected = list(reversed(eligible[:TIMELINE_DELTA_LIMIT]))
    return selected, {"complete": not bootstrap or not exhausted and len(eligible) <= TIMELINE_DELTA_LIMIT,
        "scan_limit": TIMELINE_SCAN_LIMIT, "delta_limit": TIMELINE_DELTA_LIMIT,
        "eligible_scanned": len(eligible), "older_omitted_minimum": max(0, len(eligible) - len(selected)),
        "older_scan_bounded": exhausted, "bootstrap": bootstrap}


def _validated_timeline_head(client):
    """Authenticate the current v2 publication without requiring its target fresh."""
    from management.services.ig_memory_timeline import (
        VERSION as EVENT_VERSION, TimelineError, build_timeline, render_timeline, validate_timeline,
    )
    proof = client.memory_provenance or {}
    if not isinstance(proof, dict) or proof.get("version") != TIMELINE_VERSION:
        return None, [], "timeline_provenance_missing"
    try:
        capture = proof["capture"]
        sources = capture["sources"]
        if (proof["head_version"] != client.memory_version
            or parse_datetime(proof["generated_at"]) != client.memory_updated_at
            or proof["digest"] != _digest({key: value for key, value in proof.items() if key != "digest"})
            or proof["summary_digest"] != hashlib.sha256(client.memory_summary.encode()).hexdigest()
            or capture["version"] != TIMELINE_VERSION
            or not re.fullmatch(r"[0-9a-f]{64}", capture["generation_input_digest"])
            or not isinstance(sources, list) or not 1 <= len(sources) <= TIMELINE_DELTA_LIMIT + 8
            or len({row["message_id"] for row in sources}) != len(sources)
            or any(type(row["message_id"]) is not int or not capture["scope"]["reset_floor"] <= row["message_id"] <= capture["target"]["message_id"]
                or _key(row) > _key(capture["target"]) for row in sources)
            or capture["scope"] != _scope(client, capture["scope"]["namespace"], timeline=True)):
            return None, [], "timeline_integrity_invalid"
        if _source_reason(client, capture):
            return None, [], "timeline_source_changed"
        actual_rows = {row.pk: row for row in InstagramBotMessage.objects.filter(
            client_id=client.pk, sender_id=client.igsid, pk__in=[item["message_id"] for item in sources])}
        inputs = []
        for source in sources:
            row = actual_rows[source["message_id"]]
            dto = _timeline_dto(row, capture["scope"]["namespace"])
            if (source.get("event_at") != dto["event_at"]
                or source.get("time_basis") != dto["time_basis"]
                or source.get("provider_created_at") != dto["provider_created_at"]
                or source.get("observed_created_at") != dto["observed_created_at"]
                or source.get("scope") is not None or source.get("role") != dto["role"]
                or source.get("chars_omitted") != 0 or source.get("chars_sent") != len(dto["text"])):
                return None, [], "timeline_source_changed"
            if row.role == "user" and source.get("chars_omitted") == 0:
                inputs.append(dto)
        stored = validate_timeline(proof["timeline"])
        rebuilt = build_timeline({"version": EVENT_VERSION, "events": []},
            sources=inputs, retained_events=stored["events"])
        if rebuilt["events"] != stored["events"] or render_timeline(stored) != client.memory_summary:
            return None, [], "timeline_source_changed"
        return deepcopy(proof), inputs, ""
    except (KeyError, TypeError, ValueError, TimelineError):
        return None, [], "timeline_integrity_invalid"


def _capture_timeline(client, state, owner, now):
    scope, target = state["scope"], state["dirty"]
    proof, retained_inputs, reason = _validated_timeline_head(client)
    if proof is None and (client.memory_provenance or {}).get("version") == TIMELINE_VERSION:
        raise TimelineCaptureError(reason)
    retained = proof["timeline"]["events"] if proof else []
    after = proof["capture"]["target"] if proof else None
    if after is not None and _key(after) > _key(target):
        raise TimelineCaptureError("timeline_head_after_target")
    rows, coverage = _timeline_rows(client, scope, target=target, after=after, bootstrap=proof is None)
    if not rows:
        raise TimelineCaptureError("timeline_delta_empty")
    retained_ids = {event["source_message_id"] for event in retained}
    inputs = [row for row in retained_inputs if row["source_message_id"] in retained_ids]
    previous_proofs = {row["message_id"]: row for row in proof["capture"]["sources"]} if proof else {}
    sources = [deepcopy(previous_proofs[identity]) for identity in sorted(retained_ids)]
    omissions, budget = [], TIMELINE_DELTA_CHARS
    # Admit complete source bodies only; no quote can be selected from a cut.
    for row in reversed(rows):
        text = row.text or ""
        sent = bool(text) and len(text) <= TIMELINE_SOURCE_CHARS and len(text) <= budget
        if text and not sent:
            # Advancing past an omitted correction would strand retained events
            # beyond the next read delta. Keep the old head and require a full
            # admitted input rather than silently consuming a clipped source.
            raise TimelineCaptureError("timeline_delta_char_budget")
        if sent:
            budget -= len(text)
        else:
            omissions.append({"source_message_id": row.pk, "reason": "source_text_budget" if text else "source_text_empty"})
        sources.append(_timeline_proof_row(row, scope["namespace"], sent=sent))
        if sent and row.role == "user":
            inputs.append(_timeline_dto(row, scope["namespace"]))
    sources.sort(key=lambda row: _key(row))
    inputs.sort(key=lambda row: (row["event_at"], row["source_message_id"]))
    coverage.update(char_limit=TIMELINE_DELTA_CHARS, source_char_limit=TIMELINE_SOURCE_CHARS,
        omissions=omissions, previous_head_reason="verified_timeline" if proof else reason,
        historical_order="provider_event_time_then_message_id")
    capture = {"version": TIMELINE_VERSION, "scope": scope, "target": target,
        "dirty_digest": state["dirty_digest"], "sources": sources,
        "source_interval": {"first": sources[0]["message_id"], "last": sources[-1]["message_id"]},
        "coverage": coverage, "timeline_inputs": inputs, "previous_events": retained,
        "previous_head_version": int(client.memory_version or 0),
        "previous_head_digest": _digest({"summary": client.memory_summary, "provenance": client.memory_provenance}),
        "owner": {"owner_token": owner["owner_token"], "generation": owner["generation"]},
        "captured_at": _date(now), "deadline_at": _date(now + timedelta(seconds=GENERATION_SECONDS))}
    return {**capture, "digest": _digest(capture)}


def _read_timeline_boundary(boundary):
    if not isinstance(boundary, dict):
        raise TimelineCaptureError("timeline_boundary_invalid")
    try:
        watermark = boundary["watermark"]
        stamp = parse_datetime(watermark["event_at"])
        if (type(boundary["client_id"]) is not int or boundary["client_id"] <= 0
            or not isinstance(boundary["source_namespace"], str) or not boundary["source_namespace"]
            or type(boundary["reset_floor"]) is not int or boundary["reset_floor"] <= 0
            or type(watermark["message_id"]) is not int or watermark["message_id"] < boundary["reset_floor"]
            or stamp is None or timezone.is_naive(stamp)):
            raise TimelineCaptureError("timeline_boundary_invalid")
        return {"client_id": boundary["client_id"], "source_namespace": boundary["source_namespace"],
            "reset_id": boundary["reset_id"], "reset_floor": boundary["reset_floor"],
            "erasure_epoch": boundary.get("erasure_epoch") or "",
            "watermark": {"event_at": stamp.astimezone(datetime_timezone.utc).isoformat(),
                "message_id": watermark["message_id"]}}
    except (KeyError, TypeError, ValueError):
        raise TimelineCaptureError("timeline_boundary_invalid") from None


def _timeline_clocks_valid(row):
    try:
        event = parse_datetime(row["event_at"])
        ingested = parse_datetime(row["observed_created_at"])
        provider = parse_datetime(row["provider_created_at"]) if row["provider_created_at"] is not None else None
        if event is None or ingested is None or timezone.is_naive(event) or timezone.is_naive(ingested):
            return False
        if row["provider_created_at"] is not None:
            return provider is not None and not timezone.is_naive(provider) and row["time_basis"] == "provider" and event == provider
        return row["time_basis"] == "local_ingest" and event == ingested
    except (KeyError, TypeError, ValueError):
        return False


def validate_memory_timeline_read(memory, boundary):
    """Pure request-snapshot check. Empty reason means valid; never performs I/O."""
    from management.services.ig_memory_timeline import render_timeline
    try:
        bound = _read_timeline_boundary(boundary)
        if not isinstance(memory, dict) or memory.get("reason") != "historical_as_of":
            return "timeline_read_unproven"
        proof, text = memory["provenance"], memory["text"]
        if not isinstance(text, str) or len(text) > 4000 or proof.get("version") != TIMELINE_VERSION:
            return "timeline_integrity_invalid"
        read_keys = {"read_delta", "read_boundary", "read_digest"}
        publication = {key: value for key, value in proof.items() if key not in read_keys}
        capture = proof["capture"]
        scope = capture["scope"]
        expected = {"client_id": bound["client_id"], "namespace": bound["source_namespace"],
            "reset_id": bound["reset_id"], "reset_floor": bound["reset_floor"], "erasure_at": bound["erasure_epoch"]}
        delta = proof["read_delta"]
        target = capture["target"]
        target_stamp = parse_datetime(target["event_at"])
        sources = capture["sources"]
        if (capture.get("version") != TIMELINE_VERSION
            or type(proof.get("head_version")) is not int or proof["head_version"] <= 0
            or not isinstance(capture.get("generation_input_digest"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", capture["generation_input_digest"])
            or type(target["message_id"]) is not int or target["message_id"] < bound["reset_floor"]
            or target["message_id"] > bound["watermark"]["message_id"]
            or target_stamp is None or timezone.is_naive(target_stamp)
            or not isinstance(sources, list) or not 1 <= len(sources) <= TIMELINE_DELTA_LIMIT + 8):
            return "timeline_integrity_invalid"
        seen_sources = set()
        for row in sources:
            stamp = parse_datetime(row["event_at"])
            identity = row["message_id"]
            if (set(row) != {"message_id", "source_digest", "event_at", "time_basis", "role", "scope", "chars_sent", "chars_omitted", "provider_created_at", "observed_created_at"}
                or type(identity) is not int or not bound["reset_floor"] <= identity <= target["message_id"]
                or identity in seen_sources or stamp is None or timezone.is_naive(stamp)
                or _key(row) > _key(target)
                or not isinstance(row["source_digest"], str) or not re.fullmatch(r"[0-9a-f]{64}", row["source_digest"])
                or row["role"] not in {"user", "manager", "model"}
                or row["time_basis"] not in {"provider", "local_ingest"} or row["scope"] is not None
                or type(row["chars_sent"]) is not int or row["chars_sent"] < 0
                or type(row["chars_omitted"]) is not int or row["chars_omitted"] != 0):
                return "timeline_source_invalid"
            if not _timeline_clocks_valid(row):
                return "timeline_source_invalid"
            seen_sources.add(identity)
        by_id = {row["message_id"]: row for row in sources}
        for event in proof["timeline"]["events"]:
            source = by_id.get(event["source_message_id"])
            if (source is None or source["role"] != "user"
                or any(event[key] != source[key] for key in ("source_digest", "event_at", "time_basis", "scope"))
                or _key({"message_id": event["source_message_id"], "event_at": event["event_at"]}) > _key(target)):
                return "timeline_event_source_invalid"
        if (scope != expected or proof["read_boundary"] != bound
            or proof["digest"] != _digest({key: value for key, value in publication.items() if key != "digest"})
            or proof["summary_digest"] != hashlib.sha256(text.encode()).hexdigest()
            or render_timeline(proof["timeline"]) != text
            or proof["read_digest"] != _digest({"head_digest": proof["digest"], "boundary": bound, "delta": delta})
            or _key(capture["target"]) > _key(bound["watermark"])
            or not isinstance(delta, list) or len(delta) > TIMELINE_DELTA_LIMIT
            or sum(len(row["text"]) for row in delta) > TIMELINE_DELTA_CHARS):
            return "timeline_integrity_invalid"
        seen = set()
        prior_key = _key(target)
        for row in delta:
            identity = row["source_message_id"]
            stamp = parse_datetime(row["event_at"])
            row_key = _key({"event_at": row["event_at"], "message_id": identity})
            if (type(identity) is not int or identity in seen or identity < bound["reset_floor"]
                or identity > bound["watermark"]["message_id"]
                or set(row) != {"source_message_id", "text", "event_at", "time_basis", "source_digest", "role", "scope", "provider_created_at", "observed_created_at"}
                or stamp is None or timezone.is_naive(stamp)
                or row["role"] not in {"user", "manager", "model"} or row["scope"] is not None
                or not isinstance(row["text"], str) or not isinstance(row["source_digest"], str)
                or not re.fullmatch(r"[0-9a-f]{64}", row["source_digest"])
                or row["time_basis"] not in {"provider", "local_ingest"}
                or not prior_key < row_key <= _key(bound["watermark"])):
                return "timeline_delta_invalid"
            if not _timeline_clocks_valid(row):
                return "timeline_delta_invalid"
            seen.add(identity)
            prior_key = row_key
        return ""
    except (KeyError, TypeError, ValueError, AttributeError):
        return "timeline_integrity_invalid"


def read_memory_timeline(client, *, boundary, sealed_sources=(), history=()):
    """Read verified historical events plus a complete bounded sealed delta.

    Caller collections never grant ownership or completeness: the adapter checks
    the authoritative source interval itself. It neither modifies a head nor
    invokes a provider, and never borrows an older head when the current fails.
    """
    if not _timeline_enabled():
        return MemoryRead(reason="timeline_disabled")
    try:
        bound = _read_timeline_boundary(boundary)
        client_id = getattr(client, "pk", client)
        if type(client_id) is not int or client_id != bound["client_id"]:
            return MemoryRead(reason="timeline_boundary_invalid")
        current = IgClient.objects.filter(pk=client_id).first()
        if current is None:
            return MemoryRead(reason="client_missing")
        if current.privacy_erasure_started_at:
            return MemoryRead(reason="client_erasing")
        if current.hidden_at:
            return MemoryRead(reason="client_hidden")
        scope = _scope(current, bound["source_namespace"], timeline=True)
        expected = {"client_id": bound["client_id"], "namespace": bound["source_namespace"],
            "reset_id": bound["reset_id"], "reset_floor": bound["reset_floor"], "erasure_at": bound["erasure_epoch"]}
        if scope != expected:
            return MemoryRead(reason="timeline_scope_changed")
        proof, _inputs, reason = _validated_timeline_head(current)
        if proof is None:
            return MemoryRead(reason=reason)
        if (proof["capture"]["scope"] != scope or _key(proof["capture"]["target"]) > _key(bound["watermark"])
            or proof["capture"]["target"]["message_id"] > bound["watermark"]["message_id"]):
            return MemoryRead(reason="timeline_head_after_sealed_watermark")
        rows, _coverage = _timeline_rows(current, scope, target=bound["watermark"], after=proof["capture"]["target"])
        delta = [_timeline_dto(row, scope["namespace"]) for row in rows]
        if sum(len(row["text"]) for row in delta) > TIMELINE_DELTA_CHARS:
            return MemoryRead(reason="timeline_delta_char_budget")
        proof["read_delta"] = delta
        proof["read_boundary"] = bound
        proof["read_digest"] = _digest({"head_digest": proof["digest"], "boundary": bound, "delta": delta})
        view = {"text": current.memory_summary, "reason": "historical_as_of", "provenance": proof}
        reason = validate_memory_timeline_read(view, boundary)
        if reason:
            return MemoryRead(reason=reason)
        latest = IgClient.objects.filter(pk=client_id).first()
        if (latest is None or latest.hidden_at or latest.privacy_erasure_started_at
            or latest.memory_version != current.memory_version
            or latest.memory_provenance != current.memory_provenance
            or _scope(latest, bound["source_namespace"], timeline=True) != scope):
            return MemoryRead(reason="timeline_head_or_scope_changed")
        return MemoryRead(current.memory_summary, "historical_as_of", proof)
    except TimelineCaptureError as exc:
        return MemoryRead(reason=exc.code)
    except (KeyError, TypeError, ValueError):
        return MemoryRead(reason="timeline_integrity_invalid")
