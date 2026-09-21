/* TwoComms Finance — розділ «Контрагенти»: створення/редагування/видалення. */
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

  var modal = document.getElementById('fin-cp-form-modal');
  var form = document.getElementById('fin-cp-form');
  if (!modal || !form) return;

  var els = {
    id: document.getElementById('fin-cp-id'),
    title: document.getElementById('fin-cp-form-title'),
    submit: document.getElementById('fin-cp-submit'),
    alert: document.getElementById('fin-cp-alert'),
    name: document.getElementById('fin-cp-name'),
    type: document.getElementById('fin-cp-type'),
    group: document.getElementById('fin-cp-group'),
    edrpou: document.getElementById('fin-cp-edrpou'),
    iban: document.getElementById('fin-cp-iban'),
    country: document.getElementById('fin-cp-country'),
    address: document.getElementById('fin-cp-address'),
    phone: document.getElementById('fin-cp-phone'),
    email: document.getElementById('fin-cp-email'),
    telegram: document.getElementById('fin-cp-telegram'),
    responsible: document.getElementById('fin-cp-responsible'),
    note: document.getElementById('fin-cp-note'),
  };

  function showAlert(msg) { els.alert.textContent = msg || ''; els.alert.hidden = !msg; }

  function setReqOpen(open) {
    var btn = document.getElementById('fin-cp-toggle-req');
    var wrap = document.getElementById('fin-cp-req-wrap');
    if (wrap) wrap.hidden = !open;
    if (btn) btn.setAttribute('aria-expanded', open ? 'true' : 'false');
  }

  function openModal(cp) {
    form.reset();
    showAlert('');
    setReqOpen(false);
    if (cp) {
      els.id.value = cp.id;
      els.title.textContent = 'Редагувати контрагента';
      els.submit.textContent = 'Зберегти';
      els.name.value = cp.name || '';
      els.type.value = cp.type || 'client';
      els.group.value = cp.group || '';
      els.edrpou.value = cp.edrpou || '';
      els.iban.value = cp.iban || '';
      els.country.value = cp.country || '';
      els.address.value = cp.address || '';
      var c = cp.contacts || {};
      els.phone.value = c.phone || '';
      els.email.value = c.email || '';
      els.telegram.value = c.telegram || '';
      els.responsible.value = c.responsible || '';
      els.note.value = c.note || '';
      if (cp.iban || cp.address || c.phone || c.email) setReqOpen(true);
    } else {
      els.id.value = '';
      els.title.textContent = 'Новий контрагент';
      els.submit.textContent = 'Створити';
    }
    modal.hidden = false;
    document.body.classList.add('fin-modal-open');
    setTimeout(function () { els.name.focus(); }, 50);
  }

  function closeModal() {
    modal.hidden = true;
    document.body.classList.remove('fin-modal-open');
  }

  function collect() {
    return {
      name: els.name.value.trim(),
      type: els.type.value,
      group: els.group.value.trim(),
      edrpou: els.edrpou.value.trim(),
      iban: els.iban.value.trim(),
      country: els.country.value.trim(),
      address: els.address.value.trim(),
      contacts: {
        phone: els.phone.value.trim(),
        email: els.email.value.trim(),
        telegram: els.telegram.value.trim(),
        responsible: els.responsible.value.trim(),
        note: els.note.value.trim(),
      },
    };
  }

  // --- Події відкриття ---
  function bindOpen(id) {
    var el = document.getElementById(id);
    if (el) el.addEventListener('click', function () { openModal(null); });
  }
  bindOpen('cp-add-btn');
  bindOpen('cp-add-empty-btn');

  document.querySelectorAll('[data-cp-edit]').forEach(function (btn) {
    btn.addEventListener('click', function (e) {
      e.preventDefault();
      e.stopPropagation();
      var id = btn.getAttribute('data-cp-edit');
      api('/api/counterparties/' + id + '/get/').then(function (res) {
        if (res.data.ok) openModal(res.data.counterparty);
      });
    });
  });

  document.querySelectorAll('[data-cp-delete]').forEach(function (btn) {
    btn.addEventListener('click', function (e) {
      e.preventDefault();
      e.stopPropagation();
      var id = btn.getAttribute('data-cp-delete');
      doDelete(id, false);
    });
  });

  function doDelete(id, force) {
    if (!force && !confirm('Видалити контрагента?')) return;
    api('/api/counterparties/' + id + '/delete/', 'POST', { force: force ? 1 : 0 }).then(function (res) {
      if (res.data.ok) {
        window.location.href = '/counterparties/';
      } else if (res.data.needs_force) {
        if (confirm(res.data.error)) doDelete(id, true);
      } else {
        alert(res.data.error || 'Помилка видалення');
      }
    });
  }

  // --- Закриття ---
  modal.querySelectorAll('[data-cpf-close]').forEach(function (b) { b.addEventListener('click', closeModal); });
  modal.addEventListener('click', function (e) { if (e.target === modal) closeModal(); });
  document.addEventListener('keydown', function (e) { if (e.key === 'Escape' && !modal.hidden) closeModal(); });

  var reqToggle = document.getElementById('fin-cp-toggle-req');
  if (reqToggle) reqToggle.addEventListener('click', function () {
    var wrap = document.getElementById('fin-cp-req-wrap');
    setReqOpen(wrap && wrap.hidden);
  });

  // --- Збереження ---
  form.addEventListener('submit', function (e) {
    e.preventDefault();
    var payload = collect();
    if (!payload.name) { showAlert('Вкажіть назву контрагента'); return; }
    var id = els.id.value;
    var url = id ? '/api/counterparties/' + id + '/update/' : '/api/counterparties/create/';
    showAlert('');
    api(url, 'POST', payload).then(function (res) {
      if (res.ok && res.data.ok) {
        window.location.reload();
      } else {
        showAlert(res.data.error || 'Не вдалося зберегти');
      }
    }).catch(function () { showAlert('Помилка мережі'); });
  });
})();


