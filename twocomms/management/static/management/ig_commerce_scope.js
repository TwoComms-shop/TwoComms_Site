(function (global) {
  'use strict';
  const mounts = new WeakMap();
  function id(value) {
    const text = String(value == null ? '' : value);
    return /^[1-9][0-9]{0,18}$/.test(text) && BigInt(text) <= 9223372036854775807n &&
      (typeof value !== 'number' || Number.isSafeInteger(value)) ? text : null;
  }
  function choiceLabel(field, value) {
    const labels = {
      color: { black: 'Чорний', white: 'Білий', blue: 'Синій', pink: 'Рожевий', grey: 'Сірий', gray: 'Сірий',
        green: 'Зелений', red: 'Червоний', yellow: 'Жовтий', purple: 'Фіолетовий', brown: 'Коричневий',
        orange: 'Помаранчевий', beige: 'Бежевий', navy: 'Темно-синій' },
      fit_option_code: { classic: 'Класична', oversize: 'Оверсайз' },
      garment_type: { tshirt: 'Футболка', 't-shirt': 'Футболка', hoodie: 'Худі' }
    };
    return typeof value === 'string' && labels[field] && Object.hasOwn(labels[field], value) ? labels[field][value] : value;
  }
  function create(root, options) {
    if (!root || !root.ownerDocument) throw new TypeError('Commerce scope requires a mount element');
    if (mounts.has(root)) return mounts.get(root);
    const opts = Object.assign({}, options), dom = root.ownerDocument;
    const el = (tag, className, text) => {
      const node = dom.createElement(tag); node.className = className;
      if (text != null) node.textContent = String(text); return node;
    };
    const panel = el('section', 'ig-commerce-scope'); panel.setAttribute('aria-label', 'Поточні позиції та історія замовлень');
    const header = el('div', 'ig-cs-header'), title = el('h3', 'ig-cs-title', 'Вибір і замовлення');
    const count = el('span', 'ig-cs-count'); header.append(title, count);
    const label = el('label', 'ig-cs-picker-label', 'Перегляд'), picker = el('select', 'ig-cs-picker');
    picker.setAttribute('aria-label', 'Поточний вибір або конкретне замовлення'); label.append(picker);
    const summary = el('div', 'ig-cs-summary'), positions = el('ol', 'ig-cs-positions');
    const status = el('p', 'ig-cs-status'); status.setAttribute('role', 'status'); status.setAttribute('aria-live', 'polite');
    const revenue = el('div', 'ig-cs-revenue');
    panel.append(header, revenue, label, summary, positions, status); root.replaceChildren(panel);
    let clientId = null, envelope = null, destroyed = false;
    function sourceLabel(field) {
      const refs = Array.isArray(field.source_refs) ? field.source_refs : [];
      const message = refs.find(row => row && row.kind === 'message' && id(row.id));
      const audit = refs.find(row => row && row.kind === 'commerce_transition' && id(row.actor_id));
      const origin = field.copied_from && field.copied_from.proof;
      if (origin && id(origin.source_message_id)) return `Повторено з попереднього вибору · джерело №${origin.source_message_id}` +
        (message ? ` · запит №${message.id}` : '');
      return (field.authority === 'audited_correction' ? `Уточнив менеджер${audit ? ' №' + audit.actor_id : ''}` :
        field.authority === 'validated_selection_action' ? 'Підтверджений вибір' : 'З повідомлення клієнта') +
        (message ? ` · №${message.id}` : '');
    }
    function currentPosition(line) {
      const node = el('li', 'ig-cs-position');
      const heading = el('div', 'ig-cs-position-heading');
      const caption = el('div', 'ig-cs-position-caption');
      caption.append(el('span', 'ig-cs-position-number', `Позиція ${line.index + 1}`),
        el('strong', 'ig-cs-product', line.title || 'Модель ще не обрана'));
      heading.append(caption);
      if (line.active === true) heading.append(el('span', 'ig-cs-active', line.editable === true ? 'Поточна для уточнення' : 'Поточна позиція'));
      const recipients = { self: 'Для себе', friend: 'Для друга', friend_female: 'Для подруги', mother: 'Для мами', father: 'Для тата' };
      node.append(heading, el('p', 'ig-cs-recipient', Object.hasOwn(recipients, line.recipient_id) ? recipients[line.recipient_id] : line.recipient_id ? 'Окремий отримувач' : 'Отримувача не уточнено'));
      const fields = line.fields || {}, details = el('dl', 'ig-cs-fields');
      for (const [key, name] of [['size', 'Розмір'], ['color', 'Колір'], ['fit_option_code', 'Посадка'], ['quantity', 'Кількість']]) {
        const field = fields[key];
        if (!field || field.status !== 'confirmed' || !['customer_source', 'audited_correction', 'validated_selection_action'].includes(field.authority)) continue;
        const group = el('div', 'ig-cs-field'), term = el('dt', '', name);
        const refs = Array.isArray(field.source_refs) ? field.source_refs : [];
        const message = refs.find(row => row && row.kind === 'message' && id(row.id));
        const audit = refs.find(row => row && row.kind === 'commerce_transition' && id(row.actor_id));
        if (message || audit) {
          const origin = field.copied_from && field.copied_from.proof;
          const evidence = el('span', 'ig-cs-field-source', origin && id(origin.source_message_id) ? `Повторено · №${origin.source_message_id}` : field.authority === 'audited_correction' ?
            `Менеджер${audit ? ' №' + audit.actor_id : ''}${message ? ' / №' + message.id : ''}` : message ? '№' + message.id : 'Підтверджено');
          evidence.setAttribute('title', sourceLabel(field)); term.append(evidence);
        }
        group.append(term, el('dd', '', choiceLabel(key, field.value)));
        details.append(group);
      }
      const defaultQuantity = (line.defaults || {}).quantity;
      if ((!fields.quantity || fields.quantity.status !== 'confirmed') && defaultQuantity && defaultQuantity.value === 1 &&
          defaultQuantity.authority === 'existing_cart_default' && defaultQuantity.source_confirmed === false) {
        const group = el('div', 'ig-cs-field'), term = el('dt', '', 'Кількість');
        term.append(el('span', 'ig-cs-field-source', 'За замовчуванням')); group.append(term, el('dd', '', '1')); details.append(group);
      }
      node.append(details);
      const chosen = fields.product_id;
      if (chosen && chosen.status === 'confirmed') node.append(el('p', 'ig-cs-source', sourceLabel(chosen)));
      const history = line.history;
      if (history && history.status === 'captured' && Array.isArray(history.items) && history.items.length) {
        const disclosure = el('details', 'ig-cs-history'), heading = el('summary', '', 'Попередні зміни');
        const list = el('ul', 'ig-cs-history-list');
        history.items.slice(0, 12).forEach(item => {
          const names = { size: 'Розмір', color: 'Колір', fit_option_code: 'Посадка', quantity: 'Кількість', product_id: 'Модель', garment_type: 'Тип' };
          if (!item || !Object.hasOwn(names, item.field) || !item.source) return;
          const change = item.previous_source ? `${item.previous == null ? 'Очищено' : choiceLabel(item.field, item.previous)} → ${item.value == null ? 'Очищено' : choiceLabel(item.field, item.value)}` : item.value == null ? 'Очищено' : choiceLabel(item.field, item.value);
          list.append(el('li', '', `${names[item.field]}: ${change} · ${sourceLabel(item.source)}`));
        });
        disclosure.append(heading, list);
        if (history.coverage && history.coverage.complete === false) disclosure.append(el('p', 'ig-cs-history-coverage',
          'Показано до 8 підтверджених змін. Старіші джерела можуть бути поза цим переглядом.'));
        node.append(disclosure);
      }
      return node;
    }
    function orderPosition(item, index) {
      const node = el('li', 'ig-cs-position');
      node.append(el('span', 'ig-cs-position-number', `Позиція ${index + 1}`),
        el('strong', 'ig-cs-product', item.title || 'Позиція замовлення'));
      const values = [item.size && `Розмір ${item.size}`, item.color && choiceLabel('color', item.color), item.fit && choiceLabel('fit_option_code', item.fit),
        Number.isSafeInteger(item.quantity) && item.quantity > 0 ? `× ${item.quantity}` : ''].filter(Boolean);
      node.append(el('p', 'ig-cs-order-item-detail', values.join(' · '))); return node;
    }
    function paint() {
      picker.replaceChildren(); summary.replaceChildren(); positions.replaceChildren(); revenue.replaceChildren();
      if (!envelope) { picker.disabled = true; count.textContent = ''; status.textContent = 'Оберіть клієнта та відкрийте його поточний контекст.'; return; }
      const current = envelope.current || {}, history = envelope.history || {}, orders = Array.isArray(history.orders) ? history.orders : [];
      const option = el('option', '', 'Поточний вибір'); option.value = ''; picker.append(option);
      orders.slice(0, 20).forEach(order => {
        if (!order || order.kind !== 'physical_order' || !id(order.id)) return;
        const option = el('option', '', `Замовлення ${order.number || '#' + order.id}${order.created_label ? ' · ' + order.created_label : ''}`);
        option.value = String(order.id); picker.append(option);
      });
      const selectedId = (envelope.selection || {}).order_id;
      const selected = selectedId != null ? orders.find(order => order.kind === 'physical_order' && id(order.id) === id(selectedId)) : null;
      picker.value = selected ? String(selected.id) : ''; picker.disabled = orders.length === 0;
      count.textContent = Number.isSafeInteger(history.completed_order_count) ? `Отримано замовлень: ${history.completed_order_count}` : '';
      const ltv = history.ltv || {};
      const money = value => typeof value === 'string' && /^(?:0|[1-9][0-9]{0,13})\.[0-9]{2}$/.test(value);
      const minor = value => BigInt(value.replace('.', ''));
      const confirmedRevenue = history.coverage === 'complete' && ltv.status === 'confirmed' && ltv.currency === 'UAH' &&
        ltv.currency_code === 980 && ltv.refund_coverage === 'confirmed_projection_as_of_capture' && money(ltv.value) &&
        money(ltv.gross) && money(ltv.refunded) && minor(ltv.gross) > 0n && minor(ltv.gross) >= minor(ltv.refunded) &&
        minor(ltv.gross) - minor(ltv.refunded) === minor(ltv.value) && Array.isArray(ltv.proofs) &&
        ltv.proofs.length === orders.length && new Set(ltv.proofs.map(proof => proof && id(proof.order_id))).size === orders.length && ltv.proofs.every(proof => proof && id(proof.event_id) && id(proof.projection_id) &&
          orders.some(order => id(order.id) === id(proof.order_id))) && Array.isArray(ltv.order_ids) && ltv.order_ids.length > 0 &&
        ltv.order_ids.length === orders.length && new Set(ltv.order_ids.map(id)).size === orders.length &&
        ltv.order_ids.every(orderId => id(orderId) && orders.some(order => id(order.id) === id(orderId)));
      revenue.append(el('span', 'ig-cs-revenue-label', 'Підтверджена виручка'),
        el('strong', 'ig-cs-revenue-value', confirmedRevenue ? `${ltv.value.replace('.', ',')} грн` : 'Не визначено'));
      revenue.append(el('p', 'ig-cs-revenue-note', confirmedRevenue ?
        `Надходження за всю підтверджену історію мінус повернення${ltv.refunded !== '0.00' ? ' (' + ltv.refunded.replace('.', ',') + ' грн)' : ''}. Стан на час перегляду.` :
        history.coverage !== 'complete' ? 'Історія замовлень охоплена частково; загальну виручку не підраховуємо.' :
          'Для всієї історії потрібні підтверджені надходження та дані про повернення. Статус «оплачено» сам по собі не визначає суму.'));
      if (selected) {
        summary.append(el('strong', 'ig-cs-view-title', `Замовлення ${selected.number || '#' + selected.id}`),
          el('p', 'ig-cs-scope-label', `${selected.created_label || selected.created_at || ''} · ${selected.status_label || 'Статус не підтверджено'}`));
        if (selected.payment && selected.payment.label) summary.append(el('p', 'ig-cs-payment', selected.payment.label));
        (Array.isArray(selected.items) ? selected.items : []).slice(0, 32).forEach((item, index) => positions.append(orderPosition(item, index)));
        status.textContent = 'Історія конкретного замовлення. Поточні вимоги клієнта залишаються окремо.';
      } else if (selectedId == null) {
        const scope = current.scope || {};
        const boundOrder = scope.order_id != null ? orders.find(order => id(order.id) === id(scope.order_id)) : null;
        const scopeLabel = scope.order_id == null ? 'Чернетка вибору · Без прив’язаного замовлення' :
          current.order_binding && current.order_binding.status === 'unknown' ? 'Прив’язку замовлення не підтверджено' :
            boundOrder && boundOrder.number ? `Для замовлення ${boundOrder.number}` : 'Є прив’язане замовлення';
        summary.append(el('strong', 'ig-cs-view-title', 'Поточний вибір'), el('p', 'ig-cs-scope-label', scopeLabel));
        (Array.isArray(current.lines) ? current.lines : []).slice(0, 16).forEach(line => {
          if (line && typeof line.line_id === 'string' && Number.isSafeInteger(line.index)) positions.append(currentPosition(line));
        });
        status.textContent = current.status === 'captured' ? 'Вибір позицій не підтверджує оплату чи наявність.' : 'Поточні позиції не вдалося підтвердити за джерелами.';
      } else {
        summary.append(el('strong', 'ig-cs-view-title', 'Замовлення не підтверджене в цьому перегляді'));
        status.textContent = 'Обране замовлення недоступне. Поточні позиції не підміняють його історію.';
      }
      if (history.coverage === 'bounded') status.textContent += ' Показано останні 20 прив’язаних замовлень; старіша історія тут не охоплена.';
      if (history.coverage === 'uncertain') status.textContent += ' Частина історії має непідтверджену прив’язку.';
      if (history.archived_purchase && history.archived_purchase.confirmed === true) status.textContent +=
        ' Підтверджено покупку раніше; її конкретне замовлення в цій картці не відновлюємо.';
    }
    picker.addEventListener('change', () => {
      if (destroyed || !envelope || picker.disabled) return;
      const orderId = picker.value === '' ? null : id(picker.value);
      const orders = (envelope.history || {}).orders || [];
      if (orderId !== null && !orders.some(order => order.kind === 'physical_order' && id(order.id) === orderId)) return;
      if (typeof opts.onSelectOrder === 'function') return opts.onSelectOrder(clientId, orderId);
    });
    const api = {
      render(nextClientId, data) {
        const nextId = id(nextClientId);
        if (destroyed || !nextId || !data || data.schema !== 'commerce-scope.v1' ||
          data.view_mode !== 'current_commerce_scope' || data.status !== 'captured' || id(data.client_id) !== nextId) {
          envelope = null; paint(); return false;
        }
        clientId = nextId; envelope = JSON.parse(JSON.stringify(data)); paint(); return true;
      },
      clear() { if (destroyed) return false; clientId = null; envelope = null; paint(); return true; },
      destroy() { if (destroyed) return false; destroyed = true; mounts.delete(root); root.replaceChildren(); return true; }
    };
    mounts.set(root, api); paint(); return api;
  }
  global.IgCommerceScope = Object.freeze({ create });
})(window);
