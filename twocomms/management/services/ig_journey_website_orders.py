"""Source-cited website/channel statements; never order or channel authority."""
import re
from copy import deepcopy

from django.db import DatabaseError
from django.db.models import Subquery, Value, BigIntegerField
from django.db.models.functions import Coalesce, Substr

from management.models import InstagramBotMessage, IgFunnelResetAudit

LIMIT = 200
PRODUCER = "website_order_report"
CHANNEL_PRODUCER = "channel_contact_report"
_SITE = re.compile(r"(?:на|з|с|через)\s+(?:ваш\w*\s+)?сайт\w*|(?:on|through|from)\s+(?:your\s+|the\s+)?website", re.I)
_PURCHASE = re.compile(r"\b(?:я\s+(?:вже\s+|уже\s+)?(?:замовив|замовила|замовляв|замовляла|заказал|заказала|заказывал|заказывала|купив|купила|купил|(?:зробив|зробила|зробили)\s+замовлення|(?:сделал|сделала|сделали)\s+заказ|оформив|оформила|оформил)|i\s+(?:already\s+)?(?:ordered|bought|placed\s+an?\s+order))\b", re.I)
_NEGATIVE = re.compile(r"\b(?:не|not|never|якщо|если|if)\b", re.I)


def is_website_order_statement(text):
    # Deliberately narrow: first-person past-tense claim in the same sentence.
    # Questions, hypotheticals, quoted/model text cannot establish an order.
    for sentence in re.split(r"[.!?\n]+", text[:4000]):
        purchase = _PURCHASE.search(sentence)
        if _SITE.search(sentence) and purchase and not _NEGATIVE.search(sentence[:purchase.end()]):
            return True
    return False


_CHANNEL = re.compile(r"\b(?:telegram|телеграм\w*|тг)\b", re.I)
_CHANNEL_PAST = re.compile(r"\b(?:я\s+(?:вже\s+|уже\s+)?(?:написав|написала|написал|перейшов|перейшла|перешел|перешла)|i\s+(?:already\s+)?(?:messaged|wrote))\b", re.I)
_CHANNEL_REQUEST = re.compile(r"\b(?:давайте|напишіть|напишите|(?:зможете|можете)\s+(?:файлом\s+)?(?:скинути|скинуть|відправити|отправить)|можна\s+(?:вам\s+)?написати|можно\s+(?:вам\s+)?написать)\b", re.I)


def channel_statement(text):
    for sentence in re.split(r"[.!?\n]+", text[:4000]):
        if not _CHANNEL.search(sentence) or _NEGATIVE.search(sentence):
            continue
        if _CHANNEL_PAST.search(sentence):
            return "reported"
        if _CHANNEL_REQUEST.search(sentence):
            return "requested"
    return None


def _append_channel_context(result, rows, client_id):
    reports = [(r, channel_statement(r["excerpt"])) for r in rows]
    reports = [(r, kind) for r, kind in reports if kind]
    if not reports:
        return
    row, kind = reports[0]
    speaker = "Менеджер" if row.get("role") == "manager" else "Клієнт"
    refs = [{"kind": "message", "id": r["id"]} for r, _ in reports[:8]]
    node_id = f"channel-contact-report:{client_id}"
    summary = (speaker + " повідомив, що написав у Telegram. Повідомлення в іншому каналі не перевірено."
               if kind == "reported" else speaker + " запропонував продовжити у Telegram. Фактичний перехід ще не підтверджено.")
    result["nodes"].append({
        "id": node_id, "semantic_key": CHANNEL_PRODUCER, "producer": CHANNEL_PRODUCER,
        "scope": "client", "episode_id": None, "current": False, "state": "partial",
        "label": "Telegram · звернення" if kind == "reported" else "Telegram · запит переходу",
        "short_label": "Telegram", "presentation_kind": "reported", "tone": "manager",
        "summary": summary, "evidence_refs": refs,
        "channel_report": {"channel": "telegram", "status": kind},
        "verification": "needs_channel_check",
        "facts": [{"id": node_id + ":message", "label": "Зі слів менеджера" if row.get("role") == "manager" else "Зі слів клієнта",
                   "value": row["excerpt"][:700], "state": "partial", "source": "manager_message" if row.get("role") == "manager" else "client_message",
                   "captured_at": row["event_at"].isoformat(), "evidence_refs": refs[:1]}],
        "layout": {"rank": 3, "lane": 2},
    })
    result["overview_node_ids"].append(node_id)
    inbound = [n for n in result["nodes"] if n.get("semantic_key") == "inbound"]
    if len(inbound) == 1:
        result["edges"].append({
            "id": node_id + ":context", "from_node_id": inbound[0]["id"], "to_node_id": node_id,
            "relation": "client_report_context", "authority": "manager_statement" if row.get("role") == "manager" else "customer_statement",
            "condition_label": speaker + " · Telegram", "summary": summary,
            "evidence_refs": refs, "tone": "manager",
        })
    result["coverage"]["channel_reports"] = {"status": kind, "returned": len(reports), "displayed": 1}


