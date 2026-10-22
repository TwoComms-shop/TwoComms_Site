const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const template = fs.readFileSync(path.join(__dirname, '../../twocomms/management/templates/management/bot.html'), 'utf8');
function sourceBlock(start, end) {
  const offset = template.indexOf(start);
  assert.ok(offset >= 0, start);
  const limit = template.indexOf(end, offset);
  assert.ok(limit > offset, end);
  return template.slice(offset, limit);
}
function node(tag, className = '', value = '') {
  return { tag, className, dataset: {}, children: [], textContent: String(value),
    appendChild(child) { this.children.push(child); return child; },
    set innerHTML(_) { assert.fail('analysis content must use text nodes'); },
  };
}
function text(root) { return [root.textContent, ...root.children.map(text)].join(' '); }
function descendants(root) { return [root, ...root.children.flatMap(descendants)]; }
function harness({ pii = true } = {}) {
  const sandbox = { node, URL, Number,
    fmtDateTime: value => value,
    window: { location: { href: 'https://example.test/bot/?client_id=999&message_id=999#old' } },
  };
  vm.createContext(sandbox);
  let source = sourceBlock('    function conversionAuditForDetail(', '    function renderConversation(');
  source = source.replace(/\{% if bot_can_view_pii %\}([\s\S]*?)\{% endif %\}/g, (_, body) => pii ? body : '');
  vm.runInContext(source, sandbox);
  vm.runInContext(sourceBlock('    function percent(', '    function purchaseCountLabel('), sandbox);
  return sandbox;
}
function audit(data = {}, state = 'current') {
  return { client: { id: 42 }, potential: { conversion_audit: {
    state, fresh: state === 'current', episode_id: 7,
    intent_assessment: { value: null }, data,
  } } };
}

test('missing and stale analysis stay explicit and never expose old reasons or a zero estimate', () => {
  const h = harness();
  for (const state of ['missing', 'stale', 'unobservable', 'invalid']) {
    const root = node('div');
    const detail = audit({ nonconversion_reasons: [{ code: 'price', basis: 'reported_by_customer', evidence: [{ message_id: 3, quote: 'OLD_PRIVATE_REASON' }] }] }, state);
    h.renderConversionAudit(root, detail);
    assert.doesNotMatch(text(root), /OLD_PRIVATE_REASON|0%/);
    assert.match(text(root), /Невідоме не означає нульову/);
    assert.equal(h.analysisReadinessValue(detail.potential), null);
  }
  const detail = audit();
  assert.equal(h.analysisReadinessValue(detail.potential), null);
  detail.potential.conversion_audit.intent_assessment.value = '0.55';
  assert.equal(h.analysisReadinessValue(detail.potential), '0.55');
});

test('list score labels uncalibrated readiness and never turns missing or noncommercial intent into zero conversion', () => {
  const h = harness();
  for (const potential of [{ probability: 0 }, audit().potential,
    { conversion_audit: { state: 'stale', fresh: false, intent_assessment: { value: '0' } } }]) {
    const score = h.potentialScore({ potential });
    assert.equal(score.text, 'Готовність AI · невідома');
    assert.match(score.title, /не відкалібрована ймовірність купівлі/);
    assert.doesNotMatch(score.text, /0%/);
  }
  const detail = audit(); detail.potential.conversion_audit.intent_assessment.value = '0.55';
  const score = h.potentialScore(detail);
  assert.equal(score.text, 'Готовність AI · 55%');
  const lightweight = { state: 'current', probability: '0.55', interaction_type: 'product_interest',
    conversion_audit: { state: 'not_loaded', source_fresh: true, data: {} } };
  assert.equal(h.potentialScore({ potential: lightweight }).text, 'Готовність AI · 55%');
  assert.equal(h.potentialScore({ potential: { ...lightweight, interaction_type: 'unknown', probability: 0 } }).text,
    'Готовність AI · невідома');
  assert.equal(h.potentialScore({ potential: { ...lightweight, conversion_audit: { state: 'not_loaded', source_fresh: false } } }).text,
    'Готовність AI · невідома');
});