/* Policy editor: keeps reusable counterparty suggestions next to the person or business. */
(function () {
  'use strict';
  var modal = document.getElementById('fin-cp-policy-modal');
  var form = document.getElementById('fin-cp-policy-form');
  var trigger = document.querySelector('[data-cp-policy-edit]');
  if (!modal || !form || !trigger) return;

  function csrf() { var m = document.cookie.match(/csrftoken=([^;]+)/); return m ? m[1] : ''; }
  function api(url, method, body) {
    return fetch(url, {
      method: method || 'GET',
      headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrf(), 'X-Requested-With': 'XMLHttpRequest' },
      body: body ? JSON.stringify(body) : undefined,
    }).then(function (r) { return r.json().then(function (d) { return { ok: r.ok, data: d }; }); });
  }
  var dropdowns = {};
  try {
    var source = document.getElementById('fin-dropdowns');
    if (source) dropdowns = JSON.parse(source.textContent || '{}');
  } catch (ignore) {}
  var cpId = trigger.getAttribute('data-cp-policy-edit');
  var els = {
    title: document.getElementById('fin-cp-policy-title'),
    subtitle: document.getElementById('fin-cp-policy-subtitle'),
    alert: document.getElementById('fin-cp-policy-alert'),
    enabled: document.getElementById('fin-cp-policy-enabled'),
    type: document.getElementById('fin-cp-policy-type'),
    scope: document.getElementById('fin-cp-policy-scope'),
    category: document.getElementById('fin-cp-policy-category'),
    kind: document.getElementById('fin-cp-policy-kind'),
    confirm: document.getElementById('fin-cp-policy-confirm'),
    prompt: document.getElementById('fin-cp-policy-prompt'),
  };
  function setAlert(message) { els.alert.textContent = message || ''; els.alert.hidden = !message; }
  function close() { modal.hidden = true; document.body.classList.remove('fin-modal-open'); }
  function categoryOptions(type, selected) {
    var categories = (dropdowns.categories || []).filter(function (item) {
      return item.type === type || item.type === 'both';
    });
    els.category.innerHTML = '<option value="">Оберіть категорію</option>' + categories.map(function (item) {
      return '<option value="' + item.id + '">' + item.name.replace(/</g, '&lt;') + '</option>';
    }).join('');
    els.category.value = selected || '';
  }
  function syncKinds() {
    var expense = els.type.value === 'expense';
    Array.prototype.forEach.call(els.kind.options, function (option) {
      option.hidden = expense ? option.value === 'sale' : option.value === 'operating_expense';
    });
    if (expense && els.kind.value === 'sale') els.kind.value = 'operating_expense';
    if (!expense && els.kind.value === 'operating_expense') els.kind.value = 'sale';
  }
  function open(policy) {
    setAlert('');
    els.subtitle.textContent = trigger.closest('.fin-cp-policy').parentElement.closest('.fin-cp-cols') ? document.querySelector('.fin-page-title').textContent : '';
    els.enabled.checked = !!(policy && policy.is_enabled);
    els.type.value = (policy && policy.transaction_type) || 'expense';
    categoryOptions(els.type.value, policy && policy.category_id);
    els.scope.value = (policy && policy.ownership_scope) || 'business';
    els.kind.value = (policy && policy.economic_kind) || (els.type.value === 'income' ? 'sale' : 'operating_expense');
    els.confirm.checked = !policy || policy.require_confirmation !== false;
    els.prompt.value = (policy && policy.prompt) || '';
    syncKinds();
    modal.hidden = false; document.body.classList.add('fin-modal-open');
  }
  trigger.addEventListener('click', function () {
    api('/api/counterparties/' + cpId + '/classification-policy/').then(function (res) {
      if (res.ok && res.data.ok) open(res.data.policy);
      else setAlert((res.data && res.data.error) || 'Не вдалося завантажити налаштування');
    }).catch(function () { setAlert('Помилка мережі'); });
  });
  els.type.addEventListener('change', function () { categoryOptions(els.type.value); syncKinds(); });
  modal.querySelectorAll('[data-cpp-close]').forEach(function (button) { button.addEventListener('click', close); });
  modal.addEventListener('click', function (event) { if (event.target === modal) close(); });
  form.addEventListener('submit', function (event) {
    event.preventDefault();
    var payload = {
      is_enabled: els.enabled.checked,
      transaction_type: els.type.value,
      category_id: els.category.value,
      economic_kind: els.kind.value,
      ownership_scope: els.scope.value,
      require_confirmation: els.confirm.checked,
      prompt: els.prompt.value.trim(),
    };
    api('/api/counterparties/' + cpId + '/classification-policy/save/', 'POST', payload).then(function (res) {
      if (res.ok && res.data.ok) window.location.reload();
      else setAlert((res.data && res.data.error) || 'Не вдалося зберегти правило');
    }).catch(function () { setAlert('Помилка мережі'); });
  });
})();


