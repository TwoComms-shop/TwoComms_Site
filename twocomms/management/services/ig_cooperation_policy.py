"""Reviewed public policy explanations, never individual trade/action authority.

Markdown is a bounded publication source. A process pins its reviewed source
bytes; only the same immutable published instruction body can grant a public
explanation. Current customer text cannot supply a policy or its monetary data.
"""
from dataclasses import dataclass, field
from functools import lru_cache
from hashlib import sha256
from pathlib import Path
import re

VERSION = "owner-2026-10-24.q23.2"
PARSER_VERSION = "cooperation-source.v1"
LANGUAGES = ("uk", "ru", "en")
TITLE = "Owner reviewed: public cooperation"
_TOKEN = object()
_TOPICS = frozenset({"partner_price", "monthly_sales", "wholesale", "start_without_stock",
    "delivery_terms", "payment_terms", "settlement", "channels", "photo_usage", "samples", "review"})
# Approved publication 8 (q23.1), retained only for immutable/draft reading.
# These exact source/body identities do not enter reviewed_modules() or grant
# current public facts; capture_cooperation_policy remains current-body only.
_APPROVED_HISTORICAL_SOURCES = (
    ("uk", "0db9bdc6351b57a4ab88a722973f010ff596006d3710e6c1afa810c92e92fe33",
        "7930a10ba45c8183d33354d94600914a2b2c725c550ddd68e2007ae7790ecc04"),
    ("ru", "c09651062306965c26e23deb1daed2ddc7d597b134cbdfd23663c413b4e7222c",
        "dcc4b92a8f5d09d78a9444266480b185b62d4b840678f36842adfd8fec352acf"),
    ("en", "f46f5e7b5cc543cdd2823e8017b579d6913cb7c2eaf2fa93bb08c390bb06b682",
        "549cda98d47534e0300c943a037a815705d47f4a95b12727b4463b1865ff47f4"),
)


def _hash(text):
    return sha256(text.encode()).hexdigest()


def _normal(text):
    return re.sub(r"\s+", " ", str(text or "")).strip(" .!?\n").casefold()


@lru_cache(maxsize=1)
def reviewed_modules():
    result = []
    for language in LANGUAGES:
        source_id = f"management/bot_knowledge/modules/cooperation.{language}.md"
        path = Path(__file__).resolve().parents[1] / "bot_knowledge" / "modules" / f"cooperation.{language}.md"
        raw = path.read_bytes()
        if not 1 <= len(raw) <= 16000:
            raise ValueError("cooperation_source_size")
        body = raw.decode("utf-8").strip()
        if (not body.startswith(f"# TwoComms public cooperation policy\nVersion: {VERSION}\nLanguage: {language}\n")
            or body.count("## Public answers") != 1 or body.count("## Boundaries") != 1
            or "```" in body or "<!--" in body or re.search(r"https?://", body)):
            raise ValueError("cooperation_source_shape")
        public = body.split("## Public answers\n", 1)[1].split("\n## Boundaries", 1)[0]
        facts = []
        for line in public.strip().splitlines():
            match = re.fullmatch(r"- ([a-z_]+): (.+[.])", line)
            if match is None or match[1] not in _TOPICS or any(key == match[1] for key, _ in facts):
                raise ValueError("cooperation_source_fact")
            facts.append((match[1], match[2]))
        if {key for key, _ in facts} != _TOPICS:
            raise ValueError("cooperation_source_topics")
        provenance = {"version": 1, "source_id": source_id, "source_hash": _hash(raw.decode("utf-8")),
            "section_key": "public_cooperation", "section_hash": _hash(body), "body_hash": _hash(body),
            "parser_version": PARSER_VERSION}
        result.append((language, body, tuple(facts), tuple(provenance.items())))
    return tuple(result)


def reviewed_values(language):
    row = next(row for row in reviewed_modules() if row[0] == language)
    return {"key": "public_cooperation_" + language, "title": f"{TITLE} ({language})", "body": row[1],
        "tags": ["collaboration"], "triggers": ["business_cooperation"], "locale": language,
        "programme_metadata": {"required_for_scenarios": ["business_cooperation"]}, "reviewed_source": dict(row[3])}


