(function () {
  'use strict';

  var root = document.getElementById('language-suggestion');
  if (!root) return;

  var htmlLanguage = (document.documentElement.getAttribute('lang') || '').toLowerCase().split('-')[0];
  if (htmlLanguage === 'uk' || (htmlLanguage !== 'ru' && htmlLanguage !== 'en')) return;

  var userAgent = navigator.userAgent || '';
  if (navigator.webdriver || /bot|crawler|spider|slurp|bingpreview|headless/i.test(userAgent)) return;

  var storageKey = 'twocomms_language_suggestion_v1';
  var cooldownMs = 180 * 24 * 60 * 60 * 1000;
  var storageEnabled = true;
  var previousFocus = null;
  var bodyOverflow = '';
  var open = false;
  var raf = window.requestAnimationFrame || function (callback) { window.setTimeout(callback, 0); };

  function readDecision() {
    try {
      var raw = window.localStorage.getItem(storageKey);
      if (!raw) return null;
      var value = JSON.parse(raw);
      return value && value.ts && (Date.now() - Number(value.ts) < cooldownMs) ? value : null;
    } catch (error) {
      storageEnabled = false;
      return null;
    }
  }

  function remember(state) {
    try {
      window.localStorage.setItem(storageKey, JSON.stringify({ state: state, language: htmlLanguage, ts: Date.now() }));
    } catch (error) { /* private mode or blocked storage: fail closed */ }
  }

  if (readDecision() || !storageEnabled) return;

  var title = root.querySelector('[data-language-suggestion-title]');
  var copy = root.querySelector('[data-language-suggestion-copy]');
  var stay = root.querySelector('[data-language-suggestion-stay]');
  var form = root.querySelector('[data-language-suggestion-form]');
  var next = root.querySelector('input[name="next"]');
  var close = root.querySelector('[data-language-suggestion-close]');
  var backdrop = root.querySelector('[data-language-suggestion-backdrop]');

  var labels = {
    ru: 'Остаться на русском',
    en: 'Stay in English'
  };

  function setCopy() {
    if (title) title.textContent = 'Обрати українську мову';
    if (copy) copy.textContent = 'Так буде простіше орієнтуватися в каталозі, оплаті та доставці. Ви можете залишитися на поточній мові або перейти на українську.';
    if (stay) stay.textContent = labels[htmlLanguage];
  }

  function focusables() {
    return root.querySelectorAll('button:not([disabled]), input:not([disabled]):not([type="hidden"]), a[href]');
  }

  function closePrompt(state) {
    if (!open) return;
    open = false;
    remember(state || 'dismissed');
    root.classList.remove('is-open');
    root.setAttribute('aria-hidden', 'true');
    window.setTimeout(function () { root.hidden = true; }, 180);
    document.body.style.overflow = bodyOverflow;
    if (previousFocus && typeof previousFocus.focus === 'function') previousFocus.focus();
  }

  function showPrompt() {
    if (open || document.visibilityState === 'hidden') return;
    setCopy();
    previousFocus = document.activeElement;
    bodyOverflow = document.body.style.overflow;
    root.hidden = false;
    root.setAttribute('aria-hidden', 'false');
    document.body.style.overflow = 'hidden';
    raf(function () {
      root.classList.add('is-open');
      open = true;
      if (close) close.focus();
    });
  }

  function scheduleVisible() {
    if (document.visibilityState === 'hidden') {
      document.addEventListener('visibilitychange', scheduleVisible, { once: true });
      return;
    }
    if (window.requestIdleCallback) window.requestIdleCallback(showPrompt, { timeout: 1500 });
    else showPrompt();
  }

  window.setTimeout(scheduleVisible, 7000);

  if (close) close.addEventListener('click', function () { closePrompt('dismissed'); });
  if (backdrop) backdrop.addEventListener('click', function () { closePrompt('dismissed'); });
  if (stay) stay.addEventListener('click', function () { closePrompt('stayed'); });

  if (form) {
    form.addEventListener('submit', function (event) {
      event.preventDefault();
      remember('switched');
      if (next) next.value = window.location.href;
      var tokenInput = form.querySelector('input[name="csrfmiddlewaretoken"]');
      var cookieMatch = document.cookie.match(/(?:^|;\s*)csrftoken=([^;]+)/);
      var token = cookieMatch ? decodeURIComponent(cookieMatch[1]) : '';
      if (tokenInput && token) {
        tokenInput.value = token;
        form.submit();
        return;
      }
      fetch('/api/bootstrap/', { credentials: 'same-origin' })
        .then(function () {
          var refreshed = document.cookie.match(/(?:^|;\s*)csrftoken=([^;]+)/);
          if (tokenInput && refreshed) tokenInput.value = decodeURIComponent(refreshed[1]);
          if (tokenInput && tokenInput.value) form.submit();
        })
        .catch(function () { /* keep the current page usable if bootstrap is unavailable */ });
    });
  }

  root.addEventListener('keydown', function (event) {
    if (event.key === 'Escape') { closePrompt('dismissed'); return; }
    if (event.key !== 'Tab' || !open) return;
    var items = focusables();
    if (!items.length) return;
    var first = items[0];
    var last = items[items.length - 1];
    if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
    else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
  });
})();