test('optional missing reason audit preserves an independently fresh commercial baseline for this same episode', () => {
  const h = harness();
  for (const conversion_audit of [{}, { state: 'missing', episode_id: 7 }, { state: 'unobservable', episode_id: 7 }]) {
    const detail = { potential: { state: 'current', fresh: true, source: 'conversation_analysis',
      scope: 'current_episode', interaction_type: 'product_interest', episode_id: 7, current_episode_id: 7,
      probability: '0.65', confidence: '0.8', evidence: [{ message_id: 8, quote: 'CURRENT_BASELINE' }],
      conversion_audit } };
    const p = h.analysisPotentialForDetail(detail);
    assert.equal(p.baseline_readiness, true);
    assert.equal(h.analysisReadinessValue(p), '0.65');
    assert.equal(p.confidence, '0.8');
    assert.equal(p.evidence[0].quote, 'CURRENT_BASELINE');
    assert.equal(h.potentialScore(detail).text, 'Готовність AI · 65%');
    const root = node('div'); h.renderConversionAudit(root, detail);
    assert.match(text(root), /Невідоме не означає нульову/);
    assert.doesNotMatch(text(root), /CURRENT_BASELINE/);
    for (const mismatch of [
      { potential: { ...detail.potential, state: 'stale' } },
      { potential: { ...detail.potential, fresh: false } },
      { potential: { ...detail.potential, state: 'policy' } },
      { potential: { ...detail.potential, interaction_type: 'unknown', probability: 0 } },
      { potential: { ...detail.potential, current_episode_id: 9 } },
      { ...detail, journey: { is_history: true, viewed_episode_id: 6 } },
      { ...detail, journey: { viewed_episode_id: 6 } },
    ]) {
      const hidden = h.analysisPotentialForDetail(mismatch);
      assert.notEqual(hidden.baseline_readiness, true);
      assert.equal(h.analysisReadinessValue(hidden), null);
      assert.equal(hidden.evidence.length, 0);
    }
  }
});

test('customer explanations, hypotheses and counterevidence remain separate with scoped safe sources', () => {
  const h = harness(); const root = node('div');
  h.renderConversionAudit(root, audit({
    motivation: [{ code: 'gift', basis: 'customer_stated', evidence: [{ message_id: 11, quote: 'На подарунок' }] }],
    readiness_evidence: [{ message_id: 12, quote: '<img src=x onerror=alert(1)>' }],
    nonconversion_reasons: [
      { code: 'price', basis: 'reported_by_customer', evidence: [{ message_id: 13, quote: 'Для мене дорого' }] },
      { code: 'response_delay', basis: 'hypothesis', confidence: 'low', evidence: [{ message_id: 'javascript:alert(1)', quote: 'Немає відповіді' }], counterevidence: [{ message_id: 14, quote: 'Ще думаю' }] },
    ],
  }));
  const groups = descendants(root).filter(item => item.tag === 'details');
  const customer = groups.find(item => text(item.children[0]) === 'Причини зі слів клієнта');
  const possible = groups.find(item => text(item.children[0]).startsWith('Можливі причини'));
  assert.match(text(customer), /Для мене дорого/);
  assert.doesNotMatch(text(customer), /Немає відповіді/);
  assert.match(text(possible), /Немає відповіді/);
  assert.match(text(possible), /Ще думаю/);
  assert.match(text(root), /не є відкаліброваною ймовірністю/);
  assert.match(text(root), /<img src=x onerror=alert\(1\)>/);
  const links = descendants(root).filter(item => item.tag === 'a');
  assert.equal(links.length, 4);
  links.forEach(link => {
    const url = new URL(link.href, 'https://example.test');
    assert.equal(url.pathname, '/bot/');
    assert.equal(url.searchParams.get('client_id'), '42');
    assert.equal(url.searchParams.has('view_episode_id'), false);
    assert.match(url.searchParams.get('message_id'), /^\d+$/);
    assert.equal(url.hash, '');
  });
});

test('source navigation is absent without conversation PII permission', () => {
  const h = harness({ pii: false }); const root = node('div');
  h.renderConversionAudit(root, audit({ readiness_evidence: [{ message_id: 3, quote: 'Own evidence' }] }));
  assert.equal(descendants(root).filter(item => item.tag === 'a').length, 0);
});