def validate_reviewed_source(value, body):
    if type(value) is not dict or type(body) is not str:
        raise ValueError("cooperation_source_body_mismatch")
    def matches(expected):
        return value == expected and all(type(value[key]) is type(item) for key, item in expected.items())
    for _language, approved, _facts, provenance in reviewed_modules():
        if matches(dict(provenance)) and body.strip() == approved:
            return dict(value)
    for language, source_digest, body_digest in _APPROVED_HISTORICAL_SOURCES:
        expected = {"version": 1,
            "source_id": f"management/bot_knowledge/modules/cooperation.{language}.md",
            "source_hash": source_digest, "section_key": "public_cooperation",
            "section_hash": body_digest, "body_hash": body_digest,
            "parser_version": "cooperation-source.v1"}
        if (matches(expected) and _hash(body.strip()) == body_digest
            and body.strip().startswith("# TwoComms public cooperation policy\n"
                f"Version: owner-2026-10-23.q23.1\nLanguage: {language}\n")):
            return dict(value)
    raise ValueError("cooperation_source_body_mismatch")


@dataclass(frozen=True)
class CooperationPolicy:
    publication_id: int
    publication_version: int
    publication_hash: str
    # language, topic, exact public sentence, immutable module identity
    facts: tuple
    sources: tuple
    _token: object = field(repr=False, compare=False)

    def metadata(self):
        return {"version": VERSION, "publication_id": self.publication_id,
            "publication_version": self.publication_version, "publication_hash": self.publication_hash,
            "module_ids": sorted({row[3] for row in self.facts}),
            "body_digests": sorted({_hash(body) for language, body, _, _ in reviewed_modules()
                if any(row[0] == language for row in self.facts)}),
            "source_refs": [{"message_id": pk, "source_digest": digest} for pk, digest in self.sources]}


def capture_cooperation_policy(publication, sources):
    from management.services.ig_business_cooperation import business_cooperation_for_sources, current_cooperation_sources
    from management.services.ig_reply_language import resolve_own_source_reply_language
    current = current_cooperation_sources(sources)
    if publication is None or not _own_business_enquiry(current, sources):
        return None
    if not hasattr(publication, "snapshot"):
        # Revision execution passes its immutable ID/version/hash binding.
        # Read that exact row, never a newer active head or mutable draft.
        from types import SimpleNamespace
        from management.models import BotPolicyPublication
        from management.services.ig_policy_publication import snapshot_hash
        row = BotPolicyPublication.objects.filter(pk=publication.publication_id,
            version=publication.version, snapshot_hash=publication.snapshot_hash).first()
        if row is None or snapshot_hash(row.snapshot) != publication.snapshot_hash:
            return None
        publication = SimpleNamespace(publication_id=row.pk, version=row.version,
            snapshot_hash=row.snapshot_hash, snapshot=row.snapshot)
    refs = tuple((row["message_id"], str(row.get("source_digest") or _hash(str(row.get("text") or ""))))
        for row in current if type(row.get("message_id")) is int and row["message_id"] > 0)
    if not refs:
        return None
    language = resolve_own_source_reply_language(sources=sources).knowledge_locale
    values = reviewed_values(language)
    facts = []
    for item in publication.snapshot.get("instructions", ()):
        if (not item.get("active") or item.get("trust_scope") != "public_policy"
            or item.get("title") != values["title"] or item.get("body") != values["body"]
            or item.get("locale") != language or item.get("tags") != values["tags"]
            or item.get("triggers") != values["triggers"] or item.get("allowed_actions")
            or item.get("programme_metadata") != values["programme_metadata"]
            or item.get("reviewed_source") != values["reviewed_source"]):
            continue
        module = str(item.get("id") or "")
        if not re.fullmatch(r"instruction:[1-9]\d*", module):
            continue
        facts.extend((language, topic, text, module) for topic, text in next(row[2] for row in reviewed_modules() if row[0] == language))
    if not facts:
        return None
    return CooperationPolicy(publication.publication_id, publication.version, publication.snapshot_hash,
        tuple(facts), refs, _TOKEN)


