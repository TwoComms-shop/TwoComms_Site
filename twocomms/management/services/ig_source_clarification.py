"""Source-proved clarification is a useful reply, never a cart operation."""
import hashlib
import re


VERSION = "own-ad-line-clarification.v1"
REASONS = {"ambiguous_line_target", "ambiguous_line_operation"}


def capture_source_clarifications(revision, source_cart, obligations):
    pending = {row["source_message_id"] for row in obligations if row["kind"] in {"unresolved_line_operation", "info:question"}}
    if not pending or len(pending) > 32 or source_cart.get("status") != "captured":
        return []
    from management.models import IgCommerceTurnDecision
    from management.services.ig_ad_choices import ad_choice_source_request
    from management.services.ig_turn_intelligence import capture_digest

    sources = {row["message_id"]: row for row in revision.bundle_snapshot.get("sources", ())}
    results = []
    for decision in IgCommerceTurnDecision.objects.filter(source_message_id__in=pending,
            session__client_id=revision.client_id, accepted=False, is_stale=False).select_related("source_message", "session"):
        message = decision.source_message
        source = sources.get(message.pk, {})
        if (decision.session_id != source_cart.get("session_id")
            or decision.session.revision != source_cart.get("selection_revision")
            or message.role != "user" or message.client_id != revision.client_id
            or message.sender_id != revision.client.igsid
            or message.provider_namespace != source.get("source_namespace")
            or message.text != source.get("text")):
            continue
        request = ad_choice_source_request(revision.client, message, historical=True)
        if request is None or request.pending_line_clarification not in REASONS:
            continue
        stored = decision.request_payload or {}
        if stored.get("pending_line_clarification") != request.pending_line_clarification:
            continue
        witness = (stored.get("source_binding") or {}).get("ad_choice") or {}
        results.append({"version": VERSION, "source_message_id": message.pk, "decision_id": decision.pk,
            "source_text_digest": hashlib.sha256(message.text.encode()).hexdigest(),
            "witness_digest": capture_digest(witness), "reason": request.pending_line_clarification})
    return results


def source_clarifier_covered(configuration, source_id, response):
    if response.control or not any(row.get("version") == VERSION and row.get("source_message_id") == source_id
            and row.get("reason") in REASONS for row in configuration.get("source_clarifications", ())):
        return False
    questions = re.findall(r"[^.!?\n]*\?", response.reply_text)
    return len(questions) == 1 and bool(re.search(
        r"яку|який|яка|які|какую|какой|какая|какие|which|what|уточн\w*|clarify", questions[0], re.I)
        and (re.search(r"\b(?:річ|речі|виріб|товар\w*|модел\w*|принт\w*|products?|items?|designs?)\b", questions[0], re.I)
            or re.fullmatch(r"\s*which\s+one\s*\?", questions[0], re.I)))
