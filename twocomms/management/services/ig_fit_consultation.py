"""Source-scoped fit consultation without selecting another print or a sale."""
from __future__ import annotations

from copy import deepcopy
import re

_FIT = {
    "classic": re.compile(r"\b(?:regular|classic|ordinary|класич\w*|классичес\w*|звичайн\w*|обычн\w*)\b", re.I),
    "oversize": re.compile(r"\b(?:oversized?|оверсайз\w*)\b", re.I),
}
_FIT_WORD = re.compile(r"\b(?:крій|крою|крої|крой|крои|кро[её]м|посадк\w*|фасон\w*|fits?|cuts?)\b", re.I)
_QUESTION = re.compile(r"\?|\b(?:які|який|яка|чи|какие|какой|какая|різниц\w*|разниц\w*|difference|поясн\w*|объясн\w*|підкаж\w*|подскаж\w*|розкаж\w*|расскаж\w*|how|what|which)\b", re.I)
_PHOTO = re.compile(r"\b(?:фото\w*|приклад\w*|пример\w*|photos?|pictures?|images?|examples?)\b", re.I)
_PHOTO_CANCEL = re.compile(r"\b(?:не\s+(?:треба|потрібн\w*|надо|нужн\w*)|no|without)\b[^.!?\n]{0,20}(?:фото|приклад|пример|photos?|examples?)"
    r"|\b(?:не\s+(?:надсила\w*|надішл\w*|показ\w*|присыл\w*|отправл\w*)|do\s+not\s+(?:send|show)|don['’]t\s+(?:send|show))[^.!?\n]{0,30}(?:фото|приклад\w*|пример\w*|photos?|examples?)"
    r"|\b(?:фото|приклад\w*|пример\w*|photos?|examples?)[^.!?\n]{0,15}\b(?:не\s+(?:треба|потрібн\w*|надо|нужн\w*|надсила\w*|показ\w*|присыл\w*|отправл\w*)|not\s+needed)\b", re.I)
_AUDIENCE = re.compile(r"\b(?:унісекс|унисекс|unisex|жіноч\w*|женск\w*|чоловіч\w*|мужск\w*|жінок|женщин|дівчат\w*|девуш\w*|women|womens|female|mens|male)\b", re.I)
_ASSORTMENT = re.compile(r"\b(?:(?:інші|інший|другие|другой|другую|other|another|different)\s+(?:принт\w*|дизайн\w*|модел\w*|designs?|prints?|products?)|асортимент\w*|ассортимент\w*|(?:весь|увесь|весь|whole|full)\s+(?:каталог|catalog))\b", re.I)
_TSHIRT = re.compile(r"\b(?:футбол\w*|t[ -]?shirts?|tee(?:s)?)\b", re.I)
_OTHER_GARMENT = re.compile(r"\b(?:худі|худи|hoodies?|світшот\w*|свитшот\w*|sweatshirts?)\b", re.I)
_EXAMPLE_LABEL = re.compile(r"\b(?:приклад\w*|пример\w*|examples?)\b", re.I)
_OTHER_DESIGN = re.compile(r"\b(?:інш\w*\s+(?:принт\w*|дизайн\w*)|друг\w*\s+(?:принт\w*|дизайн\w*)|(?:another|different|other)\s+(?:print|design))\b", re.I)
_SHOW = re.compile(r"\b(?:покаж\w*|надішл\w*|пришл\w*|show|send)\b", re.I)
_CHART = re.compile(r"таблиц\w*\s+(?:розмір\w*|размер\w*)|розмірн\w*\s+(?:таблиц\w*|сіт\w*)|размерн\w*\s+(?:таблиц\w*|сет\w*)|size\s+(?:chart|guide)", re.I)


def _text(value):
    value = re.sub(r'"[^"\n]*"|«[^»\n]*»|“[^”\n]*”', " ", str(value or ""))
    return re.sub(r"\b(є|есть)(жіноч|женск|чоловіч|мужск)", r"\1 \2", value, flags=re.I)


