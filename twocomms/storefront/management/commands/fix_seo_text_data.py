"""SEO v1.0 Phase 11 — one-shot data fix for legacy text fields.

Closes findings (MM) and (H) of the master audit:

* (MM) HTML entity double-encoding observed on PDP <title> / <h1>:
  product titles authored with straight ASCII quotes («Худі "Дрони…"»)
  pass through Django's autoescape layer twice when they land inside
  the ``{% block title %}`` (the parent layout already escapes the
  block output once), producing `&quot;` glyphs in the browser tab,
  SERP snippet, Facebook share card and Telegram preview. The right
  data-layer fix is to convert the straight ASCII quotes into the
  Ukrainian typographic pair `«…»` once and for all: they are
  semantically the correct quote character for UA copy AND they're
  not in the HTML escape set, so Django leaves them untouched. This
  command also still decodes literal `&quot;` strings should any
  legacy row contain them.

* (H) Category SEO titles longer than 60 characters get clipped by
  Google's mobile SERP. The current admin-authored copy on
  ``/catalog/tshirts/`` (72), ``/catalog/hoodie/`` (75) and
  ``/catalog/long-sleeve/`` (66) all overshoot. Trim the offending
  rows down to ≤60 characters at the last word boundary while
  keeping the « — TwoComms» suffix.

Both fixes are idempotent: the command checks the current value
before writing and only touches rows that still match the buggy
shape, so re-running the command is a no-op.

Usage:
    python manage.py fix_seo_text_data           # apply
    python manage.py fix_seo_text_data --dry-run # preview only
"""
from __future__ import annotations

import html as html_lib
import re

from django.core.management.base import BaseCommand

from storefront.models import Category, Product


CATEGORY_TITLE_MAX = 60

# A title cut at a whitespace boundary can still be grammatically incomplete.
# Keep this intentionally small and language-agnostic enough for the three
# translated category columns handled by the command.
_DANGLING_CONNECTORS = {
    # Ukrainian / Russian
    "і", "й", "та", "або", "чи", "з", "зі", "із", "у", "в", "на",
    "до", "від", "от", "для", "про", "по", "під", "под", "над", "за",
    "и", "или", "с", "со", "из",
    # English
    "and", "or", "with", "in", "on", "at", "by", "for", "from", "to", "of",
}

# Pattern matches a balanced pair of ASCII double-quotes around any run of
# characters that doesn't itself contain a quote. Used to upgrade the
# legacy «"Дрони навколо"» wording to typographic «Дрони навколо».
_ASCII_QUOTE_PAIR_RE = re.compile(r'"([^"\n]+?)"')


def _decode_html_entities(value: str) -> str:
    """Decode HTML entities in ``value``; idempotent."""
    if not value:
        return value
    decoded = html_lib.unescape(value)
    # Run twice in case of double-encoding («&amp;quot;» → «&quot;» → «"»).
    decoded = html_lib.unescape(decoded)
    return decoded


def _upgrade_quotes(value: str) -> str:
    """Replace ASCII ``"..."`` pairs with typographic ``«...»``.

    Idempotent: if the string contains no straight ASCII pair the input
    is returned unchanged. Stray solo ASCII quotes are left as-is to
    avoid corrupting code-like strings or product SKUs.
    """
    if not value or '"' not in value:
        return value
    return _ASCII_QUOTE_PAIR_RE.sub(lambda m: f"«{m.group(1)}»", value)


def _trim_to_word_boundary(value: str, max_len: int) -> str:
    """Trim ``value`` to ``max_len`` chars at the last whitespace before
    the limit. Preserves the literal « — TwoComms» suffix when present.
    """
    if not value or len(value) <= max_len:
        return value

    suffix = " — TwoComms"
    if value.endswith(suffix):
        head_max = max_len - len(suffix)
        head = value[: -len(suffix)]
        if len(head) > head_max:
            head = head[:head_max].rsplit(" ", 1)[0].rstrip(",.;:- ")
            head = _drop_dangling_connectors(head)
        return head + suffix

    trimmed = value[:max_len].rsplit(" ", 1)[0].rstrip(",.;:- ")
    return _drop_dangling_connectors(trimmed)


def _drop_dangling_connectors(value: str) -> str:
    """Remove connector/preposition tokens that cannot finish a title."""
    words = value.split()
    while words and words[-1].strip(",.;:-–—").casefold() in _DANGLING_CONNECTORS:
        words.pop()
    return " ".join(words).rstrip(",.;:-–— ")


