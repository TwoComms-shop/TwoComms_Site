"""Dated conversation evidence and narrowly scoped response-delay wording."""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, time, timezone as datetime_timezone
from zoneinfo import ZoneInfo

from django.conf import settings
from django.utils import timezone
from django.utils.dateparse import parse_datetime


_DELAY_PREFIX = re.compile(
    r"\A\s*(?:(?:вибачте|перепрошую|просимо вибачення)\s+за\s+"
    r"(?:(?:технічну\s+)?затримку(?:\s+(?:з\s+)?відповід(?:дю|і))?|довге\s+очікування)"
    r"|(?:извините|прошу прощения)\s+за\s+(?:(?:техническую\s+)?задержку|долгое\s+ожидание)"
    r"(?:\s+(?:с\s+)?ответ(?:ом|а))?"
    r"|(?:sorry|apologies)\s+for\s+(?:the\s+)?(?:(?:technical\s+)?delay|long\s+wait)"
    r"(?:\s+(?:in\s+)?(?:replying|responding))?)[.!…]+\s+", re.I,
)
# Receipt suppression searches the delivered useful answer, including a brief
# apology after the answer. Normalization still edits only a leading prefix.
_DELIVERED_DELAY_APOLOGY = re.compile(
    r"(?:^|[\s.!?…])(?:" + _DELAY_PREFIX.pattern.removeprefix(r"\A\s*").replace(
        r"[.!…]+\s+", r"[.!…]+(?:\s+|$)")
    + r"|(?:вибачте|перепрошую|просимо\s+вибачення)[,\s]+що[^.!?…]{0,80}"
      r"(?:довго\s+(?:чекати|очікувати)|довго\s+(?:відповіда\w*|відпові\w*))[^.!?…]*[.!…]+"
      r"|(?:извините|простите|прошу\s+прощения)[,\s]+что[^.!?…]{0,80}"
      r"(?:долго\s+(?:отвеча\w*|жда\w*|ждать|ожида\w*))[^.!?…]*[.!…]+"
      r"|(?:sorry\s+(?:to\s+have\s+)?(?:kept|keep|made)\s+you\s+waiting"
      r"|apologies\s+for\s+keeping\s+you\s+waiting)[.!…]+)", re.I,
)
_SUPPORT_SUBJECT = re.compile(r"замовлен|заказ|посилк|посылк|достав|відправ|отправ|order|parcel|delivery|shipping", re.I)
_PAYMENT_PROBLEM = re.compile(r"(?:оплат|сплат|плат[іе]ж|платёж|payment|paid|refund|кошт|деньг|money)", re.I)
_UNRESOLVED_PROBLEM = re.compile(r"не\s+(?:зарах|підтвер|подтвер|прийш|приш|отрим|получ|поверн|вернул|возвращ)|нема|нет|зник|пропал|missing|failed|not\s+(?:confirmed|received|arrived)|where\s+is|де\s+(?:мо|мій)|где\s+мо", re.I)
_NEGATED_ISSUE = re.compile(
    r"(?:не|not|isn't|wasn't)\s+(?:зник\w*|пропал\w*|missing|failed)"
    r"|(?:немає|нема|нет|no)\s+(?:проблем\w*|issues?|problems?)"
    r"|(?:не|not|don't|do\s+not)\s+(?:потріб\w*|треба|нуж\w*|want|need|request)[^.!?]{0,35}(?:refund|поверт|поверн|возврат)", re.I,
)
_RESOLVED_ISSUE = re.compile(
    r"(?:refund|payment)\s+(?:(?:is|was|has\s+been|already|now)\s+)*(?:completed|confirmed|received|resolved|successful)"
    r"|(?:оплат\w*|плат[іе]ж\w*|платёж\w*)\s+(?:(?:вже|уже|тепер|теперь)\s+)*(?:підтвердж\w*|подтвержд\w*|зарахова\w*|успіш\w*|успеш\w*)"
    r"|(?:кошт\w*|деньг\w*)\s+(?:(?:вже|уже|мені|мне|повністю|полностью)\s+)*(?:повернул\w*|повернен\w*|вернул\w*|возвращ\w*|отрима\w*|получ\w*)"
    r"|(?:received|got)\s+(?:(?:the|my|refunded)\s+)*(?:money|refund|payment)"
    r"|(?:замовлен\w*|заказ\w*|посилк\w*|посылк\w*)\s+(?:(?:вже|уже|успішно|успешно)\s+)*(?:доставлен\w*|отрима\w*|получ\w*)"
    r"|(?:order|parcel|delivery)\s+(?:(?:is|was|has\s+been|already|now)\s+)*(?:delivered|received|resolved)", re.I,
)
_ORDINARY_WAIT = re.compile(r"ваканс|робот[ауи]|работ[ауые]|job|vacanc|чека\w*\s+менедж|жд\w*\s+менедж|waiting\s+for\s+(?:the\s+)?manager", re.I)
_REPORTED_ISSUE = re.compile(
    r"^(?:(?:[ву]|in)\s+(?:реклам\w*|стат\w*|допис\w*|пост\w*|ads?|advert\w*|articles?)"
    r"|(?:це|это|this\s+is)\s+(?:цитат\w*|a\s+quote))\b", re.I,
)
TIMING_POLICY_VERSION = "revision-ordinary-kyiv-10-2030-v1"