test('viewing a historical purchase cannot relabel current readiness or reasons as historical evidence', () => {
  const h = harness(); const root = node('div');
  const detail = audit({ nonconversion_reasons: [{ code: 'price', basis: 'reported_by_customer', evidence: [{ message_id: 3, quote: 'CURRENT_PURCHASE_REASON' }] }] });
  detail.potential.probability = 0.8;
  detail.potential.confidence = 0.9;
  detail.potential.evidence = [{ quote: 'CURRENT_QUOTE' }];
  detail.journey = { viewed_episode: { conversion_audit: { state: 'historical', fresh: false, episode_id: 2, data: {} } } };
  h.renderConversionAudit(root, detail);
  const potential = h.analysisPotentialForDetail(detail);
  assert.match(text(root), /Історична покупка/);
  assert.match(text(root), /Розбір покупки #2/);
  assert.doesNotMatch(text(root), /CURRENT_PURCHASE_REASON|CURRENT_QUOTE/);
  assert.equal(h.analysisReadinessValue(potential), null);
  assert.equal(potential.confidence, null);
  assert.equal(potential.evidence.length, 0);
});

test('manual episode changes redraw only the selected client analysis without another request', () => {
  const h = harness(); const current = audit();
  const host = { _analysisDetail: current }; const messages = {};
  const draws = [], restores = [];
  const reading = { atBottom: false, anchor: 'current visible message' };
  h.activeId = 42;
  h.conversationEl = { querySelector: () => host };
  h.document = { getElementById: () => messages };
  h.conversationIsDocumentScrolled = () => false;
  h.window.TwcBotChatReading = { capture: () => reading,
    restore: (...args) => restores.push(args) };
  h.renderAnalysisCard = (root, detail) => draws.push({ root, detail });
  h.fetch = () => assert.fail('episode analysis redraw must not fetch');
  const journey = { viewed_episode: { conversion_audit: { state: 'historical', episode_id: 2 } } };
  h.renderSelectedEpisodeAnalysis(journey, 42);
  assert.equal(draws.length, 1);
  assert.equal(draws[0].root, host);
  assert.equal(draws[0].detail.journey, journey);
  assert.equal(restores.length, 1);
  assert.equal(restores[0][0], messages);
  h.activeId = 99;
  h.renderSelectedEpisodeAnalysis(journey, 42);
  assert.equal(draws.length, 1);
});

test('receipt facts do not claim delivery, reading or semantic quality; missing timing stays unknown', () => {
  const h = harness(); const root = node('div');
  h.renderConversionAudit(root, audit({ _response_review: { coverage: 'sent_receipts_observed', diagnostics: [
    { delivery_state: 'provider_accepted', sent_at: '2026-10-23T10:00:00Z',
      time_basis: 'provider_event_to_provider_accept', response_seconds: null, source_message_ids: [22] },
  ] } }));
  assert.match(text(root), /Надсилання прийнято провайдером/);
  assert.match(text(root), /Час від повідомлення до прийняття надсилання: Не виміряно/);
  assert.match(text(root), /не підтверджує прочитання клієнтом чи якість відповіді/);
  assert.doesNotMatch(text(root), /0 с|Повідомлення доставлено|Якість підтверджено/);
});

function statsHarness() {
  const sandbox = { Number,
    numberFormat: new Intl.NumberFormat('uk-UA', { maximumFractionDigits: 0 }),
    fmtDateTime: value => value,
    esc: value => String(value).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;'),
  };
  vm.createContext(sandbox);
  vm.runInContext(sourceBlock('    function tableSection(', '    function compactCohortFacts(')
    + sourceBlock('    function auditCount(', '    function renderFollowupEffectiveness('), sandbox);
  return sandbox;
}

test('aggregate report separates bounded page coverage from total window and customer reasons from hypotheses', () => {
  const h = statsHarness();
  const html = h.renderConversionAuditStats({ conversion_audit_stats: {
    status: 'available', schema_version: 'ig-conversion-audit-stats.v1',
    counts: { qualifying_episodes: 120, page_episodes: 50, observed_audits: 20,
      missing_audit: 20, stale_audit: 5, unobservable_audit: 4, invalid_audit: 1,
      paid_observed: 5, without_confirmed_purchase: 45 },
    causes: [
      { code: 'price', outcome: 'confirmed_purchase', customer_reported_count: 2, possible_count: 3 },
      { code: 'response_delay', outcome: 'no_confirmed_purchase', customer_reported_count: 1, possible_count: 8 },
      { code: '<script>bad()</script>', outcome: 'no_confirmed_purchase', customer_reported_count: null, possible_count: null },
    ],
    observed_facts: { recorded_dropoffs: 12, silence_dropoffs: 8, recovered_dropoffs: 3,
      reply_latency_seconds: null, reply_latency_sample: 0, reply_latency_status: 'not_observed' },
    pagination: { has_more: true, limit: 50 },
    scope: { since: '<script>date</script>', until_exclusive: '2026-10-23', maximum_days: 31, window_bounded: true },
  } });
  assert.match(html, /Покупки у всьому вікні спостереження<\/td><td>120/);
  assert.match(html, /Оглянуто на цій сторінці<\/td><td>50/);
  assert.match(html, /усі причини, стани й оплати стосуються лише оглянутої сторінки/);
  assert.match(html, /Зі слів клієнта/);
  assert.match(html, /Можливе пояснення/);
  assert.match(html, /Спостережуваний час відповіді<\/td><td>Не виміряно/);
  assert.match(html, /не є випадковою вибіркою/);
  assert.match(html, /поза нею є ще записи/);
  assert.match(html, /&lt;script&gt;date&lt;\/script&gt;/);
  assert.doesNotMatch(html, /<script>|0%|ймовірність купівлі/);
  const unpaid = html.indexOf('Підтвердженої покупки поки немає</h3>');
  const paid = html.indexOf('Є підтверджена покупка</h3>');
  assert.ok(unpaid > 0 && paid > unpaid);
  assert.match(html.slice(unpaid, paid), /Очікування відповіді/);
  assert.doesNotMatch(html.slice(unpaid, paid), /Ціна/);
  assert.match(html.slice(paid), /Ціна/);
});

test('missing aggregate measurements remain unknown even if an unavailable report carries zeros', () => {
  const h = statsHarness();
  for (const report of [undefined, { status: 'unavailable', counts: { observed_audits: 0 } }]) {
    const html = h.renderConversionAuditStats({ conversion_audit_stats: report });
    assert.match(html, /не виміряно або зараз недоступні/);
    assert.doesNotMatch(html, /<td>0<\/td>|0%/);
  }
});