class Command(BaseCommand):
    help = (
        "Decode HTML entities in Product.title/seo_title/seo_description (MM)"
        " and trim Category.seo_title rows over 60 chars (H)."
    )

    PRODUCT_TEXT_FIELDS = (
        "title",
        "seo_title",
        "seo_description",
        "short_description",
        "main_image_alt",
    )

    CATEGORY_TEXT_FIELDS = (
        "name",
        "seo_title",
        "seo_h1",
        "seo_description",
    )

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="Preview only.")

    def handle(self, *args, **opts):
        dry = bool(opts.get("dry_run"))
        self.stdout.write(self.style.NOTICE(
            f"Phase 11 SEO text fix{' (DRY-RUN)' if dry else ''}…"
        ))
        self._fix_products(dry_run=dry)
        self._fix_categories(dry_run=dry)
        if dry:
            self.stdout.write(self.style.WARNING("Dry-run: no changes saved."))

    # ------------------------------------------------------------------ products

    def _fix_products(self, *, dry_run: bool) -> None:
        touched = 0
        per_field: dict[str, int] = {}
        # Translation-aware scan: also patch the locale-specific columns
        # (title_uk, seo_title_uk, …) so modeltranslation parity is kept.
        for product in Product.objects.all().iterator():
            update_fields: list[str] = []
            for field in self.PRODUCT_TEXT_FIELDS:
                for attr in self._field_variants(field, product):
                    current = getattr(product, attr, None)
                    if not isinstance(current, str):
                        continue
                    # Two-pass normalisation:
                    # 1. decode legacy `&quot;` style entity strings;
                    # 2. upgrade straight ASCII `"…"` pairs to «…».
                    decoded = _decode_html_entities(current)
                    upgraded = _upgrade_quotes(decoded)
                    if upgraded != current:
                        setattr(product, attr, upgraded)
                        update_fields.append(attr)
                        per_field[attr] = per_field.get(attr, 0) + 1
            if update_fields and not dry_run:
                product.save(update_fields=update_fields)
            if update_fields:
                touched += 1

        self.stdout.write(self.style.SUCCESS(
            f"Products with decoded entities: {touched}"
        ))
        for k, v in sorted(per_field.items()):
            self.stdout.write(f"  {k:24s} {v}")

    # ------------------------------------------------------------------ categories

    def _fix_categories(self, *, dry_run: bool) -> None:
        touched_decode = 0
        touched_trim = 0
        per_field_decode: dict[str, int] = {}
        per_field_trim: dict[str, int] = {}

        for category in Category.objects.all().iterator():
            update_fields: list[str] = []
            # 1) HTML entity decode + ASCII→typographic quote upgrade.
            for field in self.CATEGORY_TEXT_FIELDS:
                for attr in self._field_variants(field, category):
                    current = getattr(category, attr, None)
                    if not isinstance(current, str):
                        continue
                    decoded = _decode_html_entities(current)
                    upgraded = _upgrade_quotes(decoded)
                    if upgraded != current:
                        setattr(category, attr, upgraded)
                        update_fields.append(attr)
                        per_field_decode[attr] = per_field_decode.get(attr, 0) + 1
            # 2) Trim seo_title (and locale-specific copies) to ≤60.
            for attr in self._field_variants("seo_title", category):
                current = getattr(category, attr, None)
                if not isinstance(current, str) or not current:
                    continue
                if len(current) <= CATEGORY_TITLE_MAX:
                    continue
                trimmed = _trim_to_word_boundary(current, CATEGORY_TITLE_MAX)
                if trimmed != current and len(trimmed) <= CATEGORY_TITLE_MAX:
                    setattr(category, attr, trimmed)
                    if attr not in update_fields:
                        update_fields.append(attr)
                    per_field_trim[attr] = per_field_trim.get(attr, 0) + 1
            if update_fields and not dry_run:
                category.save(update_fields=update_fields)
            if update_fields:
                if any(per_field_decode.get(a) for a in update_fields):
                    touched_decode += 1
                if any(per_field_trim.get(a) for a in update_fields):
                    touched_trim += 1

        self.stdout.write(self.style.SUCCESS(
            f"Categories with decoded entities: {touched_decode} | "
            f"with trimmed seo_title: {touched_trim}"
        ))
        for k, v in sorted(per_field_decode.items()):
            self.stdout.write(f"  decode {k:18s} {v}")
        for k, v in sorted(per_field_trim.items()):
            self.stdout.write(f"  trim   {k:18s} {v}")

    # ------------------------------------------------------------------ helpers

    @staticmethod
    def _field_variants(field: str, instance) -> list[str]:
        """Return ``[field, field_uk, field_ru, field_en]`` filtered to attrs
        that actually exist on the instance (so non-translatable fields
        and missing locales are silently ignored)."""
        candidates = [field, f"{field}_uk", f"{field}_ru", f"{field}_en"]
        return [c for c in candidates if hasattr(instance, c)]
