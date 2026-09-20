/* TwoComms Finance — розділ «Планові»: погашення, редагування плану, історія контрагента. */
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
    }).then(function (r) {
      return r.json().catch(function () { return { ok: false, error: 'Сервер повернув некоректну відповідь' }; })
        .then(function (d) { return { ok: r.ok, data: d }; });
    }).catch(function () {
      return { ok: false, data: { ok: false, error: 'Не вдалося зв’язатися із сервером' } };
    });
  }

  var DROPDOWNS = {};
  try { DROPDOWNS = JSON.parse(document.getElementById('fin-dropdowns').textContent); } catch (e) {}

  function opt(value, label, selected) {
    var o = document.createElement('option');
    o.value = value; o.textContent = label;
    if (selected) o.selected = true;
    return o;
  }
  function fillSelect(sel, items, placeholder, selectedId) {
    if (!sel) return;
    sel.innerHTML = '';
    if (placeholder) sel.appendChild(opt('', placeholder));
    (items || []).forEach(function (it) { sel.appendChild(opt(it.id, it.name, String(it.id) === String(selectedId))); });
  }
  function categoryItems(type) {
    return type === 'income' ? (DROPDOWNS.income_categories || []) : (DROPDOWNS.expense_categories || []);
  }
  function todayISO() { return new Date().toISOString().slice(0, 10); }

  // ----------------------------- Component payment workflow
  var v2Modal = document.getElementById('fin-v2-pay-modal');
  var v2Form = document.getElementById('fin-v2-pay-form');
  var v2Result = document.getElementById('fin-v2-pay-result');
  var v2Existing = document.getElementById('fin-v2-existing-payment');
  var v2ExistingAmount = document.getElementById('fin-v2-existing-amount');
  var v2ExistingButton = document.getElementById('fin-v2-settle-existing');
  var v2ExistingPanel = document.getElementById('fin-v2-existing-panel');
  var v2NewPanel = document.getElementById('fin-v2-new-panel');
  var v2ApiButton = document.getElementById('fin-v2-pay-api');
  var v2PickerWrap = document.getElementById('fin-v2-component-picker-wrap');
  var v2Picker = document.getElementById('fin-v2-component-picker');
  var activeIntent = null;
  var activeComponentId = null;
  var v2Mode = 'existing';
  function setV2Mode(mode) {
    v2Mode = mode;
    document.querySelectorAll('[data-v2-mode]').forEach(function (button) {
      var active = button.getAttribute('data-v2-mode') === mode;
      button.classList.toggle('is-active', active);
      button.setAttribute('aria-selected', active ? 'true' : 'false');
    });
    if (v2ExistingPanel) v2ExistingPanel.hidden = mode !== 'existing';
    if (v2NewPanel) v2NewPanel.hidden = mode !== 'new';
    if (v2Result) v2Result.hidden = true;
  }
  function showV2Recipient(data, counterpartyId) {
    var box = document.getElementById('fin-v2-recipient');
    if (!box) return;
    box.hidden = false;
    box.textContent = '';
    var card = data && data.recipient_card;
    if (card && (card.iban || card.pan_mask)) {
      box.textContent = 'Отримувач: ' + (card.label || card.bank || 'збережений реквізит') +
        ' · ' + (card.iban || card.pan_mask);
      return;
    }
    box.textContent = 'Реквізити отримувача ще не заповнені. Додайте IBAN або картку в картці контрагента перед оплатою.';
    if (counterpartyId) {
      var link = document.createElement('a');
      link.href = '/counterparties/' + counterpartyId + '/';
      link.textContent = 'Відкрити картку';
      box.appendChild(document.createTextNode(' '));
      box.appendChild(link);
    }
  }
  function loadV2Context(componentId, counterpartyId) {
    if (!v2Existing) return;
    v2Existing.innerHTML = '<option value="">Завантаження операцій…</option>';
    api('/api/v2/obligation-components/' + componentId + '/payment-context/').then(function (res) {
      if (!res.ok || !res.data.ok) {
        v2Existing.innerHTML = '<option value="">Не вдалося завантажити операції</option>';
        return;
      }
      showV2Recipient(res.data, counterpartyId);
      var rows = res.data.candidates || [];
      v2Existing.innerHTML = '';
      if (!rows.length) {
        v2Existing.appendChild(opt('', 'Немає неповʼязаних операцій', false));
      }
      rows.forEach(function (row) {
        var label = [row.date || row.date_actual || '', row.amount_display || row.amount || '', row.account_name || '', row.comment || '']
          .filter(Boolean).join(' · ');
        var option = opt(row.id, label, false);
        option.dataset.amount = row.amount || '';
        v2Existing.appendChild(option);
      });
      v2ExistingAmount.value = res.data.component.remaining || '';
    });
  }
  function activateV2Component(componentId, componentName, remaining, purpose, counterpartyId) {
    activeComponentId = componentId;
    document.getElementById('fin-v2-component-id').value = activeComponentId;
    document.getElementById('fin-v2-pay-name').textContent = componentName || '';
    document.getElementById('fin-v2-amount').value = remaining || '';
    document.getElementById('fin-v2-purpose').value = purpose || '';
    document.getElementById('fin-v2-comment').value = '';
    var recipient = document.getElementById('fin-v2-recipient');
    if (recipient) recipient.hidden = true;
    v2Result.hidden = true; v2Result.textContent = '';
    activeIntent = null;
    var v2Submit = v2ApiButton;
    if (v2Submit) {
      v2Submit.innerHTML = '<span aria-hidden="true">↗</span> Підготувати оплату';
      v2Submit.classList.remove('fin-btn--secondary');
      v2Submit.classList.add('fin-btn--primary');
      v2Submit.onclick = null;
    }
    setV2Mode('existing');
    loadV2Context(activeComponentId, counterpartyId);
  }
  function openV2Pay(btn) {
    if (!v2Modal) return;
    if (v2PickerWrap) v2PickerWrap.hidden = true;
    activateV2Component(btn.getAttribute('data-component-id'), btn.getAttribute('data-component-name'),
      btn.getAttribute('data-component-remaining'), btn.getAttribute('data-component-purpose'),
      btn.getAttribute('data-counterparty-id'));
    openModal(v2Modal);
  }
  function openV2Group(btn) {
    if (!v2Modal) return;
    var groupId = btn.getAttribute('data-v2-group');
    if (!groupId) return;
    api('/api/v2/obligation-groups/' + groupId + '/').then(function (res) {
      if (!res.ok || !res.data.ok) {
        if (v2Result) { v2Result.hidden = false; v2Result.textContent = (res.data && res.data.error) || 'Не вдалося відкрити групу оплати'; }
        openModal(v2Modal);
        return;
      }
      var group = res.data.group;
      var components = (group.components || []).filter(function (component) {
        return Number(component.remaining) > 0 || component.needs_amount;
      });
      if (!components.length) return;
      if (v2PickerWrap) v2PickerWrap.hidden = false;
      if (v2Picker) {
        v2Picker.innerHTML = '';
        components.forEach(function (component) {
          var option = opt(component.id, component.name + ' · залишок ' + component.remaining + ' грн', false);
          option.dataset.name = component.name;
          option.dataset.remaining = component.remaining;
          option.dataset.purpose = component.purpose || '';
          v2Picker.appendChild(option);
        });
        v2Picker.onchange = function () {
          var current = v2Picker.options[v2Picker.selectedIndex];
          activateV2Component(current.value, current.dataset.name, current.dataset.remaining,
            current.dataset.purpose, group.counterparty_id || '');
        };
      }
      var first = components[0];
      activateV2Component(first.id, first.name, first.remaining, first.purpose, group.counterparty_id || '');
      openModal(v2Modal);
    });
  }
  if (v2Existing) v2Existing.addEventListener('change', function () {
    var selected = v2Existing.options[v2Existing.selectedIndex];
    if (selected && selected.value) v2ExistingAmount.value = selected.dataset.amount || v2ExistingAmount.value;
  });
  if (v2ExistingButton) v2ExistingButton.addEventListener('click', function () {
    if (!activeComponentId || !v2Existing.value) return;
    v2ExistingButton.disabled = true;
    api('/api/v2/obligation-components/' + activeComponentId + '/settle-existing/', 'POST', {
      transaction_id: v2Existing.value, amount: v2ExistingAmount.value,
    }).then(function (res) {
      v2ExistingButton.disabled = false;
      v2Result.hidden = false;
      v2Result.textContent = res.ok && res.data.ok ? 'Операцію привʼязано. Залишок компонента оновлено.' : ((res.data && res.data.error) || 'Не вдалося привʼязати операцію');
      if (res.ok && res.data.ok) setTimeout(function () { window.location.reload(); }, 700);
    });
  });
  document.querySelectorAll('[data-v2-mode]').forEach(function (button) {
    button.addEventListener('click', function () { setV2Mode(button.getAttribute('data-v2-mode')); });
  });
  function createV2Intent(fromApiButton) {
    if (v2Mode !== 'new' || activeIntent) return;
    var button = fromApiButton ? v2ApiButton : v2Form.querySelector('button[type="submit"]');
    if (button) button.disabled = true;
    api('/api/v2/obligation-components/' + document.getElementById('fin-v2-component-id').value + '/payment-intent/', 'POST', {
      account_id: document.getElementById('fin-v2-account').value,
      amount: document.getElementById('fin-v2-amount').value,
      purpose: document.getElementById('fin-v2-purpose').value,
      comment: document.getElementById('fin-v2-comment').value,
      requested_mode: fromApiButton ? 'api' : 'instructions',
    }).then(function (res) {
      if (button) button.disabled = false;
      v2Result.hidden = false;
      if (!res.ok || !res.data.ok) { v2Result.textContent = (res.data && res.data.error) || 'Не вдалося сформувати платіж'; return; }
      activeIntent = res.data.intent;
      var recipient = activeIntent.recipient || {};
      var capability = activeIntent.capability || {};
      if (capability.outgoing_supported) {
        v2Result.textContent = 'Запит на оплату передано Monobank. Очікуємо підтвердження у застосунку.';
      } else {
        v2Result.textContent = 'Вихідний платіжний API Monobank Personal для цього токена недоступний. Реквізити підготовлено: ' +
          activeIntent.amount + ' ' + activeIntent.currency + ' · ' +
          (recipient.iban || recipient.pan_mask || recipient.label || 'реквізити не вказані') +
          '. Виконайте платіж у застосунку Monobank, потім натисніть «Я підтвердив оплату».';
      }
      var submit = v2ApiButton;
      submit.textContent = 'Я підтвердив оплату';
      submit.classList.remove('fin-btn--secondary');
      submit.classList.add('fin-btn--primary');
      submit.onclick = function (ev) {
        ev.preventDefault();
        api('/api/v2/payment-intents/' + activeIntent.id + '/transition/', 'POST', { status: 'submitted' })
          .then(function (transition) {
            if (transition.ok && transition.data.ok) {
              activeIntent = transition.data.intent;
              v2Result.textContent = 'Очікуємо підтвердження Monobank. Статус оновиться автоматично після виписки.';
              pollV2Intent(activeIntent.id);
            } else { v2Result.textContent = (transition.data && transition.data.error) || 'Не вдалося змінити статус платежу'; }
          });
      };
    });
  }
  if (v2Form) v2Form.addEventListener('submit', function (e) {
    e.preventDefault();
    createV2Intent(true);
  });
  function pollV2Intent(id) {
    var timer = setInterval(function () {
      api('/api/v2/payment-intents/' + id + '/').then(function (res) {
        if (!res.ok || !res.data.ok) return;
        var status = res.data.intent.status;
        if (status === 'detected' || status === 'confirmed' || status === 'rejected' || status === 'expired') {
          clearInterval(timer);
          v2Result.textContent = 'Статус платежу: ' + ({detected: 'знайдено у виписці', confirmed: 'підтверджено', rejected: 'відхилено', expired: 'прострочено'}[status] || status);
          if (status === 'confirmed') setTimeout(function () { window.location.reload(); }, 700);
        }
      });
    }, 5000);
  }
  document.addEventListener('click', function (e) {
    var groupPay = e.target.closest('button[data-v2-group]');
    if (groupPay) { e.preventDefault(); openV2Group(groupPay); return; }
    var pay = e.target.closest('[data-v2-pay]');
    if (pay) { e.preventDefault(); openV2Pay(pay); return; }
    if (e.target.closest('[data-v2-pay-close]')) closeModal(v2Modal);
  });

  // ---------------------------------------------------------------- Settle
  var settleModal = document.getElementById('fin-settle-modal');
  var settleEls = {
    txnId: document.getElementById('fin-settle-txn-id'),
    mode: document.getElementById('fin-settle-mode'),
    paymentId: document.getElementById('fin-settle-payment-id'),
    amount: document.getElementById('fin-settle-amount'),
    amountLabel: document.getElementById('fin-settle-amount-label'),
    amountHint: document.getElementById('fin-settle-amount-hint'),
    account: document.getElementById('fin-settle-account'),
    accountLabel: document.getElementById('fin-settle-account-label'),
    date: document.getElementById('fin-settle-date'),
    summary: document.getElementById('fin-settle-summary'),
    cp: document.getElementById('fin-settle-cp'),
    title: document.getElementById('fin-settle-title'),
    alert: document.getElementById('fin-settle-alert'),
    submit: document.getElementById('fin-settle-submit'),
    modes: document.getElementById('fin-settle-modes'),
    modePick: document.getElementById('fin-settle-mode-pick'),
    paneNew: document.getElementById('fin-settle-pane-new'),
    panePick: document.getElementById('fin-settle-pane-pick'),
    candidates: document.getElementById('fin-settle-candidates'),
    candidatesEmpty: document.getElementById('fin-settle-candidates-empty'),
    period: document.getElementById('fin-settle-period'),
    remainder: document.getElementById('fin-settle-remainder'),
    fullHint: document.getElementById('fin-settle-full-hint'),
    remember: document.getElementById('fin-settle-remember'),
    rememberWrap: document.getElementById('fin-settle-remember-wrap'),
    periodsWrap: document.getElementById('fin-settle-periods'),
    perN: document.getElementById('fin-settle-per-n'),
    perMinus: document.getElementById('fin-settle-per-minus'),
    perPlus: document.getElementById('fin-settle-per-plus'),
    perHint: document.getElementById('fin-settle-per-hint'),
  };
  var settleCtx = null;   // контекст із сервера
  var settlePeriods = 1;

  function openModal(el) { if (el) { el.hidden = false; document.body.classList.add('fin-modal-open'); } }
  function closeModal(el) { if (el) { el.hidden = true; document.body.classList.remove('fin-modal-open'); } }

  function fmtMoney(v) { return v; }

  function setMode(mode) {
    settleEls.mode.value = mode;
    var isPick = mode === 'pick_txn';
    settleEls.panePick.hidden = !isPick;
    settleEls.paneNew.hidden = isPick;
    if (settleEls.modes) {
      settleEls.modes.querySelectorAll('.fin-seg__btn').forEach(function (b) {
        b.classList.toggle('is-active', b.getAttribute('data-mode') === mode);
      });
    }
    if (!isPick) { settleEls.paymentId.value = ''; clearCandidateSelection(); }
    syncPeriod();
  }

  function clearCandidateSelection() {
    if (!settleEls.candidates) return;
    settleEls.candidates.querySelectorAll('.fin-cand.is-selected').forEach(function (c) {
      c.classList.remove('is-selected');
    });
  }

  function selectedAmount() {
    if (settleEls.mode.value === 'pick_txn') {
      var sel = settleEls.candidates.querySelector('.fin-cand.is-selected');
      return sel ? parseFloat(sel.getAttribute('data-amount')) : NaN;
    }
    return parseFloat(settleEls.amount.value);
  }

  function syncPeriod() {
    if (!settleCtx) return;
    var per = parseFloat(settleCtx.per_amount) || 0;
    var paid = selectedAmount();
    // Показуємо вибір «повністю/частково», якщо платимо менше за оцінку.
    var showPeriod = settleCtx.is_recurring || (!isNaN(paid) && paid < per);
    settleEls.period.hidden = !showPeriod;
    if (!isNaN(paid) && per > paid) {
      settleEls.remainder.textContent = (per - paid).toFixed(2);
    } else {
      settleEls.remainder.textContent = '0';
    }
    if (settleEls.fullHint) {
      settleEls.fullHint.textContent = settleCtx.is_recurring ? ' (наступний місяць)' : '';
    }
  }

  function renderCandidates(list) {
    settleEls.candidates.innerHTML = '';
    if (!list || !list.length) {
      settleEls.candidatesEmpty.hidden = false;
      return;
    }
    settleEls.candidatesEmpty.hidden = true;
    list.forEach(function (c) {
      var el = document.createElement('button');
      el.type = 'button';
      el.className = 'fin-cand';
      el.setAttribute('data-payment-id', c.id);
      el.setAttribute('data-amount', c.amount);
      var meta = [c.date, c.account_name].filter(Boolean).join(' · ');
      var tail = c.card_transfer_label ? ('<span class="fin-cand__card">' + c.card_transfer_label + '</span>') : '';
      el.innerHTML = '<span class="fin-cand__amt">' + c.amount_display + '</span>' +
        '<span class="fin-cand__meta">' + meta + '</span>' + tail;
      el.addEventListener('click', function () {
        clearCandidateSelection();
        el.classList.add('is-selected');
        settleEls.paymentId.value = c.id;
        syncPeriod();
      });
      settleEls.candidates.appendChild(el);
    });
  }

  function openSettle(card) {
    var txnId = card.getAttribute('data-next-txn');
    if (!txnId) return;
    settleEls.txnId.value = txnId;
    settleEls.alert.hidden = true;
    settleEls.paymentId.value = '';
    settleCtx = null;
    settleEls.summary.textContent = 'Завантаження…';
    settleEls.candidates.innerHTML = '';
    openModal(settleModal);

    api('/api/obligations/' + txnId + '/settle-context/').then(function (res) {
      if (!res.ok || !res.data.ok) { settleEls.summary.textContent = 'Не вдалося завантажити'; return; }
      settleCtx = res.data;
      var isIncome = settleCtx.ttype === 'income';
      settleEls.title.textContent = isIncome ? 'Підтвердити надходження' : 'Сплатити платіж';
      settleEls.accountLabel.textContent = isIncome ? 'Рахунок зарахування *' : 'Рахунок списання *';
      settleEls.summary.textContent = (settleCtx.title || '') + ' · ' +
        (settleCtx.estimated ? '≈ ' : '') + settleCtx.per_amount_display;
      if (settleCtx.counterparty) {
        settleEls.cp.hidden = false;
        settleEls.cp.textContent = '👤 ' + settleCtx.counterparty.name;
      } else { settleEls.cp.hidden = true; }

      // Сума.
      settleEls.amount.value = settleCtx.per_amount || '';
      settleEls.amountLabel.textContent = settleCtx.estimated ? 'Фактична сума *' : 'Сума';
      settleEls.amountHint.hidden = !settleCtx.estimated;

      // Рахунки (привʼязані до контрагента — першими).
      settleEls.account.innerHTML = '';
      (settleCtx.accounts || []).forEach(function (a) {
        var o = document.createElement('option');
        o.value = a.id; o.textContent = a.name + (a.linked ? ' 🔗' : '');
        settleEls.account.appendChild(o);
      });
      settleEls.date.value = todayISO();

      // Кандидати + режим за замовчуванням.
      renderCandidates(settleCtx.candidates);
      var hasCands = (settleCtx.candidates || []).length > 0;
      settleEls.modePick.disabled = !hasCands;
      setMode(hasCands ? 'pick_txn' : 'new_payment');

      // Запамʼятати картку — якщо є контрагент.
      settleEls.rememberWrap.hidden = !settleCtx.counterparty;
      settleEls.remember.checked = false;

      // Мультимісяць: степпер «за N місяців» для повторюваних із кількома планами.
      settlePeriods = 1;
      var maxP = settleCtx.max_periods || 1;
      settleEls.periodsWrap.hidden = !(settleCtx.is_recurring && maxP > 1);
      if (settleEls.perN) settleEls.perN.textContent = '1';
      updatePerHint();
    });
  }

  function clampPeriods(v) {
    var maxP = (settleCtx && settleCtx.max_periods) || 1;
    return Math.max(1, Math.min(maxP, v));
  }

  function updatePerHint() {
    if (!settleCtx || !settleEls.perHint) return;
    var per = parseFloat(settleCtx.per_amount) || 0;
    if (settlePeriods > 1 && per > 0) {
      settleEls.perHint.hidden = false;
      settleEls.perHint.textContent = 'Орієнтовно ' + (per * settlePeriods).toFixed(2) +
        ' за ' + settlePeriods + ' міс — таймер перестрибне на ' + settlePeriods + ' міс.';
      // У режимі «новий платіж» підставляємо орієнтовну суму за N міс.
      if (settleEls.mode.value === 'new_payment' && settleEls.amount) {
        settleEls.amount.value = (per * settlePeriods).toFixed(2);
      }
    } else {
      settleEls.perHint.hidden = true;
      if (settlePeriods === 1 && settleEls.mode.value === 'new_payment' && settleEls.amount) {
        settleEls.amount.value = settleCtx.per_amount || '';
      }
    }
    syncPeriod();
  }

  if (settleModal) {
    settleModal.querySelectorAll('[data-settle-close]').forEach(function (b) {
      b.addEventListener('click', function () { closeModal(settleModal); });
    });
    if (settleEls.modes) {
      settleEls.modes.addEventListener('click', function (e) {
        var b = e.target.closest('.fin-seg__btn');
        if (b && !b.disabled) setMode(b.getAttribute('data-mode'));
      });
    }
    if (settleEls.amount) settleEls.amount.addEventListener('input', syncPeriod);
    if (settleEls.perMinus) settleEls.perMinus.addEventListener('click', function () {
      settlePeriods = clampPeriods(settlePeriods - 1); settleEls.perN.textContent = settlePeriods; updatePerHint();
    });
    if (settleEls.perPlus) settleEls.perPlus.addEventListener('click', function () {
      settlePeriods = clampPeriods(settlePeriods + 1); settleEls.perN.textContent = settlePeriods; updatePerHint();
    });
    document.getElementById('fin-settle-form').addEventListener('submit', function (e) {
      e.preventDefault();
      var mode = settleEls.mode.value;
      var fullRadio = settleModal.querySelector('input[name="fin-settle-full"]:checked');
      var body = {
        mode: mode,
        periods: settlePeriods,
        full_period: (settleEls.period.hidden || !fullRadio) ? '1' : fullRadio.value,
        remember_card: settleEls.remember.checked ? '1' : '',
      };
      var paid = NaN;
      if (mode === 'pick_txn') {
        if (!settleEls.paymentId.value) {
          settleEls.alert.textContent = 'Оберіть платіж зі списку'; settleEls.alert.hidden = false; return;
        }
        body.payment_txn_id = settleEls.paymentId.value;
        var selCand = settleEls.candidates.querySelector('.fin-cand.is-selected');
        if (selCand) paid = parseFloat(selCand.getAttribute('data-amount'));
      } else {
        body.amount = settleEls.amount.value || '';
        body.account_id = settleEls.account.value || '';
        body.date = settleEls.date.value || '';
        paid = parseFloat(settleEls.amount.value);
        if (!body.account_id) {
          settleEls.alert.textContent = 'Оберіть рахунок'; settleEls.alert.hidden = false; return;
        }
      }
      // Підтвердження розбіжності для ТОЧНИХ сум: якщо сплачене помітно != план×N.
      if (settleCtx && !settleCtx.estimated && !isNaN(paid)) {
        var expected = (parseFloat(settleCtx.per_amount) || 0) * settlePeriods;
        if (expected > 0 && Math.abs(paid - expected) > 0.01) {
          if (!window.confirm('Сума ' + paid.toFixed(2) + ' відрізняється від планової ' +
              expected.toFixed(2) + ' (' + settlePeriods + ' міс). Підтвердити?')) return;
        }
      }
      settleEls.submit.disabled = true;
      api('/api/obligations/' + settleEls.txnId.value + '/settle/', 'POST', body).then(function (res) {
        settleEls.submit.disabled = false;
        if (res.ok && res.data.ok) { window.location.reload(); }
        else { settleEls.alert.textContent = (res.data && res.data.error) || 'Помилка'; settleEls.alert.hidden = false; }
      }).catch(function () { settleEls.submit.disabled = false; settleEls.alert.textContent = 'Помилка мережі'; settleEls.alert.hidden = false; });
    });
  }

  // ---------------------------------------------------------------- Edit plan
  var planModal = document.getElementById('fin-plan-modal');
  var planEls = {
    ruleId: document.getElementById('fin-plan-rule-id'),
    title: document.getElementById('fin-plan-title'),
    amount: document.getElementById('fin-plan-amount'),
    amountType: document.getElementById('fin-plan-amount-type'),
    frequency: document.getElementById('fin-plan-frequency'),
    interval: document.getElementById('fin-plan-interval'),
    category: document.getElementById('fin-plan-category'),
    counterparty: document.getElementById('fin-plan-counterparty'),
    endMode: document.getElementById('fin-plan-end-mode'),
    untilWrap: document.getElementById('fin-plan-until-wrap'),
    endDate: document.getElementById('fin-plan-end-date'),
    countWrap: document.getElementById('fin-plan-count-wrap'),
    count: document.getElementById('fin-plan-count'),
    alert: document.getElementById('fin-plan-alert'),
  };

  function syncPlanEnd() {
    if (!planEls.endMode) return;
    planEls.untilWrap.hidden = planEls.endMode.value !== 'until';
    planEls.countWrap.hidden = planEls.endMode.value !== 'count';
  }

  function openPlan(card) {
    if (!planModal) return;
    var ruleId = card.getAttribute('data-rule-id');
    if (!ruleId) return;
    planEls.ruleId.value = ruleId;
    planEls.alert.hidden = true;
    planEls.title.value = card.getAttribute('data-title') || '';
    planEls.amount.value = card.getAttribute('data-per-amount') || '';
    // Прелоад поточного графіка правила, щоб редагування показувало реальний
    // стан, а зміна періодичності справді застосовувалась.
    if (planEls.amountType) planEls.amountType.value = card.getAttribute('data-estimated') === '1' ? '1' : '0';
    if (planEls.frequency) planEls.frequency.value = card.getAttribute('data-frequency') || 'monthly';
    if (planEls.interval) planEls.interval.value = card.getAttribute('data-interval') || '1';
    fillSelect(planEls.category, categoryItems(card.getAttribute('data-type')), 'Без категорії');
    fillSelect(planEls.counterparty, DROPDOWNS.counterparties || [], 'Без контрагента',
               card.getAttribute('data-counterparty-id'));
    planEls.endMode.value = card.getAttribute('data-end-mode') || 'never';
    planEls.endDate.value = card.getAttribute('data-end-date') || '';
    planEls.count.value = card.getAttribute('data-count') || '';
    syncPlanEnd();
    openModal(planModal);
  }

  if (planModal) {
    planModal.querySelectorAll('[data-plan-close]').forEach(function (b) {
      b.addEventListener('click', function () { closeModal(planModal); });
    });
    if (planEls.endMode) planEls.endMode.addEventListener('change', syncPlanEnd);
    document.getElementById('fin-plan-form').addEventListener('submit', function (e) {
      e.preventDefault();
      var body = {
        title: planEls.title.value || '',
        amount: planEls.amount.value || '',
        amount_is_estimated: (planEls.amountType && planEls.amountType.value === '1') ? '1' : '0',
        frequency: planEls.frequency.value || '',
        interval: (planEls.interval && planEls.interval.value) || '1',
        category_id: planEls.category.value || '',
        counterparty_id: planEls.counterparty.value || '',
        end_mode: planEls.endMode.value || 'never',
        end_date: planEls.endDate.value || '',
        count: planEls.count.value || '',
      };
      api('/api/recurrence/' + planEls.ruleId.value + '/update/', 'POST', body).then(function (res) {
        if (res.ok && res.data.ok) { window.location.reload(); }
        else { planEls.alert.textContent = (res.data && res.data.error) || 'Помилка'; planEls.alert.hidden = false; }
      });
    });
  }

  // ---------------------------------------------------------------- Counterparty history
  var cpModal = document.getElementById('fin-cp-modal');
  function openCpHistory(cpId, name) {
    if (!cpModal) return;
    if (!cpId) return;
    document.getElementById('fin-cp-title').textContent = 'Історія: ' + (name || '');
    document.getElementById('fin-cp-totals').innerHTML = '<span class="fin-muted-cell">Завантаження…</span>';
    document.getElementById('fin-cp-accounts').innerHTML = '';
    document.getElementById('fin-cp-tx-body').innerHTML = '';
    openModal(cpModal);
    api('/api/counterparties/' + cpId + '/history/').then(function (res) {
      if (!res.ok || !res.data.ok) { document.getElementById('fin-cp-totals').textContent = 'Не вдалося завантажити'; return; }
      var d = res.data;
      var t = d.totals;
      document.getElementById('fin-cp-totals').innerHTML =
        '<div class="fin-cp-total"><span>Отримано</span><b class="is-pos">' + t.received + '</b></div>' +
        '<div class="fin-cp-total"><span>Сплачено</span><b class="is-neg">' + t.paid + '</b></div>' +
        '<div class="fin-cp-total"><span>Чисто</span><b>' + t.net + '</b></div>' +
        '<div class="fin-cp-total"><span>Заплановано ↑</span><b>' + t.planned_in + '</b></div>' +
        '<div class="fin-cp-total"><span>Заплановано ↓</span><b>' + t.planned_out + '</b></div>';
      document.getElementById('fin-cp-accounts').innerHTML = (d.accounts || []).map(function (a) {
        return '<span class="fin-cp-acc' + (a.linked ? ' is-linked' : '') + '">💳 ' + a.name + ' · ' + a.balance + (a.linked ? ' 🔗' : '') + '</span>';
      }).join('');
      document.getElementById('fin-cp-tx-body').innerHTML = (d.transactions || []).map(function (x) {
        return '<tr><td>' + (x.date || '') + '</td><td class="fin-amount--' + x.amount_class + '">' + x.amount_display +
          '</td><td>' + (x.account_name || '—') + '</td><td>' + x.status_display +
          (x.is_recurring ? ' ⟳' : '') + '</td><td>' + (x.comment || '') + '</td></tr>';
      }).join('') || '<tr><td colspan="5" class="fin-muted-cell">Операцій ще немає</td></tr>';
    });
  }
  if (cpModal) {
    cpModal.querySelectorAll('[data-cp-close]').forEach(function (b) {
      b.addEventListener('click', function () { closeModal(cpModal); });
    });
  }

  // ---------------------------------------------------------------- Delegation
  document.addEventListener('click', function (e) {
    var btn = e.target.closest('[data-act]');
    if (btn) {
      var card = btn.closest('.fin-oblig');
      var act = btn.getAttribute('data-act');
      // Закрити kebab-меню після вибору дії.
      var openMenu = btn.closest('.fin-oblig__menu');
      if (openMenu) openMenu.removeAttribute('open');
      if (act === 'settle') openSettle(card);
      else if (act === 'edit-plan') openPlan(card);
      else if (act === 'skip') skipMonth(card);
      else if (act === 'move-current') moveToCurrent(card);
      else if (act === 'stop-rule') {
        if (confirm('Зупинити повторення та прибрати майбутні планові платежі?')) {
          api('/api/recurrence/' + btn.getAttribute('data-rule-id') + '/stop/', 'POST', { delete_future: '1' })
            .then(function (res) { if (res.ok && res.data.ok) window.location.reload(); });
        }
      }
      return;
    }
    var cpChip = e.target.closest('[data-cp-history]');
    if (cpChip) {
      var card2 = cpChip.closest('.fin-oblig');
      openCpHistory(card2.getAttribute('data-counterparty-id'), cpChip.textContent.replace('👤', '').trim());
    }
  });

  function skipMonth(card) {
    var txnId = card.getAttribute('data-next-txn');
    if (!txnId) return;
    var ok = confirm('Пропустити цей місяць?\n\nOK — перенести платіж у кінець (борг зберігається).\nСкасувати — нічого не робити.');
    if (!ok) return;
    api('/api/obligations/' + txnId + '/skip/', 'POST', { mode: 'move_end' })
      .then(function (res) { if (res.ok && res.data.ok) window.location.reload();
                             else alert((res.data && res.data.error) || 'Помилка'); });
  }

  function moveToCurrent(card) {
    var txnId = card.getAttribute('data-next-txn');
    if (!txnId) return;
    api('/api/obligations/' + txnId + '/move-current/', 'POST', {})
      .then(function (res) { if (res.ok && res.data.ok) window.location.reload();
                             else alert((res.data && res.data.error) || 'Помилка'); });
  }

  // Закриття модалок по кліку на бекдроп / Esc.
  [settleModal, planModal, cpModal].forEach(function (m) {
    if (!m) return;
    m.addEventListener('click', function (e) { if (e.target === m) closeModal(m); });
  });
  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape') { closeModal(settleModal); closeModal(planModal); closeModal(cpModal); }
  });
})();
