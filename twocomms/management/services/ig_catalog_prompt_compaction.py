"""Lossless, prompt-only dictionary encoding of repeated catalogue facts.

No catalogue/price/configuration resolver lives here. Exact text fragments are
interned only when their references and the complete legend save characters.
The decoder is also an invariant oracle for prices, variants and visual facts.
"""
from collections import Counter
import json
import re


START = '[CATALOG_SHARED_V1]'
END = '[/CATALOG_SHARED_V1]'
GUIDANCE = (
    'Спільні значення: @Cчисло замінює точний текст із легенди нижче; '
    'застосовуй його тільки в місці посилання, не до сусідніх товарів/варіантів. '
    '(v=N означає (variant_id=N. Ціни в грн. Усередині дужок variant_id умови '
    'діють лише для цього варіанта; загальні фасони/розміри поза цими дужками '
    'залишаються умовами саме цього товару. Не об’єднуй розміри різних варіантів. '
    'Позитивний залишок лишається локальним; відсутність залишку не означає нуль.'
)
_REFERENCE = re.compile(r'@C[1-9]\d*')
_VARIANT = re.compile(r'\(variant_id=\d+, ')
_SHORT_VARIANT = re.compile(r'\(v=(\d+), ')
_PRICE = re.compile(r' — (\d+(?:[.,]\d+)?(?:-\d+(?:[.,]\d+)?)? грн(?: \(знижка [^()\n]*\))?)(?=;|$)')
_STOCK = re.compile(r', (?:на складі \d+ шт|обліковий stock=\d+)$')


def _fragments(line):
    """Non-overlapping configuration, headline price and generic-size spans."""
    if not line.startswith('• id='):
        return []
    output, variant_ends = [], []
    for match in _VARIANT.finditer(line):
        if variant_ends and match.start() < variant_ends[-1]:
            continue
        depth, end = 1, match.end()
        while end < len(line) and depth:
            depth += (line[end] == '(') - (line[end] == ')')
            end += 1
        if depth:
            # Malformed source remains untouched; never repair a partial fact.
            continue
        body_end = end - 1
        stock = _STOCK.search(line[match.end():body_end])
        if stock:
            body_end = match.end() + stock.start()
        output.append((match.end(), body_end, line[match.end():body_end]))
        variant_ends.append(end)
    head_end = line.find('; кольори: ')
    head = line[:head_end] if head_end >= 0 else line
    for match in _PRICE.finditer(head):
        output.append((match.start(1), match.end(1), match.group(1)))
    # The product-level fallback sizes follow its last complete variant. This
    # deliberately excludes variant-specific sizes already inside the body.
    tail_start = max(variant_ends, default=0)
    field = line.find('; фасони/розміри: ', tail_start)
    if field >= 0:
        boundaries = [line.find(marker, field) for marker in
            ('; категорія: ', '; колекції: ', '; принт: ')]
        end = min((value for value in boundaries if value >= 0), default=len(line))
        output.append((field, end, line[field:end]))
    # These relations belong to each exact product ID, including identical
    # print names on different garments. Intern their complete literal text;
    # never infer membership from a title or apply a category to nearby rows.
    category = line.find('; категорія: ', tail_start)
    if category >= 0:
        end = line.find('; принт: ', category)
        end = len(line) if end < 0 else end
        output.append((category, end, line[category:end]))
    return sorted(output)