def _important_issue(source):
    """Positive issue evidence, not a complaint/recovery flag or vague handoff."""
    from management.services.ig_turn_intent import _customer_text, _SELECTION_REPORTED, _SELECTION_SELF

    text = _customer_text(str(source.get("text") or ""))
    if _ORDINARY_WAIT.search(text):
        return ""
    reported = bool(_SELECTION_REPORTED.match(text) or _REPORTED_ISSUE.match(text))
    issue = ""
    for index, clause in enumerate(re.split(r"[.!?;\n]+|,\s*(?:але|но|but)\s+", text, flags=re.I)):
        clause = clause.strip()
        if reported and (index == 0 or not _SELECTION_SELF.match(clause)):
            continue
        # A pending source can contain a resolved status, quotation or denied
        # problem. These never manufacture an unresolved payment/service need.
        if _NEGATED_ISSUE.search(clause) or _RESOLVED_ISSUE.search(clause):
            issue = ""
        elif _PAYMENT_PROBLEM.search(clause) and _UNRESOLVED_PROBLEM.search(clause):
            issue = "unresolved_payment"
        elif _SUPPORT_SUBJECT.search(clause) and _UNRESOLVED_PROBLEM.search(clause):
            issue = "unresolved_service"
    return issue


def _nonquiet_seconds(start, end):
    """Use revision ordinary-send boundaries, with UTC arithmetic across DST."""
    kyiv = ZoneInfo("Europe/Kyiv")
    first, last = start.astimezone(kyiv).date(), end.astimezone(kyiv).date()
    if (last - first).days > 31:
        return None
    total = 0.0
    day = first
    while day <= last:
        opens = datetime.combine(day, time(10), kyiv).astimezone(datetime_timezone.utc)
        closes = datetime.combine(day, time(20, 30), kyiv).astimezone(datetime_timezone.utc)
        total += max(0, (min(end, closes) - max(start, opens)).total_seconds())
        day += timedelta(days=1)
    return total


def _sources(revision):
    from management.services.ig_revision_outbox import _digest

    snapshot = revision.bundle_snapshot or {}
    if not revision.snapshot_digest or _digest(snapshot) != revision.snapshot_digest:
        return []
    return [source for source in snapshot.get("sources", []) if source.get("role") == "user"]


