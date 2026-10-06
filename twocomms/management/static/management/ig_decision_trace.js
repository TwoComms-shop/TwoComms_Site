/* Read-only actual revision evidence. No prompt reconstruction or mutations. */
(function () {
  'use strict';
  const PROOF = {confirmed: 'Докази підтверджено', recorded: 'Зафіксовано', unknown: 'Немає доказу'};
  const PHYSICAL = {sent: 'Усі частини підтверджено', partial: 'Підтверджено частину', unknown: 'Результат не підтверджено', not_confirmed: 'Відправку не підтверджено', no_reply: 'Зафіксовано рішення не відповідати'};
  const SEMANTIC = {complete: 'План відповіді покрито', waiting_on_customer: 'Потрібен вибір клієнта', recovery: 'Відповідь ще потрібна', manual: 'Потрібен розгляд команди', unknown: 'Повноту не доведено'};
  const ORIGIN = {generate: 'Gemini', static_reply: 'Статична відповідь', no_reply: 'Без відповіді', postback: 'Дія клієнта', blocked: 'Зупинено', unknown: 'Рішення не зафіксовано'};
  const REASONS = {owner_unavailable: 'Клієнт недоступний або дані видаляються.', revision_missing: 'Цей хід недоступний.', privacy_erasure: 'Дані видаляються.', read_scope_changed: 'Дані змінилися під час читання. Оновіть трасу.', source_binding_unverified: 'Зв’язок із вихідними повідомленнями не доведено.', revision_snapshot_unverified: 'Історичний знімок не перевірено.', effect_read_limit: 'Кількість частин перевищує межу читання.', request_read_limit: 'Кількість запитів перевищує межу читання.'};
  const INPUT_REASON = {opt_out: 'Клієнт відмовився від повідомлень', spam_abuse: 'Зафіксовано спам або зловживання', reaction_only: 'Лише реакція без запиту', no_reply: 'Відповідь не потрібна за записаним рішенням', explicit_no_buy: 'Клієнт відмовився від покупки', static_trigger_absent: 'Немає тригера статичної відповіді', configured_reply: 'Спрацювала налаштована статична відповідь', model_reply: 'Запит потребує відповіді моделі', customer_action: 'Клієнт натиснув кнопку', rate_limited: 'Спрацювало обмеження частоти', static_reply_invalid: 'Налаштована відповідь не пройшла перевірку', source_role_invalid: 'Не підтверджено вхідне джерело'};
  const FAILURE = {local_semantic_rejection: 'Локальна перевірка змісту', invalid_response: 'Не пройшла схема відповіді', empty: 'Порожня відповідь', read_timeout: 'Час очікування минув', quota_429: 'Обмеження квоти', http_5xx: 'Помилка провайдера', transport: 'Помилка з’єднання', invalid_payload: 'Відхилено формат запиту', safety_blocked: 'Спрацювала перевірка безпеки'};
  const VALIDATION = {unverified_price: 'Немає підтвердженої ціни', unverified_payment: 'Не доведено оплату', unverified_order: 'Не доведено створення замовлення', unverified_shipment: 'Не доведено відправку', unverified_availability: 'Не доведено наявність', configuration_mismatch: 'Відповідь не відповідає вибору клієнта', unnecessary_manager_handoff: 'Зайва передача менеджеру', unauthorized_url: 'Непідтверджене посилання', catalog_selector_missing: 'Не вистачає вибору товару', invalid_response_schema: 'Некоректна структура відповіді'};
  const ACTION = {client_configuration_update: 'Оновити вибір клієнта', checkout_proposal_create: 'Підготувати checkout', follow_decision_prepare: 'Підготувати дію follow', manager_escalation_intent: 'Передати команді', order_fulfillment: 'Виконання замовлення', prize_review_case_create: 'Передати приз на розгляд', size_gap_notification_intent: 'Запит щодо розміру', spam_transition: 'Позначити спам'};
  const TASK = {pending: 'Очікує', processing: 'Опрацьовується', sent: 'Надіслано', ambiguous: 'Результат невідомий', completed: 'Завершено', cancelled: 'Скасовано', skipped: 'Передано на розгляд', sending: 'Надсилається', failed: 'Помилка доставки', unknown: 'Результат невідомий', dead_letter: 'Потрібен розгляд', resolved: 'Закрито оператором'};
  function node(tag, cls, text) { const value = document.createElement(tag); if (cls) value.className = cls; if (text !== undefined) value.textContent = String(text); return value; }
  function identity(value) { const number = Number(value); return Number.isSafeInteger(number) && number > 0 && String(number) === String(value) ? number : null; }
  function stamp(value) { const date = new Date(value); return value && !Number.isNaN(date.getTime()) ? new Intl.DateTimeFormat('uk-UA', {day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit'}).format(date) : 'Час не зафіксовано'; }
  function list(value) { return Array.isArray(value) ? value : []; }
  function proof(value) { return PROOF[value] || PROOF.unknown; }
  function count(value) { return Number.isSafeInteger(value) && value >= 0 ? String(value) : 'невідомо'; }
  function line(parent, label, value) { const row = node('div', 'ig-trace-fact'); row.append(node('span', '', label), node('strong', '', value)); parent.append(row); }
  function stage(parent, title, record) { const section = node('details', 'ig-trace-stage'); const head = node('summary', 'ig-trace-stage-head'); head.append(node('span', 'ig-trace-stage-title', title), node('span', 'ig-trace-proof ig-trace-proof-' + (PROOF[record?.proof] ? record.proof : 'unknown'), proof(record?.proof))); section.traceSummary = head; section.append(head); parent.append(section); return section; }
  function codes(parent, values) { const safe = list(values).filter(value => typeof value === 'string').slice(0, 12); if (safe.length) parent.append(node('p', 'ig-trace-codes', safe.join(' · '))); }
  function note(parent, text) { parent.append(node('p', 'ig-trace-note', text)); }
  function disclosure(parent, title) { const details = node('details', 'ig-trace-diagnostic'); details.append(node('summary', '', title)); parent.append(details); return details; }
  function outcome(data) {
    const delivery = data.delivery || {}, semantic = data.semantic || {};
    if (semantic.customer_reply_complete === true) return {tone: 'success', icon: '✓', title: 'Повну відповідь підтверджено', detail: 'Є receipts усіх звичайних частин і записана повнота змістовної відповіді.'};
    if (delivery.physical_state === 'no_reply') return {tone: 'neutral', icon: '–', title: 'Зафіксовано рішення не відповідати', detail: INPUT_REASON[data.decision?.reason] || 'Рішення прив’язане до вихідних повідомлень цього ходу.'};
    if (delivery.physical_state === 'partial') return {tone: 'warning', icon: '◐', title: 'Доставлено лише частину відповіді', detail: 'Докази першої частини не закривають решту. Перевірте фізичну доставку та повноту нижче.'};
    if (delivery.physical_state === 'sent' && delivery.normal_reply_complete !== true) return {tone: 'neutral', icon: '•', title: 'Повідомлення доставлено', detail: list(delivery.effects).some(effect => effect.purpose === 'technical_holding') ? 'Технічний holding не закриває повну звичайну відповідь.' : 'Фізичні receipts є. Повну звичайну відповідь не доведено.'};
    if (delivery.physical_state === 'sent') return {tone: 'warning', icon: '◐', title: 'Доставку підтверджено', detail: semantic.disposition === 'waiting_on_customer' ? 'Потрібен наступний вибір клієнта. Змістовна відповідь ще має відкриті частини.' : 'Повноту змістовної відповіді не доведено.'};
    if (data.generation?.actual_model) return {tone: 'warning', icon: '?', title: 'Генерацію прийнято', detail: 'Відправку клієнту не підтверджено. Прийнятий результат моделі має окремий receipt доставки.'};
    return {tone: 'neutral', icon: '?', title: 'Немає підтвердження доставки', detail: data.decision?.origin === 'static_reply' ? 'Зафіксовано статичну відповідь; доказ її відправки ще відсутній.' : 'Існуючих записів недостатньо, щоб стверджувати, що клієнт отримав відповідь.'};
  }

  class Trace {
    constructor(mount, options) {
      this.mount = mount; this.options = options || {}; this.clientId = null; this.epoch = 0; this.traceRequest = 0; this.controller = null; this.items = [];
      this.root = node('section', 'ig-decision-trace');
      const head = node('div', 'ig-trace-heading'); const heading = node('div', 'ig-trace-heading-copy'); heading.append(node('span', 'ig-trace-eyebrow', 'Фактичні докази'), node('h3', '', 'Як бот відповів')); head.append(heading);
      this.refresh = node('button', 'ig-trace-button', 'Оновити'); this.refresh.type = 'button'; this.refresh.addEventListener('click', () => this.selectedRevision && this.clientId ? this.loadTrace(this.selectedRevision, this.epoch) : this.render(this.clientId)); head.append(this.refresh);
      this.status = node('p', 'ig-trace-status', 'Оберіть клієнта, щоб прочитати фактичні ходи.'); this.status.setAttribute('role', 'status'); this.status.setAttribute('aria-live', 'polite');
      this.controls = node('div', 'ig-trace-controls'); this.select = node('select', 'ig-trace-select'); this.select.setAttribute('aria-label', 'Зафіксований хід клієнта');
      this.select.addEventListener('change', () => this.loadTrace(identity(this.select.value), this.epoch));
      this.more = node('button', 'ig-trace-button', 'Ще ходи'); this.more.type = 'button'; this.more.addEventListener('click', () => this.loadIndex(this.epoch, this.cursor));
      this.controls.append(this.select, this.more); this.controls.hidden = true;
      this.body = node('div', 'ig-trace-body'); this.root.append(head, this.status, this.controls, this.body); this.mount.append(this.root);
    }
    clear() { this.epoch += 1; this.controller?.abort(); this.controller = null; this.clientId = null; this.selectedRevision = null; this.items = []; this.cursor = null; this.body.replaceChildren(); this.controls.hidden = true; this.status.textContent = 'Оберіть клієнта, щоб прочитати фактичні ходи.'; }
    destroy() { this.clear(); this.root.remove(); }
    async fetch(url, epoch) {
      const response = await window.fetch(url, {credentials: 'same-origin', cache: 'no-store', headers: {'Accept': 'application/json'}, signal: this.controller.signal});
      if (epoch !== this.epoch) return null;
      if (response.redirected || response.status === 401) throw new Error('Сесія завершилася. Оновіть сторінку та увійдіть знову.');
      if (response.headers && !(response.headers.get('Content-Type') || '').includes('application/json')) throw new Error('Сервер тимчасово недоступний. Спробуйте оновити докази пізніше.');
      const payload = await response.json();
      if (epoch !== this.epoch) return null;
      if (!response.ok || payload.success !== true) throw new Error(REASONS[payload.reason] || (response.status === 403 ? 'Недостатньо прав для перегляду трасування.' : 'Не вдалося прочитати трасу.'));
      if (payload.schema_version !== 'ig-decision-trace.v1' || payload.identity?.client_id !== this.clientId) throw new Error('Зв’язок трасування з клієнтом не підтверджено.');
      return payload;
    }
    async render(clientId) {
      this.clear(); const selected = identity(clientId); if (!selected) return;
      this.clientId = selected; this.controller = new AbortController(); this.status.textContent = 'Читаємо зафіксовані ходи…';
      await this.loadIndex(this.epoch, null);
    }
    async loadIndex(epoch, before) {
      if (epoch !== this.epoch || !this.clientId) return;
      this.more.disabled = true;
      try {
        const base = this.options.indexUrl ? this.options.indexUrl(this.clientId) : '/management/bot/api/clients/' + this.clientId + '/decision-traces/';
        const url = new URL(base, window.location.href); url.searchParams.set('limit', '10'); if (before) url.searchParams.set('before_revision_id', String(before));
        const payload = await this.fetch(url.href, epoch); if (!payload) return;
        const known = new Set(this.items.map(row => row.revision_id));
        for (const row of list(payload.items)) if (identity(row.revision_id) && !known.has(row.revision_id)) { this.items.push(row); known.add(row.revision_id); }
        const previous = this.select.value; this.select.replaceChildren();
        for (const row of this.items) { const option = node('option', '', 'Хід #' + row.revision_id + ' · ' + (ORIGIN[row.input_origin] || ORIGIN.unknown) + (row.scope === 'historical' ? ' · до скидання' : row.scope === 'unknown' ? ' · зв’язок невідомий' : '') + ' · ' + stamp(row.sealed_at)); option.value = String(row.revision_id); this.select.append(option); }
        if (previous && this.items.some(row => String(row.revision_id) === previous)) this.select.value = previous;
        this.cursor = identity(payload.next_before_revision_id); this.more.hidden = !payload.has_more || !this.cursor; this.controls.hidden = !this.items.length;
        this.status.textContent = this.items.length ? 'Оберіть зафіксований хід клієнта.' : 'Зафіксованих ходів немає. Історичні рішення не відновлюємо з поточних правил.';
        if (!before && this.items.length) await this.loadTrace(identity(this.select.value), epoch);
      } catch (error) { if (epoch === this.epoch && error.name !== 'AbortError') { this.body.replaceChildren(); this.status.textContent = error.message || 'Не вдалося прочитати трасу.'; } }
      finally { if (epoch === this.epoch) this.more.disabled = false; }
    }
    async loadTrace(revisionId, epoch) {
      if (!revisionId || epoch !== this.epoch) return;
      const requestNumber = ++this.traceRequest;
      this.selectedRevision = revisionId; this.body.replaceChildren(); this.status.textContent = 'Читаємо докази ходу #' + revisionId + '…';
      try {
        const url = this.options.traceUrl ? this.options.traceUrl(this.clientId, revisionId) : '/management/bot/api/clients/' + this.clientId + '/turn-revisions/' + revisionId + '/decision-trace/';
        const payload = await this.fetch(url, epoch); if (!payload || this.selectedRevision !== revisionId || this.traceRequest !== requestNumber) return;
        if (payload.identity?.revision_id !== revisionId || payload.revision?.id !== revisionId) throw new Error('Зв’язок трасування з ходом не підтверджено.');
        this.paint(payload); this.status.textContent = 'Хід #' + revisionId + (payload.revision.scope === 'historical' ? ' · історичні докази до скидання.' : ' · зафіксовані докази.');
      } catch (error) { if (epoch === this.epoch && this.selectedRevision === revisionId && this.traceRequest === requestNumber && error.name !== 'AbortError') this.status.textContent = error.message || 'Не вдалося прочитати трасу.'; }
    }
    paint(data) {
      this.body.replaceChildren();
      const result = outcome(data), hero = node('div', 'ig-trace-outcome ig-trace-outcome-' + result.tone);
      const mark = node('span', 'ig-trace-outcome-mark', result.icon); mark.setAttribute('aria-hidden', 'true');
      const copy = node('div', 'ig-trace-outcome-copy'); copy.append(node('h4', '', result.title), node('p', '', result.detail)); hero.append(mark, copy); this.body.append(hero);
      const meta = node('div', 'ig-trace-meta');
      meta.append(node('span', '', data.generation?.actual_model || ORIGIN[data.decision?.origin] || 'Модель не підтверджено'), node('span', '', data.revision?.scope === 'historical' ? 'Історичний хід · до скидання' : 'Окремі докази кожного етапу')); this.body.append(meta);
      const nav = node('div', 'ig-trace-proof-nav'); nav.setAttribute('aria-label', 'Перейти до доказів етапу'); this.body.append(nav);
      const evidence = node('details', 'ig-trace-evidence'); evidence.append(node('summary', '', 'Переглянути докази')); const stages = node('div', 'ig-trace-stages'); evidence.append(stages); this.body.append(evidence);
      const context = stage(stages, 'Контекст', data.context); line(context, 'Повідомлення-джерела', list(data.context?.source_message_ids).map(value => '#' + value).join(', ') || 'Невідомо'); line(context, 'Зафіксовано', stamp(data.context?.captured_at));
      const decision = stage(stages, 'Рішення', data.decision); line(decision, 'Походження', ORIGIN[data.decision?.origin] || ORIGIN.unknown); line(decision, 'Записана причина', INPUT_REASON[data.decision?.reason] || 'Не зафіксовано'); note(decision, 'Історичні причини вибору моделі не зафіксовано. Поточні правила не підміняють цей запис.');
      const generation = stage(stages, 'Генерація та перевірка', data.generation); line(generation, 'Прийнята модель', data.generation?.actual_model || 'Не підтверджено');
      for (const request of list(data.generation?.requests)) {
        const details = node('div', 'ig-trace-request'), attempts = list(request.attempts); line(details, 'Запит', request.id); line(details, 'Зафіксований manifest', proof(request.proof));
        if (request.proof !== 'confirmed') note(details, 'Історичний manifest відсутній або не перевірений; його не відновлюємо з поточного prompt.');
        for (const attempt of attempts) {
          const item = node('div', 'ig-trace-attempt'); line(item, 'Спроба #' + attempt.attempt_index, attempt.model); line(item, 'Результат', attempt.winner ? 'Прийнята генерація' : attempt.state === 'failed' ? 'Результат не прийнято' : TASK[attempt.state] || 'Стан не підтверджено');
          if (attempt.failure_kind) line(item, 'Причина', FAILURE[attempt.failure_kind] || 'Зафіксовано технічну відмову');
          if (attempt.validator_layer && attempt.validator_layer !== 'unknown') line(item, 'Перевірка', attempt.validator_layer === 'local_semantic' ? 'Локальна семантика' : 'Схема відповіді');
          const named = list(attempt.validator_codes).map(code => VALIDATION[code]).filter(Boolean); if (named.length) note(item, named.join(' · '));
          const diagnostic = disclosure(item, 'HTTP, використання та коди перевірки');
          line(diagnostic, 'HTTP статус', attempt.http_status || 'Невідомо'); codes(diagnostic, attempt.validator_codes); if (attempt.validator_codes_complete === false) note(diagnostic, 'Частину причин не зафіксовано або не можна показати.');
          codes(diagnostic, [attempt.decision, attempt.not_attempted_reason].filter(Boolean)); line(diagnostic, 'Початок обліку', stamp(attempt.recorded_provider_started_at)); line(diagnostic, 'Тривалість', attempt.latency_ms == null ? 'Невідомо' : attempt.latency_ms + ' мс');
          line(diagnostic, 'Записані токени', 'вхід ' + count(attempt.usage?.tokens?.prompt) + ' · вихід ' + count(attempt.usage?.tokens?.candidates) + ' · загалом ' + count(attempt.usage?.tokens?.total));
          line(diagnostic, 'Оцінка / резерв входу', count(attempt.usage?.estimated_prompt_tokens) + ' / ' + count(attempt.usage?.reserved_prompt_tokens)); note(diagnostic, 'Грошова вартість невідома. Запис фази provider_started не доводить отримання HTTP провайдером.'); details.append(item);
        }
        const captured = disclosure(details, 'Склад контексту та підготовка виправлення'); codes(captured, request.context?.readiness_codes); line(captured, 'Включені блоки', list(request.context?.selected_block_ids).join(', ') || 'Невідомо');
        for (const omitted of list(request.context?.omitted_blocks)) codes(captured, [omitted.block_id, omitted.reason]);
        if (request.repair?.proof === 'recorded') { line(captured, 'Підготовка виправлення', request.repair.state); codes(captured, [request.repair.reason].filter(Boolean)); }
        note(captured, 'Межі lineage: 8 HTTP, 2 scarce, 1 repair. Залишок дозволу на новий запит тут не визначаємо.');
        generation.append(details);
      }
      if (!list(data.generation?.requests).length) note(generation, 'Запитів генерації не зафіксовано. Це саме по собі не доводить рішення не відповідати.');
      const proposal = stage(stages, 'Прийнятий план і дозволені дії', data.proposal); line(proposal, 'Записані дозволи', list(data.proposal?.allowed_actions).map(action => ACTION[action] || 'Дія за записаним дозволом').join(', ') || 'Не зафіксовано'); line(proposal, 'Підстави дозволу', 'факти ' + count(data.proposal?.fact_binding_count) + ' · офери ' + count(data.proposal?.offer_binding_count)); note(proposal, 'Це знімок дозволів під час генерації. Він ще не доводить виконання дії або оплату.');
      const manager = data.actions?.manager_case || {}, actions = stage(stages, 'Передача команді', manager); line(actions, 'Завдання', manager.task_id ? '#' + manager.task_id + ' · ' + (TASK[manager.task_state] || 'Не підтверджено') : 'Не зафіксовано'); line(actions, 'Сповіщення', TASK[manager.notification_state] || 'Не зафіксовано'); line(actions, 'Доставка сповіщення', manager.notification_delivered === true ? 'Підтверджено' : 'Не підтверджено');
      if (list(data.actions?.recorded_receipts).length) { const recorded = disclosure(actions, 'Записані локальні дії'); for (const receipt of list(data.actions.recorded_receipts)) codes(recorded, [receipt.kind, proof(receipt.proof)]); }
      note(actions, 'Завдання, доставка сповіщення й відповідь клієнту мають окремі докази.');
      const delivery = stage(stages, 'Фізична доставка', data.delivery); line(delivery, 'Результат', PHYSICAL[data.delivery?.physical_state] || PHYSICAL.unknown);
      for (const effect of list(data.delivery?.effects)) { const item = node('div', 'ig-trace-effect'); line(item, 'Частина ' + (effect.part_index + 1) + '/' + effect.part_count, effect.receipt_proof === 'confirmed' ? 'Доставлено' : TASK[effect.state] || 'Не підтверджено'); if (effect.purpose === 'technical_holding') note(item, 'Технічний holding · не закриває звичайну відповідь'); line(item, 'Відображення в історії', proof(effect.transcript_proof)); const receipt = disclosure(item, 'Receipt і час частини #' + effect.id); line(receipt, 'Provider receipt', proof(effect.receipt_proof) + (effect.provider_message_id ? ' · ' + effect.provider_message_id : '')); line(receipt, 'Запис історії', effect.transcript_message_id ? '#' + effect.transcript_message_id : 'Не підтверджено'); line(receipt, 'Час результату', stamp(effect.terminal_at)); delivery.append(item); }
      if (data.delivery?.reason && REASONS[data.delivery.reason]) note(delivery, REASONS[data.delivery.reason]);
      const semantic = stage(stages, 'Повнота відповіді', data.semantic); line(semantic, 'Змістовний результат', SEMANTIC[data.semantic?.disposition] || SEMANTIC.unknown); line(semantic, 'Покрито / залишилось', count(data.semantic?.covered_count) + ' / ' + count(data.semantic?.remaining_count)); if (data.semantic?.next_selector) line(semantic, 'Наступний вибір', data.semantic.next_selector); line(semantic, 'Повна звичайна відповідь', data.semantic?.customer_reply_complete === true ? 'Підтверджено' : 'Не доведено'); note(semantic, 'Holding і часткова доставка не закривають змістовну відповідь.');
      for (const [title, record, target] of [['Контекст', data.context, context], ['Рішення', data.decision, decision], ['Генерація', data.generation, generation], ['Доставка', data.delivery, delivery], ['Повнота', data.semantic, semantic]]) {
        const state = title === 'Доставка' ? (PHYSICAL[data.delivery?.physical_state] || PHYSICAL.unknown) : title === 'Повнота' ? (SEMANTIC[data.semantic?.disposition] || SEMANTIC.unknown) : proof(record?.proof);
        const button = node('button', 'ig-trace-stage-link'); button.type = 'button'; button.setAttribute('aria-label', title + ': ' + state); button.append(node('span', '', title), node('small', 'ig-trace-stage-state', state)); button.addEventListener('click', () => { evidence.open = true; target.open = true; target.traceSummary.focus?.(); }); nav.append(button);
      }
      if (data.delivery?.physical_state === 'partial' || data.delivery?.reason === 'effect_read_limit') { evidence.open = true; delivery.open = true; }
      note(this.body, 'Лише читання існуючих доказів · без повторної генерації чи відправки.');
    }
  }
  window.IgDecisionTrace = Object.freeze({create: function (mount, options) { if (!mount || typeof mount.append !== 'function') throw new TypeError('trace_mount_required'); return new Trace(mount, options); }});
}());
