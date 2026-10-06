"""Bounded reviewed Markdown adapter for the existing instruction publication.

Markdown is editorial input, never a runtime fact owner. The first adapter
admits only exact already-approved instruction wording; canonical facts retain
one owner. New business wording needs a separately reviewed adapter version.
"""
from dataclasses import dataclass
from difflib import unified_diff
from hashlib import sha256
from pathlib import Path
import re

SOURCE_ID = "management/bot_knowledge/brand.md"
PARSER_VERSION = "brand-source-v1"
MAX_SOURCE_BYTES = 128_000
MAX_SECTIONS = 24
MAX_SECTION_CHARS = 16_000
SECTION_KEYS = {
    "Про бренд": "brand", "Доставка": "dispatch", "Оплата": "payment",
    "Повернення та обмін": "service", "Промокоди та знижки": "discounts",
    "Розмірні сітки": "size_chart", "Підбір розміру": "size_guidance",
    "Колаборації та опт": "collaboration", "Тон спілкування": "tone",
    "Часті питання (FAQ)": "faq", "Особливі інструкції": "boundaries",
}
INSTRUCTION_KEYS = frozenset({"tone", "size_guidance"})
OWNER_IDS = {
    "brand": "approved_public_facts:brand", "dispatch": "approved_public_facts:dispatch",
    "payment": "checkout_policy", "service": "approved_public_facts:service_boundary",
    "discounts": "checkout_policy", "size_chart": "catalogue:size_chart",
    "collaboration": "core:published_prompt", "faq": "canonical_fact_resolvers",
    "boundaries": "core:published_prompt",
}
_SHA = re.compile(r"^[a-f0-9]{64}$")
_PRIVATE_START = frozenset({"<!-- private -->", "[private]"})
_PRIVATE_END = frozenset({"<!-- /private -->", "[/private]"})


class BrandSourceError(ValueError):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


def content_hash(text):
    return sha256(str(text).encode("utf-8")).hexdigest()


def approved_instruction_bodies():
    from management.services.ig_core_policy import CANONICAL_IG_CORE_POLICY
    paragraphs = CANONICAL_IG_CORE_POLICY.split("\n\n")
    return {
        "tone": next(body for body in paragraphs if body.startswith("Допомагай природно")),
        "size_guidance": next(body for body in paragraphs if body.startswith("Поважай уже підтверджений")),
    }


@dataclass(frozen=True)
class BrandSection:
    key: str
    title: str
    body: str
    section_hash: str


@dataclass(frozen=True)
class BrandSource:
    source_hash: str
    sections: tuple[BrandSection, ...]


def parse_brand_source(text):
    if not isinstance(text, str) or len(text.encode("utf-8")) > MAX_SOURCE_BYTES or "\x00" in text:
        raise BrandSourceError("brand_source_invalid_size")
    visible, private = [], False
    for line in text.splitlines():
        marker = line.strip().casefold()
        if marker in _PRIVATE_START:
            if private:
                raise BrandSourceError("brand_private_nested")
            private = True
            continue
        if marker in _PRIVATE_END:
            if not private:
                raise BrandSourceError("brand_private_unmatched")
            private = False
            visible.append("")
            continue
        if re.search(r"<!--\s*/?private|\[/?private", line, re.I):
            raise BrandSourceError("brand_private_marker_invalid")
        if not private:
            visible.append(line)
    if private:
        raise BrandSourceError("brand_private_unclosed")
    sections, title, body = [], None, []
    seen = set()
    def finish():
        if title is None:
            return
        text_body = "\n".join(body).strip()
        if len(text_body) > MAX_SECTION_CHARS:
            raise BrandSourceError("brand_section_too_large")
        if not text_body:
            raise BrandSourceError("brand_section_empty")
        sections.append(BrandSection(SECTION_KEYS[title], title, text_body, content_hash(text_body)))
    for line in visible:
        if line.startswith("## "):
            finish()
            title = line[3:].strip()
            if title not in SECTION_KEYS:
                raise BrandSourceError("brand_section_unknown")
            if title in seen:
                raise BrandSourceError("brand_section_duplicate")
            seen.add(title)
            body = []
        elif line.startswith("#"):
            if title is not None or line.strip() != "# База знань бота TwoComms":
                raise BrandSourceError("brand_heading_invalid")
        elif title is not None:
            if "```" in line or re.search(r"<[^>]+>", line):
                raise BrandSourceError("brand_markup_unsupported")
            body.append(line)
        elif line.strip() and not line.startswith(">"):
            raise BrandSourceError("brand_preamble_invalid")
    finish()
    if not sections or len(sections) > MAX_SECTIONS:
        raise BrandSourceError("brand_sections_invalid")
    return BrandSource(content_hash(text), tuple(sections))


def read_brand_source():
    path = Path(__file__).resolve().parents[1] / "bot_knowledge" / "brand.md"
    with path.open("rb") as source:
        raw = source.read(MAX_SOURCE_BYTES + 1)
    if len(raw) > MAX_SOURCE_BYTES:
        raise BrandSourceError("brand_source_invalid_size")
    try:
        return parse_brand_source(raw.decode("utf-8"))
    except UnicodeDecodeError as exc:
        raise BrandSourceError("brand_source_invalid_encoding") from exc


