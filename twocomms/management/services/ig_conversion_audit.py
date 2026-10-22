"""Bounded descriptive additions to the existing conversation-analysis call.

No database/provider access, profile mutation, operational decision or probability
calibration. Customer quotes prove what was said, never an objective lost cause.
The caller owns source admission and verified reply-review context capture.
"""
from __future__ import annotations

import json
import math
import re

SCHEMA_VERSION = 1
MAX_ITEMS = 3
MAX_REFS = 4
MAX_QUOTE_CHARS = 160
MAX_JSON_CHARS = 8192
MOTIVATION_CODES = frozenset({'self', 'gift', 'team', 'resale', 'custom', 'unknown'})
REASON_CODES = frozenset({'price', 'relevance', 'product', 'size_fit', 'delivery_deadline',
                         'payment_friction', 'response_delay', 'handoff', 'unknown'})
CONFIDENCE = frozenset({'low', 'medium', 'high'})

CONVERSION_AUDIT_PROMPT_FRAGMENT = """
Optional conversion_audit extends this SAME analysis; do not add customer replies or actions:
{"schema_version":1,"motivation":[{"code":"self|gift|team|resale|custom|unknown",
"basis":"customer_stated|hypothesis|unknown","confidence":"low|medium|high",
"evidence":[{"message_id":123,"quote":"exact own customer quote"}]}],
"readiness_evidence":[{"message_id":123,"quote":"exact own customer quote"}],
"nonconversion_reasons":[{"code":"price|relevance|product|size_fit|delivery_deadline|payment_friction|response_delay|handoff|unknown",
"basis":"reported_by_customer|hypothesis|unknown","confidence":"low|medium|high",
"evidence":[{"message_id":124,"quote":"exact own customer quote"}],
"diagnostic_refs":[],"counterevidence":[]}]}
At most 3 motivations and 3 reasons, at most 4 references per list; quotes at most 160 characters.
Keep readiness, recipient motivation and possible service problems separate. Wanting or choosing
an item is readiness evidence, not proof it is for the customer's own use. Do not default motivation
to self: customer_stated self requires explicit personal use; if the recipient/purpose is unstated,
omit motivation or label a supported inference hypothesis. Respect explicit negation of a purpose.
Use current admitted user evidence only. reported_by_customer means a CUSTOMER-STATED CAUSE OF
REFUSAL, not merely a customer-stated concern: its one exact evidence quote must contain the
refusal/stopping decision AND the causal link to that particular reason. Price concern alone stays
price_objection in the baseline and, if included here, price/hypothesis; never a reported loss cause.
Silence, an unanswered message or elapsed time alone proves neither abandonment nor its cause.
Separately review each verified SENT diagnostic against its linked current customer questions and
already supplied product/size choices. If the observed reply clearly misses a requested answer,
asks again for supplied information or changes the selected product, record a relevance/product
HYPOTHESIS with that exact diagnostic ref and supporting customer quote, even without a refusal.
This describes an observed service risk, NOT a customer-confirmed reason or an abandoned sale.
Do not skip this comparison merely because the customer has not refused. If SENT coverage is
unavailable, the reply is unobservable: do not invent its content or a service problem. Check
counterevidence. Diagnostic refs must come from response_review_context's verified SENT records;
bot/manager words are not customer intent. SENT alone does not prove a delay problem or completed
human handoff. Timing/handoff suspicions need the corresponding captured diagnostics, not invented
waits or completed actions. Do not infer personality, income, military status or other
sensitive characteristics, even as a hypothesis. Reuse existing readiness fields as uncalibrated
model estimates, not measured purchase probabilities. Do not create style preferences: explicit
communication choices already have a deterministic source-bound owner. Findings are internal,
episode-bound descriptions, never authority for commerce, outreach, memory/profile changes,
code changes or prompt changes. No long reasoning narrative; empty arrays are valid.
""".strip()

