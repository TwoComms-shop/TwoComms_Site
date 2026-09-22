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
    grantToggle: document.getElementById('fin-txn-grant'),
    grantSource: document.getElementById('fin-txn-grant-source'),
    grantSourceWrap: document.getElementById('fin-txn-grant-source-wrap'),
    grantToggleExtra: document.getElementById('fin-txn-grant-extra'),
    grantSourceExtra: document.getElementById('fin-txn-grant-source-extra'),
    grantSourceExtraWrap: document.getElementById('fin-txn-grant-source-extra-wrap'),
    unclassifiedSave: document.getElementById('fin-unclassified-save'),
    quickClassify: document.getElementById('fin-quick-classify'),
    quickClassifyTitle: document.getElementById('fin-quick-classify-title'),
    quickClassifyHint: document.getElementById('fin-quick-classify-hint'),
    quickClassifyYes: document.getElementById('fin-quick-classify-yes'),
    quickClassifyNo: document.getElementById('fin-quick-classify-no'),
    transferMatch: document.getElementById('fin-transfer-match'),
    transferMatchTitle: document.getElementById('fin-transfer-match-title'),
    transferMatchHint: document.getElementById('fin-transfer-match-hint'),
    transferMatchSourceLabel: document.getElementById('fin-transfer-match-source-label'),
    transferMatchSource: document.getElementById('fin-transfer-match-source'),
    transferMatchAccount: document.getElementById('fin-transfer-match-account'),
    transferMatchAccountLabel: document.getElementById('fin-transfer-match-account-label'),
    transferMatchAnchor: document.getElementById('fin-transfer-match-anchor'),
    transferMatchControls: document.getElementById('fin-transfer-match-controls'),
    transferMatchSearch: document.getElementById('fin-transfer-match-search'),
    transferMatchPeriod: document.getElementById('fin-transfer-match-period'),
    transferMatchEmpty: document.getElementById('fin-transfer-match-empty'),
    transferMatchMore: document.getElementById('fin-transfer-match-more'),
    transferMatchNew: document.getElementById('fin-transfer-match-new'),
    transferMatchAmount: document.getElementById('fin-transfer-match-amount'),
    transferMatchAmountLabel: document.getElementById('fin-transfer-match-amount-label'),
    transferMatchDate: document.getElementById('fin-transfer-match-date'),
    transferMatchFeeWrap: document.getElementById('fin-transfer-match-fee-wrap'),
    transferMatchFeeConfirm: document.getElementById('fin-transfer-match-fee-confirm'),
    transferMatchError: document.getElementById('fin-transfer-match-error'),
    transferMatchBack: document.getElementById('fin-transfer-match-back'),
    transferMatchPreview: document.getElementById('fin-transfer-match-preview'),
    transferMatchExisting: document.getElementById('fin-transfer-match-existing'),
    transferMatchConfirm: document.getElementById('fin-transfer-match-confirm'),
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
    counterpartyPolicyReview: document.getElementById('fin-counterparty-policy-review'),
    counterpartyPolicyQuestion: document.getElementById('fin-counterparty-policy-question'),
    counterpartyPolicyDetail: document.getElementById('fin-counterparty-policy-detail'),
    counterpartyPolicyAccept: document.getElementById('fin-counterparty-policy-accept'),
    counterpartyPolicyReject: document.getElementById('fin-counterparty-policy-reject'),
  };

  var terminalCandidates = null;
  var terminalReviewState = { txnId: null, review: null };
  var counterpartyPolicyReview = null;
  var INCOME_KIND_LABELS = {
    sale: 'Продаж', investment: 'Інвестиція', grant_inflow: 'Грант',
    debt_repayment: 'Повернення боргу', expense_refund: 'Повернення витрат',
    personal_transfer: 'Переказ від людини', pension_income: 'Пенсійна виплата',
    transfer_fee: 'Комісія за переказ', adjustment: 'Коригування',
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

  function fillGrantSources() {
    [els.grantSource, els.grantSourceExtra].forEach(function (select) {
      if (select) fillSelect(select, DROPDOWNS.funding_sources || [], { placeholder: 'Оберіть програму' });
    });
  }

  function syncGrantClassification(origin) {
    if (!els.grantToggle || !els.grantSourceWrap) return;
    var isExtra = origin === 'extra';
    var enabled = isExtra && els.grantToggleExtra ? els.grantToggleExtra.checked : els.grantToggle.checked;
    var selected = isExtra && els.grantSourceExtra ? els.grantSourceExtra.value : els.grantSource.value;
    if (!selected) selected = isExtra ? els.grantSource.value : (els.grantSourceExtra && els.grantSourceExtra.value);
    els.grantToggle.checked = enabled;
    if (els.grantToggleExtra) els.grantToggleExtra.checked = enabled;
    els.grantSourceWrap.hidden = !enabled;
    if (els.grantSourceExtraWrap) els.grantSourceExtraWrap.hidden = !enabled;
    if (enabled) {
      fillGrantSources();
      if (selected) {
        els.grantSource.value = selected;
        if (els.grantSourceExtra) els.grantSourceExtra.value = selected;
      }
    } else {
      els.grantSource.value = '';
      if (els.grantSourceExtra) els.grantSourceExtra.value = '';
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
    if (els.quickClassify) els.quickClassify.hidden = true;
    if (els.transferMatch) els.transferMatch.hidden = true;
    if (els.transferMatchExisting) els.transferMatchExisting.hidden = true;
    if (els.transferMatchSource) els.transferMatchSource.hidden = false;
    if (els.transferMatchSourceLabel) els.transferMatchSourceLabel.hidden = false;
    if (els.transferMatchAccount) els.transferMatchAccount.hidden = false;
    if (els.unclassifiedFundingSourceWrap) els.unclassifiedFundingSourceWrap.hidden = true;
    els.terminalReview.hidden = true;
    els.terminalChoices.hidden = false;
    els.terminalCashForm.hidden = true;
    els.terminalIncomeForm.hidden = true;
    els.terminalConfirm.hidden = true;
    els.terminalReviewError.hidden = true;
    els.terminalReviewError.textContent = '';
    counterpartyPolicyReview = null;
    if (els.counterpartyPolicyReview) els.counterpartyPolicyReview.hidden = true;
  }

  function renderCounterpartyPolicyReview(review) {
    if (!els.counterpartyPolicyReview) return;
    counterpartyPolicyReview = review || null;
    els.counterpartyPolicyReview.hidden = !review;
    if (!review) return;
    els.counterpartyPolicyQuestion.textContent = review.prompt || 'Підтвердьте запропоновану категорію';
    els.counterpartyPolicyDetail.textContent = [review.counterparty, review.category].filter(Boolean).join(' · ');
  }

  function loadCounterpartyPolicyReview(txn) {
    if (!txn || !txn.id || txn.status !== 'actual') return;
    api('/api/v2/transactions/' + txn.id + '/counterparty-policy-review/').then(function (res) {
      if (modal.hidden || String(els.id.value) !== String(txn.id)) return;
      renderCounterpartyPolicyReview(res.ok && res.data.ok ? res.data.review : null);
    }).catch(function () { renderCounterpartyPolicyReview(null); });
  }

  function resolveCounterpartyPolicyReview(action) {
    if (!counterpartyPolicyReview || !counterpartyPolicyReview.id) return;
    var review = counterpartyPolicyReview;
    var button = action === 'accept' ? els.counterpartyPolicyAccept : els.counterpartyPolicyReject;
    button.disabled = true;
    api('/api/v2/reviews/' + review.id + '/action/', 'POST', { action: action }).then(function (res) {
      if (!res.ok || !res.data.ok) { showAlert(res.data.error || 'Не вдалося оновити класифікацію'); return; }
      renderCounterpartyPolicyReview(null);
      if (action === 'accept') { window.location.reload(); return; }
      if (activeTxn && activeTxn.type === 'income') {
        els.incomingReview.hidden = false;
        els.unclassifiedForm.hidden = false;
        syncFundingSourceField();
        els.unclassifiedKind.focus();
      } else if (els.category) {
        els.category.focus();
      }
    }).catch(function () { showAlert('Помилка мережі. Спробуйте ще раз.'); })
      .then(function () { button.disabled = false; });
  }

  function renderQuickClassification(txn) {
    if (!els.quickClassify) return;
    var isPension = /пенсі|пенси/i.test(txn.account_label || txn.account_name || '');
    var isFop = !!txn.account_is_business;
    var enabled = txn.type === 'income' && txn.status === 'actual' && txn.economic_kind === 'unknown' && (isPension || isFop);
    els.quickClassify.hidden = !enabled;
    if (!enabled) return;
    els.quickClassifyYes.dataset.quickAction = isPension ? 'pension' : 'sale';
    els.quickClassifyTitle.textContent = isPension ? 'Це пенсійна виплата?' : 'Це оплата за продаж або послугу?';
    els.quickClassifyHint.textContent = isPension
      ? 'Підтвердьте, щоб не змішувати пенсію з доходом бізнесу.'
      : 'Для ФОП-рахунку це одразу буде класифіковано як продаж.';
    els.quickClassifyYes.textContent = isPension ? 'Так, це пенсія' : 'Так, це продаж';
    els.quickClassifyNo.textContent = isPension ? 'Ні, це інше надходження' : 'Ні, це інше надходження';
  }

  function saveQuickClassification() {
    var txnId = els.id.value;
    var action = els.quickClassifyYes && els.quickClassifyYes.dataset.quickAction;
    if (!txnId || !action) return;
    els.quickClassifyYes.disabled = true;
    api('/api/v2/transactions/' + txnId + '/classification/', 'POST', {
      quick_action: action,
      economic_kind: action === 'pension' ? 'pension_income' : 'sale',
      ownership_scope: action === 'pension' ? 'personal' : 'business',
      note: action === 'pension' ? 'Швидке підтвердження пенсійної виплати' : 'Швидке підтвердження продажу на ФОП-рахунку',
    }).then(function (res) {
      if (res.ok && res.data.ok) window.location.reload();
      else {
        els.quickClassifyYes.disabled = false;
        showAlert(res.data.error || 'Не вдалося зберегти класифікацію');
      }
    }).catch(function () {
      els.quickClassifyYes.disabled = false;
      showAlert('Помилка мережі. Спробуйте ще раз.');
    });
  }

  var activeTxn = null;
  var transferRequest = 0;
  var transferMatchState = { candidates: [], mode: 'existing', busy: false };
  function transferError(message) {
    els.transferMatchError.textContent = message || '';
    els.transferMatchError.hidden = !message;
  }
  function transferMoney(cents) {
    return (cents / 100).toLocaleString('uk-UA', { minimumFractionDigits: 2, maximumFractionDigits: 2 }) +
      ' ' + (activeTxn.currency || 'UAH');
  }
  function transferCents(value) {
    var n = Number(value);
    return Number.isFinite(n) ? Math.round(n * 100) : 0;
  }
  function transferAccountName(id) {
    var account = (DROPDOWNS.accounts || []).find(function (a) { return String(a.id) === String(id); });
    return account ? account.name : '';
  }
  function transferElement(tag, text, className) {
    var node = document.createElement(tag);
    node.textContent = text;
    if (className) node.className = className;
    return node;
  }
  function fillTransferMatchAccounts(txn) {
    var selected = els.transferMatchAccount.value;
    els.transferMatchAccount.innerHTML = '';
    els.transferMatchAccount.appendChild(opt('', transferMatchState.mode === 'new' ? 'Оберіть рахунок' : 'Усі рахунки'));
    (DROPDOWNS.accounts || []).forEach(function (account) {
      if (String(account.id) !== String(txn.account_id) && (!account.currency || account.currency === txn.currency)) {
        els.transferMatchAccount.appendChild(opt(account.id, account.name, String(account.id) === selected));
      }
    });
  }
  function selectedTransfer() {
    if (!activeTxn) return null;
    var item;
    if (transferMatchState.mode === 'new') {
      if (!els.transferMatchAccount.value || !els.transferMatchAmount.value || !els.transferMatchDate.value) return null;
      item = { account_name: transferAccountName(els.transferMatchAccount.value), amount: els.transferMatchAmount.value };
    } else {
      item = transferMatchState.candidates.find(function (c) { return String(c.id) === els.transferMatchSource.value; });
    }
    if (!item) return null;
    var outgoing = activeTxn.type === 'expense';
    var current = transferCents(activeTxn.amount);
    var other = transferCents(item.amount);
    var debit = outgoing ? current : other;
    var credit = outgoing ? other : current;
    return {
      item: item, debit: debit, credit: credit, fee: debit - credit,
      from: outgoing ? activeTxn.account_label : item.account_name,
      to: outgoing ? item.account_name : activeTxn.account_label,
    };
  }
  function renderTransferMatchPreview() {
    var pair = selectedTransfer();
    els.transferMatchPreview.replaceChildren();
    els.transferMatchPreview.hidden = !pair;
    els.transferMatchFeeWrap.hidden = !pair || pair.fee <= 0;
    els.transferMatchConfirm.disabled = transferMatchState.busy || !pair || pair.credit <= 0 || pair.fee < 0 ||
      (pair.fee > 0 && !els.transferMatchFeeConfirm.checked);
    if (!pair) return;
    var route = transferElement('div', '', 'fin-transfer-match__route');
    route.append(transferElement('strong', pair.from), transferElement('span', '→'), transferElement('strong', pair.to));
    els.transferMatchPreview.appendChild(route);
    var amounts = transferElement('div', '', 'fin-transfer-match__amounts');
    [['Списано', pair.debit], ['Зараховано', pair.credit]].forEach(function (row) {
      var cell = transferElement('div', '');
      cell.append(transferElement('span', row[0]), transferElement('strong', transferMoney(row[1])));
      amounts.appendChild(cell);
    });
    els.transferMatchPreview.appendChild(amounts);
    els.transferMatchPreview.appendChild(transferElement('p',
      pair.fee < 0 ? 'Зараховано більше, ніж списано. Перевірте суми: це не банківська комісія.' :
        pair.fee ? 'Комісія банку: −' + transferMoney(pair.fee) : 'Без комісії — суми збігаються',
      pair.fee ? 'fin-transfer-match__fee' : 'fin-transfer-match__note'));
    els.transferMatchPreview.appendChild(transferElement('p',
      transferMatchState.mode === 'new'
        ? 'Додамо відсутню операцію на обраному рахунку та об’єднаємо записи. Його залишок зміниться на вказану суму.'
        : 'У журналі буде один синій переказ. Залишки рахунків не зміняться: обидві операції вже враховані.',
      'fin-transfer-match__note'));
    if (pair.fee >= 0) els.transferMatchPreview.appendChild(transferElement('p',
      'Основна сума не буде доходом чи витратою. Комісія врахується окремою витратою один раз.',
      'fin-transfer-match__note'));
  }
  function renderTransferMatchCandidates() {
    els.transferMatchSource.innerHTML = '<option value="">Оберіть операцію</option>';
    transferMatchState.candidates.forEach(function (item) {
      var date = new Date(item.date).toLocaleDateString('uk-UA');
      els.transferMatchSource.appendChild(opt(item.id, transferMoney(transferCents(item.amount)) + ' · ' +
        date + ' · ' + item.account_name + (item.comment ? ' · ' + item.comment.slice(0, 65) : '')));
    });
    els.transferMatchEmpty.hidden = transferMatchState.candidates.length > 0;
    els.transferMatchEmpty.textContent = 'Відповідних операцій не знайдено. Розширте період, змініть рахунок або додайте відсутню операцію.';
    els.transferMatchFeeConfirm.checked = false;
    renderTransferMatchPreview();
  }
  function fetchTransferCandidates(append) {
    var requestId = ++transferRequest;
    var txnId = activeTxn.id;
    var params = new URLSearchParams({ transaction_id: txnId, period_days: els.transferMatchPeriod.value });
    if (els.transferMatchAccount.value) params.set('account_id', els.transferMatchAccount.value);
    if (append) params.set('offset', transferMatchState.candidates.length);
    transferMatchState.busy = true;
    els.transferMatchSource.disabled = true;
    els.transferMatchMore.disabled = true;
    els.transferMatchConfirm.disabled = true;
    transferError('');
    return api('/api/v2/transfers/match/?' + params.toString()).then(function (res) {
      if (requestId !== transferRequest || modal.hidden || !activeTxn || String(activeTxn.id) !== String(txnId)) return;
      if (!res.ok || !res.data.ok) throw new Error(res.data.error || 'Не вдалося завантажити операції.');
      transferMatchState.busy = false;
      els.transferMatchSource.disabled = false;
      els.transferMatchMore.disabled = false;
      if (res.data.existing) {
        var match = res.data.existing;
        els.transferMatchControls.hidden = true;
        els.transferMatchConfirm.hidden = true;
        els.transferMatchTitle.textContent = 'Переказ уже об’єднано';
        els.transferMatchHint.textContent = 'У журналі це одна операція. Банківські записи збережені для звірки.';
        els.transferMatchExisting.hidden = false;
        els.transferMatchExisting.textContent = 'Зустрічний рахунок: ' + match.partner_account +
          ' · Переказ: ' + transferMoney(transferCents(match.principal_amount)) +
          ' · Комісія: ' + transferMoney(transferCents(match.fee_amount));
        return;
      }
      transferMatchState.candidates = (append ? transferMatchState.candidates : []).concat(res.data.candidates || []);
      els.transferMatchMore.hidden = !res.data.has_more;
      renderTransferMatchCandidates();
    }).catch(function (error) {
      if (requestId !== transferRequest) return;
      transferMatchState.busy = false;
      els.transferMatchMore.disabled = false;
      transferError(error.message || 'Помилка мережі. Спробуйте ще раз.');
    });
  }
  function setTransferMode(mode) {
    ++transferRequest;
    transferMatchState.mode = mode;
    transferMatchState.busy = false;
    transferMatchState.candidates = [];
    els.transferMatchFeeConfirm.checked = false;
    els.transferMatchSearch.hidden = mode !== 'existing';
    els.transferMatchNew.hidden = mode !== 'new';
    els.transferMatchConfirm.textContent = mode === 'new' ? 'Додати й об’єднати' : 'Об’єднати в переказ';
    modal.querySelectorAll('[data-transfer-mode]').forEach(function (button) {
      var selected = button.dataset.transferMode === mode;
      button.classList.toggle('is-active', selected);
      button.setAttribute('aria-pressed', String(selected));
    });
    fillTransferMatchAccounts(activeTxn);
    transferError('');
    if (mode === 'existing') fetchTransferCandidates();
    else renderTransferMatchPreview();
  }
  function loadTransferMatch(txn) {
    if (!txn || !txn.id || (txn.type !== 'income' && txn.type !== 'expense')) return;
    if (txn.status !== 'actual') {
      showAlert('Об’єднати можна фактичні операції. Спочатку підтвердьте, що кошти надійшли або списані.');
      return;
    }
    form.classList.add('is-transfer-matching');
    els.transferMatch.hidden = false;
    els.transferMatchControls.hidden = false;
    els.transferMatchConfirm.hidden = false;
    els.transferMatchExisting.hidden = true;
    els.transferMatchAccount.value = '';
    els.transferMatchPeriod.value = '7';
    els.transferMatchAmount.value = txn.amount;
    els.transferMatchDate.value = txn.date_actual;
    els.transferMatchTitle.textContent = 'Об’єднати в один переказ';
    els.transferMatchHint.textContent = txn.type === 'expense'
      ? 'Це списання. Оберіть свій рахунок, на який надійшли кошти.'
      : 'Це зарахування. Оберіть свій рахунок, з якого надійшли кошти.';
    els.transferMatchAccountLabel.textContent = txn.type === 'expense' ? 'Куди переказали' : 'Звідки переказали';
    els.transferMatchSourceLabel.textContent = txn.type === 'expense' ? 'Наявне зарахування' : 'Наявне списання';
    els.transferMatchAmountLabel.textContent = txn.type === 'expense' ? 'Зараховано на інший рахунок' : 'Списано з іншого рахунку';
    els.transferMatchAnchor.replaceChildren(
      transferElement('span', txn.type === 'expense' ? 'Відкрите списання' : 'Відкрите зарахування'),
      transferElement('strong', (txn.type === 'expense' ? '−' : '+') + transferMoney(transferCents(txn.amount))),
      transferElement('span', (txn.account_label || txn.account_name) + ' · ' + new Date(txn.date_actual).toLocaleDateString('uk-UA'))
    );
    modal.querySelectorAll('.fin-txn-tab').forEach(function (tab) { tab.classList.toggle('active', tab.dataset.type === 'transfer'); });
    setTransferMode('existing');
    els.transferMatch.scrollIntoView({ block: 'nearest' });
    els.transferMatchAccount.focus({ preventScroll: true });
  }
  function leaveTransferMatch() {
    ++transferRequest;
    form.classList.remove('is-transfer-matching');
    els.transferMatch.hidden = true;
    transferMatchState.busy = false;
    applyTypeVisibility(els.type.value);
  }
  function confirmTransferMatch() {
    if (transferMatchState.busy) return;
    var pair = selectedTransfer();
    if (!pair || pair.credit <= 0 || pair.fee < 0 || (pair.fee > 0 && !els.transferMatchFeeConfirm.checked)) return;
    var body = { confirm: true, expected_fee_amount: (pair.fee / 100).toFixed(2), fee_amount: (pair.fee / 100).toFixed(2) };
    if (transferMatchState.mode === 'new') {
      body.transaction_id = activeTxn.id;
      body.counterpart_account_id = els.transferMatchAccount.value;
      body.counterpart_amount = els.transferMatchAmount.value;
      body.counterpart_date = els.transferMatchDate.value;
      body.create_counterpart = true;
    } else {
      body.source_transaction_id = activeTxn.type === 'expense' ? activeTxn.id : pair.item.id;
      body.destination_transaction_id = activeTxn.type === 'expense' ? pair.item.id : activeTxn.id;
    }
    transferMatchState.busy = true;
    renderTransferMatchPreview();
    transferError('');
    api('/api/v2/transfers/match/', 'POST', body).then(function (res) {
      if (res.ok && res.data.ok) window.location.reload();
      else {
        transferMatchState.busy = false;
        transferError(res.data.error || 'Не вдалося об’єднати переказ.');
        renderTransferMatchPreview();
      }
    }).catch(function () {
      transferMatchState.busy = false;
      transferError('Не вдалося отримати відповідь. Відкрийте операцію ще раз, щоб перевірити результат.');
      // Do not replay a potentially completed financial mutation blindly.
    });
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
    if (els.quickClassify) els.quickClassify.hidden = true;
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
      renderQuickClassification(txn);
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
    activeTxn = txn || null;
    ++transferRequest;
    form.classList.remove('is-transfer-matching');
    transferMatchState = { candidates: [], mode: 'existing', busy: false };
    form.reset();
    els.id.value = '';
    showAlert('');
    hideTerminalReview();
    populateAccounts();
    if (els.grantToggle) els.grantToggle.checked = false;
    if (els.grantSource) els.grantSource.value = '';
    if (els.grantSourceExtra) els.grantSourceExtra.value = '';
    syncGrantClassification();
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
      if (els.grantToggle) els.grantToggle.checked = !!txn.funding_source_id;
      if (els.grantToggleExtra) els.grantToggleExtra.checked = !!txn.funding_source_id;
      if (els.grantToggle && els.grantToggle.checked) {
        fillGrantSources();
        if (els.grantSource) els.grantSource.value = txn.funding_source_id || '';
        if (els.grantSourceExtra) els.grantSourceExtra.value = txn.funding_source_id || '';
      }
      syncGrantClassification();
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
    if (txn) {
      loadCounterpartyPolicyReview(txn);
      loadTerminalReview(txn);
    }
    if (txn && txn.economic_kind === 'internal_transfer' && txn.type !== 'transfer') loadTransferMatch(txn);
  }

  function closeModal() {
    ++transferRequest;
    modal.hidden = true;
    document.body.classList.remove('fin-modal-open');
  }

  function acknowledgeNotification(notificationId) {
    if (!notificationId || !/^\d+$/.test(String(notificationId))) return;
    fetch('/api/notifications/' + notificationId + '/ack/', {
      method: 'POST', headers: { 'X-CSRFToken': csrf(), 'X-Requested-With': 'XMLHttpRequest' },
    }).catch(function () {});
  }

  function openNotificationTransaction(transactionId, notificationId, action) {
    if (!transactionId || !/^\d+$/.test(String(transactionId))) return;
    api('/api/transactions/' + transactionId + '/').then(function (res) {
      if (!res.ok || !res.data || !res.data.ok) return;
      openModal(res.data.transaction.type, res.data.transaction);
      acknowledgeNotification(notificationId);
      var started = Date.now();
      (function applyAction() {
        if (action === 'confirm' && els.quickClassify && els.quickClassifyYes && !els.quickClassify.hidden) {
          els.quickClassifyYes.click();
          return;
        }
        if (action === 'choose' && els.unclassifiedOpen && els.unclassifiedForm && els.unclassifiedForm.hidden) {
          els.unclassifiedOpen.click();
          return;
        }
        if (Date.now() - started < 4000) window.setTimeout(applyAction, 100);
      })();
    });
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

  function persistGrantClassification(txnId) {
    if (!txnId || (els.type.value !== 'income' && els.type.value !== 'expense') || !els.grantToggle || !els.grantToggle.checked) {
      return Promise.resolve({ ok: true });
    }
    if (!els.grantSource || !els.grantSource.value) {
      return Promise.resolve({ ok: false, data: { error: 'Оберіть грантову програму' } });
    }
    var isIncome = els.type.value === 'income';
    return api('/api/v2/transactions/' + txnId + '/classification/', 'POST', {
      economic_kind: isIncome ? 'grant_inflow' : 'operating_expense',
      ownership_scope: 'business',
      funding_source_id: els.grantSource.value,
      note: isIncome ? 'Позначено як цільове грантове надходження у модалці операції'
        : 'Позначено як цільова грантова витрата у модалці операції',
    }).then(function (result) {
      if (!result.ok || !result.data.ok || isIncome) return result;
      return api('/api/v2/funding/' + els.grantSource.value + '/allocate/', 'POST', {
        transaction_id: txnId, amount: els.amount.value, allocation_type: 'spent',
        note: 'Позначено як грантова витрата у модалці операції', replace: true,
      });
    });
  }

  function save(keepOpen) {
    if (form.classList.contains('is-transfer-matching')) return;
    var id = els.id.value;
    if ((els.type.value === 'income' || els.type.value === 'expense') && els.grantToggle && els.grantToggle.checked &&
        (!els.grantSource || !els.grantSource.value)) {
      showAlert('Оберіть грантову програму');
      syncGrantClassification();
      return;
    }
    var url = id ? '/api/transactions/' + id + '/update/' : '/api/transactions/create/';
    showAlert('');
    return apiForm(url, collectFormData()).then(function (res) {
      if (res.ok && res.data.ok) {
        var savedTxn = res.data.transaction;
        return persistGrantClassification(savedTxn && savedTxn.id).then(function (classification) {
          if (!classification.ok || (classification.data && classification.data.ok === false)) {
            showAlert((classification.data && classification.data.error) || 'Не вдалося зберегти ознаку гранту');
            return;
          }
        if (keepOpen) {
          var type = els.type.value;
          form.reset(); els.id.value = ''; populateAccounts(); collapseDisclosures();
          renderAttachments([]); els.date.value = nowLocal(); setStatus('actual'); setType(type);
        } else {
          var txn = savedTxn;
          // Обернений потік: новий фактичний дохід/витрата може бути погашенням
          // запланованого зобовʼязання — пропонуємо привʼязати (один клік).
          if (!id && txn && txn.status === 'actual' &&
              (txn.type === 'expense' || txn.type === 'income')) {
            maybeReversePrompt(txn);
          } else {
            window.location.reload();
          }
        }
        });
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
    tab.addEventListener('click', function () {
      if (!els.id.value) setType(tab.dataset.type);
      else if (tab.dataset.type === 'transfer' && activeTxn && activeTxn.type !== 'transfer') loadTransferMatch(activeTxn);
      else if (activeTxn && tab.dataset.type === activeTxn.type) leaveTransferMatch();
    });
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
    if (els.quickClassify) els.quickClassify.hidden = true;
    els.unclassifiedForm.hidden = false;
    syncFundingSourceField();
    els.unclassifiedKind.focus();
  });
  if (els.quickClassifyYes) els.quickClassifyYes.addEventListener('click', saveQuickClassification);
  if (els.quickClassifyNo) els.quickClassifyNo.addEventListener('click', function () {
    if (els.quickClassify) els.quickClassify.hidden = true;
    els.unclassifiedForm.hidden = false;
    syncFundingSourceField();
    els.unclassifiedKind.focus();
  });
  if (els.transferMatchSource) els.transferMatchSource.addEventListener('change', function () {
    els.transferMatchFeeConfirm.checked = false;
    renderTransferMatchPreview();
  });
  if (els.transferMatchAccount) els.transferMatchAccount.addEventListener('change', function () {
    els.transferMatchFeeConfirm.checked = false;
    if (transferMatchState.mode === 'existing') fetchTransferCandidates();
    else renderTransferMatchPreview();
  });
  if (els.transferMatchPeriod) els.transferMatchPeriod.addEventListener('change', function () { fetchTransferCandidates(); });
  if (els.transferMatchMore) els.transferMatchMore.addEventListener('click', function () { fetchTransferCandidates(true); });
  if (els.transferMatchBack) els.transferMatchBack.addEventListener('click', leaveTransferMatch);
  if (els.transferMatchFeeConfirm) els.transferMatchFeeConfirm.addEventListener('change', renderTransferMatchPreview);
  [els.transferMatchAmount, els.transferMatchDate].forEach(function (input) {
    if (input) input.addEventListener('input', function () {
      els.transferMatchFeeConfirm.checked = false;
      renderTransferMatchPreview();
    });
  });
  modal.querySelectorAll('[data-transfer-mode]').forEach(function (button) {
    button.addEventListener('click', function () { setTransferMode(button.dataset.transferMode); });
  });
  if (els.transferMatchConfirm) els.transferMatchConfirm.addEventListener('click', confirmTransferMatch);
  if (els.unclassifiedKind) els.unclassifiedKind.addEventListener('change', syncFundingSourceField);
  if (els.grantToggle) els.grantToggle.addEventListener('change', function () { syncGrantClassification('main'); });
  if (els.grantToggleExtra) els.grantToggleExtra.addEventListener('change', function () { syncGrantClassification('extra'); });
  if (els.grantSource) els.grantSource.addEventListener('change', function () { syncGrantClassification('main'); });
  if (els.grantSourceExtra) els.grantSourceExtra.addEventListener('change', function () { syncGrantClassification('extra'); });
  if (els.unclassifiedSave) els.unclassifiedSave.addEventListener('click', saveUnclassifiedIncome);
  if (els.counterpartyPolicyAccept) els.counterpartyPolicyAccept.addEventListener('click', function () { resolveCounterpartyPolicyReview('accept'); });
  if (els.counterpartyPolicyReject) els.counterpartyPolicyReject.addEventListener('click', function () { resolveCounterpartyPolicyReview('reject'); });

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
    showAlert('');
    loadTransferMatch(activeTxn);
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
      // Always use the API path so an action survives pagination/filtering and
      // can be applied after the modal's async review controls are ready.
      openNotificationTransaction(txnId, params.get('fin_notification'), params.get('classification_action') || 'open');
    });
  })();

  if ('serviceWorker' in navigator) {
    navigator.serviceWorker.addEventListener('message', function (event) {
      var data = event.data || {};
      if (data.type === 'OPEN_TRANSACTION') {
        openNotificationTransaction(data.transactionId, data.notificationId, data.action || 'open');
      }
    });
  }

  // --- Період: показ діапазону ---
  var periodSel = document.getElementById('fin-period');
  var rangeWrap = document.getElementById('fin-custom-range');
  if (periodSel && rangeWrap) {
    periodSel.addEventListener('change', function () { rangeWrap.hidden = periodSel.value !== 'custom'; });
  }

  // --- Швидкі фільтри журналу: застосовуються без окремої кнопки ---
  (function () {
    var form = document.getElementById('fin-filters');
    if (!form) return;
    var liveInputs = form.querySelectorAll('#fin-period, #fin-scope, input[name="date_from"], input[name="date_to"], input[name="amount_min"], input[name="amount_max"], input[name="search"]');
    var typeChips = form.querySelectorAll('[data-fin-live-types]');
    var statusChips = form.querySelectorAll('[data-fin-live-statuses]');
    var timer = null;
    var typesTouched = false;
    var statusesTouched = false;

    function selectedValues(nodes) {
      return Array.prototype.slice.call(nodes).filter(function (node) { return node.checked; }).map(function (node) { return node.value; });
    }

    function syncQuickChips() {
      var params = new URLSearchParams(window.location.search);
      var selectedTypes = new Set((params.get('types') || '').split(',').filter(Boolean));
      var selectedStatuses = new Set((params.get('statuses') || '').split(',').filter(Boolean));
      Array.prototype.forEach.call(typeChips, function (node) { node.checked = selectedTypes.has(node.value); });
      Array.prototype.forEach.call(statusChips, function (node) { node.checked = selectedStatuses.has(node.value); });
    }

    function applyLiveFilters() {
      var params = new URLSearchParams(window.location.search);
      ['period', 'date_from', 'date_to', 'search', 'amount_min', 'amount_max', 'scope'].forEach(function (name) {
        var input = form.querySelector('[name="' + name + '"]');
        if (!input || !input.value) params.delete(name);
        else params.set(name, input.value);
      });
      if (typesTouched) {
        var types = selectedValues(typeChips);
        if (types.length) params.set('types', types.join(',')); else params.delete('types');
      }
      if (statusesTouched) {
        var statuses = selectedValues(statusChips);
        if (statuses.length) params.set('statuses', statuses.join(','));
        else params.delete('statuses');
      }
      params.delete('page');
      window.location.search = params.toString();
    }

    syncQuickChips();
    Array.prototype.forEach.call(liveInputs, function (input) {
      input.addEventListener('change', applyLiveFilters);
      if (input.type === 'search' || input.type === 'number') {
        input.addEventListener('input', function () {
          window.clearTimeout(timer);
          timer = window.setTimeout(applyLiveFilters, 420);
        });
      }
    });
    Array.prototype.forEach.call(typeChips, function (input) {
      input.addEventListener('change', function () { typesTouched = true; applyLiveFilters(); });
    });
    Array.prototype.forEach.call(statusChips, function (input) {
      input.addEventListener('change', function () { statusesTouched = true; applyLiveFilters(); });
    });
  })();

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
  var filterParamNames = ['accounts', 'categories', 'projects', 'counterparties', 'tags', 'types', 'statuses'];
  function selectedUrlValues(name) {
    var raw = new URLSearchParams(window.location.search).get(name) || '';
    return new Set(raw.split(',').map(function (value) { return value.trim(); }).filter(Boolean));
  }
  function buildChecklist(container, items) {
    container.innerHTML = '';
    var selected = selectedUrlValues(container.dataset.multi);
    (items || []).forEach(function (it) {
      var lbl = document.createElement('label');
      lbl.className = 'fin-checklist__option';
      var input = document.createElement('input');
      input.type = 'checkbox';
      input.value = it.id;
      input.checked = selected.has(String(it.id));
      var text = document.createElement('span');
      text.textContent = it.name || 'Без назви';
      lbl.appendChild(input);
      lbl.appendChild(text);
      container.appendChild(lbl);
    });
  }
  function syncChipValues() {
    filterModal.querySelectorAll('[data-chips]').forEach(function (c) {
      var selected = selectedUrlValues(c.dataset.chips);
      c.querySelectorAll('input[type="checkbox"]').forEach(function (input) {
        input.checked = selected.has(input.value);
      });
    });
  }
  function preserveBaseQuery(form) {
    var current = new URLSearchParams(window.location.search);
    var advanced = new Set(filterParamNames.concat(['page']));
    ['period', 'date_from', 'date_to', 'search', 'amount_min', 'amount_max', 'scope', 'mcc_group', 'per_page'].forEach(function (name) {
      if (advanced.has(name) || !current.has(name)) return;
      var input = form.querySelector('[data-preserved-filter="' + name + '"]');
      if (!input) {
        input = document.createElement('input');
        input.type = 'hidden';
        input.name = name;
        input.dataset.preservedFilter = name;
        form.appendChild(input);
      }
      input.value = current.get(name) || '';
    });
  }
  if (filterBtn && filterModal) {
    filterBtn.addEventListener('click', function () {
      filterModal.querySelectorAll('[data-multi]').forEach(function (c) {
        buildChecklist(c, DROPDOWNS[c.dataset.multi]);
      });
      syncChipValues();
      filterModal.hidden = false; document.body.classList.add('fin-modal-open');
    });
    filterModal.querySelectorAll('[data-fin-close]').forEach(function (b) {
      b.addEventListener('click', function () { filterModal.hidden = true; document.body.classList.remove('fin-modal-open'); });
    });
    var filterForm = document.getElementById('fin-filter-form');
    if (filterForm) filterForm.addEventListener('submit', function () {
      preserveBaseQuery(filterForm);
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
      filterModal.querySelectorAll('input[type="checkbox"]:checked').forEach(function (i) { i.checked = false; });
    });
  }
})();
