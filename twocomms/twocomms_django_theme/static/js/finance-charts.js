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
      if (flow) {
        function compact(rows) { var top = rows.slice(0, 4), rest = rows.slice(4); if (rest.length) top.push({ name: 'Інші категорії', total: rest.reduce(function (sum, row) { return sum + Number(row.total || 0); }, 0) }); return top; }
        var incomes = compact(d.income_by_category || []);
        var expenses = compact(d.expense_by_category || []);
        var totalIncome = incomes.reduce(function (sum, row) { return sum + Number(row.total || 0); }, 0);
        var totalExpense = expenses.reduce(function (sum, row) { return sum + Number(row.total || 0); }, 0);
        var colors = ['#ff5f89', '#8c78ef', '#36d2a0', '#f4b34e', '#36b9dd'];
        function item(row, color, total) { var pct = total ? (Number(row.total) / total * 100).toFixed(1) : '0.0'; return '<div class="pnl-flow-item"><i style="background:' + color + '"></i><span>' + row.name + '<br><b>' + fmt(row.total) + ' ₴</b> <small>(' + pct + '%)</small></span></div>'; }
        function flowPaths(rows, total, side) { return rows.map(function (row, index) { var y = 32 + index * 50, width = Math.max(7, Math.min(38, Number(row.total) / (total || 1) * 120)); var path = side === 'income' ? 'M 205 ' + y + ' C 300 ' + y + ', 330 135, 420 135' : 'M 580 135 C 670 135, 700 ' + y + ', 795 ' + y; return '<path d="' + path + '" style="stroke-width:' + width.toFixed(1) + 'px" class="pnl-flow-path--' + side + '"/>'; }).join(''); }
        flow.innerHTML = '<div class="pnl-flow-side pnl-flow-side--income">' + (incomes.length ? incomes.map(function (r, i) { return item(r, i ? '#aab6c8' : '#36d2a0', totalIncome); }).join('') : '<span class="pnl-muted">Немає доходів</span>') + '</div><div class="pnl-flow-center"><b>' + fmt(totalIncome) + ' ₴</b><span>Загальні надходження</span></div><div class="pnl-flow-side pnl-flow-side--expense">' + (expenses.length ? expenses.map(function (r, i) { return item(r, colors[i % colors.length], totalExpense); }).join('') : '<span class="pnl-muted">Немає витрат</span>') + '</div><svg class="pnl-flow-lines" viewBox="0 0 1000 300" preserveAspectRatio="none" aria-hidden="true">' + flowPaths(incomes, totalIncome, 'income') + flowPaths(expenses, totalExpense, 'expense') + '</svg>';
      }
      var detailsGrid = document.querySelector('.pnl-details-grid');
      if (detailsGrid) detailsGrid.classList.add('is-list');
      document.querySelectorAll('.pnl-view-toggle button').forEach(function (button) { button.addEventListener('click', function () { document.querySelectorAll('.pnl-view-toggle button').forEach(function (b) { b.classList.toggle('is-active', b === button); }); document.querySelectorAll('[data-view-panel]').forEach(function (panel) { panel.hidden = panel.dataset.viewPanel !== button.dataset.view; }); if (detailsGrid) { detailsGrid.classList.toggle('is-list', button.dataset.view === 'list'); detailsGrid.classList.toggle('is-chart', button.dataset.view === 'chart'); } }); });
      document.querySelectorAll('.pnl-period-btn').forEach(function (button) { button.addEventListener('click', function () { var select = document.getElementById('pnl-period'); if (select) { select.value = button.dataset.period; select.form.submit(); } }); });
      var search = document.querySelector('.pnl-search input');
      if (search) search.addEventListener('input', function () { var needle = this.value.toLowerCase(); document.querySelectorAll('.pnl-table tbody tr').forEach(function (row) { row.hidden = needle && row.textContent.toLowerCase().indexOf(needle) < 0; }); });
    },
  };
})();