def _own_business_enquiry(sources, originals):
    """Own enquiry proof supplements, and never rewrites, the old classifier.

    A policy sentence or a copied module can mention dropshipping without any
    customer request. Require an independently classified customer clause with
    its own question or first-person business intent. A personal retail suffix
    cannot turn a preceding policy quotation into that proof.
    """
    from management.services.ig_business_cooperation import business_cooperation_for_sources
    from management.services.ig_reply_language import own_reply_text
    from management.services.ig_response_plan import _PERSONAL_PURCHASE
    stance = re.compile(r"\b(?:(?:i|we)\s+(?:want|would\s+like|plan|intend|run|own|operate|am\s+interested|are\s+interested)|"
        r"(?:я|ми|мы)\s+(?:хочу|хочемо|хотим|маю|маємо|план\w*|відкри\w*|откры\w*|запуска\w*)|"
        r"хочу|хочемо|планую|плануємо|планирую|планируем|відкриваю|открываю|запускаю|маю\s+магазин|у\s+мене\s+магазин|у\s+меня\s+магазин)\b", re.I)
    raw_by_id = {row.get("message_id"): str(row.get("text") or "") for row in originals if isinstance(row, dict)}
    for row in sources:
        for raw in re.findall(r"[^.!?\n]+[.!?]?", str(row.get("text") or "")):
            text = own_reply_text(raw)
            personal = _PERSONAL_PURCHASE.search(text)
            if personal:
                text = text[:personal.start()].rstrip(" ,")
            if not text:
                continue
            quote = raw.strip().rstrip(".!?")
            original = raw_by_id.get(row.get("message_id"), "")
            position = original.find(quote)
            question = position >= 0 and original[position + len(quote):].lstrip().startswith("?")
            if (question or stance.search(text)) and business_cooperation_for_sources([{**row, "text": text}]):
                return True
    return False


def public_statement_topic(text, capability):
    if not isinstance(capability, CooperationPolicy) or capability._token is not _TOKEN:
        return ""
    exact = next((topic for _, topic, statement, _ in capability.facts if _normal(statement) == _normal(text)), "")
    if exact:
        return exact
    return _public_paraphrase_topic(text, {row[1] for row in capability.facts})