def append_website_order_reports(graph, *, client_id, is_history):
    result = deepcopy(graph)
    stale = {n["id"] for n in result["nodes"] if n.get("producer") in {PRODUCER, CHANNEL_PRODUCER}}
    result["nodes"] = [n for n in result["nodes"] if n["id"] not in stale]
    result["edges"] = [e for e in result["edges"] if e["from_node_id"] not in stale and e["to_node_id"] not in stale]
    result["overview_node_ids"] = [v for v in result.get("overview_node_ids", []) if v not in stale]
    coverage = {"status": "historical_view" if is_history else "missing_source", "limit": LIMIT, "truncated": False}
    result.setdefault("coverage", {})["website_order_reports"] = coverage
    result["coverage"]["channel_reports"] = dict(coverage)
    if is_history or type(client_id) is not int or client_id <= 0:
        return result
    try:
        reset = IgFunnelResetAudit.objects.filter(client_id=client_id).order_by("-pk").values("reset_after_message_id")[:1]
        floor = Coalesce(Subquery(reset, output_field=BigIntegerField()), Value(0), output_field=BigIntegerField()) + 1
        rows = list(InstagramBotMessage.objects.filter(client_id=client_id, role__in=["user", "manager"], id__gte=floor)
                    .exclude(status=InstagramBotMessage.Status.FAILED)
                    .annotate(event_at=Coalesce("provider_created_at", "created_at"), excerpt=Substr("text", 1, 4000))
                    .order_by("-event_at", "-id").values("id", "role", "excerpt", "event_at")[:LIMIT + 1])
    except DatabaseError:
        coverage["status"] = "unavailable"
        result["coverage"]["channel_reports"]["status"] = "unavailable"
        return result
    coverage["truncated"] = len(rows) > LIMIT
    result["coverage"]["channel_reports"]["truncated"] = coverage["truncated"]
    _append_channel_context(result, rows[:LIMIT], client_id)
    reports = [r for r in rows[:LIMIT] if r["role"] == "user" and is_website_order_statement(r["excerpt"])]
    if not reports:
        return result
    # One card summarises statements, without pretending repeated mentions are orders.
    refs = [{"kind": "message", "id": r["id"]} for r in reports[:8]]
    node_id = f"website-order-report:{client_id}"
    linked = any(n.get("semantic_key") == "client_order_context" for n in result["nodes"])
    summary = ("Клієнт повідомив про замовлення із сайту. Є пов’язані замовлення; потрібно звірити, про яке йдеться."
               if linked else "Клієнт повідомив про замовлення із сайту. Номер і належність замовлення ще потрібно звірити.")
    result["nodes"].append({
        "id": node_id, "semantic_key": "website_order_report", "producer": PRODUCER,
        "scope": "client", "episode_id": None, "current": False,
        "label": "Звернення щодо замовлення із сайту", "short_label": "Замовив із сайту",
        "presentation_kind": "reported", "state": "partial", "tone": "manager",
        "summary": summary, "evidence_refs": refs,
        "facts": [{"id": node_id + ":message", "label": "Зі слів клієнта",
                   "value": reports[0]["excerpt"][:700], "state": "partial", "source": "client_message",
                   "captured_at": reports[0]["event_at"].isoformat(), "evidence_refs": refs[:1]}],
        "verification": "needs_order_match", "layout": {"rank": 2, "lane": 2},
    })
    result["overview_node_ids"].append(node_id)
    inbound = [n for n in result["nodes"] if n.get("semantic_key") == "inbound"]
    if len(inbound) == 1:
        result["edges"].append({
            "id": node_id + ":context", "from_node_id": inbound[0]["id"], "to_node_id": node_id,
            "relation": "client_report_context", "authority": "customer_statement",
            "condition_label": "Звернення про замовлення", "summary": summary,
            "evidence_refs": refs, "tone": "manager",
        })
    coverage.update(status="reported", returned=len(reports), displayed=1)
    return result
