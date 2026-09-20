/* TwoComms Finance — журнал платежів: модалки операцій, фільтри, масові дії.
   Працює поверх finance.js (бургер/сайдбар). */
(function () {
  'use strict';

  function csrf() {
    var m = document.cookie.match(/csrftoken=([^;]+)/);
    return m ? m[1] : '';
  }

  function api(url, method, body) {
    return fetch(url, {
      method: method || 'GET',
      headers: {
        'Content-Type': 'application/json',
        'X-CSRFToken': csrf(),
        'X-Requested-With': 'XMLHttpRequest',
      },
      body: body ? JSON.stringify(body) : undefined,
    }).then(function (r) { return r.json().then(function (d) { return { ok: r.ok, data: d }; }); });
  }

  function apiForm(url, formData) {
    return fetch(url, {
      method: 'POST',
      headers: { 'X-CSRFToken': csrf(), 'X-Requested-With': 'XMLHttpRequest' },
      body: formData,
    }).then(function (r) { return r.json().then(function (d) { return { ok: r.ok, data: d }; }); });
  }

  var DROPDOWNS = {};
  try {
    var ddEl = document.getElementById('fin-dropdowns') || document.getElementById('fin-dropdowns-shell');
    if (ddEl) DROPDOWNS = JSON.parse(ddEl.textContent);
  } catch (e) {}

  var modal = document.getElementById('fin-txn-modal');
  var form = document.getElementById('fin-txn-form');
  if (!modal || !form) return;

  var TYPE_LABELS = { income: 'Додати дохід', expense: 'Додати витрату', transfer: 'Додати переказ' };
  var DATE_LABELS = { income: 'Фактична дата', expense: 'Дата списання', transfer: 'Дата переказу коштів' };

  var els = {
    type: document.getElementById('fin-txn-type'),
    status: document.getElementById('fin-txn-status'),
    id: document.getElementById('fin-txn-id'),
    account: document.getElementById('fin-txn-account'),
    from: document.getElementById('fin-txn-from'),
    to: document.getElementById('fin-txn-to'),
    amount: document.getElementById('fin-txn-amount'),
    currency: document.getElementById('fin-txn-currency'),
    toAmount: document.getElementById('fin-txn-to-amount'),
    toAmountWrap: document.getElementById('fin-to-amount-wrap'),
    category: document.getElementById('fin-txn-category'),
    counterparty: document.getElementById('fin-txn-counterparty'),
    date: document.getElementById('fin-txn-date'),
    agreement: document.getElementById('fin-txn-agreement'),
    project: document.getElementById('fin-txn-project'),
    comment: document.getElementById('fin-txn-comment'),
    file: document.getElementById('fin-txn-file'),
    attachList: document.getElementById('fin-attach-list'),
    recurrence: document.getElementById('fin-txn-recurrence'),
    submit: document.getElementById('fin-txn-submit'),
    similar: document.getElementById('fin-txn-similar'),
    alert: document.getElementById('fin-txn-alert'),
    dateLabel: document.getElementById('fin-date-label'),
    editActions: document.getElementById('fin-edit-actions'),
    markActual: document.getElementById('fin-act-mark-actual'),
    tags: document.getElementById('fin-txn-tags'),
    statusToggle: document.getElementById('fin-status-toggle'),
    statusHint: document.getElementById('fin-status-hint'),
    incomingReview: document.getElementById('fin-incoming-review'),
    unclassifiedNotice: document.getElementById('fin-unclassified-notice'),
    unclassifiedOpen: document.getElementById('fin-unclassified-open'),
    unclassifiedForm: document.getElementById('fin-unclassified-form'),
    unclassifiedKind: document.getElementById('fin-unclassified-kind'),
    unclassifiedFundingSource: document.getElementById('fin-unclassified-funding-source'),
    unclassifiedFundingSourceWrap: document.getElementById('fin-unclassified-funding-source-wrap'),
    unclassifiedSave: document.getElementById('fin-unclassified-save'),
    terminalReview: document.getElementById('fin-terminal-review'),
    terminalContext: document.getElementById('fin-terminal-review-context'),
    terminalChoices: document.getElementById('fin-terminal-review-choices'),
    terminalCashForm: document.getElementById('fin-terminal-cash-form'),
    terminalCashAccount: document.getElementById('fin-terminal-cash-account'),
    terminalCashPropose: document.getElementById('fin-terminal-cash-propose'),
    terminalIncomeForm: document.getElementById('fin-terminal-income-form'),
    terminalIncomeKind: document.getElementById('fin-terminal-income-kind'),
    terminalIncomePropose: document.getElementById('fin-terminal-income-propose'),
    terminalConfirm: document.getElementById('fin-terminal-confirm'),
    terminalConfirmSummary: document.getElementById('fin-terminal-confirm-summary'),
    terminalReviewChange: document.getElementById('fin-terminal-review-change'),
    terminalReviewAccept: document.getElementById('fin-terminal-review-accept'),
    terminalReviewError: document.getElementById('fin-terminal-review-error'),
  };

  var terminalCandidates = null;
  var terminalReviewState = { txnId: null, review: null };
  var INCOME_KIND_LABELS = {
    sale: 'Продаж', investment: 'Інвестиція', grant_inflow: 'Грант',
    debt_repayment: 'Повернення боргу', expense_refund: 'Повернення витрат',
    personal_transfer: 'Переказ від людини', adjustment: 'Коригування',
  };

  function opt(value, label, selected) {
    var o = document.createElement('option');
    o.value = value; o.textContent = label;
    if (selected) o.selected = true;
    return o;
  }

  function fillSelect(sel, items, opts) {
    opts = opts || {};
    sel.innerHTML = '';
    if (opts.placeholder) sel.appendChild(opt('', opts.placeholder));
    items.forEach(function (it) { sel.appendChild(opt(it.id, it.name)); });
  }

  function syncFundingSourceField() {
    if (!els.unclassifiedFundingSource || !els.unclassifiedFundingSourceWrap) return;
    var isGrant = els.unclassifiedKind && els.unclassifiedKind.value === 'grant_inflow';
    els.unclassifiedFundingSourceWrap.hidden = !isGrant;
    if (isGrant) {
      fillSelect(els.unclassifiedFundingSource, DROPDOWNS.funding_sources || [], {
        placeholder: 'Оберіть програму',
      });
    } else {
      els.unclassifiedFundingSource.value = '';
    }
  }

  function populateAccounts() {
    fillSelect(els.account, DROPDOWNS.accounts || []);
    fillSelect(els.from, DROPDOWNS.accounts || []);
    fillSelect(els.to, DROPDOWNS.accounts || []);
    fillSelect(els.project, DROPDOWNS.projects || [], { placeholder: 'Без проекта' });
    fillSelect(els.counterparty, DROPDOWNS.counterparties || [], { placeholder: 'Вказати' });
    syncCurrency();
    renderTags([]);
  }

  function categoryItems(type) {
    return type === 'income' ? (DROPDOWNS.income_categories || []) : (DROPDOWNS.expense_categories || []);
  }

  function syncCategory(type) {
    fillSelect(els.category, categoryItems(type), { placeholder: 'Вказати' });
  }

  function accountById(id) {
    return (DROPDOWNS.accounts || []).find(function (a) { return String(a.id) === String(id); });
  }

  // Підставляє валюту обраного рахунку (користувач може змінити вручну).
  function syncCurrency() {
    var type = els.type.value;
    var accId = type === 'transfer' ? els.from.value : els.account.value;
    var acc = accountById(accId);
    if (acc && els.currency) els.currency.value = acc.currency;
    if (type === 'transfer') {
      var toAcc = accountById(els.to.value);
      var diff = acc && toAcc && acc.currency !== toAcc.currency;
      els.toAmountWrap.hidden = !diff;
    }
  }

  // --- Статус Факт / План ---
  function setStatus(status) {
    els.status.value = status;
    if (els.statusToggle) {
      els.statusToggle.querySelectorAll('.fin-status-opt').forEach(function (b) {
        b.classList.toggle('is-active', b.dataset.status === status);
      });
    }
  }

  function isFutureDate(value) {
    if (!value) return false;
    var d = new Date(value);
    return !isNaN(d.getTime()) && d.getTime() > Date.now();
  }

  // Авто-план: майбутня дата → План.
  function syncStatusFromDate() {
    var future = isFutureDate(els.date.value);
    if (future) {
      setStatus('planned');
      if (els.statusHint) els.statusHint.hidden = false;
    } else {
      if (els.statusHint) els.statusHint.hidden = true;
    }
  }

  // --- Теги (мультивибір) ---
  function renderTags(selectedIds) {
    if (!els.tags) return;
    var sel = new Set((selectedIds || []).map(String));
    els.tags.innerHTML = '';
    (DROPDOWNS.tags || []).forEach(function (t) {
      var chip = document.createElement('button');
      chip.type = 'button';
      chip.className = 'fin-tag-toggle' + (sel.has(String(t.id)) ? ' is-on' : '');
      chip.dataset.id = t.id;
      chip.textContent = t.name;
      chip.addEventListener('click', function () { chip.classList.toggle('is-on'); });
      els.tags.appendChild(chip);
    });
  }
  function selectedTagIds() {
    if (!els.tags) return [];
    return Array.prototype.slice.call(els.tags.querySelectorAll('.fin-tag-toggle.is-on'))
      .map(function (c) { return c.dataset.id; });
  }

  function renderAttachments(list) {
    if (!els.attachList) return;
    els.attachList.innerHTML = '';
    (list || []).forEach(function (a) {
      var row = document.createElement('a');
      row.className = 'fin-attach-item';
      row.href = a.url || '#'; row.target = '_blank';
      row.textContent = '📎 ' + (a.name || 'файл');
      els.attachList.appendChild(row);
    });
  }

  // Згортувані секції: показуємо лише потрібні для типу, але завжди згорнуті.
  function applyTypeVisibility(type) {
    modal.querySelectorAll('[data-show]').forEach(function (el) {
      var types = el.getAttribute('data-show').split(',');
      var show = types.indexOf(type) !== -1;
      el.hidden = !show;
      el.querySelectorAll('[data-field]').forEach(function (f) { f.disabled = !show; });
    });
    els.dateLabel.textContent = DATE_LABELS[type] || 'Дата';
    els.submit.textContent = els.id.value ? 'Зберегти зміни' : (TYPE_LABELS[type] || 'Додати');
    modal.querySelectorAll('.fin-txn-tab').forEach(function (tab) {
      tab.classList.toggle('active', tab.dataset.type === type);
    });
  }

  function collapseDisclosures() {
    ['agreement', 'recurring', 'extra'].forEach(function (key) {
      var btn = document.getElementById('fin-toggle-' + key);
      var wrap = document.getElementById('fin-' + key + '-wrap');
      if (wrap) wrap.hidden = true;
      if (btn) btn.setAttribute('aria-expanded', 'false');
    });
  }

  function setType(type) {
    els.type.value = type;
    syncCategory(type);
    applyTypeVisibility(type);
    syncCurrency();
  }

  function nowLocal() {
    var d = new Date();
    d.setMinutes(d.getMinutes() - d.getTimezoneOffset());
    return d.toISOString().slice(0, 16);
  }

  function showAlert(msg) {
    els.alert.textContent = msg; els.alert.hidden = !msg;
  }

  function hideTerminalReview() {
    if (!els.incomingReview) return;
    terminalReviewState = { txnId: null, review: null };
    els.incomingReview.hidden = true;
    els.unclassifiedNotice.hidden = true;
    els.unclassifiedForm.hidden = true;
    if (els.unclassifiedFundingSourceWrap) els.unclassifiedFundingSourceWrap.hidden = true;
    els.terminalReview.hidden = true;
    els.terminalChoices.hidden = false;
    els.terminalCashForm.hidden = true;
    els.terminalIncomeForm.hidden = true;
    els.terminalConfirm.hidden = true;
    els.terminalReviewError.hidden = true;
    els.terminalReviewError.textContent = '';
  }

  function setTerminalError(message) {
    els.terminalReviewError.textContent = message || '';
    els.terminalReviewError.hidden = !message;
  }

  function terminalCandidateFor(txnId) {
    var candidates = terminalCandidates && terminalCandidates.candidates || [];
    return candidates.find(function (candidate) {
      return String(candidate.transaction && candidate.transaction.id) === String(txnId);
    });
  }

  function setTerminalChoice(choice) {
    // No selection is the initial state (including a pending
    // ``terminal_cash_decision`` review): keep both decisions visible until
    // the user explicitly chooses one.
    els.terminalChoices.hidden = choice !== null;
    els.terminalCashForm.hidden = choice !== 'cash_transfer';
    els.terminalIncomeForm.hidden = choice !== 'income';
    els.terminalConfirm.hidden = true;
    setTerminalError('');
  }

  function defaultCashAccount(accounts) {
    return accounts.find(function (account) { return /готівка|cash/i.test(account.name || ''); }) || accounts[0];
  }

  function fillTerminalCashAccounts(accounts) {
    els.terminalCashAccount.innerHTML = '';
    accounts.forEach(function (account) {
      els.terminalCashAccount.appendChild(opt(account.id, account.name + ' · ' + account.currency));
    });
    var selected = defaultCashAccount(accounts);
    if (selected) els.terminalCashAccount.value = selected.id;
  }

  function fillTerminalIncomeKinds(kinds) {
    var selected = els.terminalIncomeKind.value;
    els.terminalIncomeKind.innerHTML = '';
    els.terminalIncomeKind.appendChild(opt('', 'Оберіть вид надходження'));
    (kinds || []).forEach(function (kind) {
      els.terminalIncomeKind.appendChild(opt(kind, INCOME_KIND_LABELS[kind] || kind));
    });
    els.terminalIncomeKind.value = selected;
  }

  function terminalReviewSummary(review) {
    var proposal = review.proposal || {};
    if (proposal.kind === 'terminal_cash_transfer') {
      var sourceId = proposal.source_cash_account_id;
      var accounts = terminalCandidates && terminalCandidates.cash_accounts || [];
      var source = accounts.find(function (account) { return String(account.id) === String(sourceId); });
      var candidate = terminalCandidateFor(terminalReviewState.txnId);
      var destination = candidate && candidate.transaction && candidate.transaction.account_name || 'картку';
      var amount = candidate && candidate.transaction && candidate.transaction.amount || '';
      return (source ? source.name : 'Власний рахунок') + ' \u2192 ' + destination +
        (amount ? ': ' + amount + ' грн.' : '.') + ' P&L та управлінський Cash Flow не зміняться.';
    }
    return 'Класифікація надходження: ' + (INCOME_KIND_LABELS[proposal.economic_kind] || proposal.economic_kind || 'дохід') + '.';
  }

  function showTerminalConfirmation(review) {
    terminalReviewState.review = review;
    els.terminalChoices.hidden = true;
    els.terminalCashForm.hidden = true;
    els.terminalIncomeForm.hidden = true;
    els.terminalConfirmSummary.textContent = terminalReviewSummary(review);
    els.terminalConfirm.hidden = false;
    setTerminalError('');
  }

  function renderTerminalCandidate(txn, candidate) {
    if (!candidate || !els.terminalReview) return;
    var providers = candidate.evidence && candidate.evidence.providers || [];
    terminalReviewState.txnId = txn.id;
    els.incomingReview.hidden = false;
    els.terminalReview.hidden = false;
    els.unclassifiedForm.hidden = true;
    els.unclassifiedOpen.hidden = true;
    els.terminalContext.textContent = providers.length
      ? 'Виявлено: ' + providers.join(', ') + '. Перевірте походження коштів.'
      : 'Перевірте походження коштів перед класифікацією.';
    fillTerminalCashAccounts(terminalCandidates.cash_accounts || []);
    fillTerminalIncomeKinds(terminalCandidates.income_economic_kinds || []);
    if (candidate.review && candidate.review.status === 'pending' &&
        (candidate.review.proposal || {}).kind !== 'terminal_cash_decision') showTerminalConfirmation(candidate.review);
    else {
      setTerminalChoice(null);
      if (!(terminalCandidates.cash_accounts || []).length) {
        setTerminalError('Немає активного рахунку «Готівка», з якого можна підготувати переказ.');
      }
    }
  }

  function loadTerminalReview(txn) {
    if (!txn || !txn.id || txn.type !== 'income' || txn.status !== 'actual') return;
    if (txn.economic_kind === 'unknown') {
      els.incomingReview.hidden = false;
      els.unclassifiedNotice.hidden = false;
    }
    function render() {
      if (modal.hidden || String(els.id.value) !== String(txn.id)) return;
      renderTerminalCandidate(txn, terminalCandidateFor(txn.id));
    }
    if (terminalCandidates) { render(); return; }
    api('/api/v2/terminal-cash/candidates/').then(function (res) {
      if (!res.ok || !res.data.ok) return;
      terminalCandidates = res.data;
      render();
    }).catch(function () {
      // The normal transaction editor stays available if the optional review API is unavailable.
    });
  }

  function createTerminalProposal(action) {
    var txnId = terminalReviewState.txnId;
    if (!txnId) return;
    var body = { action: action };
    if (action === 'cash_transfer') {
      if (!els.terminalCashAccount.value) { setTerminalError('Оберіть активний рахунок-джерело.'); return; }
      body.source_cash_account_id = els.terminalCashAccount.value;
    } else {
      if (!els.terminalIncomeKind.value) { setTerminalError('Оберіть вид надходження.'); return; }
      body.economic_kind = els.terminalIncomeKind.value;
    }
    setTerminalError('');
    var button = action === 'cash_transfer' ? els.terminalCashPropose : els.terminalIncomePropose;
    button.disabled = true;
    api('/api/v2/terminal-cash/candidates/' + txnId + '/review/', 'POST', body).then(function (res) {
      if (res.ok && res.data.ok && res.data.review) showTerminalConfirmation(res.data.review);
      else setTerminalError(res.data.error || 'Не вдалося підготувати пропозицію.');
    }).catch(function () { setTerminalError('Помилка мережі. Спробуйте ще раз.'); })
      .then(function () { button.disabled = false; });
  }

  function acceptTerminalProposal() {
    var review = terminalReviewState.review;
    if (!review || !review.id) return;
    setTerminalError('');
    els.terminalReviewAccept.disabled = true;
    api('/api/v2/reviews/' + review.id + '/action/', 'POST', { action: 'accept' }).then(function (res) {
      if (res.ok && res.data.ok) window.location.reload();
      else {
        els.terminalReviewAccept.disabled = false;
        setTerminalError(res.data.error || 'Не вдалося підтвердити зміни.');
      }
    }).catch(function () {
      els.terminalReviewAccept.disabled = false;
      setTerminalError('Помилка мережі. Спробуйте ще раз.');
    });
  }

  function saveUnclassifiedIncome() {
    var txnId = els.id.value;
    var kind = els.unclassifiedKind.value;
    if (!txnId || !kind) { showAlert('Оберіть вид надходження'); return; }
    if (kind === 'grant_inflow' && !els.unclassifiedFundingSource.value) {
      showAlert('Оберіть джерело гранту');
      return;
    }
    els.unclassifiedSave.disabled = true;
    api('/api/v2/transactions/' + txnId + '/classification/', 'POST', {
      economic_kind: kind,
      ownership_scope: 'unknown',
      funding_source_id: kind === 'grant_inflow' ? els.unclassifiedFundingSource.value : '',
    }).then(function (res) {
      if (res.ok && res.data.ok) window.location.reload();
      else {
        els.unclassifiedSave.disabled = false;
        showAlert(res.data.error || 'Не вдалося зберегти класифікацію');
      }
    }).catch(function () {
      els.unclassifiedSave.disabled = false;
      showAlert('Помилка мережі. Спробуйте ще раз.');
    });
  }

  function openModal(type, txn) {
    form.reset();
    els.id.value = '';
    showAlert('');
    hideTerminalReview();
    populateAccounts();
    collapseDisclosures();
    renderAttachments([]);
    els.editActions.hidden = true;
    els.similar.hidden = false;
    els.date.value = nowLocal();
    setStatus('actual');
    if (els.statusHint) els.statusHint.hidden = true;

    if (txn) {
      // Режим редагування.
      els.id.value = txn.id;
      setStatus(txn.status);
      setType(txn.type);
      els.amount.value = txn.amount;
      if (txn.currency && els.currency) els.currency.value = txn.currency;
      if (txn.type === 'transfer') {
        if (txn.account_id) els.from.value = txn.account_id;
        if (txn.to_account_id) els.to.value = txn.to_account_id;
        if (txn.to_amount) els.toAmount.value = txn.to_amount;
      } else {
        if (txn.account_id) els.account.value = txn.account_id;
        if (txn.category_id) els.category.value = txn.category_id;
        if (txn.counterparty_id) els.counterparty.value = txn.counterparty_id;
      }
      if (txn.date_actual) els.date.value = txn.date_actual;
      if (txn.date_agreement) els.agreement.value = txn.date_agreement;
      if (txn.project_id) els.project.value = txn.project_id;
      els.comment.value = txn.comment || '';
      var bizEl = document.getElementById('fin-txn-business');
      if (bizEl) bizEl.checked = !!txn.is_business;
      renderTags((txn.tags || []).map(function (t) { return t.id; }));
      renderAttachments(txn.attachments || []);
      els.editActions.hidden = false;
      els.similar.hidden = true;
      els.markActual.hidden = txn.status !== 'planned';
      syncCurrency();
      if (txn.currency && els.currency) els.currency.value = txn.currency;
      prefillRecurrence(txn);
    } else {
      setType(type || 'income');
    }
    modal.hidden = false;
    document.body.classList.add('fin-modal-open');
    if (txn) loadTerminalReview(txn);
  }

  function closeModal() {
    modal.hidden = true;
    document.body.classList.remove('fin-modal-open');
  }

  // Прелоад секції повторення при редагуванні: показує реальний графік правила,
  // щоб «Зробити повторюваним» не виглядав вимкненим і зберігав поточний стан.
  function prefillRecurrence(txn) {
    var sel = els.recurrence;
    if (!sel) return;
    var fire = function (el) { if (el && el.dispatchEvent) el.dispatchEvent(new Event('change')); };
    if (!txn.is_recurring || !txn.recurrence_frequency) { sel.value = ''; fire(sel); return; }
    var wrap = document.getElementById('fin-recurring-wrap');
    var toggle = document.getElementById('fin-toggle-recurring');
    if (wrap) wrap.hidden = false;
    if (toggle) toggle.setAttribute('aria-expanded', 'true');
    sel.value = txn.recurrence_frequency;
    var end = document.getElementById('fin-txn-rec-end');
    var interval = document.getElementById('fin-txn-rec-interval');
    var est = document.getElementById('fin-txn-rec-est');
    var title = document.getElementById('fin-txn-rec-title');
    var until = document.getElementById('fin-txn-rec-until');
    var count = document.getElementById('fin-txn-rec-count');
    if (interval) interval.value = txn.recurrence_interval || '1';
    if (est) est.value = txn.amount_is_estimated ? '1' : '0';
    if (title) title.value = txn.recurrence_title || '';
    if (end) end.value = txn.recurrence_end_mode || 'never';
    if (until) until.value = txn.recurrence_end_date || '';
    if (count) count.value = txn.recurrence_count || '';
    fire(sel);   // показати опції повторення
    fire(end);   // показати поле дати/кількості за режимом завершення
  }

  // Expose to shell quick-action buttons (finance.js calls FinanceModals.open).
  window.FinanceModals = { open: function (kind) { openModal(kind); } };

  // --- Збір payload у FormData (підтримка файлів) ---
  function collectFormData() {
    var type = els.type.value;
    var fd = new FormData();
    fd.append('type', type);
    fd.append('status', els.status.value);
    fd.append('amount', els.amount.value || '');
    fd.append('date_actual', els.date.value || '');
    fd.append('comment', els.comment.value || '');
    fd.append('project', els.project.value || '');
    fd.append('tags', selectedTagIds().join(','));
    if (els.agreement.value) fd.append('date_agreement', els.agreement.value);
    if (els.recurrence && els.recurrence.value) {
      fd.append('recurrence', els.recurrence.value);
      var recEnd = document.getElementById('fin-txn-rec-end');
      var recTitle = document.getElementById('fin-txn-rec-title');
      var recUntil = document.getElementById('fin-txn-rec-until');
      var recCount = document.getElementById('fin-txn-rec-count');
      var recInterval = document.getElementById('fin-txn-rec-interval');
      var recEst = document.getElementById('fin-txn-rec-est');
      if (recEnd) fd.append('recurrence_end_mode', recEnd.value || 'never');
      if (recTitle && recTitle.value) fd.append('recurrence_title', recTitle.value);
      if (recInterval && recInterval.value) fd.append('recurrence_interval', recInterval.value);
      if (recEst) fd.append('recurrence_amount_estimated', recEst.value || '0');
      if (recEnd && recEnd.value === 'until' && recUntil && recUntil.value) fd.append('recurrence_until', recUntil.value);
      if (recEnd && recEnd.value === 'count' && recCount && recCount.value) fd.append('recurrence_count', recCount.value);
    }
    if (type === 'transfer') {
      fd.append('from_account', els.from.value || '');
      fd.append('to_account', els.to.value || '');
      if (!els.toAmountWrap.hidden && els.toAmount.value) fd.append('to_amount', els.toAmount.value);
    } else {
      fd.append('account', els.account.value || '');
      fd.append('currency', els.currency ? els.currency.value : 'UAH');
      fd.append('category', els.category.value || '');
      fd.append('counterparty', els.counterparty.value || '');
    }
    if (els.file && els.file.files) {
      for (var i = 0; i < els.file.files.length; i++) fd.append('attachments', els.file.files[i]);
    }
    // Бізнес/особисте — лише для доходів/витрат (перекази нейтральні).
    if (type !== 'transfer') {
      var bizEl = document.getElementById('fin-txn-business');
      fd.append('is_business', bizEl && bizEl.checked ? '1' : '0');
    }
    return fd;
  }

  function save(keepOpen) {
    var id = els.id.value;
    var url = id ? '/api/transactions/' + id + '/update/' : '/api/transactions/create/';
    showAlert('');
    return apiForm(url, collectFormData()).then(function (res) {
      if (res.ok && res.data.ok) {
        if (keepOpen) {
          var type = els.type.value;
          form.reset(); els.id.value = ''; populateAccounts(); collapseDisclosures();
          renderAttachments([]); els.date.value = nowLocal(); setStatus('actual'); setType(type);
        } else {
          var txn = res.data.transaction;
          // Обернений потік: новий фактичний дохід/витрата може бути погашенням
          // запланованого зобовʼязання — пропонуємо привʼязати (один клік).
          if (!id && txn && txn.status === 'actual' &&
              (txn.type === 'expense' || txn.type === 'income')) {
            maybeReversePrompt(txn);
          } else {
            window.location.reload();
          }
        }
      } else {
        showAlert(res.data.error || 'Не вдалося зберегти операцію');
      }
    }).catch(function () { showAlert('Помилка мережі'); });
  }

  // --- Обернений потік: «Цей переказ у рахунок зобовʼязання?» ---
  function maybeReversePrompt(txn) {
    api('/api/payments/' + txn.id + '/reverse-candidates/').then(function (res) {
      var obligations = (res.ok && res.data.ok) ? (res.data.obligations || []) : [];
      if (!obligations.length) { window.location.reload(); return; }
      showReversePrompt(txn, obligations);
    }).catch(function () { window.location.reload(); });
  }

  function showReversePrompt(txn, obligations) {
    var old = document.getElementById('fin-revprompt');
    if (old) old.remove();
    var box = document.createElement('div');
    box.className = 'fin-revprompt';
    box.id = 'fin-revprompt';
    var rows = obligations.slice(0, 6).map(function (g) {
      var est = g.amount_is_estimated ? '≈ ' : '';
      var cp = g.counterparty ? (' · ' + g.counterparty) : '';
      return '<button type="button" class="fin-btn fin-btn--ghost fin-btn--sm" ' +
             'data-rev-attach="' + g.next_txn_id + '">' +
             (g.title || 'Зобовʼязання') + cp + ' · ' + est + g.per_amount + '</button>';
    }).join('');
    box.innerHTML =
      '<div class="fin-revprompt__title">Цей платіж — у рахунок зобовʼязання?</div>' +
      '<div class="fin-revprompt__sub">Оберіть зобовʼязання, щоб закрити період, або пропустіть.</div>' +
      '<div class="fin-revprompt__list">' + rows + '</div>' +
      '<div class="fin-revprompt__actions">' +
      '<button type="button" class="fin-btn fin-btn--ghost" data-rev-skip>Ні, окремий платіж</button>' +
      '</div>';
    document.body.appendChild(box);
    box.addEventListener('click', function (e) {
      var attach = e.target.closest('[data-rev-attach]');
      if (attach) {
        api('/api/payments/' + txn.id + '/attach-obligation/', 'POST',
            { planned_txn_id: attach.getAttribute('data-rev-attach'), full_period: '1',
              remember_card: '1' })
          .then(function () { window.location.reload(); });
        return;
      }
      if (e.target.closest('[data-rev-skip]')) { window.location.reload(); }
    });
  }

  // --- Події ---
  modal.querySelectorAll('.fin-txn-tab').forEach(function (tab) {
    tab.addEventListener('click', function () { if (!els.id.value) setType(tab.dataset.type); });
  });
  modal.querySelectorAll('[data-fin-close]').forEach(function (b) { b.addEventListener('click', closeModal); });
  modal.addEventListener('click', function (e) { if (e.target === modal) closeModal(); });
  document.addEventListener('keydown', function (e) { if (e.key === 'Escape' && !modal.hidden) closeModal(); });

  els.account.addEventListener('change', syncCurrency);
  els.from.addEventListener('change', syncCurrency);
  els.to.addEventListener('change', syncCurrency);
  els.date.addEventListener('change', syncStatusFromDate);
  els.date.addEventListener('input', syncStatusFromDate);

  if (els.statusToggle) {
    els.statusToggle.querySelectorAll('.fin-status-opt').forEach(function (b) {
      b.addEventListener('click', function () { setStatus(b.dataset.status); });
    });
  }

  // Згортувані секції (chevron + aria-expanded).
  ['agreement', 'recurring', 'extra'].forEach(function (key) {
    var btn = document.getElementById('fin-toggle-' + key);
    var wrap = document.getElementById('fin-' + key + '-wrap');
    if (btn && wrap) btn.addEventListener('click', function () {
      var open = wrap.hidden;
      wrap.hidden = !open;
      btn.setAttribute('aria-expanded', open ? 'true' : 'false');
    });
  });

  // Повторення: показуємо додаткові опції лише коли обрано періодичність.
  (function () {
    var recSel = els.recurrence;
    var recOpts = document.getElementById('fin-recurring-opts');
    var recEnd = document.getElementById('fin-txn-rec-end');
    var untilWrap = document.getElementById('fin-txn-rec-until-wrap');
    var countWrap = document.getElementById('fin-txn-rec-count-wrap');
    function syncRecOpts() {
      if (recOpts) recOpts.hidden = !(recSel && recSel.value);
    }
    function syncRecEnd() {
      if (!recEnd) return;
      if (untilWrap) untilWrap.hidden = recEnd.value !== 'until';
      if (countWrap) countWrap.hidden = recEnd.value !== 'count';
    }
    if (recSel) recSel.addEventListener('change', syncRecOpts);
    if (recEnd) recEnd.addEventListener('change', syncRecEnd);
  })();

  form.addEventListener('submit', function (e) { e.preventDefault(); save(false); });
  els.similar.addEventListener('click', function () { save(true); });

  if (els.terminalChoices) els.terminalChoices.addEventListener('click', function (e) {
    var choice = e.target.closest('[data-terminal-choice]');
    if (choice) setTerminalChoice(choice.dataset.terminalChoice);
  });
  if (els.terminalCashPropose) els.terminalCashPropose.addEventListener('click', function () { createTerminalProposal('cash_transfer'); });
  if (els.terminalIncomePropose) els.terminalIncomePropose.addEventListener('click', function () { createTerminalProposal('income'); });
  if (els.terminalReviewChange) els.terminalReviewChange.addEventListener('click', function () {
    terminalReviewState.review = null;
    els.terminalConfirm.hidden = true;
    els.terminalChoices.hidden = false;
    setTerminalError('');
  });
  if (els.terminalReviewAccept) els.terminalReviewAccept.addEventListener('click', acceptTerminalProposal);
  if (els.unclassifiedOpen) els.unclassifiedOpen.addEventListener('click', function () {
    els.unclassifiedForm.hidden = false;
    syncFundingSourceField();
    els.unclassifiedKind.focus();
  });
  if (els.unclassifiedKind) els.unclassifiedKind.addEventListener('change', syncFundingSourceField);
  if (els.unclassifiedSave) els.unclassifiedSave.addEventListener('click', saveUnclassifiedIncome);

  // Швидке створення сутностей із дропдаунів.
  modal.querySelectorAll('[data-create]').forEach(function (btn) {
    btn.addEventListener('click', function () {
      var kind = btn.dataset.create;
      var name = prompt('Назва нового запису:');
      if (!name) return;
      var body = { kind: kind, name: name };
      if (kind === 'category') body.type = els.type.value;
      api('/api/entity/create/', 'POST', body).then(function (res) {
        if (res.ok && res.data.ok) {
          var listKey = { project: 'projects', counterparty: 'counterparties',
                          category: els.type.value === 'income' ? 'income_categories' : 'expense_categories' }[kind];
          (DROPDOWNS[listKey] = DROPDOWNS[listKey] || []).push({ id: res.data.id, name: res.data.name });
          if (kind === 'project') { fillSelect(els.project, DROPDOWNS.projects, { placeholder: 'Без проекта' }); els.project.value = res.data.id; }
          else if (kind === 'counterparty') { fillSelect(els.counterparty, DROPDOWNS.counterparties, { placeholder: 'Вказати' }); els.counterparty.value = res.data.id; }
          else { syncCategory(els.type.value); els.category.value = res.data.id; }
        }
      });
    });
  });

  // --- Дії редагування ---
  function withId(fn) { var id = els.id.value; if (id) fn(id); }
  var delBtn = document.getElementById('fin-act-delete');
  if (delBtn) delBtn.addEventListener('click', function () {
    if (!confirm('Видалити операцію?')) return;
    withId(function (id) { api('/api/transactions/' + id + '/delete/', 'POST').then(function () { window.location.reload(); }); });
  });
  var dupBtn = document.getElementById('fin-act-duplicate');
  if (dupBtn) dupBtn.addEventListener('click', function () {
    withId(function (id) { api('/api/transactions/' + id + '/duplicate/', 'POST').then(function () { window.location.reload(); }); });
  });
  var convBtn = document.getElementById('fin-act-convert');
  if (convBtn) convBtn.addEventListener('click', function () {
    // Reuse the styled transfer fields rather than asking for an opaque account ID.
    var sourceId = els.account.value;
    setType('transfer');
    if (sourceId) els.from.value = sourceId;
    var alternatives = (DROPDOWNS.accounts || []).filter(function (account) {
      return String(account.id) !== String(sourceId);
    });
    if (alternatives.length) els.to.value = alternatives[0].id;
    syncCurrency();
    showAlert('Оберіть рахунок отримувача та збережіть зміни.');
    els.to.focus();
  });
  if (els.markActual) els.markActual.addEventListener('click', function () {
    withId(function (id) { api('/api/transactions/' + id + '/mark-actual/', 'POST').then(function () { window.location.reload(); }); });
  });

  // --- Клік по рядку → редагування (або toggle у bulk-режимі) ---
  // Прив'язуємось лише до рядків журналу операцій (мають data-txn-id), щоб
  // на сторінках звітів (де є .fin-row без txn-id) нічого не ламалось при
  // глобальному підключенні скрипта.
  document.querySelectorAll('.fin-row[data-txn-id]').forEach(function (row) {
    row.addEventListener('click', function (e) {
      if (e.target.closest('.fin-col-check')) return;
      // У bulk-режимі клік перемикає вибір, не відкриває модалку
      if (document.body.classList.contains('fin-bulk-mode')) {
        var check = row.querySelector('.fin-row-check');
        if (check) {
          check.checked = !check.checked;
          check.dispatchEvent(new Event('change', { bubbles: true }));
        }
        return;
      }
      var id = row.dataset.txnId;
      api('/api/transactions/' + id + '/').then(function (res) {
        if (res.data.ok) openModal(res.data.transaction.type, res.data.transaction);
      });
    });
  });

  // Notification links open the exact imported operation once an authenticated
  // finance page has loaded. They never execute a classification by GET.
  (function () {
    var params = new URLSearchParams(window.location.search);
    var txnId = params.get('terminal_review') || params.get('transaction');
    if (!txnId || !/^\d+$/.test(txnId)) return;
    window.requestAnimationFrame(function () {
      var row = document.querySelector('.fin-row[data-txn-id="' + txnId + '"]');
      if (row) row.click();
    });
  })();

  // --- Період: показ діапазону ---
  var periodSel = document.getElementById('fin-period');
  var rangeWrap = document.getElementById('fin-custom-range');
  if (periodSel && rangeWrap) {
    periodSel.addEventListener('change', function () { rangeWrap.hidden = periodSel.value !== 'custom'; });
  }

  // --- Розгортання планових ---
  var plannedToggle = document.getElementById('fin-planned-toggle');
  var plannedTable = document.getElementById('fin-planned-table') ||
                     document.getElementById('fin-planned-body');
  if (plannedToggle && plannedTable) {
    plannedToggle.addEventListener('click', function () {
      var open = plannedTable.hidden;
      plannedTable.hidden = !open;
      plannedToggle.setAttribute('aria-expanded', open ? 'true' : 'false');
    });
  }

  // --- Згортання панелі фільтрів в одну кнопку «Фільтри» ---
  (function () {
    var toggle = document.getElementById('fin-filters-toggle');
    var panel = document.getElementById('fin-filters');
    if (!toggle || !panel) return;
    function paint(open) {
      toggle.setAttribute('aria-expanded', open ? 'true' : 'false');
      toggle.classList.toggle('is-open', open);
    }
    function setOpen(open) {
      panel.hidden = !open;
      paint(open);
      try { localStorage.setItem('fin_filters_open', open ? '1' : '0'); } catch (e) {}
    }
    var saved = null;
    try { saved = localStorage.getItem('fin_filters_open'); } catch (e) {}
    // Якщо є активні фільтри — завжди показуємо панель (щоб вони були на видноті).
    if (!panel.hidden) { paint(true); }
    else if (saved === '1') { setOpen(true); }
    else { paint(false); }
    toggle.addEventListener('click', function () { setOpen(panel.hidden); });
  })();

  // --- Пагінація: перемикач кількості на сторінці + перехід по сторінках ---
  (function () {
    function goWith(mutate) {
      var params = new URLSearchParams(window.location.search);
      mutate(params);
      window.location.search = params.toString();
    }
    var perPage = document.getElementById('fin-perpage-select');
    if (perPage) perPage.addEventListener('change', function () {
      goWith(function (p) { p.set('per_page', perPage.value); p.delete('page'); });
    });
    document.querySelectorAll('.fin-pager__btn[data-page]').forEach(function (btn) {
      btn.addEventListener('click', function () {
        if (btn.disabled) return;
        goWith(function (p) { p.set('page', btn.dataset.page); });
      });
    });
  })();

  // --- Масовий вибір ---
  var checkAll = document.getElementById('fin-check-all');
  var bulkbar = document.getElementById('fin-bulkbar');
  var bulkCount = document.getElementById('fin-bulk-count');
  function rowChecks() { return Array.prototype.slice.call(document.querySelectorAll('.fin-row-check')); }
  function selectedIds() { return rowChecks().filter(function (c) { return c.checked; }).map(function (c) { return c.value; }); }
  function refreshBulk() {
    var ids = selectedIds();
    if (bulkbar) bulkbar.hidden = ids.length === 0;
    if (bulkCount) bulkCount.textContent = ids.length;
    // Мобільний bulk-режим: показуємо чекбокси доки є вибрані
    document.body.classList.toggle('fin-bulk-mode', ids.length > 0);
  }
  if (checkAll) checkAll.addEventListener('change', function () {
    rowChecks().forEach(function (c) { c.checked = checkAll.checked; });
    refreshBulk();
  });
  rowChecks().forEach(function (c) { c.addEventListener('change', refreshBulk); });

  // Кнопка «✕» — зняти весь вибір і сховати панель.
  var bulkClear = document.getElementById('fin-bulk-clear');
  if (bulkClear) bulkClear.addEventListener('click', function () {
    rowChecks().forEach(function (c) { c.checked = false; });
    if (checkAll) checkAll.checked = false;
    refreshBulk();
  });

  // Кнопка згортання панелі дій (щоб не займала багато місця на телефоні).
  var bulkToggle = document.getElementById('fin-bulk-toggle');
  if (bulkToggle && bulkbar) {
    // Відновлюємо збережений стан.
    try {
      if (localStorage.getItem('fin_bulk_collapsed') === '1') {
        bulkbar.classList.add('is-collapsed');
        bulkToggle.setAttribute('aria-expanded', 'false');
      }
    } catch (e) {}
    bulkToggle.addEventListener('click', function () {
      var collapsed = bulkbar.classList.toggle('is-collapsed');
      bulkToggle.setAttribute('aria-expanded', collapsed ? 'false' : 'true');
      try { localStorage.setItem('fin_bulk_collapsed', collapsed ? '1' : '0'); } catch (e) {}
    });
  }

  // --- Long-press на рядку активує bulk-режим (мобільні) ---
  (function () {
    var timer = null;
    document.querySelectorAll('.fin-row[data-txn-id]').forEach(function (row) {
      var check = row.querySelector('.fin-row-check');
      if (!check) return;
      row.addEventListener('touchstart', function () {
        timer = setTimeout(function () {
          check.checked = true;
          refreshBulk();
          if (navigator.vibrate) navigator.vibrate(15);
        }, 450);
      }, { passive: true });
      row.addEventListener('touchend', function () { clearTimeout(timer); });
      row.addEventListener('touchmove', function () { clearTimeout(timer); }, { passive: true });
    });
  })();

  // Bulk-дії
  var bulkModal = document.getElementById('fin-bulk-modal');
  var bulkValue = document.getElementById('fin-bulk-value');
  var bulkLabel = document.getElementById('fin-bulk-label');
  var pendingBulk = null;
  function runBulk(action, value) {
    api('/api/transactions/bulk/', 'POST', { action: action, ids: selectedIds().join(','), value: value })
      .then(function (res) { if (res.data.ok) window.location.reload(); else alert(res.data.error || 'Помилка'); });
  }
  if (bulkbar) bulkbar.querySelectorAll('[data-bulk]').forEach(function (btn) {
    btn.addEventListener('click', function () {
      var action = btn.dataset.bulk;
      if (action === 'delete') { if (confirm('Видалити обрані операції?')) runBulk('delete'); return; }
      if (action === 'mark_actual') { runBulk('mark_actual'); return; }
      if (action === 'set_business') { runBulk('set_business', btn.dataset.value); return; }
      pendingBulk = action;
      var map = { set_category: ['expense_categories', 'Категорія'], set_project: ['projects', 'Проект'],
                  set_counterparty: ['counterparties', 'Контрагент'], add_tag: ['tags', 'Тег'] };
      var conf = map[action];
      fillSelect(bulkValue, DROPDOWNS[conf[0]] || []);
      bulkLabel.textContent = conf[1];
      if (bulkModal) { bulkModal.hidden = false; document.body.classList.add('fin-modal-open'); }
    });
  });
  if (bulkModal) {
    bulkModal.querySelectorAll('[data-fin-close]').forEach(function (b) {
      b.addEventListener('click', function () { bulkModal.hidden = true; document.body.classList.remove('fin-modal-open'); });
    });
    var applyBtn = document.getElementById('fin-bulk-apply');
    if (applyBtn) applyBtn.addEventListener('click', function () {
      if (pendingBulk) runBulk(pendingBulk, bulkValue.value);
    });
  }

  // --- Розширений фільтр ---
  var filterBtn = document.getElementById('fin-advanced-filter-btn');
  var filterModal = document.getElementById('fin-filter-modal');
  function buildChecklist(container, items) {
    container.innerHTML = '';
    (items || []).forEach(function (it) {
      var lbl = document.createElement('label');
      lbl.innerHTML = '<input type="checkbox" value="' + it.id + '"> ' + it.name;
      container.appendChild(lbl);
    });
  }
  if (filterBtn && filterModal) {
    filterBtn.addEventListener('click', function () {
      filterModal.querySelectorAll('[data-multi]').forEach(function (c) {
        buildChecklist(c, DROPDOWNS[c.dataset.multi]);
      });
      filterModal.hidden = false; document.body.classList.add('fin-modal-open');
    });
    filterModal.querySelectorAll('[data-fin-close]').forEach(function (b) {
      b.addEventListener('click', function () { filterModal.hidden = true; document.body.classList.remove('fin-modal-open'); });
    });
    var filterForm = document.getElementById('fin-filter-form');
    if (filterForm) filterForm.addEventListener('submit', function () {
      filterModal.querySelectorAll('[data-multi]').forEach(function (c) {
        var ids = Array.prototype.slice.call(c.querySelectorAll('input:checked')).map(function (i) { return i.value; });
        document.getElementById('flt-' + c.dataset.multi).value = ids.join(',');
      });
      filterModal.querySelectorAll('[data-chips]').forEach(function (c) {
        var vals = Array.prototype.slice.call(c.querySelectorAll('input:checked')).map(function (i) { return i.value; });
        document.getElementById('flt-' + c.dataset.chips).value = vals.join(',');
      });
    });
    var resetBtn = document.getElementById('fin-filter-reset');
    if (resetBtn) resetBtn.addEventListener('click', function () {
      filterModal.querySelectorAll('input:checked').forEach(function (i) { i.checked = false; });
    });
  }
})();