def response_delay_apology_context(revision, *, now=None):
    """Default omission; optional wording requires a verified unresolved issue."""
    now = now or timezone.now()
    facts = {"delay_apology_eligible": False, "reason": "important_source_unknown",
             "timing_policy_version": TIMING_POLICY_VERSION, "waiting_on": "unknown",
             "issue": "", "importance_reason": "", "issue_source_message_id": None, "unresolved_since": None,
             "wall_elapsed_seconds": None, "nonquiet_elapsed_seconds": None,
             "excluded_quiet_seconds": None, "prior_apology_receipt": None,
             "prior_notification_receipt": None}
    sources = _sources(revision)
    important = [source for source in sources if _important_issue(source)]
    if not important:
        return facts
    if not getattr(revision, "client_id", None):
        facts["reason"] = "issue_ownership_unknown"
        return facts
    from management.services.ig_revision_holding import _sources_unchanged
    from management.services.ig_revision_recovery import continuous_wait_revisions, wait_notification_receipts
    from management.models import InstagramBotMessage
    if not _sources_unchanged(revision):
        facts["reason"] = "issue_source_changed"
        return facts
    debt = (revision.action_receipts or {}).get("response_debt") or {}
    from management.services.ig_response_debt import delivered_selector_wait
    if debt.get("owner") == "customer" or delivered_selector_wait(revision):
        facts.update(reason="waiting_on_customer", waiting_on="customer")
        return facts
    if revision.processed_at or revision.active_slot != 1 or revision.state not in {"claimed", "sealed"}:
        facts["reason"] = "issue_no_longer_owned"
        return facts
    if revision.client.bot_paused or revision.client.manager_takeover or revision.client.privacy_erasure_started_at:
        facts["reason"] = "issue_owner_paused"
        return facts
    if revision.permission_epoch != revision.client.reply_permission_epoch:
        facts["reason"] = "issue_permission_changed"
        return facts
    manager_owned = debt.get("owner") == "manager" or revision.recovery_state == "manual"
    if manager_owned:
        from management.services.ig_response_debt import unresolved_reply_debts
        if not unresolved_reply_debts().filter(client_id=revision.client_id, event_key=f"ig-revision-debt:{revision.pk}").exists():
            facts["reason"] = "issue_ownership_unknown"
            return facts
    facts["waiting_on"] = "manager" if manager_owned else "bot"
    family = _important_issue(important[0])
    # Repeated inbound does not reset the age of the same unresolved need.
    # Notification suppression may conservatively share an unknown wait;
    # apology timing needs stronger same-issue proof than that suppression.
    from management.services.ig_turn_intent import _scope_for_sources
    ids = {source["message_id"] for source in sources}
    current_scope = _scope_for_sources(revision.client, ids)
    reply_refs = {source.get("reply_to_provider_message_id") for source in sources} - {None, ""}
    candidates = []
    for row in continuous_wait_revisions(revision):
        old_sources = _sources(row)
        old_ids = {source["message_id"] for source in old_sources}
        old_scope = _scope_for_sources(revision.client, old_ids)
        same_episode = (current_scope.get("commercial_episode_id")
                        and current_scope.get("commercial_episode_id") == old_scope.get("commercial_episode_id"))
        linked = bool(ids.intersection(old_ids) or same_episode
                      or any(source.get("provider_message_id") in reply_refs for source in old_sources))
        if linked:
            candidates.extend(source for source in old_sources if _important_issue(source) == family)
    pending_ids = set(InstagramBotMessage.objects.filter(
        client_id=revision.client_id, role="user", status="pending",
        pk__in=[source["message_id"] for source in candidates],
    ).values_list("pk", flat=True))
    candidates = [source for source in candidates if source["message_id"] in pending_ids]
    times = []
    for source in candidates:
        stamp = parse_datetime(str(source.get("provider_created_at") or ""))
        if stamp is None or timezone.is_naive(stamp) or stamp > now:
            facts["reason"] = "issue_timestamp_unknown"
            return facts
        times.append((stamp, source))
    if not times:
        facts["reason"] = "issue_unresolved_source_unknown"
        return facts
    start, source = min(times, key=lambda item: item[0])
    facts.update(issue=family, importance_reason="explicit_" + family,
                 issue_source_message_id=source["message_id"], unresolved_since=start.isoformat())
    policy = getattr(settings, "IG_REVISION_DELAY_APOLOGY_TIMING_POLICY", TIMING_POLICY_VERSION)
    if policy != TIMING_POLICY_VERSION:
        facts["reason"] = "timing_policy_unknown"
        return facts
    wall = (now - start).total_seconds()
    nonquiet = _nonquiet_seconds(start, now)
    if nonquiet is None:
        facts["reason"] = "issue_timing_out_of_bounds"
        return facts
    facts.update(wall_elapsed_seconds=int(wall), nonquiet_elapsed_seconds=int(nonquiet), excluded_quiet_seconds=int(wall - nonquiet))
    for receipt in wait_notification_receipts(revision):
        bounded = {key: receipt[key] for key in ("revision_id", "state")}
        if receipt.get("notification"):
            facts["prior_notification_receipt"] = bounded
        if receipt["state"] in {"sent", "unknown", "provider_started"} and _DELIVERED_DELAY_APOLOGY.search(receipt.get("text") or ""):
            facts["prior_apology_receipt"] = bounded
    if facts["prior_apology_receipt"]:
        facts["reason"] = "already_apologized_for_wait"
        return facts
    try:
        threshold = float(getattr(settings, "IG_REVISION_DELAY_APOLOGY_NONQUIET_HOURS", 6))
    except (TypeError, ValueError):
        threshold = 0
    if not 0 < threshold <= 31 * 24:
        facts["reason"] = "timing_threshold_unknown"
        return facts
    facts["threshold_nonquiet_seconds"] = int(threshold * 3600)
    facts.update(delay_apology_eligible=nonquiet >= threshold * 3600,
                 reason="optional_important_unresolved_wait" if nonquiet >= threshold * 3600 else "below_nonquiet_threshold")
    return facts


