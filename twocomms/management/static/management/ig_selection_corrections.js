(function (global) {
  'use strict';
  const mounts = new WeakMap();
  const reasons = {
    source_interpretation_corrected: 'Трактування джерела',
    requirement_verified: 'Вимогу перевірено',
    clear_unverified_requirement: 'Немає підтвердження'
  };
  const sizes = new Set(['XS', 'S', 'M', 'L', 'XL', 'XXL', 'XXXL', 'XXXXL', '5XL', '6XL', '7XL', '8XL', 'ONE SIZE']);
  const errors = {
    correction_context_conflict: 'Контекст змінився. Перегляньте актуальну картку перед новим збереженням.',
    correction_selection_conflict: 'Вимога змінилася. Перегляньте актуальну картку перед новим збереженням.',
    correction_state_unavailable: 'Актуальний стан недоступний. Оновіть картку перед збереженням.',
    correction_permission_denied: 'Немає дозволу на уточнення розміру.',
    client_unavailable: 'Клієнт недоступний. Уточнення вимкнено.',
    correction_request_invalid: 'Запит відхилено. Зміни в полі залишено.',
    correction_receipt_invalid: 'Аудит операції не підтверджено. Перевірте актуальну картку.',
    correction_operation_conflict: 'Ідентифікатор операції вже має інший вміст. Перевірте актуальну картку.'
  };
  function id(value) {
    const text = String(value == null ? '' : value);
    return /^[1-9][0-9]{0,18}$/.test(text) && BigInt(text) <= 9223372036854775807n &&
      (typeof value !== 'number' || Number.isSafeInteger(value)) ? text : null;
  }
  function normalize(value) {
    if (typeof value !== 'string') return null;
    const result = value.trim().toUpperCase().replace(/\s+/g, ' ');
    return sizes.has(result) || /^(?:[2-5][0-9]|1[0-7][0-9])$/.test(result) ? result : null;
  }
  function newUuid() {
    if (!global.crypto) throw new Error('secure_random_unavailable');
    if (global.crypto.randomUUID) return global.crypto.randomUUID();
    const bytes = global.crypto.getRandomValues(new Uint8Array(16));
    bytes[6] = (bytes[6] & 15) | 64; bytes[8] = (bytes[8] & 63) | 128;
    const hex = Array.from(bytes, byte => byte.toString(16).padStart(2, '0')).join('');
    return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
  }
  function captured(clientId, envelope) {
    const state = envelope && envelope.state, boundary = state && state.boundary;
    const proof = boundary && boundary.size_correction_context, context = proof && proof.context;
    if (!envelope || envelope.status !== 'captured' || envelope.view_mode !== 'current_admin' ||
        !state || state.status !== 'captured' || !boundary || boundary.historical !== false ||
        boundary.view_mode !== 'current_admin' || id(boundary.client_id) !== clientId ||
        !proof || proof.available !== true || !/^[a-f0-9]{64}$/.test(proof.context_digest || '') ||
        !context || context.schema !== 'size-correction-context.v1' || context.field !== 'size' ||
        !context.scope || id(context.scope.client_id) !== clientId ||
        !Number.isSafeInteger(context.selection_revision) || context.selection_revision <= 0 ||
        context.selection_revision !== envelope.selection_revision || context.selection_revision !== boundary.selection_revision ||
        !context.source || !id(context.source.source_message_id) ||
        (context.value !== null && normalize(context.value) !== context.value)) return null;
    // Never retain a mutable reference to a later root-card update.
    return JSON.parse(JSON.stringify({ context, context_digest: proof.context_digest,
      slot: (state.slots || {})['choice.size'] || {} }));
  }
  function create(root, options) {
    if (!root || !root.ownerDocument) throw new TypeError('Size corrections require a mount element');
    if (mounts.has(root)) return mounts.get(root);
    const opts = Object.assign({}, options), dom = root.ownerDocument;
    const el = (tag, className, text) => {
      const node = dom.createElement(tag); node.className = className;
      if (text != null) node.textContent = text;
      return node;
    };
    const button = (action, text, callback) => {
      const node = el('button', 'ig-sc-button', text); node.type = 'button'; node.dataset.action = action;
      node.addEventListener('click', callback); return node;
    };
    const panel = el('section', 'ig-selection-corrections');
    panel.setAttribute('aria-label', 'Уточнення розміру менеджером');
    const heading = el('div', 'ig-sc-heading');
    heading.append(el('h3', 'ig-sc-title', 'Вимога розміру'));
    const authority = el('span', 'ig-sc-authority'); heading.append(authority);
    const summary = el('div', 'ig-sc-summary'), currentValue = el('strong', 'ig-sc-current-value');
    const summaryLabel = el('span', 'ig-sc-summary-label', 'Поточна вимога');
    const valueBlock = el('div', 'ig-sc-value-block'); valueBlock.append(summaryLabel, currentValue);
    const edit = button('edit', 'Уточнити', () => {
      if (edit.disabled || destroyed) return false;
      editing = true; paint(); size.focus(); say(''); return true;
    });
    summary.append(valueBlock, edit);
    const provenance = el('p', 'ig-sc-provenance');
    panel.append(heading, summary, provenance);
    const editor = el('div', 'ig-sc-editor'), fields = el('div', 'ig-sc-fields');
    const modeLabel = el('label', 'ig-sc-label', 'Дія'), mode = el('select', 'ig-sc-operation');
    for (const [value, title] of [['set', 'Уточнити розмір'], ['clear', 'Очистити вимогу розміру']]) {
      const item = el('option', '', title); item.value = value; mode.append(item);
    }
    modeLabel.append(mode);
    const sizeLabel = el('label', 'ig-sc-label', 'Уточнений розмір'), size = el('input', 'ig-sc-size');
    size.type = 'text'; size.maxLength = 12; size.autocomplete = 'off'; size.setAttribute('aria-label', 'Уточнений розмір');
    sizeLabel.append(size);
    const reasonLabel = el('label', 'ig-sc-label ig-sc-reason-label', 'Підстава'), reason = el('select', 'ig-sc-reason');
    Object.entries(reasons).forEach(([value, title]) => { const item = el('option', '', title); item.value = value; reason.append(item); });
    reasonLabel.append(reason);
    const actions = el('div', 'ig-sc-actions');
    const save = button('save', 'Зберегти уточнення', () => commit());
    const discard = button('discard', 'Скасувати', () => {
      if (busy || pending) return false;
      if (staged) { capture = staged; staged = null; validView = true; }
      editing = false; fill(); say('Незбережені зміни відкинуто.'); return true;
    });
    const accept = button('accept-context', 'Прийняти актуальний контекст', () => {
      if (busy || pending || !staged) return false;
      capture = staged; staged = null; validView = true; invalidated = false;
      baseline = initial(capture); paint(); say('Актуальний контекст прийнято. Перевірте розмір у полі перед збереженням.'); return true;
    });
    actions.append(save, discard, accept);
    const impact = el('p', 'ig-sc-impact');
    const status = el('p', 'ig-sc-status'); status.setAttribute('role', 'status'); status.setAttribute('aria-live', 'polite'); status.tabIndex = -1;
    fields.append(modeLabel, sizeLabel, reasonLabel); editor.append(fields, impact, actions);
    panel.append(editor, status); root.replaceChildren(panel);
    let clientId = null, capture = null, staged = null, baseline = null, pending = null;
    let busy = false, validView = false, invalidated = false, destroyed = false, editing = false;
    const initial = proof => ({ operation: 'set', value: proof ? proof.context.value || '' : '', reason: 'source_interpretation_corrected' });
    const draft = () => ({ operation: mode.value, value: size.value, reason: reason.value });
    const dirty = () => !!baseline && JSON.stringify(draft()) !== JSON.stringify(baseline);
    const protectedWork = () => busy || !!pending || dirty();
    function say(message, error) {
      status.textContent = message; status.className = 'ig-sc-status' + (error ? ' ig-sc-error' : '');
      if (error) status.focus();
    }
    function fill() {
      baseline = initial(capture); mode.value = baseline.operation; size.value = baseline.value; reason.value = baseline.reason;
      invalidated = false; paint();
    }
    function paint() {
      const editable = editing && !!capture && validView && !invalidated && !pending && !busy && !destroyed;
      editor.hidden = !editing && !pending && !staged;
      edit.hidden = editing || !!pending || !!staged;
      edit.disabled = !capture || !validView || invalidated || busy || destroyed;
      edit.setAttribute('aria-expanded', String(!editor.hidden));
      panel.dataset.state = busy ? 'busy' : pending ? 'unknown' : staged ? 'conflict' : !validView ? 'unavailable' : invalidated ? 'confirmed' : editing ? 'editing' : 'current';
      mode.disabled = !editable; size.disabled = !editable || mode.value === 'clear'; reason.disabled = !editable;
      size.readOnly = !editable;
      save.disabled = busy || destroyed || !capture || !validView || invalidated ||
        (!pending && (!dirty() || !Object.hasOwn(reasons, reason.value) ||
          (mode.value !== 'clear' && (mode.value !== 'set' || !normalize(size.value)))));
      save.textContent = busy ? 'Зберігаємо…' : pending ? 'Перевірити й повторити' : mode.value === 'clear' ? 'Очистити вимогу' : 'Зберегти уточнення';
      discard.disabled = busy || !!pending || !editing;
      discard.textContent = dirty() ? 'Відкинути зміни' : 'Скасувати';
      accept.hidden = !staged; accept.disabled = busy || !!pending;
      impact.textContent = mode.value === 'clear' ? 'Приберемо лише поточну вимогу розміру. Повідомлення клієнта та історія залишаться.' :
        'Збережемо уточнення від менеджера. Початкове повідомлення залишиться; наявність перевіряється окремо.';
      if (!capture) { currentValue.textContent = '—'; authority.textContent = 'Не підтверджено'; provenance.textContent = 'Для уточнення потрібне підтверджене поточне джерело.'; return; }
      const visible = staged || capture;
      const sourceId = visible.context.source.source_message_id;
      const refs = Array.isArray(visible.slot.source_refs) ? visible.slot.source_refs : [];
      const actor = refs.find(item => item && item.kind === 'commerce_transition' && id(item.actor_id));
      summaryLabel.textContent = staged ? 'Актуальна вимога змінилася' : invalidated && !dirty() ? 'Картка до збереження' : 'Поточна вимога';
      currentValue.textContent = visible.context.value || 'Очищено';
      authority.textContent = visible.slot.authority === 'audited_correction' ? `Менеджер${actor ? ' №' + actor.actor_id : ''}` : 'Клієнт';
      provenance.textContent = `Джерело: повідомлення №${sourceId}. ` + (visible.slot.authority === 'audited_correction' ? 'Початковий текст збережено в історії.' : 'Вимога з повідомлення клієнта.');
    }
    function changed() { paint(); }
    mode.addEventListener('change', changed); size.addEventListener('input', changed); reason.addEventListener('change', changed);
    async function commit() {
      if (save.disabled || destroyed) return false;
      if (!pending) {
        let operationId;
        try { operationId = newUuid(); } catch (_) { say('Безпечний ідентифікатор операції недоступний.', true); return false; }
        const body = { field: 'size', operation_id: operationId, expected_selection_revision: capture.context.selection_revision,
          expected_context_digest: capture.context_digest, operation: mode.value,
          value: mode.value === 'clear' ? null : normalize(size.value), reason_code: reason.value };
        pending = { clientId, body, wire: JSON.stringify(body) };
      }
      const attempt = pending; busy = true; paint(); say('Збереження уточнення…');
      let response, data;
      try {
        response = await global.fetch(`/bot/api/clients/${attempt.clientId}/state/size/`, {
          method: 'POST', credentials: 'same-origin', headers: { 'Content-Type': 'application/json', 'X-CSRFToken': opts.csrfToken || '' },
          body: attempt.wire
        });
        data = await response.json();
      } catch (_) {
        busy = false; paint(); say('Результат операції невідомий. Повторіть ту саму операцію для перевірки.', true); return false;
      }
      const success = response.status === 200 && data && data.success === true && data.operation_id === attempt.body.operation_id &&
        data.field === 'size' && ['applied', 'replayed', 'noop'].includes(data.status) && id(data.selection_revision) &&
        (data.status === 'noop' ? data.transition_id === null : !!id(data.transition_id));
      busy = false;
      if (success) {
        pending = null; staged = null; invalidated = true; editing = false;
        baseline = draft(); paint(); say(data.status === 'noop' ? 'Вимога вже має цей стан. Оновлюємо картку.' : 'Уточнення підтверджено. Оновлюємо картку.');
        try { if (typeof opts.onCommitted === 'function') await opts.onCommitted(attempt.clientId, data); }
        catch (_) { say('Уточнення підтверджено. Актуальну картку поки не завантажено.', true); }
        return true;
      }
      if (response.status >= 400 && response.status < 500 && data && data.success === false && typeof data.code === 'string') {
        pending = null; invalidated = true; staged = null; paint();
        say(errors[data.code] || 'Збереження відхилено. Перегляньте актуальну картку; зміни в полі залишено.', true); return false;
      }
      paint(); say('Результат операції невідомий. Повторіть ту саму операцію для перевірки.', true); return false;
    }
    const api = {
      render(nextClientId, envelope) {
        const nextId = id(nextClientId);
        if (destroyed || !nextId) return false;
        if (clientId && nextId !== clientId && protectedWork()) { say('Спочатку збережіть або відкиньте зміни для поточного клієнта.', true); return false; }
        const fresh = captured(nextId, envelope);
        if (clientId === nextId && pending) {
          validView = !!fresh; paint();
          say('Попередня операція ще не має підтвердженого результату. Її ідентифікатор і контекст збережено.', true); return false;
        }
        if (clientId === nextId && dirty()) {
          if (!fresh) { validView = false; staged = null; paint(); say('Ця картка не дозволяє уточнення поточного стану. Ваші зміни залишено.', true); return false; }
          if (!capture || fresh.context_digest !== capture.context_digest || invalidated) {
            staged = fresh; invalidated = true; validView = true; paint();
            say('Картка змінилася. Ваш розмір залишено; перегляньте актуальну вимогу та прийміть новий контекст.', true); return false;
          }
          validView = true; paint(); return true;
        }
        clientId = nextId; capture = fresh; staged = null; validView = !!fresh; editing = false; fill();
        say(fresh ? '' : 'Уточнення недоступне для цієї картки.');
        return !!fresh;
      },
      canLeave(nextClientId) { return !destroyed && (!protectedWork() || (id(nextClientId) !== null && id(nextClientId) === clientId)); },
      clear() {
        if (protectedWork() || destroyed) return false;
        clientId = null; capture = null; staged = null; validView = false; editing = false; fill(); say(''); return true;
      },
      destroy() {
        if (protectedWork() || destroyed) return false;
        destroyed = true; mounts.delete(root); root.replaceChildren(); return true;
      }
    };
    mounts.set(root, api); fill(); say('Оберіть клієнта та завантажте його поточну картку.'); return api;
  }
  global.IgSelectionCorrections = Object.freeze({ create });
})(window);