def _public_paraphrase_topic(text, topics):
    """Admit a complete public claim, never a matching fragment plus a right.

    These bounded grammars deliberately have no open-ended prose slots. Brand
    asset ownership, numbers and agreement qualifiers belong to the same
    complete claim. Unsupported paraphrases retain the ordinary truth guards;
    exact reviewed sentences remain available for every published topic.
    """
    value = _normal(text)
    if len(value) > 650 or re.search(r'["«»“”\n]', value):
        return ""
    # A denial of an individual entitlement may accompany public information.
    # This is a complete negative clause, not a negation word anywhere nearby.
    negative = r"(?:your\s+discount\s+is\s+not\s+guaranteed|this\s+does\s+not\s+(?:approve\s+your\s+partnership|guarantee\s+(?:a|your)\s+discount)|це\s+не\s+гарантує\s+(?:вам\s+)?знижк\w*|ваша\s+знижка\s+не\s+гарантована|это\s+не\s+гарантирует\s+(?:вам\s+)?скидк\w*|ваша\s+скидка\s+не\s+гарантирована)"
    value = re.sub(r"(?:,?\s+(?:and|but|та|але|і|и|но)\s+)" + negative + r"$", "", value, flags=re.I)
    patterns = {
        "partner_price": (
            r"(?:for\s+dropshipping,?\s+)?(?:the\s+)?(?:public\s+)?(?:dropshipping\s+)?discount(?:\s+(?:guide|range))?\s+(?:is\s+|can\s+be\s+|ranges\s+from\s+)?10\s*(?:[–—-]|to)\s*15\s*%\s+off\s+the\s+current\s+full\s+(?:website|site)\s+price,?\s+(?:(?:with|while|and)\s+)?(?:the\s+)?exact\s+(?:percentage|discount)(?:\s+and\s+terms)?\s+(?:(?:is|are)\s+)?agreed\s+individually",
            r"(?:публічний\s+)?(?:орієнтир|діапазон)\s+знижки\s+для\s+дропшипінгу\s*[—–-]?\s*10\s*[–—-]\s*15\s*%\s+від\s+(?:поточної|актуальної)\s+повної\s+ціни\s+(?:на\s+сайті|сайту),?\s+(?:а\s+)?точний\s+відсоток(?:\s+та\s+умови)?\s+(?:погоджуємо|погоджується|погоджуються|узгоджуємо)\s+індивідуально",
            r"(?:для\s+дропшиппинга\s+)?(?:публичный\s+)?(?:ориентир|диапазон)\s+скидки(?:\s+для\s+дропшиппинга)?\s*(?:составляет\s+|[—–-]\s*)?10\s*[–—-]\s*15\s*%\s+от\s+(?:текущей|актуальной)\s+полной\s+цены\s+(?:на\s+сайте|сайта),?\s+(?:а\s+)?точный\s+процент(?:\s+и\s+условия)?\s+(?:согласуем|согласовывается|согласовываются)\s+индивидуально",
        ),
        "monthly_sales": (
            r"(?:from|at\s+least)\s+8\s+sales\s+per\s+month,?\s+(?:(?:you|partners?)\s+can\s+discuss\s+(?:partnership|partner)\s+terms\s+with\s+our\s+team|our\s+team\s+can\s+discuss\s+(?:partnership|partner)\s+terms)",
            r"від\s+8\s+продажів\s+на\s+місяць,?\s+можна\s+обговорити\s+(?:партнерські\s+умови|умови\s+співпраці)\s+з\s+командою",
            r"от\s+8\s+продаж\s+в\s+месяц,?\s+можно\s+обсудить\s+(?:партнерские\s+условия|условия\s+сотрудничества)\s+с\s+командой",
        ),
        "photo_usage": (
            r"(?:you\s+(?:may|can)\s+(?:reuse|use)|(?:existing\s+)?public\s+(?:photos|materials|images)\s+may\s+be\s+reused)\s+(?:(?:existing\s+)?public\s+(?:photos|materials|images)\s+)?from\s+(?:our|the\s+brand(?:['’]s)?)\s+(?:website(?:\s+(?:and|or)\s+(?:our\s+)?instagram)?|instagram(?:\s+(?:and|or)\s+(?:our\s+)?website)?)(?:\s+for\s+resale)?",
            r"(?:можна|можете|дозволено)\s+використовувати\s+(?:існуючі\s+)?публічні\s+(?:фото|матеріали)\s+(?:з|із)\s+нашого\s+(?:сайту(?:\s+(?:та|і|або)\s+(?:нашого\s+)?instagram)?|instagram(?:\s+(?:та|і|або)\s+нашого\s+сайту)?)(?:\s+для\s+перепродажу)?",
            r"(?:можно|можете|разрешено)\s+использовать\s+(?:существующие\s+)?публичные\s+(?:фото|материалы)\s+с\s+нашего\s+(?:сайта(?:\s+(?:и|или)\s+(?:нашего\s+)?instagram)?|instagram(?:\s+(?:и|или)\s+нашего\s+сайта)?)(?:\s+для\s+перепродажи)?",
        ),
        "wholesale": (
            r"wholesale\s+(?:buyout|purchases?)\s+(?:is\s+(?:a\s+separate\s+purchasing\s+format\s+)?in|are\s+in|comes\s+in)\s+multiples\s+of\s+8(?:,?\s+for\s+example\s+8\s+or\s+16\s+garments)?",
            r"оптовий\s+викуп\s*[—–-]?\s*(?:це\s+)?(?:окремий\s+формат\s+закупівлі\s+)?(?:кількості,?\s+кратної|кратний)\s+8(?:,?\s+наприклад\s+8\s+або\s+16\s+речей)?",
            r"оптовый\s+выкуп\s*[—–-]?\s*(?:это\s+)?(?:отдельный\s+формат\s+закупки\s+)?(?:количества,?\s+кратного|кратен)\s+8(?:,?\s+например\s+8\s+или\s+16\s+вещей)?",
        ),
        "start_without_stock": (
            r"(?:in|for)\s+dropshipping,?\s+the\s+brand\s+keeps\s+(?:the\s+)?(?:inventory|stock)\s+and\s+(?:the\s+)?partner\s+does\s+not\s+need\s+to\s+buy\s+a\s+batch\s+in\s+advance",
            r"у\s+дропшипінгу\s+(?:товарний\s+)?запас\s+залишається\s+у\s+бренду,?\s+(?:а|і)\s+партнеру\s+не\s+потрібно\s+заздалегідь\s+викуповувати\s+партію",
            r"при\s+дропшиппинге\s+(?:товарный\s+)?запас\s+оста[её]тся\s+у\s+бренда,?\s+(?:а|и)\s+партн[её]ру\s+не\s+нужно\s+заранее\s+выкупать\s+партию",
        ),
        "delivery_terms": (
            r"(?:the\s+)?(?:partner\s+or\s+(?:the\s+)?brand|brand\s+or\s+(?:the\s+)?partner)\s+can\s+prepare\s+(?:the\s+)?(?:shipping\s+)?waybill,?\s+and\s+(?:the\s+)?origin\s+branch\s+for\s+(?:a\s+)?partner\s+waybill\s+(?:must\s+be|is)\s+agreed\s+in\s+advance",
            r"ттн\s+може\s+оформляти\s+партнер\s+або\s+бренд,?\s+(?:а|і)\s+для\s+ттн\s+партнера\s+відділення\s+відправлення\s+потрібно\s+погодити\s+заздалегідь",
            r"ттн\s+может\s+оформлять\s+партн[её]р\s+или\s+бренд,?\s+(?:а|и)\s+для\s+ттн\s+партн[её]ра\s+отделение\s+отправления\s+нужно\s+согласовать\s+заранее",
        ),
        "payment_terms": (
            r"before\s+dispatch,?\s+the\s+brand\s+must\s+receive\s+the\s+agreed\s+full\s+iban\s+payment\s+or\s+a\s+200\s+uah\s+deposit",
            r"перед\s+відправленням\s+бренд\s+має\s+отримати\s+погоджену\s+повну\s+оплату\s+на\s+iban\s+або\s+передоплату\s+200\s+грн",
            r"перед\s+отправкой\s+бренд\s+должен\s+получить\s+согласованную\s+полную\s+оплату\s+на\s+iban\s+или\s+предоплату\s+200\s+грн",
        ),
        "settlement": (
            r"(?:the\s+)?(?:settlement\s+of\s+the\s+)?partner\s+margin\s+(?:for|on)\s+cash\s+on\s+delivery\s+is\s+agreed\s+individually",
            r"(?:розрахунок\s+маржі\s+партнера\s+при\s+післяплаті\s+погоджується|партнерську\s+маржу\s+при\s+післяплаті\s+погоджуємо)\s+індивідуально",
            r"(?:расч[её]т\s+маржи\s+партн[её]ра\s+при\s+наложенном\s+платеже\s+согласовывается|партнерскую\s+маржу\s+при\s+наложенном\s+платеже\s+согласуем)\s+индивидуально",
        ),
        "channels": (
            r"telegram\s+is\s+the\s+main\s+(?:working\s+)?channel\s+for\s+partnerships,?\s+but\s+this\s+chat\s+is\s+enough\s+for\s+an\s+enquiry,?\s+and\s+the\s+website\s+cabinet\s+is\s+still\s+in\s+testing",
            r"основний\s+робочий\s+канал\s+партнерства\s*[—–-]\s*telegram,?\s+але\s+поточний\s+чат\s+достатній\s+для\s+звернення,?\s+(?:а|і)\s+кабінет\s+на\s+сайті\s+поки\s+тестовий",
            r"основной\s+рабочий\s+канал\s+партн[её]рства\s*[—–-]\s*telegram,?\s+но\s+текущего\s+чата\s+достаточно\s+для\s+обращения,?\s+(?:а|и)\s+кабинет\s+на\s+сайте\s+пока\s+тестовый",
        ),
    }
    return next((topic for topic, choices in patterns.items() if topic in topics
        and any(re.fullmatch(pattern, value, flags=re.I) for pattern in choices)), "")