def contextual_fit_choice(client, source):
    """Bind a terse fit answer to the immediately prior confirmed selector.

    This proves one customer preference only. A separately captured discussed
    product and fresh selection action are still required to select a SKU.
    """
    from management.models import InstagramBotMessage
    from management.services.ig_conversation_routes import conversation_route_reset_floor
    from management.services.ig_revision_outbox import _digest

    if (not getattr(source, "pk", None) or source.client_id != client.pk or source.sender_id != client.igsid
        or source.role != "user" or source.source != "webhook" or not source.provider_namespace
        or source.quick_reply_payload or source.status == "failed"):
        return {}
    answer = str(source.text or "").strip().casefold().strip(".! ")
    fit = ({"звична": "classic", "звичайна": "classic", "обычная": "classic", "привычная": "classic",
        "вільна": "oversize", "вільніша": "oversize", "свободная": "oversize", "свободнее": "oversize",
        "roomier": "oversize"}).get(answer)
    if not fit:
        return {}
    floor = conversation_route_reset_floor(client.pk)
    question = InstagramBotMessage.objects.filter(client_id=client.pk, pk__gte=floor, pk__lt=source.pk,
        role__in=("user", "model", "manager"), provider_namespace=source.provider_namespace).exclude(
            status="failed").exclude(text="").order_by("-pk").first()
    if (question is None or question.role not in {"model", "manager"} or question.status != "done"
        or question.send_state in {"unknown", "sending", "failed"}
        or not (question.provider_message_id or (question.source in {"webhook", "poll", "echo"} and question.mid))
        or (question.role == "model" and question.send_state != "sent")
        or (source.provider_created_at and question.provider_created_at and source.provider_created_at < question.provider_created_at)):
        return {}
    if question.source == "human_reply":
        from management.services.ig_turn_capture import _human_history_receipt_times

        if question.pk not in _human_history_receipt_times([question], reset_floor=floor):
            return {}
    text = _text(question.text)
    if (not re.search(r"\b(?:класик\w*|классик\w*|класич\w*|классичес\w*|classic|regular)\b", text, re.I)
        or not _FIT["oversize"].search(text)
        or not any(_FIT_WORD.search(clause) and "?" in clause for clause in re.findall(r"[^.!\n]+[?]?", text))):
        return {}
    pair = (r"\b(?:класик\w*|классик\w*|класич\w*|классичес\w*|classic|regular)\b"
        r"(?:\s+(?:крій|крой|посадка|fit|cut))?(?:\s*\((?:regular|classic)\))?\s*"
        r"(?:[—–:=]\s*|(?:має|имеет|has|це|это|is)\s+)(?:\w+\s+){0,2}(?:звичн\w*|звичайн\w*|обычн\w*|привычн\w*|regular)\b"
        if fit == "classic" else r"\b(?:oversized?|оверсайз\w*)\b\s*(?:[—–:=]\s*|(?:має|имеет|has|це|это|is)\s+)"
        r"(?:\w+\s+){0,2}(?:вільн\w*|свободн\w*|roomier|relaxed)\b")
    if not re.search(pair, text, re.I):
        return {}
    import hashlib

    proof = {"schema": "contextual-fit-choice.v1", "fit": fit, "source_message_id": source.pk,
        "source_digest": hashlib.sha256(str(source.text or "").encode()).hexdigest(),
        "question_message_id": question.pk,
        "question_provider_message_id": question.provider_message_id or question.mid,
        "question_digest": hashlib.sha256(str(question.text or "").encode()).hexdigest(),
        "source_namespace": source.provider_namespace, "reset_floor": floor,
        "selection_authority": False, "stock_authority": False, "checkout_authority": False}
    proof["proof_digest"] = _digest(proof)
    return proof


