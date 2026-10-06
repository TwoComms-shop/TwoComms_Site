(function (global) {
  'use strict';
  const mounts = new WeakMap();
  const kinds = { reply_draft: 'Чернетка відповіді', internal_note: 'Приватна нотатка' };
  const states = { open: 'Відкрито', consumed: 'Передано на надсилання', archived: 'В архіві' };
  const errors = {
    actor_not_authorized: 'Немає доступу до приватних документів.',
    client_unavailable: 'Клієнт недоступний. Документ не можна відновити.',
    private_document_missing: 'Документ недоступний.',
    private_document_stale: 'Версія змінилася. Ваш текст збережено в полі; перевірте серверну версію.',
    private_document_conflict: 'Документ з таким ідентифікатором уже має інший вміст.',
    private_document_closed: 'Документ уже закрито.',
    private_context_changed: 'Контекст клієнта змінився. Збережений документ не можна надіслати.',
    newer_inbound: 'Є нове повідомлення клієнта. Ця чернетка більше не відповідає контексту.',
    reply_window_closed: 'Вікно відповіді закрито.',
    permission_epoch_changed: 'Дозвіл на відповідь змінився.',
    operation_conflict: 'Операція вже пов’язана з іншою командою.',
    competing_command: 'Інша відповідь уже обробляється.',
    private_note_not_sendable: 'Приватна нотатка не надсилається клієнту.',
    human_dispatch_unavailable: 'Команду прийнято. Результат ще недоступний; можна перевірити ту саму операцію.',
    settings_unavailable: 'Налаштування тимчасово недоступні.',
  };
  function id(value) {
    const text = String(value == null ? '' : value);
    return /^[1-9][0-9]{0,18}$/.test(text) && BigInt(text) <= 9223372036854775807n &&
      (typeof value !== 'number' || Number.isSafeInteger(value)) ? text : null;
  }
  function uuid(value) { return typeof value === 'string' && /^[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}$/.test(value) && value !== '00000000-0000-0000-0000-000000000000'; }
  function newUuid() {
    if (global.crypto.randomUUID) return global.crypto.randomUUID();
    const bytes = global.crypto.getRandomValues(new Uint8Array(16));
    bytes[6] = (bytes[6] & 15) | 64; bytes[8] = (bytes[8] & 63) | 128;
    const hex = Array.from(bytes, b => b.toString(16).padStart(2, '0')).join('');
    return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
  }
  function validDocument(doc, textRequired) {
    return doc && uuid(doc.document_id) && Object.hasOwn(kinds, doc.kind) && Object.hasOwn(states, doc.state) &&
      id(doc.version) && /^[a-f0-9]{64}$/.test(doc.text_hash) &&
      (!textRequired || (typeof doc.text === 'string' && Array.from(doc.text).length <= 4000));
  }
  function mount(root, options) {
    if (!root || !root.ownerDocument) throw new TypeError('Human documents require a root element');
    const existing = mounts.get(root);
    if (existing) { existing.update(options); return existing; }
    let opts = Object.assign({}, options);
    let clientId = id(opts.clientId), contextId = id(opts.contextMessageId);
    if (!clientId) throw new TypeError('Invalid human document client');
    const dom = root.ownerDocument;
    const el = (tag, className, text) => {
      const node = dom.createElement(tag);
      if (className) node.className = className;
      if (text != null) node.textContent = text;
      return node;
    };
    const button = (name, text, callback) => {
      const node = el('button', 'ig-hd-button', text);
      node.type = 'button'; node.dataset.action = name;
      node.addEventListener('click', callback); return node;
    };
    const panel = el('section', 'ig-human-documents');
    panel.setAttribute('aria-label', 'Мої приватні чернетки та нотатки');
    panel.append(el('h3', 'ig-hd-title', 'Мої чернетки та нотатки'));
    panel.append(el('p', 'ig-hd-help', 'Документи бачите лише ви. Збереження не надсилає текст клієнту.'));
    const layout = el('div', 'ig-hd-layout'), library = el('div', 'ig-hd-library'), editor = el('div', 'ig-hd-editor');
    const load = button('load', 'Завантажити мої документи', () => loadList(false));
    const list = el('ul', 'ig-hd-list');
    list.setAttribute('aria-label', 'Збережені приватні документи');
    const more = button('more', 'Ще документи', () => loadList(true)); more.hidden = true;
    library.append(load, list, more);
    const label = el('label', 'ig-hd-label', 'Тип документа');
    const kind = el('select', 'ig-hd-kind'); kind.setAttribute('aria-label', 'Тип документа');
    Object.entries(kinds).forEach(([value, title]) => { const item = el('option', '', title); item.value = value; kind.append(item); });
    label.append(kind);
    const newButton = button('new', 'Новий документ', () => startNew());
    const textLabel = el('label', 'ig-hd-label', 'Текст приватного документа');
    const text = el('textarea', 'ig-hd-text'); text.rows = 7;
    text.setAttribute('aria-label', 'Текст приватного документа');
    textLabel.append(text);
    const count = el('span', 'ig-hd-count', '0 / 4000');
    const description = el('p', 'ig-hd-help');
    const meta = el('p', 'ig-hd-meta', 'Новий документ · ще не збережено');
    const actions = el('div', 'ig-hd-actions');
    const save = button('save', 'Зберегти приватно', () => saveDocument());
    const refresh = button('refresh', 'Перевірити серверну версію', () => checkDocument());
    const discard = button('discard', 'Відкинути незбережений текст', () => { if (!pendingSave && !uncertainSubmit()) { text.value = doc ? doc.text : ''; editGeneration++; render(); say('Незбережені зміни відкинуто.'); } });
    const archive = button('archive', 'Архівувати', () => saveDocument(true));
    const submit = button('submit', 'Надіслати збережену чернетку', () => submitDocument());
    actions.append(save, refresh, discard, archive, submit);
    const status = el('p', 'ig-hd-status'); status.setAttribute('role', 'status'); status.setAttribute('aria-live', 'polite'); status.tabIndex = -1;
    editor.append(label, newButton, textLabel, count, description, meta, actions, status);
    layout.append(library, editor); panel.append(layout); root.replaceChildren(panel);
    let doc = null, creationId = null, pendingSave = null, submission = null, busy = false, mutation = false;
    let generation = 0, editGeneration = 0, aborter = null, destroyed = false, next = null;
    const dirty = () => text.value !== (doc ? doc.text : '');
    const uncertainSubmit = () => submission && submission.retryable;
    const protectedWork = () => dirty() || !!pendingSave || !!uncertainSubmit();
    function say(message, error) {
      status.textContent = message; status.className = 'ig-hd-status' + (error ? ' ig-hd-error' : '');
      if (error) status.focus();
    }
    function render() {
      const open = !doc || doc.state === 'open';
      const frozen = !!pendingSave || !!submission;
      kind.disabled = busy || !!doc || frozen || dirty();
      text.readOnly = !open || mutation || frozen;
      newButton.disabled = busy || protectedWork();
      load.disabled = busy; more.disabled = busy || list.children.length >= 100;
      save.disabled = busy || !open || !!submission || (!pendingSave && (!text.value.trim() || Array.from(text.value).length > 4000 || (!dirty() && !!doc))) || (!doc && !contextId);
      save.textContent = pendingSave ? 'Повторити те саме збереження' : 'Зберегти приватно';
      refresh.disabled = busy || (!doc && !pendingSave) || !!submission;
      discard.disabled = busy || !dirty() || !!pendingSave || !!submission;
      archive.disabled = busy || !doc || !open || dirty() || !!pendingSave || !!submission;
      submit.hidden = (doc ? doc.kind : kind.value) !== 'reply_draft';
      submit.disabled = busy || !doc || dirty() || !!pendingSave || (submission ? !submission.retryable : !open);
      submit.textContent = submission && submission.retryable ? 'Перевірити ту саму операцію' : 'Надіслати збережену чернетку';
      count.textContent = `${Array.from(text.value).length} / 4000`;
      description.textContent = (doc ? doc.kind : kind.value) === 'internal_note'
        ? 'Приватна нотатка. Кнопки надсилання немає.' : 'Спочатку збережіть текст. Надсилання використовує лише збережену версію та передає діалог менеджеру.';
      meta.textContent = doc ? `${kinds[doc.kind]} · ${states[doc.state]} · версія ${doc.version}${dirty() ? ' · є незбережені зміни' : ''}`
        : 'Новий документ · ще не збережено';
      panel.setAttribute('aria-busy', String(busy));
    }
    function base() {
      const url = new URL(opts.baseUrl || '/bot/api/', global.location.origin);
      if (url.origin !== global.location.origin || url.search || url.hash) throw new Error('invalid_base');
      return `${url.pathname.replace(/\/$/, '')}/clients/${clientId}/human-documents/`;
    }
    function errorMessage(result) {
      if (Object.hasOwn(errors, result.data.code)) return errors[result.data.code];
      if (result.status === 403) return 'Доступ заборонено. Оновіть сторінку та перевірте вхід.';
      if (result.status === 404) return 'Документ або клієнт недоступний.';
      if (result.status === 409) return 'Стан змінився. Текст у полі залишено; перевірте серверну версію.';
      if (result.status === 400) return 'Не вдалося прийняти документ. Перевірте текст і його довжину.';
      return 'З’єднання або сервіс недоступні. Дані в полі залишено.';
    }
    async function request(path, body) {
      const token = ++generation;
      if (aborter) aborter.abort();
      aborter = new AbortController(); busy = true; mutation = body !== undefined; render();
      try {
        const csrf = typeof opts.csrf === 'function' ? opts.csrf() : opts.csrf;
        const response = await global.fetch(base() + path, {
          method: body === undefined ? 'GET' : 'POST', credentials: 'same-origin', cache: 'no-store',
          redirect: 'error', signal: aborter.signal,
          headers: body === undefined ? { Accept: 'application/json' } : { Accept: 'application/json', 'Content-Type': 'application/json', 'X-CSRFToken': csrf || '' },
          ...(body === undefined ? {} : { body: JSON.stringify(body) }),
        });
        let data = {}; try { data = await response.json(); } catch (_) { /* Finite generic error below. */ }
        if (destroyed || token !== generation) return null;
        return { status: response.status, ok: response.ok && data.success === true, data };
      } catch (_) {
        if (destroyed || token !== generation) return null;
        return { status: 0, ok: false, data: {} };
      } finally {
        if (!destroyed && token === generation) { busy = false; mutation = false; render(); }
      }
    }
    function notify(detail) {
      if (typeof opts.onRefresh === 'function') { try { opts.onRefresh(Object.assign({ clientId }, detail)); } catch (_) { /* Parent refresh must not alter operation recovery. */ } }
    }
    function acceptDocument(value) {
      doc = Object.assign({}, value); kind.value = doc.kind; text.value = doc.text;
      creationId = doc.document_id; pendingSave = null; editGeneration++; render();
    }
    async function loadList(append) {
      if (busy || (append && (!next || list.children.length >= 100))) return;
      const result = await request(`?state=all&limit=20${append ? '&before_id=' + next : ''}`);
      if (!result) return;
      if (!result.ok || !Array.isArray(result.data.documents) || result.data.documents.length > 20 ||
          !result.data.documents.every(item => validDocument(item, false)) ||
          (result.data.next_before_id != null && !id(result.data.next_before_id))) { say(errorMessage(result), true); return; }
      if (!append) list.replaceChildren();
      result.data.documents.forEach(item => {
        const row = el('li'); const select = button('detail', `${kinds[item.kind]} · ${states[item.state]} · версія ${item.version}`, () => loadDocument(item.document_id));
        row.append(select); list.append(row);
      });
      next = result.data.next_before_id == null ? null : id(result.data.next_before_id); more.hidden = !next;
      render();
      say(list.children.length >= 100 ? 'Показано 100 документів. Для початку списку натисніть «Завантажити мої документи».'
        : result.data.documents.length ? 'Завантажено лише ваші документи.' : 'Збережених документів немає.');
    }
    async function loadDocument(documentId) {
      if (busy) return;
      if (protectedWork()) { say('Спочатку збережіть або явно відкиньте локальні зміни. Поточний текст залишено.', true); return; }
      const before = editGeneration;
      const result = await request(`${documentId}/`);
      if (!result) return;
      if (before !== editGeneration || dirty()) { say('Під час завантаження ви змінили текст. Його залишено в полі.', true); return; }
      if (!result.ok || !validDocument(result.data.document, true) || result.data.document.document_id !== documentId) { say(errorMessage(result), true); return; }
      submission = null; acceptDocument(result.data.document); say('Завантажено приватний документ.'); text.focus();
    }
    function startNew() {
      if (busy || protectedWork()) { say('Спочатку збережіть або відкиньте локальні зміни.', true); return; }
      doc = null; creationId = null; submission = null; text.value = ''; editGeneration++; render(); text.focus();
    }
    async function saveDocument(archiveOnly) {
      if (busy || submission || (archiveOnly && (!doc || dirty()))) return;
      if (!pendingSave) {
        if (!archiveOnly && (!text.value.trim() || Array.from(text.value).length > 4000 || (!doc && !contextId))) { say('Введіть текст до 4000 символів і виберіть діалог із повідомленням клієнта.', true); return; }
        if (!creationId) creationId = newUuid();
        const body = doc ? { expected_version: doc.version, expected_hash: doc.text_hash,
          ...(archiveOnly ? { archive: true } : { text: text.value }) } :
          { document_id: creationId, kind: kind.value, text: text.value, context_message_id: contextId };
        pendingSave = { path: doc ? `${doc.document_id}/update/` : 'create/', body,
          text: archiveOnly ? doc.text : text.value.trim(), archive: !!archiveOnly };
      }
      const snapshot = pendingSave;
      const result = await request(snapshot.path, snapshot.body);
      if (!result) return;
      if (result.ok && validDocument(result.data.document, true) && result.data.document.document_id === creationId &&
          result.data.document.kind === (snapshot.body.kind || doc.kind) && result.data.document.text === snapshot.text &&
          Number(result.data.document.version) === (snapshot.body.expected_version ? Number(snapshot.body.expected_version) + 1 : 1) &&
          (!snapshot.body.context_message_id || id(result.data.document.context_message_id) === snapshot.body.context_message_id) &&
          result.data.document.state === (snapshot.archive ? 'archived' : 'open')) {
        acceptDocument(result.data.document); say(snapshot.archive ? 'Документ архівовано.' : 'Приватний документ збережено.'); notify({ documentId: doc.document_id });
      } else {
        // A malformed success still leaves persistence uncertain. Keep the same
        // body/UUID until an explicit retry or matching server read resolves it.
        if (!result.ok && result.status > 0 && result.status < 500) pendingSave = null;
        render(); say(errorMessage(result), true);
      }
    }
    async function checkDocument() {
      if (busy || submission) return;
      const documentId = doc ? doc.document_id : creationId;
      if (!documentId) return;
      const before = editGeneration, snapshot = pendingSave;
      const result = await request(`${documentId}/`);
      if (!result) return;
      if (!result.ok || !validDocument(result.data.document, true) || result.data.document.document_id !== documentId) { say(errorMessage(result), true); return; }
      const server = result.data.document;
      if (before !== editGeneration) { say('Текст змінено під час перевірки; його залишено в полі.', true); return; }
      if (snapshot && server.text === snapshot.text && server.kind === (snapshot.body.kind || doc.kind) &&
          server.state === (snapshot.archive ? 'archived' : 'open')) {
        acceptDocument(server); say('Сервер підтвердив збереження.'); notify({ documentId: doc.document_id }); return;
      }
      if (dirty() || snapshot) { say(`На сервері версія ${server.version}. Ваш текст залишено. Відкиньте локальні зміни перед завантаженням серверної версії.`, true); return; }
      acceptDocument(server); say('Серверну версію перевірено.');
    }
    async function submitDocument() {
      if (busy || !doc || doc.kind !== 'reply_draft' || dirty() || pendingSave || (submission && !submission.retryable)) return;
      if (!submission) {
        if (doc.state !== 'open') return;
        submission = { documentId: doc.document_id, operation_id: newUuid(), expected_version: doc.version, expected_hash: doc.text_hash, retryable: true };
      }
      const saved = submission;
      const result = await request(`${saved.documentId}/submit/`, {
        operation_id: saved.operation_id, expected_version: saved.expected_version, expected_hash: saved.expected_hash,
      });
      if (!result) return;
      const data = result.data;
      if (data.accepted === true) {
        if (data.operation_id !== saved.operation_id || !id(data.command_id)) { say('Не вдалося підтвердити ідентичність команди. Збережіть цю операцію для перевірки.', true); render(); return; }
        doc.state = 'consumed'; saved.commandId = data.command_id;
        const labels = {
          sent: 'Надсилання підтверджено.',
          unknown: 'Результат надсилання невідомий. Перевірте діалог; повторне надсилання вимкнено.',
          provider_started: 'Надсилання розпочато. Повторне надсилання вимкнено.',
          claimed: 'Команда обробляється. Повторне надсилання вимкнено.',
          pending: 'Команда очікує. Можна перевірити ту саму операцію.',
          definite_failed: 'Надсилання відхилено. Цю операцію завершено.',
          cancelled: 'Надсилання скасовано. Цю операцію завершено.',
        };
        saved.retryable = data.state === 'pending' || (!data.state && result.status >= 500);
        say(labels[data.state] || errorMessage(result), data.state !== 'sent');
        notify({ documentId: doc.document_id, commandId: data.command_id, state: data.state || 'accepted' });
      } else {
        // A lost/503 response can follow commit. Keep the original UUID and CAS.
        saved.retryable = result.status === 0 || result.status >= 500;
        say(errorMessage(result), true);
      }
      render();
    }
    text.addEventListener('input', () => { editGeneration++; render(); });
    kind.addEventListener('change', () => { creationId = null; render(); });
    const controller = {
      hasUnsavedChanges: protectedWork,
      update(nextOptions) {
        const nextClient = id(nextOptions.clientId), nextContext = id(nextOptions.contextMessageId);
        if (!nextClient) return false;
        const changed = nextClient !== clientId || nextContext !== contextId;
        if (changed && (protectedWork() || mutation)) { say('Є незавершена робота в цьому діалозі. Текст і операцію залишено; спочатку завершіть або відкиньте зміни.', true); return false; }
        if (changed) { generation++; if (aborter) aborter.abort(); busy = false; mutation = false; }
        opts = Object.assign({}, nextOptions);
        if (nextClient !== clientId) { doc = null; creationId = null; pendingSave = null; submission = null; text.value = ''; list.replaceChildren(); next = null; more.hidden = true; editGeneration++; }
        clientId = nextClient; contextId = nextContext; render(); return true;
      },
      destroy(config) {
        if ((protectedWork() || mutation) && !(config && config.discard === true)) { say('Є незавершена робота. Текст і операцію залишено.', true); return false; }
        destroyed = true; generation++; if (aborter) aborter.abort(); mounts.delete(root); root.replaceChildren(); return true;
      },
    };
    mounts.set(root, controller); render(); return controller;
  }
  global.IgHumanDocuments = { mount };
  if (typeof module !== 'undefined' && module.exports) module.exports = { mount };
})(typeof window !== 'undefined' ? window : globalThis);