def is_reviewed_public_statement(text):
    """Recognizing wording never grants it authority on an old publication."""
    return (any(_normal(statement) == _normal(text) for _, _, facts, _ in reviewed_modules()
        for _, statement in facts) or bool(_public_paraphrase_topic(text, _TOPICS)))


def unreviewed_business_entitlement(text):
    """Positive individual entitlement needs a decision, not public guidance.

    Public claims are validated separately before this guard. Negation belongs
    to the same coordinated clause; a prior denial cannot excuse a later
    guarantee. This is a B2B guard, not global retail discount authority.
    """
    from management.services.ig_reply_truth import _locally_negated
    # Batch multiples do not approve combining sizes or prints. Keep this
    # negative guard inside the existing business decision boundary.
    mix_scope = re.compile(r"\b(?:розмір\w*|размер\w*|принт\w*|sizes?|prints?)\b", re.I)
    mix_permission = re.compile(
        r"\b(?:можна|можете|можемо|можно|можем|дозволено|разрешено)\s+"
        r"(?:буде\s+|будет\s+)?(?:мішат\w*|зміш\w*|смеш\w*)\b|"
        r"\byou\s+(?:can|may)\s+(?:mix|combine)\b|"
        r"\b(?:sizes?|prints?)(?:\s+and\s+(?:sizes?|prints?))?\s+can\s+be\s+(?:mixed|combined)\b|"
        r"\b(?:mixing|combining)\s+(?:sizes?|prints?)(?:\s+and\s+(?:sizes?|prints?))?\s+is\s+(?:allowed|permitted)\b", re.I)
    for clause in re.split(r"[.;\n]+", str(text or "")):
        if "?" in clause or not mix_scope.search(clause):
            continue
        for match in mix_permission.finditer(clause):
            if (_locally_negated(clause, match.start())
                or re.search(r"\b(?:чи|ли|whether|if)\s*$", clause[:match.start()], re.I)):
                continue
            return True
    entitlement = re.compile(r"гарантован\w*|гарантирован\w*|гаранту\w*|guarantee\w*|entitled|eligible|qualified|"
        r"призначен\w*|назначен\w*|схвален\w*|одобрен\w*|approved|assigned|unlocked", re.I)
    for clause in re.split(r"[,;:]|\b(?:and|but|та|але|і|и|но)\b", str(text or ""), flags=re.I):
        for match in entitlement.finditer(clause):
            if _locally_negated(clause, match.start()):
                continue
            # Only a direct denial qualifies. Qualifiers such as "without
            # doubt" or "not only" assert the guarantee rather than deny it.
            if re.search(r"\b(?:without\s+(?:(?:a|any)\s+)?|без\s+(?:жодн\w*\s+|будь-як\w*\s+)?)$", clause[:match.start()], re.I):
                continue
            return True
    return False


def statement_modules(text, capability):
    if not isinstance(capability, CooperationPolicy) or capability._token is not _TOKEN:
        return ()
    from management.services.ig_reply_truth import _claim_sentences
    topics = {public_statement_topic(sentence, capability) for sentence in _claim_sentences(text, ())}
    return tuple(sorted({module for _, topic, _, module in capability.facts if topic in topics}))


def selected_policy_reason(text, capability, manifest):
    modules = statement_modules(text, capability)
    if not modules:
        return ""
    publication = (manifest or {}).get("instruction_publication") or {}
    selection = (manifest or {}).get("instruction_selection") or {}
    if (publication.get("id") != capability.publication_id or publication.get("version") != capability.publication_version
        or publication.get("hash") != capability.publication_hash
        or not set(modules) <= set(selection.get("selected_ids") or ())
        or selection.get("publication_id") != capability.publication_id
        or selection.get("publication_hash") != capability.publication_hash):
        return "public_policy_cooperation_not_selected"
    return ""