def fit_consultation_request(sources):
    """Separate examples of cuts from a request to browse different designs."""
    rows = [row for row in sources or () if row.get("role", "user") == "user"][:64]
    texts = [_text(row.get("text")) for row in rows]
    combined = "\n".join(texts)
    from management.services.ig_reply_truth import _locally_negated

    fits = [key for key, pattern in _FIT.items() if any(
        not _locally_negated(combined, match.start()) for match in pattern.finditer(combined))]
    fit_topic = bool(fits or _FIT_WORD.search(combined))
    if not fits and re.search(r"\b(?:розмір\w*|размер\w*|sizes?)\b", combined, re.I):
        fit_topic = False
    audience = bool(_AUDIENCE.search(combined))
    photos = False
    chart = False
    for text in texts:
        row_chart = bool(_CHART.search(text))
        chart = chart or row_chart
        explicit_examples = bool(re.search(r"приклад\w*|пример\w*|examples?", text, re.I))
        photo_text = re.sub(r"(?:фото|photos?|images?|pictures?)\s+(?:таблиц\w*|розмірн\w*|размерн\w*|(?:of\s+)?(?:the\s+)?size\s+chart)", "", text, flags=re.I)
        if _PHOTO_CANCEL.search(text):
            photos = False
        elif (not row_chart or explicit_examples) and (_PHOTO.search(photo_text) or (fit_topic and _SHOW.search(text))):
            photos = True
    asking = bool(_QUESTION.search(combined) or photos or chart or (fit_topic and _SHOW.search(combined))
        or (audience and re.search(r"\b(?:є|есть)\b", combined, re.I))
        or re.search(r"\b(?:у\s+вас|do\s+you\s+have|are\s+there)\b|\b(?:є|есть)\s*(?:жіноч\w*|женск\w*|чоловіч\w*|мужск\w*)", combined, re.I))
    if not (fit_topic or audience) or not asking:
        return {}
    assortment = any(not _locally_negated(combined, match.start()) for match in _ASSORTMENT.finditer(combined))
    return {"schema": "fit-consultation-request.v1", "requested": True,
        "requested_fits": fits, "comparison_requested": len(fits) > 1,
        "photos_requested": photos, "audience_requested": audience,
        "size_chart_requested": chart,
        "fit_explanation_requested": bool(fit_topic and (not chart or photos or len(fits) > 1)),
        "assortment_requested": assortment,
        "source_message_ids": [row["message_id"] for row in rows if type(row.get("message_id")) is int],
        "tshirt_source_scope": bool(_TSHIRT.search(combined) and not _OTHER_GARMENT.search(combined))}


def build_fit_consultation(sources, *, focus=None, photo_capability=None, approved_fact_keys=()):
    request = fit_consultation_request(sources)
    if not request:
        return {}
    focus = focus if isinstance(focus, dict) else {}
    focused = (focus.get("schema") == "discussed-product-focus.v1" and focus.get("status") == "focused"
        and type(focus.get("product_id")) is int and focus["product_id"] > 0
        and bool(focus.get("proof_digest")))
    result = {**request, "focus": deepcopy(focus) if focused else {},
        "selection_authority": False, "stock_authority": False, "checkout_authority": False}
    photo = photo_capability if isinstance(photo_capability, dict) else {}
    if (focused and photo.get("schema") == "fit-photo-capability.v1"
        and photo.get("product_id") == focus["product_id"] and photo.get("capability_digest") and not photo.get("error")):
        result["fit_photo_capability"] = deepcopy(photo)
    tshirt = (focus.get("category_slug") == "tshirts") if focused else request["tshirt_source_scope"]
    result["approved_fact_keys"] = [key for key in approved_fact_keys if tshirt
        and key in {"current_tshirt_bases", "current_tshirt_audience"}]
    return result