_REASON_WORDS = {
    'price': r'цін\w*|цен\w*|дорог\w*|варт\w*|бюджет\w*|\b(?:price|cost|expensive|afford)\w*',
    'relevance': r'не\s+(?:відпові\w*|ответ\w*)|не\s+те\s+питан\w*|не\s+тот\s+вопрос|\b(?:irrelevant|unanswered)\b|didn.t\s+answer|not\s+(?:my|the)\s+question',
    'product': r'інш\w*\s+(?:товар|принт)|друг\w*\s+(?:товар|принт)|не\s+той\s+(?:товар|принт)|\b(?:wrong|different)\s+(?:product|print|item)',
    'size_fit': r'розмір\w*|размер\w*|посад\w*|\b(?:size|fit|tight|loose)\b',
    'delivery_deadline': r'достав\w*|відправ\w*|отправ\w*|дедлайн\w*|\b(?:delivery|shipping|deadline|arrival)\b',
    'payment_friction': r'оплат\w*|плат[іеё]ж\w*|передоплат\w*|\b(?:payment|prepayment|pay|card)\b',
    'response_delay': r'довго\w*|долго\w*|чек\w*|жда\w*|затрим\w*|задерж\w*|\b(?:wait|slow|delay)\w*',
    'handoff': r'менедж\w*|переда\w*|команд\w*|\b(?:manager|handoff|team)\b',
}
_REASON_PATTERNS = {key: re.compile(value, re.I) for key, value in _REASON_WORDS.items()}
_MOTIVATION_PATTERNS = {
    'self': re.compile(r'для\s+себе|собі|себе\s+беру|\b(?:myself|for\s+me)\b', re.I),
    'gift': re.compile(r'подар\w*|\b(?:gift|present)\b', re.I),
    'team': re.compile(r'команд\w*|колектив\w*|коллектив\w*|\bteam\b', re.I),
    'resale': re.compile(r'перепрод\w*|магазин\w*|опт\w*|\b(?:resale|resell|wholesale|shop|store)\b', re.I),
    'custom': re.compile(r'кастом\w*|свій\s+принт|свой\s+принт|\b(?:custom|own\s+print)\b', re.I),
}
_REFUSAL = re.compile(
    r'не\s+(?:беру|візьму|куплю|купую|купуємо|купуват\w*|замовлятим\w*|заказ\w*|покупа\w*)|'
    r'не\s+буду\s+(?:брат\w*|куп\w*|покуп\w*|замов\w*|заказ\w*)|'
    r'(?:брать|покупать|купувати|замовляти)\s+не\s+буд\w*|'
    r'відмовля\w*|отказыва\w*|передумав|передумала|'
    r'\b(?:won[’\x27]?t|will\s+not|not)\s+(?:buy\w*|order\w*)|'
    r'\b(?:cancel|decline|no\s+longer\s+interested)\b', re.I)
_CAUSAL = re.compile(r'\b(?:бо|тому|через|потому|поэтому|из-за|because|due\s+to|therefore|so)\b', re.I)
_NEGATED_REASON = re.compile(r'\b(?:не|not|no)\b\s+(?:(?:через|из-за|because\s+of|due\s+to)\s+)?$', re.I)
_MOTIVE_NEGATION = re.compile(r'\b(?:не|not|no|without|без)\b\s+(?:(?:в|на|як|as|a|an|for|для)\s+){0,2}$', re.I)
_REASON_DISCLAIMER = re.compile(
    r'^\s*(?:(?:тут|це|это|here|is|was|the|a|an)\s+){0,3}'
    r'(?:ні\s+до\s+чого|ни\s+при\s+ч[её]м|не\s+(?:причин\w*|проблем\w*)|not\s+(?:(?:the|a|an)\s+)?(?:problem|reason|issue|cause)\b)', re.I)
_REPORTED = re.compile(
    r'(?:менеджер\w*|клієнт\w*|клиент\w*|друг\w*|подруг\w*|він|вона|он|она|\b(?:manager|friend|he|she|they)\b)'
    r'[^.!?;]{0,40}(?:сказ\w*|каж\w*|говор\w*|напис\w*|\b(?:said|says|wrote|told)\b)', re.I)
_REF = re.compile(r'^revision_effect:[1-9][0-9]{0,18}$')
_HEX = re.compile(r'^[a-f0-9]{64}$')


def _enum(value, allowed, default):
    return value if isinstance(value, str) and value in allowed else default


def _sequence(value, limit):
    return value[:limit] if isinstance(value, list) else ()


def _user_source(by_id, message_id):
    if type(message_id) is not int or message_id <= 0 or not isinstance(by_id, dict):
        return None
    source = by_id.get(message_id)
    if (not isinstance(source, dict) or source.get('role') != 'user'
            or source.get('evidence_eligible') is False
            or source.get('analysis_scope', 'current_episode') != 'current_episode'
            or not isinstance(source.get('text'), str)):
        return None
    return source


