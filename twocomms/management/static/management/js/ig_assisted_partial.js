(function (global, factory) {
  'use strict';
  const api = factory();
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  if (global) global.IgAssistedPartial = api;
})(typeof window !== 'undefined' ? window : null, function () {
  'use strict';
  const CAS = ['expected_request_version', 'expected_request_digest', 'expected_full_capture_digest', 'expected_decision_version', 'expected_draft_digest'];
  const scopes = {now: 'Готові зараз', later: 'Решта пізніше', together: 'Усі товари разом'};
  const dispositions = {ready: 'Готові зараз', deferred: 'Відкладено · ще не придбано', settled: 'У пов’язаному замовленні', inflight: 'Оформлення вже відкрите'};
  const clone = value => JSON.parse(JSON.stringify(value));
  function money(value) {
    if (typeof value !== 'string' || !/^(?:0|[1-9]\d{0,6})(?:[.,]\d{1,2})?$/.test(value.trim())) return null;
    const parts = value.trim().replace(',', '.').split('.');
    const cents = Number(parts[0]) * 100 + Number((parts[1] || '').padEnd(2, '0'));
    return Number.isSafeInteger(cents) && cents <= 100000000 ? Math.floor(cents / 100) + '.' + String(cents % 100).padStart(2, '0') : null;
  }
  function amount(value, currency) {const normalized = money(value); return normalized !== null && currency === 'UAH' ? normalized.replace('.', ',') + ' UAH' : 'ще не підтверджено';}
  function date(value) {
    if (typeof value !== 'string' || !value) return '';
    const at = new Date(value);
    return Number.isNaN(at.valueOf()) ? '' : at.toLocaleString('uk-UA', {timeZone: 'Europe/Kyiv', day: '2-digit', month: '2-digit', year: 'numeric', hour: '2-digit', minute: '2-digit'}) + ' (Київ)';
  }
  function timing(value, checkedAt) {
    if (!value || !Number.isInteger(value.min_days) || !Number.isInteger(value.max_days) || value.min_days < 0 || value.max_days < value.min_days || value.max_days > 90 || !['calendar', 'business'].includes(value.unit)) return 'Строк не визначено';
    const anchor = date(value.anchor_at || checkedAt), range = value.min_days === value.max_days ? String(value.min_days) : value.min_days + '–' + value.max_days;
    if (value.min_days === 0 && value.max_days === 0) return anchor ? 'У день перевірки · ' + anchor : 'У день перевірки · дату не підтверджено';
    return range + (value.unit === 'business' ? ' робочих днів' : ' календарних днів') + (anchor ? ' · від перевірки ' + anchor : ' · початок відліку не підтверджено');
  }
  function reason(code) {
    const groups = [
      [['source_changed', 'source_capture_changed', 'request_source_changed', 'request_expectation_changed', 'coverage_changed', 'coverage_digest_changed', 'terms_changed', 'decision_head_changed', 'assisted_catalog_price_changed', 'assisted_catalog_facts_changed'], 'Кошик, перевірка або умови змінилися. Чернетку залишено для звірки; потрібне актуальне підтвердження.'],
      [['choice_missing', 'purchase_authority_missing', 'customer_purchase_required', 'choice_not_current', 'choice_digest_changed'], 'Потрібне справжнє підтвердження клієнта для цих товарів і цих умов. Вибір очікування не дозволяє оформлення.'],
      [['delivery_unknown', 'offer_delivery_unknown', 'native_receipt_missing', 'offer_not_sent'], 'Надсилання клієнту ще не підтверджено. Звірте нативний результат; нове повідомлення автоматично не створюється.'],
      [['selected_scope_inflight', 'coverage_overlap', 'resource_review_required', 'payment_reconciliation_required', 'intention_inflight'], 'Для цих товарів уже є незавершене оформлення або звірка оплати. Перевірте наявний результат перед новою дією.'],
      [['assisted_customer_shipping_maximum_exceeded', 'assisted_merchant_shipping_recipient_charge'], 'Фактична оплата доставки клієнтом не відповідає погодженим умовам. Клієнту не можна додати різницю; звірте платника й тариф існуючої ТТН.'],
      [['assisted_carrier_fee_unknown', 'assisted_carrier_evidence_missing', 'assisted_carrier_document_unverified', 'assisted_carrier_evidence_invalid', 'assisted_shipping_provider_review_required'], 'Перевізник ще не підтвердив фактичну вартість і платника цієї ТТН. Доставка потребує звірки; погоджений максимум залишається незмінним.'],
      [['assisted_shipping_review_owner_changed', 'assisted_shipping_attachment_changed', 'assisted_carrier_evidence_owner_changed'], 'Документ або погоджені умови доставки змінилися. Оновіть дані й звірте актуальну ТТН перед повторною дією.'],
      [['approval_expired', 'manager_review_required', 'fresh_manager_review_required', 'remainder_manager_review_required'], 'Потрібна свіжа перевірка менеджером і затвердження актуальних товарів, ціни та умов.'],
      [['assisted_stock_recount_required', 'insufficient_stock', 'catalog_stock_missing'], 'Підтвердіть фактичні залишки через переоблік перед оформленням. Кількість у кошику не є загальним складським залишком.']
    ];
    const found = groups.find(([codes]) => codes.includes(code));
    return found ? found[1] : code === 'operation_conflict' ? 'Операція має інший збережений вміст. Звірте її результат; нова операція не створюється.' : 'Дія зараз недоступна. Звірте поточне звернення, умови та підтвердження клієнта.';
  }
  function deliveryScopes(quote) {
    if (quote && quote.delivery_scope === 'later' && Array.isArray(quote.deferred_line_ids)) {
      return quote.deferred_line_ids.length ? [['later', 'Готові зараз та наступні окремі відправки'], ['together', 'Уся решта разом']] : [['later', 'Ця відправка']];
    }
    return Object.entries(scopes);
  }
  function isRelevant(p) {
    if (!p) return false;
    const lines = Array.isArray(p.lines) ? p.lines : [], coverage = p.coverage || {}, executions = Array.isArray(coverage.executions) ? coverage.executions : [];
    const knownFull = executions.length > 0 && executions.every(execution => execution.scope === 'full');
    const blocked = ['resource_review_required', 'payment_reconciliation_required', 'intention_inflight', 'coverage_overlap', 'intention_scope_overlap', 'settled_execution_projection_unverified', 'released_intention_requires_review', 'historical_origin_unverified', 'intention_quantity_overlap'].includes(p.reason);
    return Boolean(blocked || lines.some(line => line.disposition === 'deferred') && lines.some(line => line.disposition === 'ready') || p.offer || p.continuation_offer || (p.orders || []).length && !knownFull || p.can_resume === true || p.can_prepare_remainder === true);
  }
  class Controller {
    constructor(root, options) {
      this.root = root; this.document = root.ownerDocument; this.options = options || {};
      this.apiUrl = this.options.apiUrl || root.dataset.apiUrl || '';
      this.fetch = this.options.fetch || (typeof window !== 'undefined' && window.fetch.bind(window));
      this.canOperate = this.options.canOperate === true;
      this.csrf = this.options.csrfToken || root.dataset.csrfToken || '';
      this.uuid = this.options.operationId || (() => {
        const crypto = this.options.crypto || (typeof window !== 'undefined' && window.crypto);
        if (!crypto || typeof crypto.randomUUID !== 'function') throw new Error('Оновіть сторінку для безпечного збереження.');
        return crypto.randomUUID();
      });
      this.commerce = null; this.projection = null; this.draft = null; this.pending = null;
      this.busy = false; this.unknown = false; this.destroyed = false; this.generation = 0; this.checkout = null; this.lastState = ''; this.termsEditorOpen = false; this.termsDisclosure = null;
      root.classList.add('ig-assisted-partial'); this.draw();
    }
    node(tag, cls, text) {const node = this.document.createElement(tag); if (cls) node.className = cls; if (text != null) node.textContent = String(text); return node;}
    note(text, cls) {return this.node('p', cls || 'ig-ap-muted', text);}
    button(text, callback, primary) {const node = this.node('button', 'ig-ap-button' + (primary ? ' primary' : ''), text); node.type = 'button'; node.addEventListener('click', callback); return node;}
    field(label, control) {const node = this.node('label', 'ig-ap-field', label); node.appendChild(control); return node;}
    disabled(node, value) {node.dataset.apDisabled = String(Boolean(value)); node.disabled = Boolean(value) || this.busy || this.unknown;}
    lock(value) {this.busy = value; this.root.setAttribute('aria-busy', String(value)); this.root.querySelectorAll('button,input,select').forEach(node => {node.disabled = value || this.unknown || node.dataset.apDisabled === 'true';}); if (this.refreshButton) this.refreshButton.disabled = value; if (this.recoveryButton) this.recoveryButton.disabled = value; this.keepTermsEditorOpen(); this.notifyState();}
    notifyState() {const signature = JSON.stringify([this.busy, this.dirty, Boolean(this.pending), this.unknown]); if (signature !== this.lastState) {this.lastState = signature; if (this.options.onStateChange) this.options.onStateChange();}}
    say(text, error) {if (this.feedback) {this.feedback.textContent = text || ''; this.feedback.classList.toggle('is-error', Boolean(error));}}
    get dirty() {return Boolean(this.draft && this.draft.dirty);}
    canLeave() {return !this.dirty && !this.pending && !this.busy;}
    canAct() {return this.canOperate && !this.pending && !this.unknown && (!this.options.canAct || this.options.canAct() === true);}
    frame() {const p = this.projection || {}; return JSON.stringify([p.request_id, p.request_digest, this.commerce && this.commerce.cas, p.coverage_digest]);}
    draftFrom() {
      const saved = this.projection.shipping_terms || {}, terms = {};
      Object.keys(scopes).forEach(scope => {const value = saved[scope] || {}; terms[scope] = {payer: value.payer || '', customer_maximum: typeof value.customer_maximum === 'string' ? value.customer_maximum : '', currency: 'UAH'};});
      return {frame: this.frame(), requestId: this.projection.request_id, cas: clone(this.commerce.cas || {}), items: clone(this.commerce.editable_items || []), terms, dirty: false};
    }
    conflicted() {return Boolean(this.draft && this.draft.frame !== this.frame());}
    receiptMatches(receipt, action) {
      if (!this.pending || this.pending.action !== action || !receipt) return false;
      const body = JSON.parse(this.pending.body);
      if (receipt.operation_id !== body.operation_id) return false;
      const keys = ['request_id', 'decision_id', 'expected_request_digest', 'expected_decision_digest', 'expected_coverage_digest', 'expected_terms_digest', 'task_id', 'expected_choice_digest'].filter(key => body[key] != null);
      return keys.every(key => key === 'task_id' ? Number.isSafeInteger(receipt[key]) && receipt[key] > 0 && receipt[key] === body[key] : typeof receipt[key] === 'string' && receipt[key] === body[key]);
    }
    shippingReceiptMatches(receipt) {if (!this.pending || this.pending.action !== 'resolve_shipping_review' || !receipt || receipt.ready !== true) return false; const body = JSON.parse(this.pending.body); return receipt.operation_id === body.operation_id && receipt.order_id === body.order_id && receipt.expected_document_ref === body.expected_document_ref && receipt.expected_terms_digest === body.expected_terms_digest;}
    update(commerce) {
      if (this.destroyed) return false;
      const previous = this.projection && this.projection.request_id;
      this.commerce = commerce || {}; this.projection = this.commerce.partial_intention || null;
      if (previous && this.projection && previous !== this.projection.request_id) this.checkout = null;
      const p = this.projection || {}; if ((p.orders || []).some(order => this.shippingReceiptMatches((order.shipping_review || {}).operation_receipt))) {this.pending = null; this.unknown = false;} const offer = this.pending && this.pending.action === 'issue_continuation_offer' ? p.continuation_offer : p.offer;
      if (this.pending && ['issue_partial_offer', 'issue_continuation_offer'].includes(this.pending.action) && offer && offer.task_id && offer.card_digest && this.receiptMatches(offer, this.pending.action)) {this.pending = null; this.unknown = false;}
      const prepared = this.commerce.prepared_checkout;
      if (prepared && prepared.ready === true && prepared.action === 'prepare_selected') {const matched = this.receiptMatches(prepared, 'prepare_selected'); if (matched) {this.pending = null; this.unknown = false;} if (!this.pending || !this.unknown) this.checkout = clone(prepared);}
      if (this.projection && (!this.draft || !this.draft.dirty && !this.pending)) this.draft = this.draftFrom();
      if (!this.projection && !this.dirty && !this.pending) this.draft = null;
      this.draw(); return true;
    }
    async refresh() {
      if (this.busy || this.destroyed) return false;
      if (!this.options.onRefresh) {this.say('Оновіть картку клієнта для актуальних даних.'); return false;}
      this.lock(true); this.say('Оновлюємо погоджені умови…');
      try {const result = await this.options.onRefresh({requestId: this.pending && this.pending.requestId || null}); if (this.destroyed) return false; if (result === false) {this.say('Не вдалося прочитати актуальні умови. Введені значення збережено.', true); return false;} this.say(this.unknown ? 'Результат попередньої операції ще не звірено.' : ''); return true;}
      catch (error) {if (!this.destroyed) this.say(error.message || 'Не вдалося прочитати актуальні умови. Введені значення збережено.', true); return false;}
      finally {if (!this.destroyed) {this.lock(false); this.updateActions();}}
    }
    termsEditorNeedsOpen() {return Boolean(this.dirty || this.conflicted() || this.pending && ['draft', 'approve'].includes(this.pending.action));}
    keepTermsEditorOpen() {if (this.termsDisclosure && this.termsEditorNeedsOpen()) {this.termsDisclosure.open = true; this.termsEditorOpen = true;}}
    termsEditable() {return Boolean(this.canAct() && this.projection && this.projection.ready === true && this.commerce && this.commerce.status === 'current' && this.commerce.request && this.commerce.request.id === this.projection.request_id && !this.conflicted());}
    termsPayload() {
      if (!this.termsEditable() || !this.draft || !this.draft.dirty) return null;
      const terms = {};
      for (const scope of Object.keys(scopes)) {
        const value = this.draft.terms[scope], maximum = money(value.customer_maximum);
        if (!['customer', 'merchant'].includes(value.payer) || maximum === null || value.payer === 'merchant' && maximum !== '0.00') return null;
        terms[scope] = {payer: value.payer, customer_maximum: maximum, currency: 'UAH'};
      }
      const payload = {action: 'draft', request_id: this.draft.requestId, items: clone(this.draft.items), shipping_terms: terms};
      CAS.forEach(key => {payload[key] = this.draft.cas[key];}); return payload;
    }
    canApprove() {const p = this.projection || {}, c = this.commerce || {}; return Boolean(this.termsEditable() && !this.dirty && p.can_approve_terms === true && c.can_approve === true && c.decision_head && c.decision_head.status === 'draft' && c.decision_head.id === p.decision_id && p.scoped_quote && money(p.scoped_quote.total) !== null && p.scoped_quote.currency === 'UAH');}
    approvePayload() {if (!this.canApprove()) return null; const payload = {action: 'approve', request_id: this.projection.request_id}; CAS.forEach(key => {payload[key] = this.commerce.cas[key];}); return payload;}
    actionPayload(action) {
      const p = this.projection || {};
      if (!this.canAct() || this.dirty || this.conflicted()) return null;
      const allowed = action === 'issue_partial_offer' ? p.can_issue === true : action === 'prepare_selected' ? p.can_prepare_selected === true || p.can_prepare_remainder === true : action === 'issue_continuation_offer' ? p.can_resume === true : false;
      if (!allowed) return null;
      const payload = {action, request_id: p.request_id, expected_request_digest: p.request_digest, expected_coverage_digest: p.coverage_digest};
      if (action !== 'issue_continuation_offer') Object.assign(payload, {decision_id: p.decision_id, expected_decision_digest: p.decision_digest, expected_terms_digest: p.terms_digest});
      if (action === 'prepare_selected') {
        const offer = p.offer || {}, choice = offer.choice || {};
        if (choice.action !== 'partial' || !choice.source_id || (offer.delivery || {}).status !== 'sent' || (offer.delivery || {}).receipt_verified !== true || !(offer.delivery || {}).native_mid) return null;
        Object.assign(payload, {task_id: offer.task_id, expected_choice_digest: choice.choice_digest});
      }
      return Object.entries(payload).every(([key, value]) => key === 'action' || (key === 'task_id' ? Number.isSafeInteger(value) && value > 0 : typeof value === 'string' && value)) ? payload : null;
    }
    updateActions() {
      (this.termControls || []).forEach(({payer, cap, value}) => {const enabled = this.termsEditable(); this.disabled(payer, !enabled); this.disabled(cap, !enabled || value.payer === 'merchant');});
      if (this.saveButton) this.disabled(this.saveButton, !this.termsPayload());
      if (this.approveButton) this.disabled(this.approveButton, !this.canApprove());
      if (this.issueButton) this.disabled(this.issueButton, !this.actionPayload('issue_partial_offer'));
      if (this.prepareButton) this.disabled(this.prepareButton, !this.actionPayload('prepare_selected'));
      if (this.resumeButton) this.disabled(this.resumeButton, !this.actionPayload('issue_continuation_offer'));
      if (this.discardButton) this.disabled(this.discardButton, !this.dirty); (this.reviewControls || []).forEach(({button, order}) => this.disabled(button, !this.shippingPayload(order))); this.keepTermsEditorOpen(); this.notifyState();
    }
    stageLabel(p) {
      const consumed = ['scope_overlap', 'assisted_scope_overlap', 'intention_scope_overlap', 'coverage_overlap', 'selected_capacity_already_consumed', 'selected_scope_inflight', 'intention_inflight', 'assisted_checkout_already_prepared', 'already_prepared'];
      const offer = p.offer || {}, quote = p.scoped_quote || (offer.request_id === p.request_id ? offer.scoped_quote : null) || {};
      const selected = quote.selected_line_ids || [], lines = p.lines || [];
      const matched = selected.map(id => lines.find(line => line.line_id === id));
      const complete = lines.length > 0 && lines.every(line => line.disposition === 'settled') && (p.coverage || {}).ready === true && (p.orders || []).length > 0;
      const settled = complete || selected.length > 0 && matched.every(line => line && line.disposition === 'settled');
      const inflight = selected.length > 0 && matched.every(line => line && ['inflight', 'settled'].includes(line.disposition)) && matched.some(line => line.disposition === 'inflight');
      if (p.reason === 'existing_invoice_commitment_requires_review' && !this.pending && !this.dirty && !this.unknown && this.commerce.status === 'prepared' && (p.coverage || {}).ready === true && inflight && ((p.coverage || {}).executions || []).some(row => row.request_id === p.request_id && row.decision_id === p.decision_id && row.state === 'inflight' && Array.isArray(row.line_ids) && selected.every(id => row.line_ids.includes(id)))) return 'Оформлення готової частини вже підготовлено. Перегляньте наявну пропозицію; оплата ще не підтверджена.';
      if (consumed.includes(p.reason) || ['coverage_changed', 'residual_coverage_changed'].includes(p.reason) && (settled || inflight)) {
        if (settled) return 'Ці товари вже у пов’язаному замовленні. Повторне оформлення не потрібне; оплату й доставку перевірте в замовленні.';
        if (inflight || this.checkout && this.checkout.ready === true) return 'Оформлення цих товарів уже відкрите. Перегляньте наявну пропозицію; нова не створюється. Оплата ще не підтверджена.';
        return 'Для цих товарів уже є пов’язане оформлення. Звірте наявну пропозицію або замовлення перед наступною дією.';
      }
      return p.reason_label || (p.reason ? reason(p.reason) : '');
    }
    settledOffer(p, offer) {
      if (this.pending || this.dirty || offer.request_id !== p.request_id || offer.expected_decision_digest !== p.decision_digest || offer.expected_terms_digest !== p.terms_digest) return false;
      const quote = offer.scoped_quote || {}, selected = quote.selected_line_ids, current = p.scoped_quote;
      const delivery = offer.delivery || {}, choice = offer.choice || {};
      if (!Array.isArray(selected) || !selected.length || current && JSON.stringify(current.selected_line_ids) !== JSON.stringify(selected) || delivery.status !== 'sent' || delivery.receipt_verified !== true || !delivery.native_mid || choice.action !== 'partial' || !choice.choice_digest || !choice.source_id) return false;
      return selected.every(id => (p.lines || []).some(line => line.line_id === id && line.disposition === 'settled') &&
        (p.orders || []).some(order => Number.isSafeInteger(order.order_id) && order.order_id > 0 && Array.isArray(order.line_ids) && order.line_ids.includes(id)));
    }
    settledCheckoutOrder() {
      if(this.pending || this.dirty || this.unknown || !this.checkout)return null;
      const p=this.projection||{},coverage=p.coverage||{},ref=this.checkout,quote=p.scoped_quote||((p.offer||{}).request_id===p.request_id?(p.offer||{}).scoped_quote:null)||{},ids=quote.selected_line_ids;
      if(typeof ref.request_id!=='string'||!ref.request_id||typeof ref.decision_id!=='string'||!ref.decision_id||coverage.ready!==true||p.request_id!==ref.request_id||p.decision_id!==ref.decision_id||!Array.isArray(ids)||!ids.length||ref.expected_decision_digest&&ref.expected_decision_digest!==p.decision_digest||ref.expected_request_digest&&ref.expected_request_digest!==p.request_digest)return null;
      const execution=(coverage.executions||[]).find(row=>typeof row.execution_id==='string'&&row.execution_id&&row.request_id===ref.request_id&&row.decision_id===ref.decision_id&&row.state==='settled'&&Array.isArray(row.line_ids)&&ids.every(id=>row.line_ids.includes(id)));
      return execution&&(p.orders||[]).find(order=>Number.isSafeInteger(order.order_id)&&order.order_id>0&&order.execution_id===execution.execution_id&&Array.isArray(order.line_ids)&&ids.every(id=>order.line_ids.includes(id)))||null;
    }
    basket(p) {
      const columns = this.node('div', 'ig-ap-columns'), lines = Array.isArray(p.lines) ? p.lines : [];
      const checked = this.commerce && this.commerce.approved_head && this.commerce.approved_head.physical_checked_at;
      [['Готові зараз', ['ready']], ['Решта кошика', ['deferred']], ['В оформленні та замовленнях', ['settled', 'inflight']]].forEach(([title, types]) => {
        const groupLines = lines.filter(line => types.includes(line.disposition)); if (!groupLines.length) return;
        const group = this.node('section', 'ig-ap-group'); group.appendChild(this.node('h4', '', title));
        groupLines.forEach(line => {
          const row = this.node('article', 'ig-ap-line'), head = this.node('div', 'ig-ap-line-head');
          head.append(this.node('b', 'ig-ap-line-title', line.title || 'Назву ще не підтверджено'), this.node('span', 'ig-ap-state ' + line.disposition, dispositions[line.disposition]));
          row.append(head, this.note([line.color, line.size, Number.isInteger(line.quantity) ? line.quantity + ' шт.' : 'кількість не підтверджена'].filter(Boolean).join(' · '), 'ig-ap-facts'));
          if (line.recipient_label) row.appendChild(this.note('Для кого: ' + line.recipient_label));
          if (line.disposition !== 'settled' && line.disposition !== 'inflight') row.append(this.note('Готовність: ' + timing(line.preparation, line.checked_at || checked)), this.note('Відправка: ' + timing(line.dispatch, line.checked_at || checked)));
          group.appendChild(row);
        }); columns.appendChild(group);
      }); return columns;
    }
    termsEditor(p) {
      const section = this.node('section', 'ig-ap-terms'); section.append(this.node('h4', '', 'Ціна готових товарів і умови доставки'), this.note('Усі кількості позиції залишаються разом. Максимум для клієнта — погоджена межа, а не розрахунок перевізника; фактична вартість може бути нижчою.'));
      const quote = p.scoped_quote;
      section.appendChild(this.note((this.commerce.decision_head && this.commerce.decision_head.status === 'approved' ? 'Товари до оплати зараз: ' : 'Ціна готових товарів для затвердження: ') + (quote ? amount(quote.total, quote.currency) : 'ціна ще не затверджена'), 'ig-ap-due'));
      if (quote && scopes[quote.delivery_scope]) {const currentScope = deliveryScopes(quote).find(([scope]) => scope === quote.delivery_scope); section.appendChild(this.note('Для цього оформлення діють умови доставки: «' + (currentScope ? currentScope[1] : scopes[quote.delivery_scope]) + '».'));}
      section.appendChild(this.note('Ця сума не включає відкладені товари та їхню майбутню доставку. Перевізник визначає прибуття; воно не гарантується.'));
      if (!this.draft) return section;
      if (this.conflicted()) section.appendChild(this.note('Актуальна версія змінилася. Введені значення збережено для звірки; перенесення на нову версію немає.', 'ig-ap-warning'));
      let editHost = section; const compact = Boolean(this.commerce.decision_head && this.commerce.decision_head.status === 'approved' && p.shipping_terms);
      if (compact) {const saved = this.node('dl', 'ig-ap-saved-terms'); deliveryScopes(quote).forEach(([scope, title]) => {const term = p.shipping_terms[scope]; if (!term) return; const row = this.node('div', quote && quote.delivery_scope === scope ? 'is-current' : ''); row.append(this.node('dt', '', title + ' · ' + (term.payer === 'merchant' ? 'TwoComms' : term.payer === 'customer' ? 'Клієнт' : 'платника не підтверджено')), this.node('dd', '', amount(term.customer_maximum, term.currency))); saved.appendChild(row);}); section.append(this.note('Максимальні витрати на доставку, затверджені менеджером'), saved); const details = this.node('details', 'ig-ap-terms-editor'); details.open = this.termsEditorOpen || this.termsEditorNeedsOpen(); details.appendChild(this.node('summary', '', this.canOperate ? 'Змінити умови доставки' : 'Переглянути поля умов')); details.addEventListener('toggle', () => {if (!details.open && this.termsEditorNeedsOpen()) details.open = true; this.termsEditorOpen = details.open;}); this.termsDisclosure = details; editHost = details;}
      const list = this.node('div', 'ig-ap-terms-list');
      (this.conflicted() ? Object.entries(scopes) : deliveryScopes(quote)).forEach(([scope, title]) => {
        const value = this.draft.terms[scope], box = this.node('fieldset', 'ig-ap-term'); box.appendChild(this.node('legend', '', title));
        const payer = this.node('select'); payer.setAttribute('aria-label', 'Платник — ' + title);
        [['', 'Оберіть платника'], ['customer', 'Клієнт'], ['merchant', 'TwoComms']].forEach(([key, text]) => {const option = this.node('option', '', text); option.value = key; payer.appendChild(option);}); payer.value = value.payer;
        const cap = this.node('input'); cap.type = 'text'; cap.inputMode = 'decimal'; cap.maxLength = 10; cap.value = value.customer_maximum; cap.setAttribute('aria-label', 'Максимальна вартість для клієнта — ' + title);
        const editable = this.termsEditable();
        payer.addEventListener('change', () => {if (!this.termsEditable()) return; value.payer = payer.value; if (value.payer === 'merchant') {value.customer_maximum = '0.00'; cap.value = '0.00';} this.draft.dirty = true; this.disabled(cap, !this.termsEditable() || value.payer === 'merchant'); this.updateActions();});
        cap.addEventListener('input', () => {if (!this.termsEditable()) return; value.customer_maximum = cap.value; this.draft.dirty = true; this.updateActions();});
        this.termControls.push({payer, cap, value}); this.disabled(payer, !editable); this.disabled(cap, !editable || value.payer === 'merchant'); box.append(this.field('Платник доставки', payer), this.field('Максимум для клієнта, UAH', cap)); list.appendChild(box);
      }); editHost.appendChild(list);
      if (p.can_issue === true && Array.isArray(p.buttons_preview) && p.buttons_preview.length) {const previews = this.node('div', 'ig-ap-options'); p.buttons_preview.forEach(button => {const label = typeof button === 'string' ? button : button.title || button.label; if (label) previews.appendChild(this.node('span', '', label));}); section.append(this.note('Варіанти, які буде запропоновано клієнту'), previews);}
      if (this.canOperate) {
        const actions = this.node('div', 'ig-ap-actions');
        this.saveButton = this.button('Зберегти умови доставки', () => {const body = this.termsPayload(); if (body) this.perform(body);}, true);
        this.approveButton = this.button('Затвердити умови й ціну готових товарів', () => {const body = this.approvePayload(); if (body) this.perform(body);});
        this.discardButton = this.button('Відкинути чернетку умов', () => {if (this.pending) return; this.draft = this.draftFrom(); this.draw();});
        actions.append(this.saveButton, this.approveButton, this.discardButton); editHost.append(actions, this.note('Збереження і затвердження — окремі дії. Змінені умови потребують нового вибору клієнта.'));
      } if (compact) section.appendChild(this.termsDisclosure); return section;
    }
    offerBox(offer, continuation) {
      const section = this.node('section', continuation ? 'ig-ap-resume' : 'ig-ap-offer');
      section.appendChild(this.node('h4', '', continuation ? 'Повернення до решти товарів' : 'Вибір клієнта'));
      if (!continuation && offer.scoped_quote) section.appendChild(this.note('За збереженою пропозицією: товари готової частини ' + amount(offer.scoped_quote.total, offer.scoped_quote.currency), 'ig-ap-due'));
      if (!continuation && offer.shipping_terms) {const caps = this.node('div', 'ig-ap-terms-list'); deliveryScopes(offer.scoped_quote).forEach(([scope, title]) => {const term = offer.shipping_terms[scope]; if (!term) return; const card = this.node('section', 'ig-ap-term'); card.append(this.node('h5', '', title), this.note(term.payer === 'merchant' ? 'Доставку оплачує TwoComms' : term.payer === 'customer' ? 'Доставку оплачує клієнт' : 'Платника не підтверджено'), this.note('Максимум для клієнта: ' + amount(term.customer_maximum, term.currency), 'ig-ap-cap')); caps.appendChild(card);}); section.appendChild(caps);}
      if (!continuation && offer.scoped_quote && offer.scoped_quote.delivery_scope === 'later' && (offer.scoped_quote.deferred_line_ids || []).length) section.appendChild(this.note('Для наступних товарів потрібні свіжа перевірка менеджера, нове погодження умов і власний вибір клієнта.'));
      if (continuation) section.appendChild(this.note('Це запрошення повернутися до невиконаних побажань. Воно не підтверджує наявність, ціну, оплату або підписку на сповіщення.'));
      const previews = this.node('div', 'ig-ap-options'), buttons = Array.isArray(offer.buttons) ? offer.buttons : [];
      const labels = buttons.map(button => typeof button === 'string' ? button : button.label || button.title).filter(Boolean);
      labels.forEach(label => previews.appendChild(this.node('span', '', label))); if (!labels.length) previews.appendChild(this.note('Кнопки у нативному повідомленні ще не підтверджено.'));
      section.append(this.note('Кнопки у повідомленні клієнту'), previews);
      if (offer.text) {const details = this.node('details'); details.append(this.node('summary', '', 'Точне повідомлення клієнту'), this.note(offer.text)); section.appendChild(details);}
      const delivery = offer.delivery || {}, sent = delivery.status === 'sent' && delivery.receipt_verified === true && typeof delivery.native_mid === 'string' && delivery.native_mid;
      const status = sent ? 'Нативне надсилання підтверджено' : delivery.status === 'unknown' ? 'Результат надсилання невідомий. Потрібна звірка; нове повідомлення не створюйте.' : ['pending', 'queued'].includes(delivery.status) ? 'Повідомлення очікує надсилання' : delivery.status === 'cancelled' ? 'Надсилання скасовано' : 'Підтвердження надсилання відсутнє';
      section.appendChild(this.note(status + (sent && date(delivery.sent_at) ? ' · ' + date(delivery.sent_at) : ''), 'ig-ap-receipt'));
      if (date(offer.expires_at)) section.appendChild(this.note('Умови діють до: ' + date(offer.expires_at)));
      const choice = offer.choice;
      if (choice && ['partial', 'together', 'resume'].includes(choice.action) && choice.choice_digest && choice.source_id && sent) {
        const text = choice.action === 'partial' ? 'Клієнт підтвердив оформлення готових товарів' : choice.action === 'together' ? 'Клієнт обрав дочекатися всіх товарів. Нове оформлення ще не дозволено.' : 'Клієнт повернувся до решти товарів. Потрібна свіжа перевірка менеджера.';
        section.appendChild(this.note(text, 'ig-ap-accepted'));
      } else section.appendChild(this.note('Справжній вибір клієнта ще не підтверджено. Менеджер не обирає за клієнта.'));
      return section;
    }
    shippingPayload(order) {
      const review = order.shipping_review || {}, id = Number.isSafeInteger(order.order_id) ? order.order_id : order.id;
      if (!this.canAct() || this.dirty || review.state !== 'requires_review' || review.can_resolve !== true || !Number.isSafeInteger(id) || id <= 0 || review.order_id !== id || typeof review.expected_document_ref !== 'string' || review.expected_document_ref.length > 36 || !/^[a-f0-9]{64}$/.test(review.expected_terms_digest || '')) return null;
      return {action: 'resolve_shipping_review', order_id: id, expected_document_ref: review.expected_document_ref, expected_terms_digest: review.expected_terms_digest};
    }
    shippingReviewBox(order) {
      const review = order.shipping_review; if (!review || !['requires_review', 'resolved'].includes(review.state)) return null;
      const box = this.node('section', 'ig-ap-shipping-review');
      box.appendChild(this.node('h5', '', review.state === 'resolved' ? 'Існуючу ТТН звірено' : 'Потрібна звірка доставки'));
      if (review.state === 'requires_review') {
        box.appendChild(this.note(review.reason_label || reason(review.reason), 'ig-ap-shipping-warning'));
        if (typeof review.tracking_number === 'string' && /^\d{14}$/.test(review.tracking_number)) box.appendChild(this.note('Існуюча ТТН: ' + review.tracking_number));
        box.appendChild(this.note('Перевіряється існуючий документ перевізника. Ця дія не створює нову ТТН і не змінює погоджений максимум для клієнта.'));
        if (this.canOperate) {const button = this.button('Звірити існуючу ТТН', () => {const payload = this.shippingPayload(order); if (payload) this.perform(payload);}, true); this.reviewControls.push({button, order}); box.appendChild(button); if (review.can_resolve !== true) box.appendChild(this.note('Звірка стане доступною після підтвердження існуючої ТТН та погоджених умов.'));}
      } else box.appendChild(this.note('Платника й відповідність погодженому максимуму перевірено. Оплата та отримання товарів відображаються окремо.'));
      return box;
    }
    orderBox(orders) {
      const section = this.node('section', 'ig-ap-orders'); section.appendChild(this.node('h4', '', 'Пов’язані оформлення та замовлення'));
      orders.forEach(order => {
        const row = this.node('section', 'ig-ap-order'), orderId = Number.isSafeInteger(order.order_id) ? order.order_id : order.id, number = order.order_number || order.number || (Number.isSafeInteger(orderId) ? '№' + orderId : '');
        row.appendChild(this.node('h5', '', number ? 'Замовлення ' + number : 'Пов’язане замовлення'));
        const list = this.node('ul'); (order.lines || []).forEach(line => {if (line.title || line.label) list.appendChild(this.node('li', '', (line.title || line.label) + (Number.isInteger(line.quantity) ? ' · ' + line.quantity + ' шт.' : '')));}); if (list.children.length) row.appendChild(list); else if (Array.isArray(order.line_ids) && order.line_ids.length) {const quantities = Object.values(order.quantities || {}); const total = quantities.length && quantities.every(value => Number.isSafeInteger(value) && value > 0) ? quantities.reduce((sum, value) => sum + value, 0) : null; row.appendChild(this.note('Позицій у замовленні: ' + order.line_ids.length + (total !== null ? ' · ' + total + ' шт.' : '')));}
        row.append(this.note(order.payment_status_label || 'Стан оплати перевіряється у замовленні.'), this.note(order.shipping_status_label || 'Стан доставки перевіряється окремо від оплати.'));
        if (order.customer_maximum != null) row.appendChild(this.note('Максимальна вартість доставки для клієнта: ' + amount(order.customer_maximum, order.currency)));
        const review = this.shippingReviewBox(order); if (review) row.appendChild(review);
        if (order.payer === 'merchant' || order.payer === 'customer') row.appendChild(this.note(order.payer === 'merchant' ? 'Доставку оплачує TwoComms' : 'Доставку оплачує клієнт'));
        if (this.options.onOpenOrder && Number.isSafeInteger(orderId) && orderId > 0) row.appendChild(this.button('Відкрити замовлення', () => this.options.onOpenOrder(orderId)));
        else if (this.options.onOpenProposal && order.public_id) row.appendChild(this.button('Відкрити пропозицію', () => this.options.onOpenProposal(order.public_id, order.proposal_state)));
        section.appendChild(row);
      }); return section;
    }
    draw() {
      this.root.replaceChildren(); this.termsDisclosure = null; this.termControls = []; this.reviewControls = []; this.saveButton = this.approveButton = this.issueButton = this.prepareButton = this.resumeButton = this.discardButton = this.recoveryButton = null;
      const p = this.projection; this.root.hidden = !isRelevant(p) && !this.pending && !this.dirty;
      const header = this.node('div', 'ig-ap-head'), title = this.node('div'); title.append(this.node('span', 'ig-ap-eyebrow', 'Увесь кошик залишається видимим'), this.node('h3', '', 'Готові зараз · решта пізніше'));
      this.refreshButton = this.button('Оновити', () => this.refresh()); header.append(title, this.refreshButton); this.root.appendChild(header);
      this.feedback = this.node('div', 'ig-ap-feedback'); this.feedback.setAttribute('role', 'status'); this.feedback.setAttribute('aria-live', 'polite');
      if (p) {
        this.root.appendChild(this.basket(p));
        const checked = date(this.commerce.approved_head && this.commerce.approved_head.physical_checked_at); if (checked) this.root.appendChild(this.note('Перевірка менеджера: ' + checked));
        const stage = this.stageLabel(p); if (stage) this.root.appendChild(this.note(stage, 'ig-ap-stage'));
        if (p.ready === true && this.draft && (p.lines || []).some(line => line.disposition === 'ready')) this.root.appendChild(this.termsEditor(p));
        if (p.offer) {const offerBox = this.offerBox(p.offer, false); if (p.offer.request_id && p.offer.request_id !== p.request_id || p.offer.expected_decision_digest && p.offer.expected_decision_digest !== p.decision_digest || p.offer.expected_terms_digest && p.offer.expected_terms_digest !== p.terms_digest) offerBox.appendChild(this.note('Це збережені попередні умови. Для актуальної версії потрібні нове погодження та власний вибір клієнта.', 'ig-ap-warning')); if (this.settledOffer(p, p.offer)) {const agreement = this.node('details', 'ig-ap-history ig-ap-settled-offer'); agreement.open = false; agreement.append(this.node('summary', '', 'Погоджені умови та вибір клієнта'), offerBox); this.root.appendChild(agreement);} else this.root.appendChild(offerBox);}
        if (p.continuation_offer) this.root.appendChild(this.offerBox(p.continuation_offer, true));
        if (Array.isArray(p.offer_history) && p.offer_history.length) {const history = this.node('details', 'ig-ap-history'); history.appendChild(this.node('summary', '', 'Попередні погодження')); p.offer_history.forEach(offer => {const old = this.offerBox(offer, offer.kind === 'continuation'); old.appendChild(this.note('Історичне погодження. Воно не дозволяє нове оформлення.', 'ig-ap-muted')); history.appendChild(old);}); this.root.appendChild(history);}
        if (Array.isArray(p.orders) && p.orders.length) this.root.appendChild(this.orderBox(p.orders));
        const timeline = this.node('ol', 'ig-ap-timeline');
        const deferred = (p.lines || []).some(line => line.disposition === 'deferred') || ((p.coverage || {}).deferred_line_ids || []).length > 0;
        ['Готові товари оформлюються лише після справжнього вибору клієнта.', deferred ? 'Решта залишається невиконаним побажанням і ще не оплачена.' : 'Відкладених позицій для нового оформлення немає. Перевіряйте кожне пов’язане оформлення окремо.', 'Пізніше оформлення потребує: повернення клієнта → свіжа перевірка менеджера → нові умови й згода → окреме замовлення.'].forEach(text => timeline.appendChild(this.node('li', '', text))); this.root.appendChild(timeline);
        if (this.canOperate) {
          const actions = this.node('div', 'ig-ap-actions');
          if (p.can_issue === true) {this.issueButton = this.button('Надіслати варіанти клієнту', () => {const body = this.actionPayload('issue_partial_offer'); if (body) this.perform(body);}, true); actions.appendChild(this.issueButton);}
          if (p.can_prepare_selected === true || p.can_prepare_remainder === true) {this.prepareButton = this.button(p.can_prepare_remainder === true ? 'Підготувати оформлення решти' : 'Підготувати оформлення готових товарів', () => {const body = this.actionPayload('prepare_selected'); if (body) this.perform(body);}, true); actions.appendChild(this.prepareButton);}
          if (p.can_resume === true) {this.resumeButton = this.button('Запросити повернутися до решти товарів', () => {const body = this.actionPayload('issue_continuation_offer'); if (body) this.perform(body);}); actions.appendChild(this.resumeButton);}
          if (actions.children.length) this.root.appendChild(actions);
        } else this.root.appendChild(this.note('Доступний перегляд. Умови змінює оператор із правом керування.'));
      } else this.root.appendChild(this.note('Поточні умови часткового оформлення недоступні.'));
      if (this.dirty && (!p || p.ready !== true || !(p.lines || []).some(line => line.disposition === 'ready')) && !this.pending) this.root.append(this.note('Попередню чернетку умов збережено для звірки.', 'ig-ap-warning'), this.button('Відкинути недоступну чернетку умов', () => {this.draft = p ? this.draftFrom() : null; this.draw();}));
      if (this.checkout && this.checkout.ready === true) {const box = this.node('section', 'ig-ap-offer'),order=this.settledCheckoutOrder(); box.append(this.node('h4', '', order?'Товари у пов’язаному замовленні':'Оформлення підготовлено'), this.note(order?('Замовлення '+(order.order_number||'')+' · '+(order.payment_status_label||'Перевірте оплату в замовленні')+' · '+(order.shipping_status_label||'Статус доставки ще не підтверджено')):'Посилання не підтверджує оплату чи створення замовлення.')); if (this.options.onOpenProposal && this.checkout.public_id) box.appendChild(this.button('Відкрити пропозицію', () => this.options.onOpenProposal(this.checkout.public_id, this.checkout.state))); this.root.appendChild(box);}
      if (this.unknown && this.pending) {const recovery = this.node('section', 'ig-ap-recovery'); this.recoveryButton = this.button('Звірити ту саму операцію', () => this.perform(null), true); recovery.append(this.note('Результат операції невідомий. Її умови зафіксовано; перевіряється та сама операція без нового вибору товарів чи нового надсилання.'), this.recoveryButton); this.root.appendChild(recovery);}
      this.root.appendChild(this.feedback); this.updateActions(); this.lock(this.busy);
    }
    async perform(payload) {
      if (!this.canOperate || this.busy || this.destroyed || this.pending && payload) return false;
      if (!this.pending) {
        if (!payload || this.options.canAct && this.options.canAct() !== true) return false;
        try {const body = Object.assign({}, clone(payload), {operation_id: this.uuid()}); this.pending = {action: body.action, requestId: body.request_id || this.projection && this.projection.request_id || null, body: JSON.stringify(body)};} catch (error) {this.say(error.message, true); return false;}
      }
      const pending = this.pending, version = this.generation; this.lock(true); this.say('Зберігаємо перевірену операцію…');
      try {
        const response = await this.fetch(this.apiUrl, {method: 'POST', credentials: 'same-origin', headers: {'Content-Type': 'application/json', 'X-CSRFToken': this.csrf, 'X-Requested-With': 'XMLHttpRequest'}, body: pending.body});
        const data = await response.json(); if (this.destroyed || version !== this.generation) return false;
        const code = data.reason || data.code || data.checkout && data.checkout.reason || data.shipping_review && data.shipping_review.reason; const human = data.message || data.shipping_review && data.shipping_review.message;
        if (response.status === 409 && code === 'operation_conflict') {this.unknown = true; this.draw(); this.say(reason(code), true); return false;}
        if ([400, 401, 403, 404, 409, 410, 413, 422].includes(response.status)) {this.pending = null; this.unknown = false; this.lock(false); if (data.commerce) {this.update(data.commerce); if (this.options.onCommerce) this.options.onCommerce(data.commerce);} else await this.refresh(); this.say(human || reason(code), true); return false;}
        if (!response.ok || data.success !== true || !data.commerce || pending.action === 'resolve_shipping_review' && !this.shippingReceiptMatches(data.shipping_review) || pending.action === 'prepare_selected' && (!data.checkout || data.checkout.ready !== true || typeof data.checkout.public_id !== 'string' || !data.checkout.public_id) || ['issue_partial_offer', 'issue_continuation_offer'].includes(pending.action) && (!data.offer || !Number.isSafeInteger(data.offer.task_id) || data.offer.task_id <= 0 || !data.offer.card_digest || !this.receiptMatches(data.offer, pending.action))) throw new Error('Результат не підтверджено.');
        this.pending = null; this.unknown = false; this.draft = null; this.termsEditorOpen = false;
        if (pending.action === 'prepare_selected') this.checkout = clone(data.checkout);
        if (data.commerce) {this.update(data.commerce); if (this.options.onCommerce) this.options.onCommerce(data.commerce);} else {this.lock(false); await this.refresh(); this.draw();}
        this.say(pending.action === 'resolve_shipping_review' ? 'Існуючу ТТН звірено. Погоджений максимум для клієнта залишається незмінним.' : pending.action === 'prepare_selected' ? 'Оформлення підготовлено. Оплата й замовлення перевіряються окремо.' : pending.action === 'approve' ? 'Умови й ціну затверджено. Потрібен справжній вибір клієнта.' : pending.action === 'draft' ? 'Чернетку умов збережено. Затвердіть точну збережену версію.' : 'Операцію збережено. Надсилання й відповідь клієнта перевіряються окремо.'); return true;
      } catch (error) {if (!this.destroyed && version === this.generation) {this.unknown = Boolean(this.pending); this.draw(); this.say(this.pending ? 'Результат операції невідомий. Звірте ту саму операцію; нова операція не створюється.' : 'Нативний результат операції вже звірено. Оновіть картку для актуального стану.', Boolean(this.pending));} return false;}
      finally {if (!this.destroyed && version === this.generation) {this.lock(false); this.updateActions();}}
    }
    syncActions() {if (!this.destroyed) {this.updateActions(); this.lock(this.busy);}}
    destroy() {if (!this.canLeave()) return false; this.destroyed = true; this.generation++; this.root.replaceChildren(); return true;}
  }
  function create(root, options) {return new Controller(root, options);}
  return {Controller, create, money, timing, isRelevant};
});