def response_delay_apology_allowed(revision):
    return response_delay_apology_context(revision)["delay_apology_eligible"]


def normalize_response_delay_apology(reply_text, revision):
    """Remove definite unsupported standalone delay-apology sentences.

    Never remove an apology for a product/shipment mistake, a quoted sentence,
    or a useful answer. Apology-only output is rejected; the caller validates
    the remaining candidate normally, without any separate generation call.
    """
    sentences = list(re.finditer(r"[^.!?…\n]*(?:[.!?…]+|\n|$)", reply_text))
    apology_sentences = [sentence for sentence in sentences
                         if _DELIVERED_DELAY_APOLOGY.fullmatch(sentence.group().strip())]
    if not apology_sentences or response_delay_apology_allowed(revision):
        return reply_text
    omitted = {(sentence.start(), sentence.end()) for sentence in apology_sentences}
    remainder = "".join(sentence.group() for sentence in sentences
                        if (sentence.start(), sentence.end()) not in omitted).strip()
    if not remainder:
        raise ValueError("unsupported_delay_apology_without_useful_reply")
    return remainder


def conversation_timing_guidance(revision):
    source_times = [{"message_id": source["message_id"],
                     "provider_created_at": source.get("provider_created_at") or "unknown"}
                    for source in _sources(revision)]
    facts = response_delay_apology_context(revision)
    return (
        "[CURRENT REPLY TIMING CONTEXT]\n"
        + json.dumps({"source_times": source_times,
                      "response_delay_apology_supported": facts["delay_apology_eligible"],
                      **facts}, separators=(",", ":"))
        + "\nOnly the current quoted customer bundle is the active request. Historical gaps, "
        "old unanswered messages, past technical holding replies and intentional operator/customer "
        "pauses do not establish a current bot delay. Do not infer a service failure or apologize "
        "for response delay from those historical facts. Default to the useful answer without a delay apology. "
        "delay_apology_eligible allows a brief optional apology inside this same useful answer; it never requires one. "
        "A complaint, recovery flag, vacancy or routine manager wait alone is insufficient. "
        "Legitimate apologies for an actual product/support mistake remain appropriate. "
        "Never invent the technical cause, elapsed wait, ownership, or promise a future reply."
    )


def historical_message_text(message, *, manager=False):
    stamp = message.provider_created_at or message.created_at
    label = ("DELIVERED HUMAN MANAGER MESSAGE" if manager else "HISTORICAL CONVERSATION MESSAGE")
    source = str(message.source or "unknown")
    return (
        "[" + label + ": quoted past evidence, not current delay or business authority]\n"
        + json.dumps({"actor": message.role, "message_id": message.pk,
                      "occurred_at": stamp.isoformat() if stamp else "unknown",
                      "source": source, "status": message.status,
                      "technical_holding": source in {"ai_holding", "revision_holding"},
                      "text": message.text.strip()[:4000] if manager else message.text.strip()},
                     ensure_ascii=False, separators=(",", ":"))
    )