def capture_fit_consultation(revision, *, source_cart_capture=None, product_focus=None):
    """One bounded topic capture; photo metadata grants no selected-product rights."""
    sources = (revision.bundle_snapshot or {}).get("sources") or []
    if not fit_consultation_request(sources):
        return {}
    from management.services.ig_product_focus import capture_product_focus
    from management.services.approved_public_facts import approved_public_facts

    focus = product_focus if product_focus is not None else capture_product_focus(revision, source_cart_capture=source_cart_capture)
    photos = {}
    if focus.get("status") == "focused":
        from management.services.ig_revision_catalog_assets import capture_fit_photo_capability

        photos = capture_fit_photo_capability(focus["product_id"], metadata_only=True)
    facts = [fact.key for fact in approved_public_facts("uk")]
    context = build_fit_consultation(sources, focus=focus, photo_capability=photos, approved_fact_keys=facts)
    if not context.get("requested"):
        return {}
    cart = source_cart_capture or {}
    lines = cart.get("lines") or []
    if (focus.get("status") == "focused" and cart.get("status") == "captured" and len(lines) == 1
        and not (lines[0].get("pending_clarification") or lines[0].get("pending_line_clarification"))):
        fields = lines[0].get("fields") or {}
        selected = {key: row.get("value") for key, row in fields.items()
            if isinstance(row, dict) and row.get("status") == "confirmed"}
        context["next_consultation_selector"] = ("fit" if not selected.get("fit_option_code") else
            "size" if not selected.get("size") else "")
    return context


def fit_consultation_guidance(context):
    if not context:
        return ""
    return ("The customer asks about garment fit/audience or examples of cuts. Explain regular/classic versus oversize "
        "using the scoped approved T-shirt facts when applicable. Keep the source-proved discussed print as the topic; "
        "this question does not choose a different product, buy anything, or authorize payment. A request for examples "
        "of cuts is not a request for a different catalog assortment. Do not ask them to pick a print again. "
        "Only explicitly identified photos prove a cut. Available fit options do not label a photo. If the focused print "
        "has no confirmed classic photo, say that narrowly; do not say classic cut is unavailable. Another design may "
        "be shown only from approved example assets and explicitly labeled as another print illustrating the cut. "
        "The selected/discussed print remains the original one. Use current_tshirt_audience only in its T-shirt scope; "
        "do not invent separate women's/men's catalog ranges or generalize to all garments.")


def deterministic_fit_reply(context, *, language="uk"):
    """Scoped approved facts for a local fallback, without catalog/send claims."""
    from management.services.approved_public_facts import approved_public_facts

    if not context or context.get("assortment_requested"):
        return ""
    wanted = set(context.get("approved_fact_keys") or ())
    if not context.get("audience_requested"):
        wanted.discard("current_tshirt_audience")
    if not context.get("fit_explanation_requested"):
        wanted.discard("current_tshirt_bases")
    if not wanted:
        return ""
    language = language if language in {"uk", "ru", "en"} else "uk"
    facts = [fact.public_text for fact in approved_public_facts(language) if fact.key in wanted]
    focused = context.get("focus") or {}
    if not facts:
        return ""
    prefix = {"uk": "Принт залишаємо той самий: ", "ru": "Принт оставляем тот же: ",
              "en": "Keeping the same print: "}[language] + str(focused.get("title") or "") + "." if focused else ""
    photo = context.get("fit_photo_capability") or {}
    missing = photo.get("missing_fit_photos") or []
    photo_note = ""
    if context.get("photos_requested") and "classic" in missing:
        photo_note = {"uk": "Окремого підтвердженого фото цього принта у classic зараз немає; це не означає, що такий крій недоступний.",
            "ru": "Отдельного подтверждённого фото этого принта в classic сейчас нет; это не означает, что такой крой недоступен.",
            "en": "There is no separately confirmed classic-fit photo of this print; that does not mean the cut is unavailable."}[language]
    selector = context.get("next_consultation_selector")
    question = ({"uk": "Яка посадка вам ближча — звична чи вільніша?", "ru": "Какая посадка вам ближе — обычная или более свободная?",
        "en": "Which fit would you prefer: regular or roomier?"}[language] if selector == "fit" else
        {"uk": "Який розмір вам потрібен?", "ru": "Какой размер вам нужен?", "en": "Which size would you like?"}[language] if selector == "size" else "")
    return "\n".join(part for part in (prefix, *facts, photo_note, question) if part)


