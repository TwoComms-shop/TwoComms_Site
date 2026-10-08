(function (global) {
  'use strict';

  const TIME_LABELS = Object.freeze({
    provider: 'Час повідомлення', local_ingest: 'Час збереження джерела',
    unknown: 'Час джерела не визначено'
  });
  const STATES = Object.freeze({
    empty: ['Збережених подій ще немає', 'Коли з’являться підтверджені джерела, тут буде коротка історія слів клієнта.'],
    legacy: ['Попередній формат пам’яті', 'Для цього запису немає перевіреної стрічки подій. Звірте потрібні деталі у переписці.'],
    unavailable: ['Пам’ять недоступна', 'Наразі не вдалося перевірити збережені джерела. Звірте потрібні деталі у переписці.']
  });

  function text(value) { return typeof value === 'string' ? value : ''; }
  function count(value) { return Number.isSafeInteger(value) && value >= 0 ? value : null; }
  function plural(value, forms) {
    const last = value % 10, hundred = value % 100;
    return forms[hundred >= 11 && hundred <= 14 ? 2 : last === 1 ? 0 : last >= 2 && last <= 4 ? 1 : 2];
  }
  function sourceId(value) {
    const id = typeof value === 'number' ? value : (/^[1-9]\d*$/.test(text(value)) ? Number(value) : 0);
    return Number.isSafeInteger(id) && id > 0 ? id : null;
  }
  function kyivDate(value) {
    // Reject ambiguous local timestamps: source times must carry an explicit offset.
    if (!/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:\d{2})$/.test(text(value))) return null;
    const date = new Date(value);
    if (!Number.isFinite(date.getTime())) return null;
    try {
      return new Intl.DateTimeFormat('uk-UA', {
        timeZone: 'Europe/Kyiv', day: '2-digit', month: '2-digit', year: 'numeric',
        hour: '2-digit', minute: '2-digit', hourCycle: 'h23'
      }).format(date);
    } catch (_) { return null; }
  }
  function element(doc, tag, className, value) {
    const node = doc.createElement(tag);
    if (className) node.className = className;
    if (value !== undefined) node.textContent = value;
    return node;
  }
  function details(doc, title) {
    const box = element(doc, 'details', 'ig-memory-details');
    box.appendChild(element(doc, 'summary', '', title));
    return box;
  }
  function metaRow(doc, parent, label, value) {
    const row = element(doc, 'div');
    row.append(element(doc, 'dt', '', label), element(doc, 'dd', '', value));
    parent.appendChild(row);
  }
  function renderEvent(doc, event, options) {
    const card = element(doc, 'li', 'ig-memory-event');
    const head = element(doc, 'div', 'ig-memory-event-head');
    const formatted = kyivDate(event.event_at);
    const date = element(doc, formatted ? 'time' : 'span', 'ig-memory-date', formatted || 'Дата не визначена');
    if (formatted) { date.setAttribute('datetime', event.event_at); date.title = 'Дата й час за Києвом'; }
    head.appendChild(date);
    if (text(event.topic_label)) head.appendChild(element(doc, 'span', 'ig-memory-topic', 'Тема (підказка): ' + event.topic_label));
    card.append(head, element(doc, 'blockquote', 'ig-memory-quote', event.quote));
    const footer = element(doc, 'div', 'ig-memory-event-footer');
    footer.appendChild(element(doc, 'span', 'ig-memory-origin', TIME_LABELS[event.time_basis] || TIME_LABELS.unknown));
    const id = sourceId(event.source_id);
    const href = id && typeof options.sourceHref === 'function' ? options.sourceHref(id) : null;
    // Links are local routes created by the caller, never URLs from quoted source data.
    if (typeof href === 'string' && href.startsWith('/') && !href.startsWith('//') && !/[\u0000-\u0020\\]/.test(href)) {
      const link = element(doc, 'a', 'ig-memory-source', 'До повідомлення ↗');
      link.href = href;
      link.setAttribute('aria-label', 'Відкрити повідомлення №' + id + ' — джерело цитати');
      if (typeof options.onSource === 'function') link.addEventListener('click', function (click) {
        if (click.button || click.ctrlKey || click.metaKey || click.shiftKey || click.altKey) return;
        click.preventDefault(); options.onSource(id);
      });
      footer.appendChild(link);
    }
    card.appendChild(footer);
    const provenance = details(doc, 'Про джерело');
    const meta = element(doc, 'dl', 'ig-memory-meta');
    metaRow(doc, meta, 'Час:', TIME_LABELS[event.time_basis] || TIME_LABELS.unknown);
    if (event.time_basis === 'local_ingest') metaRow(doc, meta, 'Уточнення:', 'Початковий час повідомлення не визначено. Показано час збереження джерела.');
    if (formatted) metaRow(doc, meta, 'Часовий пояс:', 'Київ · Europe/Kyiv');
    metaRow(doc, meta, 'Джерело:', id ? 'Повідомлення №' + id : 'Без посилання на локальне повідомлення');
    if (event.scope_label === 'Розмова клієнта') metaRow(doc, meta, 'Походження:', event.scope_label);
    provenance.appendChild(meta); card.appendChild(provenance);
    return card;
  }
  function renderCoverage(doc, root, view, eventCount) {
    const coverage = view.coverage && typeof view.coverage === 'object' ? view.coverage : {};
    const total = count(coverage.source_count), omitted = count(coverage.omitted_count), pending = count(coverage.pending_count);
    const coverageBox = element(doc, 'div', 'ig-memory-coverage');
    const sentence = total !== null ? 'Огляд охоплює ' + total + ' ' + plural(total, ['повідомлення', 'повідомлення', 'повідомлень']) + ' клієнта. Показано ' + eventCount + ' ' + plural(eventCount, ['подію', 'події', 'подій']) + '.' : eventCount ? 'Показано збережені події з перевірених джерел.' : 'Датованих подій не відібрано.';
    coverageBox.appendChild(element(doc, 'p', 'ig-memory-note', sentence));
    if (omitted) coverageBox.appendChild(element(doc, 'p', 'ig-memory-note', 'Зафіксовано пропусків: ' + omitted + '.'));
    if (pending) coverageBox.appendChild(element(doc, 'p', 'ig-memory-note', 'Нових повідомлень після огляду: ' + pending + '.'));
    const omissions = Array.isArray(coverage.omissions) ? coverage.omissions.filter(item => item && text(item.label).trim() && count(item.count) > 0).slice(0, 12) : [];
    if (omissions.length) {
      const disclosure = details(doc, 'Що не увійшло в історію'), ul = element(doc, 'ul', 'ig-memory-detail-list');
      omissions.forEach(item => ul.appendChild(element(doc, 'li', '', item.label + ' · ' + item.count))); disclosure.appendChild(ul); coverageBox.appendChild(disclosure);
    }
    const updated = kyivDate(view.updated_at);
    if (updated) coverageBox.appendChild(element(doc, 'p', 'ig-memory-updated', 'Оновлено ' + updated + ' · за Києвом'));
    root.appendChild(coverageBox);
  }
  function render(root, view, options) {
    options = options || {};
    const doc = root.ownerDocument;
    root.replaceChildren(); root.className = 'ig-memory';
    const valid = view && view.schema === 'ig-memory-presentation.v1';
    const status = valid && ['timeline', 'empty', 'legacy', 'unavailable'].includes(view.status) ? view.status : 'unavailable';
    if (status !== 'timeline') {
      const copy = STATES[status], box = element(doc, 'div', 'ig-memory-empty' + (status === 'unavailable' ? ' is-unavailable' : ''));
      box.append(element(doc, 'p', 'ig-memory-heading', copy[0]), element(doc, 'p', 'ig-memory-note', valid && text(view.reason_label) ? view.reason_label : copy[1]));
      if (status === 'legacy' && text(view.legacy_text).trim()) {
        const previous = details(doc, 'Переглянути попередній запис');
        previous.appendChild(element(doc, 'p', 'ig-memory-quote', view.legacy_text)); box.appendChild(previous);
      }
      root.appendChild(box);
      const coverage = view && view.coverage;
      if (status === 'empty' && coverage && [coverage.source_count, coverage.omitted_count, coverage.pending_count].some(value => count(value) > 0)) {
        renderCoverage(doc, root, view, 0);
      }
      return status;
    }
    const events = Array.isArray(view.events) ? view.events.filter(event => event && text(event.quote).trim()).slice(0, 100) : [];
    if (!events.length) return render(root, { schema: 'ig-memory-presentation.v1', status: 'unavailable' }, options);
    const overview = element(doc, 'div', 'ig-memory-overview'), copy = element(doc, 'div', 'ig-memory-overview-copy');
    copy.append(element(doc, 'p', 'ig-memory-heading', 'Збережені слова клієнта'),
      element(doc, 'p', 'ig-memory-note', 'Цитати з попередніх повідомлень. Актуальні побажання звіряйте у діалозі.'));
    const eventCount = element(doc, 'span', 'ig-memory-count', String(events.length));
    eventCount.setAttribute('aria-label', 'Збережених подій: ' + events.length);
    overview.append(copy, eventCount);
    root.appendChild(overview);
    const list = element(doc, 'ol', 'ig-memory-list');
    list.setAttribute('role', 'list'); list.setAttribute('aria-label', 'Історія збережених повідомлень клієнта');
    events.forEach(event => list.appendChild(renderEvent(doc, event, options))); root.appendChild(list);
    renderCoverage(doc, root, view, events.length); return status;
  }
  global.IgMemoryCard = Object.freeze({ render, kyivDate });
})(window);