def _proofs(value, by_id):
    result, seen = [], set()
    for raw in _sequence(value, 24):
        if not isinstance(raw, dict):
            continue
        message_id, quote = raw.get('message_id'), raw.get('quote')
        source = _user_source(by_id, message_id)
        from management.services.ig_reply_language import own_reply_text
        own = own_reply_text(source['text']) if source is not None else ''
        if _REPORTED.search(own):
            # Mask only the reported clause, retaining punctuation and exact
            # bytes of independent own clauses for literal quote verification.
            own = ''.join(' ' * len(part) if _REPORTED.search(part) else part
                          for part in re.split(r'([.!?;\n]+|\b(?:але|но|but)\b)', own, flags=re.I))
        if (source is None or not isinstance(quote, str) or not quote.strip()
                or len(quote) > MAX_QUOTE_CHARS or quote not in source['text'] or quote not in own):
            continue
        key = (message_id, quote)
        if key in seen:
            continue
        seen.add(key)
        result.append({'message_id': message_id, 'quote': quote})
        if len(result) == MAX_REFS:
            break
    return result


def _scope(context):
    if not isinstance(context, dict) or type(context.get('schema_version')) is not int or context['schema_version'] != 1:
        return {}
    required = ('client_id', 'watermark_message_id', 'reset_floor')
    if any(type(context.get(key)) is not int or context[key] <= 0 for key in required):
        return {}
    namespace, digest = context.get('namespace'), context.get('digest')
    epoch, episode = context.get('client_permission_epoch'), context.get('episode_id')
    if (not isinstance(namespace, str) or not namespace or len(namespace) > 128
            or not isinstance(digest, str) or not _HEX.fullmatch(digest)
            or type(epoch) is not int or epoch < 0
            or episode is not None and (type(episode) is not int or episode <= 0)):
        return {}
    return {'client_id': context['client_id'], 'source_namespace': namespace,
            'episode_id': episode, 'watermark_message_id': context['watermark_message_id'],
            'reset_floor': context['reset_floor'], 'client_permission_epoch': epoch,
            'response_review_digest': digest}


def _diagnostics(context, by_id):
    if not _scope(context) or context.get('coverage') != 'sent_receipts_observed':
        return {}
    allowed = context.get('diagnostic_refs')
    if not isinstance(allowed, list):
        return {}
    valid = {}
    for row in _sequence(context.get('diagnostics'), 16):
        if not isinstance(row, dict):
            continue
        ref, effect_id = row.get('ref'), row.get('effect_id')
        sources = row.get('source_message_ids')
        if (not isinstance(ref, str) or not _REF.fullmatch(ref) or ref not in allowed[:16]
                or type(effect_id) is not int or ref != f'revision_effect:{effect_id}'
                or row.get('kind') != 'sent_reply' or row.get('state') != 'sent'
                or not isinstance(row.get('provider_message_id'), str) or not row['provider_message_id']
                or not isinstance(row.get('text'), str) or (not row['text'].strip() and row.get('media_observed') is not True)
                or not isinstance(sources, list) or not 1 <= len(sources) <= 32
                or any(_user_source(by_id, value) is None for value in sources)):
            continue
        codes = set(REASON_CODES) if row['text'].strip() else {'response_delay', 'handoff', 'unknown'}
        seconds = row.get('response_seconds')
        if type(seconds) not in (int, float) or not math.isfinite(seconds) or seconds < 0:
            codes.discard('response_delay')
        valid[ref] = codes
    return valid


def _has_reason(code, quote):
    pattern = _REASON_PATTERNS.get(code)
    return bool(pattern and any(
        not _NEGATED_REASON.search(quote[max(0, match.start()-40):match.start()])
        and not _REASON_DISCLAIMER.search(quote[match.end():match.end()+72])
        for match in pattern.finditer(quote)))


def _motivation_negated(code, proofs):
    pattern = _MOTIVATION_PATTERNS.get(code)
    matches = [(row['quote'], match) for row in proofs for match in (pattern.finditer(row['quote']) if pattern else ())]
    return bool(matches and all(_MOTIVE_NEGATION.search(quote[max(0, match.start()-32):match.start()])
        or _REASON_DISCLAIMER.search(quote[match.end():match.end()+72]) for quote, match in matches))


def _reported_cause(code, proofs):
    # Bind the target reason to the actual causal span. A refusal elsewhere
    # in the sentence does not make every mentioned/exonerated topic its cause.
    for row in proofs:
        for clause in re.split(r'[.!?;\n]+|\b(?:але|но|but|однак|проте|однако|however|yet)\b', row['quote'], flags=re.I):
            if not _REFUSAL.search(clause):
                continue
            for connector in _CAUSAL.finditer(clause):
                backward = connector.group().casefold() in {'тому', 'поэтому', 'therefore', 'so'}
                span = clause[:connector.start()] if backward else clause[connector.end():]
                span = span.rstrip(' ,').rsplit(',', 1)[-1] if backward else span.split(',', 1)[0]
                if _has_reason(code, span):
                    return True
    return False