def compact_catalog_text(text, *, abbreviate_variant_ids=False):
    """Intern profitable repeated facts; preserve an exact reversible source."""
    if not isinstance(text, str) or not text:
        return text
    if START in text or _REFERENCE.search(text) or _SHORT_VARIANT.search(text):
        # A literal reserved marker in source data cannot become executable
        # catalogue syntax. Existing encoded snapshots are idempotent too.
        return text
    lines = text.splitlines(keepends=True)
    spans = [_fragments(line.rstrip('\r\n')) for line in lines]
    counts = Counter(value for rows in spans for _, _, value in rows)
    # Sorted content makes references deterministic, independent of dict order.
    values = [value for value, count in sorted(counts.items()) if count > 1]
    mapping = {}
    for value in values:
        key = f'@C{len(mapping)+1}'
        declaration = f'{key}={json.dumps(value, ensure_ascii=False)}\n'
        if counts[value] * (len(value) - len(key)) > len(declaration):
            mapping[value] = key
    if not mapping:
        return text
    rendered = []
    for line, rows in zip(lines, spans, strict=True):
        cursor, parts = 0, []
        for start, end, value in rows:
            parts.append(line[cursor:start])
            parts.append(mapping.get(value, value))
            cursor = end
        parts.append(line[cursor:])
        encoded = ''.join(parts)
        if abbreviate_variant_ids and encoded.startswith('• id='):
            encoded = _VARIANT.sub(lambda match: match.group().replace('variant_id=', 'v=', 1), encoded)
        rendered.append(encoded)
    legend = '\n'.join([START, GUIDANCE,
        *(f'{key}={json.dumps(value, ensure_ascii=False)}' for value, key in mapping.items()), END])
    # Insert after the unchanged catalogue heading, retaining the exact source
    # newline on both sides so decoding also preserves a trailing newline.
    first = rendered[0]
    if first.endswith('\n'):
        candidate = first + legend + '\n' + ''.join(rendered[1:])
    else:
        candidate = legend + '\n' + ''.join(rendered)
    return candidate if len(candidate) < len(text) else text


def fit_compact_catalog(lines, *, limit, footer='', minimum_products=0):
    """Count the entire legend, footer and explicit coverage notice in budget.

    Recompute references after dropping a whole product, so every retained
    reference is declared and no discarded product leaves misleading facts.
    """
    if type(limit) is not int or limit <= 0 or not lines:
        raise ValueError('catalog_budget_invalid')
    header, products = lines[0], list(lines[1:])
    if any(not row.startswith('• id=') for row in products):
        raise ValueError('catalog_product_line_invalid')
    total = len(products)
    if type(minimum_products) is not int or not 0 <= minimum_products <= total:
        raise ValueError('catalog_required_products_invalid')
    for keep in range(total, minimum_products - 1, -1):
        dropped = total - keep
        notice = ''
        heading = header
        if dropped:
            heading = header.replace('усі актуальні товари', 'неповний перелік актуальних товарів')
            notice = (f'\n…(показано {keep} товарів із {total}; {dropped} не вміщено — '
                'відсутність у цьому знімку не означає відсутність товару чи залишку; '
                'якщо клієнт питає про не показаний товар, уточни у менеджера)')
        source = '\n'.join([heading, *products[:keep]]) + notice + footer
        encoded = compact_catalog_text(source)
        if len(encoded) <= limit:
            return encoded, dropped
    # A meaningful coverage warning and mandatory rules cannot be partially cut.
    raise ValueError('catalog_budget_cannot_fit_coverage_and_rules')


def expand_compact_catalog_text(text):
    """Decode one shared legend, rejecting unknown or malformed references."""
    if START not in text:
        return text
    start = text.index(START)
    end = text.find(END + '\n', start)
    if end < 0 or text.count(START) != 1 or text.count(END) != 1:
        raise ValueError('catalog_shared_legend_invalid')
    declarations = text[start+len(START)+1:end].splitlines()
    if not declarations or declarations[0] != GUIDANCE:
        raise ValueError('catalog_shared_guidance_invalid')
    mapping = {}
    for declaration in declarations[1:]:
        key, separator, value = declaration.partition('=')
        if not separator or not _REFERENCE.fullmatch(key) or key in mapping:
            raise ValueError('catalog_shared_reference_invalid')
        parsed = json.loads(value)
        if not isinstance(parsed, str):
            raise ValueError('catalog_shared_value_invalid')
        mapping[key] = parsed
    body = text[:start] + text[end+len(END)+1:]
    def replace(match):
        if match.group() not in mapping:
            raise ValueError('catalog_shared_reference_missing')
        return mapping[match.group()]
    # Expand source fragments exactly once; literal content is never recursively
    # interpreted as another reference or another compact variant declaration.
    body = '\n'.join(_SHORT_VARIANT.sub(r'(variant_id=\1, ', line)
        if line.startswith('• id=') else line for line in body.split('\n'))
    return _REFERENCE.sub(replace, body)