def validate_reviewed_source(value, body):
    if value in ({}, None):
        return {}
    keys = {"version", "source_id", "source_hash", "section_key", "section_hash", "body_hash", "parser_version"}
    if (not isinstance(value, dict) or set(value) != keys or type(value["version"]) is not int
        or value["version"] != 1 or value["source_id"] != SOURCE_ID
        or value["parser_version"] != PARSER_VERSION or not isinstance(value["section_key"], str)
        or value["section_key"] not in INSTRUCTION_KEYS
        or any(not isinstance(value[key], str) or not _SHA.fullmatch(value[key])
               for key in ("source_hash", "section_hash", "body_hash"))):
        raise BrandSourceError("brand_provenance_invalid")
    approved = approved_instruction_bodies()[value["section_key"]]
    if body.strip() != approved or value["body_hash"] != content_hash(body.strip()) or value["section_hash"] != value["body_hash"]:
        raise BrandSourceError("brand_provenance_body_mismatch")
    return dict(value)


def source_preview(parsed, rows=()):
    """No writes, no private bodies; only admitted source sections show a diff."""
    from management.services.approved_public_facts import approved_public_facts
    facts = {fact.key: fact.body for fact in approved_public_facts("uk")}
    owners = {}
    for row in rows:
        provenance = getattr(row, "reviewed_source", {}) or {}
        reserved_title = next((key for title, key in SECTION_KEYS.items()
            if key in INSTRUCTION_KEYS and row.title == f"Brand: {title}"), None)
        key = provenance.get("section_key") or reserved_title
        if key:
            if key in owners:
                raise BrandSourceError("brand_target_ambiguous")
            owners[key] = row
    approved = approved_instruction_bodies()
    result = []
    for section in parsed.sections:
        row = owners.get(section.key)
        owner = OWNER_IDS.get(section.key, f"instruction:brand:{section.key}")
        reason, ready = "canonical_owner_conflict", False
        if section.key in INSTRUCTION_KEYS:
            ready = section.body == approved[section.key]
            reason = "ready" if ready else "brand_instruction_requires_reviewed_adapter"
            if ready and row is not None and row.body.strip() == section.body:
                reason = "equivalent_noop"
        elif section.body == facts.get(section.key):
            reason = "equivalent_noop"
        result.append({"section_key": section.key, "title": section.title,
            "section_hash": section.section_hash, "target": owner,
            "existing_owner": f"instruction:{row.pk}" if row else OWNER_IDS.get(section.key),
            "instruction_id": row.pk if row else None, "ready": ready, "reason": reason,
            "diff": "\n".join(unified_diff((row.body if row else "").splitlines(), section.body.splitlines(),
                fromfile="current instruction", tofile=section.title, lineterm="")) if ready else ""})
    return {"source_id": SOURCE_ID, "source_hash": parsed.source_hash,
        "parser_version": PARSER_VERSION, "sections": result}


def reviewed_import(*, expected_revision, expected_snapshot_hash, expected_source_hash,
                    section_key, reviewed, actor=None):
    from django.db import transaction
    from management.models import BotInstruction, InstagramBotSettings
    from management.services.ig_policy_publication import (
        DraftRevisionConflict, DraftState, save_instruction_draft,
        snapshot_from_rows, snapshot_hash,
    )
    if reviewed is not True:
        raise BrandSourceError("brand_explicit_review_required")
    from management.bot_access import EDIT_IG_PROMPT_PERMISSION
    if actor is None or not actor.has_perm(EDIT_IG_PROMPT_PERMISSION):
        raise BrandSourceError("brand_review_permission_required")
    parsed = read_brand_source()
    if parsed.source_hash != expected_source_hash:
        raise BrandSourceError("brand_source_hash_conflict")
    with transaction.atomic():
        settings_row = InstagramBotSettings.objects.select_for_update().filter(pk=1).first()
        rows = list(BotInstruction.objects.select_for_update().order_by("priority", "id"))
        if settings_row is None or settings_row.instruction_draft_revision != expected_revision or snapshot_hash(snapshot_from_rows(rows)) != expected_snapshot_hash:
            raise DraftRevisionConflict()
        preview = source_preview(parsed, rows)
        target = next((item for item in preview["sections"] if item["section_key"] == section_key), None)
        if target is None or not target["ready"]:
            raise BrandSourceError("brand_import_not_admitted")
        section = next(item for item in parsed.sections if item.key == section_key)
        provenance = {"version": 1, "source_id": SOURCE_ID, "source_hash": parsed.source_hash,
            "section_key": section.key, "section_hash": section.section_hash,
            "body_hash": content_hash(section.body), "parser_version": PARSER_VERSION}
        if read_brand_source().source_hash != expected_source_hash:
            raise BrandSourceError("brand_source_hash_conflict")
        existing = next((row for row in rows if row.pk == target["instruction_id"]), None)
        if existing is not None and existing.reviewed_source == provenance and existing.body.strip() == section.body:
            return existing, DraftState(expected_revision, snapshot_from_rows(rows), expected_snapshot_hash)
        return save_instruction_draft(expected_revision=expected_revision,
            expected_snapshot_hash=expected_snapshot_hash, instruction_id=target["instruction_id"], actor=actor,
            note=f"reviewed source {SOURCE_ID} sha256={parsed.source_hash}",
            values={"title": f"Brand: {section.title}", "body": section.body, "active": True,
                "priority": 100, "locale": "uk", "tags": ["global"] if section.key == "tone" else ["fit"],
                "triggers": [], "allowed_actions": [], "trust_scope": "public_policy", "reviewed_source": provenance})
