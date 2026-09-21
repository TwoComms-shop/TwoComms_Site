/* TwoComms Finance — конструктор автоправил. */
(function () {
  'use strict';
  var modal = document.getElementById('fin-rule-modal');
  if (!modal) return;

  function csrf() { var m = document.cookie.match(/csrftoken=([^;]+)/); return m ? m[1] : ''; }
  function api(url, body) {
    return fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrf() },
      body: body ? JSON.stringify(body) : undefined }).then(function (r) { return r.json(); });
  }
  function jp(id) { try { return JSON.parse(document.getElementById(id).textContent); } catch (e) { return []; } }

  var DD = jp('fin-dropdowns'), FIELDS = jp('fin-rule-fields'), OPS = jp('fin-rule-operators'), ACTIONS = jp('fin-rule-actions');
  var condBox = document.getElementById('fin-conditions');
  var actBox = document.getElementById('fin-actions');

  function options(pairs) {
    return pairs.map(function (p) { return '<option value="' + p[0] + '">' + p[1] + '</option>'; }).join('');
  }
  // Поля, значення яких — вибір із довідника.
  var FIELD_SOURCE = { account: 'accounts', category: 'categories', counterparty: 'counterparties', project: 'projects' };
  var ACTION_SOURCE = { set_category: 'categories', set_project: 'projects', set_counterparty: 'counterparties', add_tag: 'tags' };

  function ddOptions(key) {
    return (DD[key] || []).map(function (o) { return '<option value="' + o.id + '">' + o.name + '</option>'; }).join('');
  }

  function addCondition(c) {
    c = c || {};
    var row = document.createElement('div');
    row.className = 'fin-rule-row';
    row.innerHTML =
      '<select class="fin-select cond-field">' + options(FIELDS) + '</select>' +
      '<select class="fin-select cond-op">' + options(OPS) + '</select>' +
      '<span class="cond-value-wrap"><input class="fin-input cond-value" placeholder="значення"></span>' +
      '<button type="button" class="fin-icon-btn fin-icon-btn--sm cond-del">×</button>';
    condBox.appendChild(row);
    var fieldSel = row.querySelector('.cond-field');
    if (c.field) fieldSel.value = c.field;
    if (c.operator) row.querySelector('.cond-op').value = c.operator;
    function syncValue() {
      var src = FIELD_SOURCE[fieldSel.value];
      var wrap = row.querySelector('.cond-value-wrap');
      if (src) wrap.innerHTML = '<select class="fin-select cond-value">' + ddOptions(src) + '</select>';
      else wrap.innerHTML = '<input class="fin-input cond-value" placeholder="значення">';
      if (c.value !== undefined) row.querySelector('.cond-value').value = c.value;
    }
    fieldSel.addEventListener('change', syncValue);
    syncValue();
    row.querySelector('.cond-del').addEventListener('click', function () { row.remove(); });
  }

  function addAction(a) {
    a = a || {};
    var row = document.createElement('div');
    row.className = 'fin-rule-row';
    row.innerHTML =
      '<select class="fin-select act-type">' + options(ACTIONS) + '</select>' +
      '<span class="act-value-wrap"><input class="fin-input act-value" placeholder="значення"></span>' +
      '<label class="fin-checkbox act-ow"><input type="checkbox" checked> перезаписувати</label>' +
      '<button type="button" class="fin-icon-btn fin-icon-btn--sm act-del">×</button>';
    actBox.appendChild(row);
    var typeSel = row.querySelector('.act-type');
    if (a.action) typeSel.value = a.action;
    function syncValue() {
      var src = ACTION_SOURCE[typeSel.value];
      var wrap = row.querySelector('.act-value-wrap');
      if (src) wrap.innerHTML = '<select class="fin-select act-value">' + ddOptions(src) + '</select>';
      else if (typeSel.value === 'set_comment') wrap.innerHTML = '<input class="fin-input act-value" placeholder="текст коментаря">';
      else wrap.innerHTML = '';
      if (a.value !== undefined && row.querySelector('.act-value')) row.querySelector('.act-value').value = a.value;
    }
    typeSel.addEventListener('change', syncValue);
    syncValue();
    if (a.overwrite === false) row.querySelector('.act-ow input').checked = false;
    row.querySelector('.act-del').addEventListener('click', function () { row.remove(); });
  }

  document.getElementById('fin-add-condition').addEventListener('click', function () { addCondition(); });
  document.getElementById('fin-add-action').addEventListener('click', function () { addAction(); });

  function openModal(rule) {
    condBox.innerHTML = ''; actBox.innerHTML = '';
    document.getElementById('fin-rule-alert').hidden = true;
    if (rule) {
      document.getElementById('fin-rule-title').textContent = 'Редагувати правило';
      document.getElementById('fin-rule-id').value = rule.id;
      document.getElementById('fin-rule-name').value = rule.name;
      document.getElementById('fin-rule-priority').value = rule.priority;
      document.getElementById('fin-rule-type').value = rule.transaction_type || '';
      document.getElementById('fin-rule-enabled').checked = rule.is_enabled;
      (rule.conditions || []).forEach(addCondition);
      (rule.actions || []).forEach(addAction);
    } else {
      document.getElementById('fin-rule-title').textContent = 'Нове правило';
      document.getElementById('fin-rule-id').value = '';
      document.getElementById('fin-rule-form').reset();
      addCondition(); addAction();
    }
    modal.hidden = false; document.body.classList.add('fin-modal-open');
  }
  function closeModal() { modal.hidden = true; document.body.classList.remove('fin-modal-open'); }

  ['fin-rule-add', 'fin-rule-add-2'].forEach(function (id) {
    var b = document.getElementById(id); if (b) b.addEventListener('click', function () { openModal(null); });
  });
  modal.querySelectorAll('[data-fin-close]').forEach(function (b) { b.addEventListener('click', closeModal); });

  document.querySelectorAll('.fin-rule-edit').forEach(function (b) {
    b.addEventListener('click', function () {
      var card = b.closest('.fin-rule-card');
      try { openModal(JSON.parse(card.dataset.rule)); } catch (e) { openModal(null); }
    });
  });

  document.getElementById('fin-rule-form').addEventListener('submit', function (e) {
    e.preventDefault();
    var conditions = Array.prototype.slice.call(condBox.querySelectorAll('.fin-rule-row')).map(function (r) {
      return { field: r.querySelector('.cond-field').value, operator: r.querySelector('.cond-op').value,
               value: r.querySelector('.cond-value') ? r.querySelector('.cond-value').value : '' };
    });
    var actions = Array.prototype.slice.call(actBox.querySelectorAll('.fin-rule-row')).map(function (r) {
      var valEl = r.querySelector('.act-value');
      return { action: r.querySelector('.act-type').value, value: valEl ? valEl.value : '',
               overwrite: r.querySelector('.act-ow input').checked };
    });
    var id = document.getElementById('fin-rule-id').value;
    var url = id ? '/api/rules/' + id + '/save/' : '/api/rules/save/';
    api(url, {
      name: document.getElementById('fin-rule-name').value,
      priority: document.getElementById('fin-rule-priority').value,
      transaction_type: document.getElementById('fin-rule-type').value,
      is_enabled: document.getElementById('fin-rule-enabled').checked,
      apply_to_existing: document.getElementById('fin-rule-existing').checked,
      conditions: conditions, actions: actions,
    }).then(function (d) {
      if (d.ok) { if (d.applied) alert('Застосовано до ' + d.applied + ' операцій'); location.reload(); }
      else { var a = document.getElementById('fin-rule-alert'); a.textContent = d.error; a.hidden = false; }
    });
  });

  document.querySelectorAll('.fin-rule-toggle').forEach(function (t) {
    t.addEventListener('click', function () {
      var id = t.closest('.fin-rule-card').dataset.ruleId;
      api('/api/rules/' + id + '/toggle/');
    });
  });
  document.querySelectorAll('.fin-rule-del').forEach(function (b) {
    b.addEventListener('click', function () {
      if (!confirm('Видалити правило?')) return;
      api('/api/rules/' + b.dataset.id + '/delete/').then(function (d) { if (d.ok) location.reload(); });
    });
  });
  document.querySelectorAll('.fin-rule-apply').forEach(function (b) {
    b.addEventListener('click', function () {
      api('/api/rules/' + b.dataset.id + '/preview/').then(function (d) {
        if (!d.ok) return;
        if (confirm('Правило змінить ' + d.count + ' операцій. Застосувати?')) {
          api('/api/rules/' + b.dataset.id + '/apply/').then(function (r) {
            if (r.ok) { alert('Застосовано до ' + r.applied + ' операцій'); location.reload(); }
          });
        }
      });
    });
  });
})();
