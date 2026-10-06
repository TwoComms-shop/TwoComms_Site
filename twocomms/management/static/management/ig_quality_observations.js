/* Small manual observation sample; never extrapolate it to a whole week. */
(function () {
  'use strict';
  const UNITS = {sampled_revisions: 'знімків ходів у вибірці', generation_applicable_revisions: 'знімків із можливою генерацією', reply_applicable_revisions: 'знімків із можливою відповіддю', recorded_attempts: 'записаних спроб', recorded_terminal_attempts: 'завершених спроб із часом', recorded_physical_parts: 'записаних фізичних частин', confirmed_receipt_parts: 'підтверджень доставки', bound_manager_cases: 'підтверджених кейсів команди'};
  function el(tag, cls, text) { const node = document.createElement(tag); if (cls) node.className = cls; if (text !== undefined) node.textContent = String(text); return node; }
  function count(value) { return Number.isSafeInteger(value) && value >= 0 ? String(value) : 'невідомо'; }
  function stamp(value) { const date = new Date(value); return value && !Number.isNaN(date.getTime()) ? new Intl.DateTimeFormat('uk-UA', {day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit'}).format(date) : 'невідомо'; }
  class Observations {
    constructor(mount, options) {
      this.options = options || {}; this.epoch = 0; this.controller = null; this.root = el('section', 'ig-quality-observations');
      const head = el('div', 'ig-quality-heading'), title = el('div', ''); title.append(el('span', 'ig-quality-eyebrow', 'Спостереження за відповідями'), el('h3', '', 'Що підтверджено у вибірці')); head.append(title);
      this.days = el('select', 'ig-quality-select'); this.days.setAttribute('aria-label', 'Період створення знімків ходів');
      for (const days of [7, 14, 31]) { const option = el('option', '', days + ' днів'); option.value = String(days); this.days.append(option); }
      this.days.value = '7'; head.append(this.days);
      this.start = el('button', 'ig-quality-button', 'Переглянути до 5 знімків'); this.start.type = 'button'; this.start.addEventListener('click', () => this.render({days: Number(this.days.value)})); head.append(this.start);
      this.status = el('p', 'ig-quality-status', 'Огляд читає до 5 записів за запитом.'); this.status.setAttribute('role', 'status'); this.status.setAttribute('aria-live', 'polite');
      this.body = el('div', 'ig-quality-body'); this.actions = el('div', 'ig-quality-actions'); this.actions.hidden = true;
      this.next = el('button', 'ig-quality-button', 'Наступні 5'); this.next.type = 'button'; this.next.addEventListener('click', () => this.load({...this.query, before_revision_id: this.cursor}, this.epoch));
      this.export = el('a', 'ig-quality-export', 'Експорт CSV'); this.export.setAttribute('download', ''); this.actions.append(this.next, this.export); this.root.append(head, this.status, this.body, this.actions); mount.append(this.root);
    }
    clear() { this.epoch += 1; this.controller?.abort(); this.controller = null; this.body.replaceChildren(); this.actions.hidden = true; this.query = null; this.cursor = null; this.start.disabled = false; this.next.disabled = false; this.status.textContent = 'Огляд читає до 5 записів за запитом.'; }
    destroy() { this.clear(); this.root.remove(); }
    url(base, query) { const url = new URL(base, window.location.href); Object.entries(query).forEach(([key, value]) => { if (value !== null && value !== undefined) url.searchParams.set(key, String(value)); }); return url.href; }
    async render(options) { this.clear(); const days = options?.days || 7; this.controller = new AbortController(); await this.load({days, limit: 5}, this.epoch); }
    async load(query, epoch) {
      if (epoch !== this.epoch) return;
      const requestNumber = (this.requestNumber || 0) + 1; this.requestNumber = requestNumber;
      this.start.disabled = true; this.next.disabled = true; this.actions.hidden = true; this.body.replaceChildren(); this.status.textContent = 'Читаємо обмежену вибірку існуючих доказів…';
      try {
        const response = await window.fetch(this.url(this.options.reportUrl || '/management/bot/api/quality-observations/', query), {credentials: 'same-origin', cache: 'no-store', headers: {'Accept': 'application/json'}, signal: this.controller.signal});
        if (epoch !== this.epoch || requestNumber !== this.requestNumber) return;
        if (response.redirected || response.status === 401) throw new Error('Сесія завершилася. Оновіть сторінку та увійдіть знову.');
        if (response.headers && !(response.headers.get('Content-Type') || '').includes('application/json')) throw new Error('Сервер тимчасово недоступний. Спробуйте прочитати вибірку пізніше.');
        const data = await response.json(); if (epoch !== this.epoch || requestNumber !== this.requestNumber) return;
        if (!response.ok || data.success !== true || data.schema_version !== 'ig-quality-observations.v1') throw new Error(response.status === 403 ? 'Недостатньо прав для перегляду.' : data.reason === 'read_scope_changed' ? 'Дані змінилися під час читання. Оновіть вибірку.' : 'Не вдалося прочитати вибірку.');
        this.query = {since: data.cohort.start, until: data.cohort.end_exclusive, limit: 5, before_revision_id: query.before_revision_id}; this.cursor = data.sample.next_before_revision_id;
        this.paint(data); this.status.textContent = 'Знімки створено: ' + stamp(data.cohort.start) + ' — ' + stamp(data.cohort.end_exclusive) + ' · докази прочитано ' + stamp(data.observation.read_finished_at);
        this.next.hidden = !data.sample.has_more || !this.cursor;
        this.export.href = this.url(this.options.exportUrl || '/management/bot/api/quality-observations/export/', this.query); this.actions.hidden = !data.sample.count;
      } catch (error) { if (epoch === this.epoch && requestNumber === this.requestNumber && error.name !== 'AbortError') this.status.textContent = error.message || 'Не вдалося прочитати вибірку.'; }
      finally { if (epoch === this.epoch && requestNumber === this.requestNumber) { this.start.disabled = false; this.next.disabled = false; } }
    }
    paint(data) {
      const population = el('div', 'ig-quality-population'); population.append(el('strong', '', count(data.sample.count) + ' / ' + count(data.cohort.population_count_at_read_start)), el('span', '', 'знімків прочитано / доступно за період')); this.body.append(population);
      this.body.append(el('p', 'ig-quality-note', data.sample.whole_window_sampled ? 'Вибірка охоплює доступні знімки за період. Невідомі докази кожного етапу залишаються окремо.' : 'Це обмежена сторінка, а не повний звіт за вікно. Результати не переносимо на решту популяції.'));
      const metrics = Array.isArray(data.metrics) ? data.metrics : [], cards = el('div', 'ig-quality-cards');
      for (const key of ['generation_winner', 'normal_reply_sent', 'semantic_reply_complete']) { const metric = metrics.find(row => row.key === key); if (!metric) continue; const card = el('div', 'ig-quality-card'); card.append(el('span', 'ig-quality-label', metric.label), el('strong', 'ig-quality-value', count(metric.numerator) + ' / ' + count(metric.denominator)), el('span', 'ig-quality-unit', UNITS[metric.denominator_unit] || 'записів для цього показника')); if (metric.unknown_count) card.append(el('span', 'ig-quality-unknown', 'Невідомо: ' + count(metric.unknown_count))); cards.append(card); }
      this.body.append(cards);
      const detail = el('details', 'ig-quality-details'); detail.append(el('summary', '', 'Основа розрахунку, прогалини та використання')); const table = el('div', 'ig-quality-table');
      for (const metric of metrics) { const row = el('div', 'ig-quality-row'); row.append(el('span', '', metric.label), el('strong', '', count(metric.numerator) + ' / ' + count(metric.denominator)), el('small', '', (UNITS[metric.denominator_unit] || metric.denominator_unit) + ' · невідомо ' + count(metric.unknown_count) + ' · не застосовується ' + count(metric.not_applicable_count))); table.append(row); } detail.append(table);
      const stages = el('div', 'ig-quality-stage-gaps'); const labels = {context: 'Контекст', decision: 'Рішення', generation: 'Генерація', proposal: 'Прийнятий план', delivery: 'Доставка', semantic: 'Повнота', manager_case: 'Кейс команди'};
      for (const [key, value] of Object.entries(data.stage_coverage || {})) stages.append(el('span', '', (labels[key] || key) + ': доказів немає у ' + count(value.unknown) + ' / ' + count(value.denominator))); detail.append(stages);
      detail.append(el('p', 'ig-quality-note', 'Записані токени: ' + (data.usage.total_tokens_sum === null ? 'невідомо' : count(data.usage.total_tokens_sum)) + ' · спроб із відомим використанням ' + count(data.usage.observed_attempts) + ' · без цих даних ' + count(data.usage.unknown_attempts) + '. Грошова вартість невідома.'));
      detail.append(el('p', 'ig-quality-note', 'Показано підтвердження, доступні зараз. Вони можуть надійти пізніше за вибраний період; стан на минулу дату не відновлюється.'));
      detail.append(el('p', 'ig-quality-note', 'CSV повторно читає цю сторінку за тим самим періодом і містить час читання. Пізні підтвердження можуть змінити числа. Тексти, ідентифікатори клієнтів і повідомлень та запити моделі не експортуються.')); this.body.append(detail);
      this.body.append(el('p', 'ig-quality-footnote', 'Рахуємо знімки ходів за часом створення. Один запит клієнта може мати кілька знімків і спроб; частини відповіді рахуються окремо. Якість формулювань і вплив на продажі тут не оцінюються.'));
    }
  }
  window.IgQualityObservations = Object.freeze({create: (mount, options) => new Observations(mount, options)});
}());
