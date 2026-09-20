/* TwoComms Finance — графіки звітів (Chart.js 4). */
(function () {
  'use strict';

  var POS = '#34d399', NEG = '#fb7185', LINE = '#ffb270';
  var PIE = ['#ff7e29', '#6f95ff', '#34d399', '#fb7185', '#f3a43d', '#a78bfa', '#22d3ee', '#facc15', '#fb923c', '#94a3b8'];
  var GRID = 'rgba(255,255,255,0.06)', TICK = '#9aa4b8';

  // Визначаємо чи це мобільний пристрій
  var isMobile = window.innerWidth <= 900;
  var isSmallMobile = window.innerWidth <= 640;

  function data() {
    try { return JSON.parse(document.getElementById('fin-chart-data').textContent); }
    catch (e) { return {}; }
  }

  function fmt(n) {
    var v = Math.round(Number(n) || 0);
    return v.toString().replace(/\B(?=(\d{3})+(?!\d))/g, ' ');
  }

  function setDefaults() {
    if (!window.Chart) return;
    Chart.defaults.color = TICK;
    Chart.defaults.font.family = "'Inter', system-ui, sans-serif";
    Chart.defaults.font.size = isMobile ? 11 : 12;
    Chart.defaults.plugins.legend.display = !isSmallMobile; // Ховаємо легенду на малих екранах
  }

  function seriesChart(d, posLabel, negLabel, netLabel) {
    var el = document.getElementById('fin-chart-series');
    if (!el || !window.Chart || !d.series) return;
    var labels = d.series.map(function (s) { return s.label; });
    var inData = d.series.map(function (s) { return s.in; });
    var outData = d.series.map(function (s) { return s.out; });
    var netData = d.series.map(function (s) { return (s.in || 0) - (s.out || 0); });

    new Chart(el, {
      data: {
        labels: labels,
        datasets: [
          { type: 'bar', label: posLabel, data: inData, backgroundColor: POS, borderRadius: isMobile ? 4 : 6, maxBarThickness: isMobile ? 24 : 34, order: 2 },
          { type: 'bar', label: negLabel, data: outData, backgroundColor: NEG, borderRadius: isMobile ? 4 : 6, maxBarThickness: isMobile ? 24 : 34, order: 2 },
          { type: 'line', label: netLabel, data: netData, borderColor: LINE, backgroundColor: LINE,
            borderWidth: isMobile ? 1.5 : 2, tension: 0.35, pointRadius: isMobile ? 2 : 3, pointBackgroundColor: LINE, order: 1, fill: false },
        ],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        interaction: { mode: 'index', intersect: false },
        plugins: {
          legend: {
            display: !isSmallMobile,
            labels: {
              usePointStyle: true,
              boxWidth: isMobile ? 6 : 8,
              padding: isMobile ? 10 : 16,
              font: { size: isMobile ? 10 : 12 }
            }
          },
          tooltip: {
            enabled: true,
            callbacks: {
              label: function (c) { return c.dataset.label + ': ' + fmt(c.parsed.y) + ' ₴'; }
            },
            titleFont: { size: isMobile ? 11 : 12 },
            bodyFont: { size: isMobile ? 10 : 11 },
            padding: isMobile ? 8 : 10
          },
        },
        scales: {
          x: {
            ticks: {
              color: TICK,
              font: { size: isMobile ? 10 : 11 },
              maxRotation: isMobile ? 45 : 0,
              minRotation: isMobile ? 45 : 0
            },
            grid: { display: false }
          },
          y: {
            ticks: {
              color: TICK,
              font: { size: isMobile ? 10 : 11 },
              callback: function (v) { return fmt(v); }
            },
            grid: { color: GRID }
          },
        },
      },
    });
  }

  function donut(id, rows) {
    var el = document.getElementById(id);
    if (!el || !window.Chart || !rows || !rows.length) return;
    new Chart(el, {
      type: 'doughnut',
      data: {
        labels: rows.map(function (r) { return r.name; }),
        datasets: [{
          data: rows.map(function (r) { return r.total; }),
          backgroundColor: PIE,
          borderColor: 'rgba(11,14,20,0.6)',
          borderWidth: isMobile ? 1 : 2
        }],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        cutout: isMobile ? '65%' : '62%',
        plugins: {
          legend: { display: false },
          tooltip: {
            enabled: true,
            callbacks: {
              label: function (c) { return c.label + ': ' + fmt(c.parsed) + ' ₴'; }
            },
            titleFont: { size: isMobile ? 11 : 12 },
            bodyFont: { size: isMobile ? 10 : 11 },
            padding: isMobile ? 8 : 10
          },
        },
      },
    });
  }

  function renderPnlFlow(flow, report) {
    var expanded = { income: false, expense: false };
    var selected = null;
    var source = {
      income: report.income_by_category || [],
      expense: report.expense_by_category || []
    };
    var totals = {};
    Object.keys(source).forEach(function (side) {
      source[side] = source[side].map(function (row) {
        return { name: String(row.name || 'Без категорії'), total: Number(row.total) || 0 };
      });
      totals[side] = source[side].reduce(function (sum, row) { return sum + row.total; }, 0);
    });
    var colors = ['#fb658c', '#a48afa', '#34cca0', '#f3b557', '#36bdd5'];
    var money = new Intl.NumberFormat('uk-UA', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
    var visible, svg, center, height;
    function node(tag, className, text, parent) {
      var element = document.createElement(tag);
      element.className = className;
      if (text != null) element.textContent = text;
      if (parent) parent.appendChild(element);
      return element;
    }
    function svgNode(tag, attributes, parent) {
      var element = document.createElementNS('http://www.w3.org/2000/svg', tag);
      Object.keys(attributes).forEach(function (key) { element.setAttribute(key, attributes[key]); });
      parent.appendChild(element);
      return element;
    }
    function rowsFor(side) {
      var rows = source[side];
      if (expanded[side] || rows.length <= 5) return rows;
      return rows.slice(0, 4).concat({ name: 'Інші категорії', total: rows.slice(4).reduce(function (sum, row) { return sum + row.total; }, 0), grouped: true });
    }
    function rowY(index, count) { return height / 2 + (index - (count - 1) / 2) * 62; }
    function highlight(key) {
      flow.classList.toggle('has-highlight', !!key);
      flow.querySelectorAll('[data-flow-key]').forEach(function (element) {
        element.classList.toggle('is-highlighted', element.dataset.flowKey === key);
        if (element.tagName === 'BUTTON' && !element.hasAttribute('aria-expanded')) {
          element.setAttribute('aria-pressed', element.dataset.flowKey === selected ? 'true' : 'false');
        }
      });
    }
    function draw() {
      var width = flow.clientWidth;
      if (!width) return;
      svg.replaceChildren();
      svg.setAttribute('viewBox', '0 0 ' + width + ' ' + height);
      var defs = svgNode('defs', {}, svg);
      ['income', 'expense'].forEach(function (side) {
        var color = side === 'income' ? '#32d49d' : '#f36088';
        var gradient = svgNode('linearGradient', { id: 'pnl-ribbon-' + side, x1: '0%', x2: '100%' }, defs);
        svgNode('stop', { offset: '0%', 'stop-color': color, 'stop-opacity': side === 'income' ? '.56' : '.24' }, gradient);
        svgNode('stop', { offset: '100%', 'stop-color': color, 'stop-opacity': side === 'income' ? '.24' : '.56' }, gradient);
        var rows = visible[side];
        var share = function (row) { return totals[side] > 0 ? Math.max(0, row.total / totals[side]) : 0; };
        // Separate ordered ports preserve both edges of each ribbon without crossings.
        var gap = Math.min(2.5, 20 / Math.max(1, rows.length));
        var widths = rows.map(function (row) { return row.total > 0 ? Math.max(.5, share(row) * 32) : 0; });
        var cursor = height / 2 - (widths.reduce(function (sum, value) { return sum + value; }, 0) + Math.max(0, rows.length - 1) * gap) / 2;
        var portX = width * (side === 'income' ? .205 : .795);
        var centerX = side === 'income' ? center.offsetLeft : center.offsetLeft + center.offsetWidth;
        rows.forEach(function (row, index) {
          var innerWidth = widths[index];
          var innerY = cursor + innerWidth / 2;
          cursor += innerWidth + gap;
          if (row.total <= 0) return;
          var y = rowY(index, rows.length);
          var outerWidth = Math.max(1, share(row) * 52);
          var x1 = side === 'income' ? portX : centerX;
          var x2 = side === 'income' ? centerX : portX;
          var y1 = side === 'income' ? y : innerY;
          var y2 = side === 'income' ? innerY : y;
          var w1 = side === 'income' ? outerWidth : innerWidth;
          var w2 = side === 'income' ? innerWidth : outerWidth;
          var bend = (x2 - x1) * .55;
          var path = 'M ' + x1 + ' ' + (y1 - w1 / 2) +
            ' C ' + (x1 + bend) + ' ' + (y1 - w1 / 2) + ' ' + (x2 - bend) + ' ' + (y2 - w2 / 2) + ' ' + x2 + ' ' + (y2 - w2 / 2) +
            ' L ' + x2 + ' ' + (y2 + w2 / 2) +
            ' C ' + (x2 - bend) + ' ' + (y2 + w2 / 2) + ' ' + (x1 + bend) + ' ' + (y1 + w1 / 2) + ' ' + x1 + ' ' + (y1 + w1 / 2) + ' Z';
          var group = svgNode('g', { 'class': 'pnl-flow-branch', 'data-flow-key': side + '-' + index }, svg);
          svgNode('path', { d: path, fill: 'url(#pnl-ribbon-' + side + ')' }, group);
          svgNode('line', { x1: portX, x2: portX, y1: y - Math.max(7, outerWidth / 2), y2: y + Math.max(7, outerWidth / 2), stroke: color, 'stroke-width': 2, 'class': 'pnl-flow-port' }, group);
        });
      });
      highlight(selected);
    }
    function render() {
      visible = { income: rowsFor('income'), expense: rowsFor('expense') };
      height = Math.max(280, Math.max(visible.income.length, visible.expense.length) * 62 + 12);
      flow.style.setProperty('--flow-height', height + 'px');
      flow.replaceChildren();
      svg = svgNode('svg', { 'class': 'pnl-flow-ribbons', 'aria-hidden': 'true' }, flow);
      center = node('div', 'pnl-flow-center', null, flow);
      node('b', '', money.format(totals.income) + ' ₴', center);
      node('span', '', 'Загальні надходження', center);
      ['income', 'expense'].forEach(function (side) {
        var column = node('div', 'pnl-flow-side pnl-flow-side--' + side, null, flow);
        if (!visible[side].length) node('span', 'pnl-flow-empty', side === 'income' ? 'Немає доходів' : 'Немає витрат', column);
        visible[side].forEach(function (row, index) {
          var key = side + '-' + index;
          var pct = totals[side] ? row.total / totals[side] * 100 : 0;
          var pctText = pct > 0 && pct < .1 ? '<0,1' : pct.toLocaleString('uk-UA', { minimumFractionDigits: 1, maximumFractionDigits: 1 });
          var item = node('button', 'pnl-flow-item', null, column);
          item.type = 'button';
          item.dataset.flowKey = key;
          item.style.setProperty('--flow-y', rowY(index, visible[side].length) + 'px');
          item.title = row.name + ': ' + money.format(row.total) + ' ₴ (' + pctText + '%)';
          item.setAttribute('aria-label', item.title);
          var dot = node('i', '', null, item);
          dot.style.backgroundColor = row.total === 0 ? '#8994a6' : side === 'income' ? '#31d39b' : colors[index % colors.length];
          dot.setAttribute('aria-hidden', 'true');
          var text = node('span', 'pnl-flow-copy', null, item);
          node('span', 'pnl-flow-name', row.name, text);
          var amount = node('span', 'pnl-flow-amount', null, text);
          node('b', '', money.format(row.total) + ' ₴', amount);
          node('small', '', '(' + pctText + '%)', amount);
          if (row.grouped) {
            item.setAttribute('aria-expanded', 'false');
            node('span', 'pnl-flow-expand', '+', item).setAttribute('aria-hidden', 'true');
          } else item.setAttribute('aria-pressed', 'false');
          item.addEventListener('mouseenter', function () { highlight(key); });
          item.addEventListener('mouseleave', function () { highlight(selected); });
          item.addEventListener('focus', function () { highlight(key); });
          item.addEventListener('blur', function () { highlight(selected); });
          item.addEventListener('keydown', function (event) { if (event.key === 'Escape') { selected = null; highlight(null); } });
          item.addEventListener('click', function () {
            if (row.grouped) {
              expanded[side] = true;
              selected = null;
              render();
              flow.querySelector('button[data-flow-key="' + side + '-4"]').focus({ preventScroll: true });
            } else { selected = selected === key ? null : key; highlight(selected); }
          });
        });
        if (expanded[side]) {
          var collapse = node('button', 'pnl-flow-collapse', 'Згорнути категорії', column);
          collapse.type = 'button';
          collapse.setAttribute('aria-expanded', 'true');
          collapse.addEventListener('click', function () {
            expanded[side] = false;
            selected = null;
            render();
            flow.querySelector('button[data-flow-key="' + side + '-4"]').focus({ preventScroll: true });
          });
        }
      });
      draw();
    }
    render();
    if (window.ResizeObserver) new ResizeObserver(draw).observe(flow);
    else window.addEventListener('resize', draw);
  }

  window.FinanceCharts = {
    renderCashflow: function () {
      setDefaults();
      var d = data();
      seriesChart(d, 'Поступлення', 'Списання', 'Чистий потік');
      donut('fin-chart-income', d.income_by_category);
      donut('fin-chart-expense', d.expense_by_category);
    },
    renderPnl: function () {
      setDefaults();
      var d = data();
      seriesChart(d, 'Доходи', 'Витрати', 'Прибуток');
      donut('fin-chart-income', d.income_by_category);
      donut('fin-chart-expense', d.expense_by_category);
    },
    renderPnlDashboard: function () {
      setDefaults();
      var d = data();
      var el = document.getElementById('pnl-trend-chart');
      var donutEl = document.getElementById('pnl-expense-donut');
      if (el && window.Chart) {
        var current = d.series || [];
        var previous = d.previous_series || [];
        var labels = current.map(function (s) { return s.label.slice(5).replace('-', ' '); });
        var previousIncome = previous.map(function (s) { return s.in || 0; });
        var previousExpense = previous.map(function (s) { return s.out || 0; });
        var chart = new Chart(el, {
          type: 'line',
          data: { labels: labels, datasets: [
            { label: 'Доходи', data: current.map(function (s) { return s.in || 0; }), borderColor: '#38e2a1', backgroundColor: 'rgba(52,211,153,.14)', fill: true, tension: .35, pointRadius: 2, borderWidth: 2 },
            { label: 'Витрати', data: current.map(function (s) { return s.out || 0; }), borderColor: '#fb7185', backgroundColor: 'rgba(251,113,133,.10)', fill: true, tension: .35, pointRadius: 2, borderWidth: 2 },
            { label: 'Прибуток', data: current.map(function (s) { return (s.in || 0) - (s.out || 0); }), borderColor: '#f3a43d', backgroundColor: 'transparent', fill: false, tension: .35, pointRadius: 2, borderWidth: 2 },
            { label: 'Доходи (мін. період)', data: previousIncome, borderColor: '#38e2a1', borderDash: [4, 4], backgroundColor: 'transparent', fill: false, tension: .35, pointRadius: 0, borderWidth: 1.2 },
            { label: 'Витрати (мін. період)', data: previousExpense, borderColor: '#fb7185', borderDash: [4, 4], backgroundColor: 'transparent', fill: false, tension: .35, pointRadius: 0, borderWidth: 1.2 }
          ] },
          options: { responsive: true, maintainAspectRatio: false, interaction: { mode: 'index', intersect: false }, plugins: { legend: { display: false }, tooltip: { callbacks: { label: function (c) { return c.dataset.label + ': ' + fmt(c.parsed.y) + ' ₴'; } } } }, scales: { x: { grid: { color: GRID }, ticks: { color: TICK, maxTicksLimit: 10, font: { size: 10 } } }, y: { grid: { color: GRID }, ticks: { color: TICK, callback: function (v) { return fmt(v); }, font: { size: 10 } } } } }
        });
        var compare = document.getElementById('pnl-compare');
        if (compare) compare.addEventListener('change', function () { chart.data.datasets[3].hidden = !this.checked; chart.data.datasets[4].hidden = !this.checked; chart.update(); });
      }
      if (donutEl && window.Chart) {
        var rows = (d.expense_by_category || []);
        new Chart(donutEl, { type: 'doughnut', data: { labels: rows.map(function (r) { return r.name; }), datasets: [{ data: rows.map(function (r) { return r.total; }), backgroundColor: PIE, borderColor: '#111b2a', borderWidth: 2 }] }, options: { responsive: true, maintainAspectRatio: false, cutout: '67%', plugins: { legend: { display: false }, tooltip: { callbacks: { label: function (c) { return c.label + ': ' + fmt(c.parsed) + ' ₴'; } } } } } });
      }
      document.querySelectorAll('.pnl-day:not(.pnl-day--empty)').forEach(function (day) {
        function showDay() {
          var tip = document.querySelector('.pnl-calendar-tooltip');
          var cal = document.querySelector('.pnl-calendar');
          if (!tip || !cal) return;
          var monthNames = ['', 'січ.', 'лют.', 'бер.', 'кві.', 'тра.', 'чер.', 'лип.', 'сер.', 'вер.', 'жов.', 'лис.', 'гру.'];
          var weekdays = ['нд', 'пн', 'вт', 'ср', 'чт', 'пт', 'сб'];
          var month = Number(cal.dataset.month), year = Number(cal.dataset.year), number = Number(day.dataset.day);
          var date = new Date(year, month - 1, number);
          tip.innerHTML = '<strong>' + number + ' ' + monthNames[month] + ' ' + year + ' (' + weekdays[date.getDay()] + ')</strong><span><i class="is-income"></i>Доходи <b>' + day.dataset.income + '</b></span><span><i class="is-expense"></i>Витрати <b>' + day.dataset.expense + '</b></span><span><i class="is-profit"></i>Прибуток <b>' + day.dataset.profit + '</b></span>';
          tip.hidden = false;
          var left = day.offsetLeft + day.offsetWidth + 7;
          if (left + tip.offsetWidth > cal.clientWidth - 6) left = day.offsetLeft - tip.offsetWidth - 7;
          var top = day.offsetTop + day.offsetHeight + 7;
          if (top + tip.offsetHeight > cal.clientHeight - 8) top = day.offsetTop - tip.offsetHeight - 7;
          tip.style.left = Math.max(6, left) + 'px';
          tip.style.top = Math.max(6, top) + 'px';
        }
        day.addEventListener('mouseenter', showDay);
        day.addEventListener('focus', showDay);
        day.addEventListener('click', function (event) { event.stopPropagation(); document.querySelectorAll('.pnl-day.is-selected').forEach(function (selected) { selected.classList.remove('is-selected'); }); day.classList.add('is-selected'); showDay(); });
        day.addEventListener('mouseleave', function () { var tip = document.querySelector('.pnl-calendar-tooltip'); if (tip && !day.classList.contains('is-selected')) tip.hidden = true; });
      });
      document.addEventListener('click', function () { var tip = document.querySelector('.pnl-calendar-tooltip'); if (tip) tip.hidden = true; document.querySelectorAll('.pnl-day.is-selected').forEach(function (day) { day.classList.remove('is-selected'); }); });
      var flow = document.getElementById('pnl-flow-chart');
      if (flow) renderPnlFlow(flow, d);
      var detailsGrid = document.querySelector('.pnl-details-grid');
      if (detailsGrid) detailsGrid.classList.add('is-list');
      document.querySelectorAll('.pnl-view-toggle button').forEach(function (button) { button.addEventListener('click', function () { document.querySelectorAll('.pnl-view-toggle button').forEach(function (b) { b.classList.toggle('is-active', b === button); }); document.querySelectorAll('[data-view-panel]').forEach(function (panel) { panel.hidden = panel.dataset.viewPanel !== button.dataset.view; }); if (detailsGrid) { detailsGrid.classList.toggle('is-list', button.dataset.view === 'list'); detailsGrid.classList.toggle('is-chart', button.dataset.view === 'chart'); } }); });
      document.querySelectorAll('.pnl-period-btn').forEach(function (button) { button.addEventListener('click', function () { var select = document.getElementById('pnl-period'); if (select) { select.value = button.dataset.period; select.form.submit(); } }); });
      var search = document.querySelector('.pnl-search input');
      if (search) search.addEventListener('input', function () { var needle = this.value.toLowerCase(); document.querySelectorAll('.pnl-table tbody tr').forEach(function (row) { row.hidden = needle && row.textContent.toLowerCase().indexOf(needle) < 0; }); });
    },
  };
})();