/* Картки контрагента: ручне додавання/видалення (авто-збереження — при погашенні). */
(function () {
  'use strict';
  var wrap = document.getElementById('fin-cp-cards');
  if (!wrap) return;

  function csrf() { var m = document.cookie.match(/csrftoken=([^;]+)/); return m ? m[1] : ''; }
  function api(url, method, body) {
    return fetch(url, {
      method: method || 'GET',
      headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrf(), 'X-Requested-With': 'XMLHttpRequest' },
      body: body ? JSON.stringify(body) : undefined,
    }).then(function (r) { return r.json().then(function (d) { return { ok: r.ok, data: d }; }); });
  }

  var cpId = wrap.getAttribute('data-cp-id');

  document.addEventListener('click', function (e) {
    var addBtn = e.target.closest('[data-cp-card-add]');
    if (addBtn) {
      var bank = prompt('Банк (напр. monobank):', '');
      if (bank === null) return;
      var last4 = prompt('Останні 4 цифри картки:', '');
      if (last4 === null) return;
      var iban = prompt('IBAN (необов\'язково):', '') || '';
      api('/api/counterparties/' + cpId + '/cards/save/', 'POST',
          { bank: bank, last4: last4, iban: iban }).then(function (res) {
        if (res.ok && res.data.ok) window.location.reload();
        else alert((res.data && res.data.error) || 'Помилка');
      });
      return;
    }
    var delBtn = e.target.closest('[data-cp-card-del]');
    if (delBtn) {
      if (!confirm('Видалити цю картку?')) return;
      var cardId = delBtn.getAttribute('data-cp-card-del');
      api('/api/counterparties/' + cpId + '/cards/' + cardId + '/delete/', 'POST').then(function (res) {
        if (res.ok && res.data.ok) window.location.reload();
      });
      return;
    }
  });
})();