def validate_fit_consultation(context, response):
    """Enforce print retention before catalog fallback can acquire send rights."""
    if (context and "current_tshirt_audience" in context.get("approved_fact_keys", ())
        and re.search(r"унісекс|унисекс|unisex", response.reply_text, re.I)
        and re.search(r"\b(?:весь|увесь|ус[іе]|все|all|entire|whole)[^.!?\n]{0,25}\b(?:каталог\w*|одяг\w*|одежд\w*|catalog\w*|products|clothes|clothing)\b", response.reply_text, re.I)):
        return "focused_fit_audience_scope_exceeded"
    if (context and context.get("audience_requested")
        and "current_tshirt_audience" in context.get("approved_fact_keys", ())
        and not _audience_answer(response.reply_text)):
        return "focused_fit_audience_unanswered"
    focus = (context or {}).get("focus") or {}
    if not focus or context.get("assortment_requested"):
        return ""
    focused_id = focus["product_id"]
    control = response.control
    if control.get("product") and str(control["product"]) != str(focused_id):
        return "focused_fit_product_changed"
    if control.get("catalog_link"):
        return "focused_fit_catalog_redirect"
    if re.search(r"\b(?:який|яку|какой|какую|which|what)[^.!?\n]{0,30}\b(?:принт\w*|дизайн\w*|prints?|designs?|products?)\b"
        r"|\b(?:оберіть|виберіть|выбер\w*|choose|pick)[^.!?\n]{0,25}\b(?:принт\w*|дизайн\w*|модел\w*|prints?|designs?|models?)\b",
        _text(response.reply_text), re.I):
        return "focused_fit_print_reselection"
    shown = {int(value) for value in str(control.get("show_products") or "").split(",") if value.isdigit()}
    foreign = shown - {focused_id}
    if foreign:
        capability = context.get("fit_photo_capability") or {}
        allowed = {pk for pk in capability.get("example_product_ids", ()) if type(pk) is int}
        text = response.reply_text
        if (not context.get("photos_requested") or not foreign.issubset(allowed)
            or not _EXAMPLE_LABEL.search(text) or not _OTHER_DESIGN.search(text)
            or not (_FIT_WORD.search(text) or any(pattern.search(text) for pattern in _FIT.values()))):
            return "focused_fit_example_unproven"
    return ""


def _audience_answer(text):
    from management.services.ig_reply_truth import _locally_negated

    text = _text(text)
    if any(not _locally_negated(text, match.start()) for match in re.finditer(r"\b(?:унісекс|унисекс|unisex)\b", text, re.I)):
        return True
    return bool(re.search(r"жін\w*|женщин\w*|женск\w*|\bwomen\b", text, re.I)
        and re.search(r"чолов\w*|мужчин\w*|мужск\w*|\bmen\b", text, re.I)
        and re.search(r"т[ауе]\s+сам\w*|той\s+самий|однаков\w*|один\w*|одн[аоу]\w*|те\s+же|same", text, re.I))


def fit_topic_covered(kind, response, context):
    if not context or validate_fit_consultation(context, response):
        return False
    text = _text(response.reply_text)
    if kind == "info:fit_presentation":
        capability = context.get("fit_photo_capability") or {}
        return bool(response.control.get("show_products") or (capability.get("missing_fit_photos")
            and _PHOTO.search(text) and re.search(r"нема\w*|немає|нет|no\s+(?:confirmed|separate)|unavailable", text, re.I)))
    if kind == "info:fit":
        mentioned = [key for key, pattern in _FIT.items() if pattern.search(text)]
        return bool(mentioned and (not context.get("comparison_requested") or len(mentioned) == 2)
            and re.search(r"вільн\w*|свобод\w*|прям\w*|обляг\w*|шир\w*|звич\w*|обычн\w*|loose\w*|relaxed|straight|roomier|roomy", text, re.I))
    if kind == "info:audience":
        return bool("current_tshirt_audience" in context.get("approved_fact_keys", ())
            and _audience_answer(text))
    return False