def normalize_conversion_audit(value, by_id, response_review_context=None):
    """Fail-soft optional JSON with exact own-source proof and no side effects.

    Caller-provided by_id and response_review_context are admitted captures,
    never model output. Scope is rebuilt from that context, not accepted from
    the optional JSON. A malformed section cannot invalidate baseline analysis.
    """
    if not isinstance(value, dict) or type(value.get('schema_version')) is not int or value['schema_version'] != SCHEMA_VERSION:
        return {}
    scope = _scope(response_review_context)
    if scope and isinstance(by_id, dict):
        by_id = {mid: source for mid, source in list(by_id.items())[:256]
            if type(mid) is int and scope['reset_floor'] <= mid <= scope['watermark_message_id']
            and isinstance(source, dict)
            and source.get('client_id', scope['client_id']) == scope['client_id']
            and source.get('provider_namespace', scope['source_namespace']) == scope['source_namespace']}
    result = {'schema_version': SCHEMA_VERSION, 'motivation': [],
              'readiness_evidence': _proofs(value.get('readiness_evidence'), by_id),
              'nonconversion_reasons': []}
    seen = set()
    for row in _sequence(value.get('motivation'), 16):
        if not isinstance(row, dict) or _enum(row.get('code'), MOTIVATION_CODES, '') == '' or row['code'] in seen:
            continue
        code = row['code']; proofs = _proofs(row.get('evidence'), by_id)
        if not proofs and code != 'unknown':
            continue
        if code != 'unknown' and _motivation_negated(code, proofs):
            continue
        basis = _enum(row.get('basis'), {'customer_stated', 'hypothesis', 'unknown'}, 'hypothesis')
        confidence = _enum(row.get('confidence'), CONFIDENCE, 'low')
        if code == 'unknown':
            basis, confidence = 'unknown', 'low'
        elif basis == 'customer_stated' and not any(_MOTIVATION_PATTERNS[code].search(p['quote']) for p in proofs):
            basis, confidence = 'hypothesis', 'low'
        result['motivation'].append(dict(code=code, basis=basis, confidence=confidence, evidence=proofs))
        seen.add(code)
        if len(result['motivation']) == MAX_ITEMS:
            break
    allowed_refs = _diagnostics(response_review_context, by_id)
    seen = set()
    for row in _sequence(value.get('nonconversion_reasons'), 24):
        if not isinstance(row, dict) or _enum(row.get('code'), REASON_CODES, '') == '' or row['code'] in seen:
            continue
        code = row['code']; proofs = _proofs(row.get('evidence'), by_id)
        counter = _proofs(row.get('counterevidence'), by_id)
        refs = list(dict.fromkeys(ref for ref in _sequence(row.get('diagnostic_refs'), 24)
                                 if isinstance(ref, str) and code in allowed_refs.get(ref, ())))[:MAX_REFS]
        if code != 'unknown' and not any(_has_reason(code, proof['quote']) for proof in proofs) and (not refs or code in {'price', 'payment_friction'}):
            continue
        basis = _enum(row.get('basis'), {'reported_by_customer', 'hypothesis', 'unknown'}, 'hypothesis')
        confidence = _enum(row.get('confidence'), CONFIDENCE, 'low')
        if code == 'unknown':
            basis, confidence = 'unknown', 'low'
        elif basis == 'reported_by_customer' and not _reported_cause(code, proofs):
            basis, confidence = 'hypothesis', 'low'
        # A linked SENT reply is diagnostic evidence, not a customer causal report.
        if not proofs or counter:
            confidence = 'low'
        if counter and basis == 'reported_by_customer':
            basis = 'hypothesis'
        result['nonconversion_reasons'].append(dict(code=code, basis=basis, confidence=confidence,
            evidence=proofs, diagnostic_refs=refs, counterevidence=counter))
        seen.add(code)
        if len(result['nonconversion_reasons']) == MAX_ITEMS:
            break
    if scope:
        result['_source_scope'] = scope
    # Whole entries are omitted if the optional metadata would crowd out the
    # baseline analysis; never truncate a quote into a different assertion.
    for key in ('nonconversion_reasons', 'motivation', 'readiness_evidence'):
        while result[key] and len(json.dumps(result, ensure_ascii=False, separators=(',', ':'))) > MAX_JSON_CHARS:
            result[key].pop()
    return result
