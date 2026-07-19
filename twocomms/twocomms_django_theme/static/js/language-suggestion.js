(function () {
  'use strict';

  var root = document.getElementById('language-suggestion');
  if (!root) return;

  var htmlLanguage = (document.documentElement.getAttribute('lang') || '').toLowerCase().split('-')[0];
  if (htmlLanguage === 'uk' || (htmlLanguage !== 'ru' && htmlLanguage !== 'en')) return;

  var userAgent = navigator.userAgent || '';
  if (navigator.webdriver || /bot|crawler|spider|slurp|bingpreview|headless/i.test(userAgent)) return;

  // Store one decision for this browser. Ukrainian is the canonical language
  // and exits above, so it never gets a prompt or a stored decision.
  var storageKey = 'twocomms_language_suggestion_v3';
  var legacyStorageKeys = ['twocomms_language_suggestion_v1', 'twocomms_language_suggestion_v2'];
  var storageEnabled = true;
  var previousFocus = null;
  var bodyOverflow = '';
  var open = false;
  var raf = window.requestAnimationFrame || function (callback) { window.setTimeout(callback, 0); };

  function readStore() {
    try {
      var raw = window.localStorage.getItem(storageKey);
      if (!raw) return null;
      var value = JSON.parse(raw);
      return value && value.version === 3 && value.decision ? value : null;
    } catch (error) {
      storageEnabled = false;
      return null;
    }
  }

  function remember(state) {
    try {
      window.localStorage.setItem(storageKey, JSON.stringify({
        version: 3,
        decision: { state: state, language: htmlLanguage, ts: Date.now() }
      }));
    } catch (error) { /* private mode or blocked storage: fail closed */ }
  }

  function hasLegacyDecision() {
    try {
      for (var index = 0; index < legacyStorageKeys.length; index += 1) {
        var raw = window.localStorage.getItem(legacyStorageKeys[index]);
        if (!raw) continue;
        var value = JSON.parse(raw);
        if ((value && value.state) || (value && value.decisions && Object.keys(value.decisions).length)) return true;
      }
      return false;
    } catch (error) {
      storageEnabled = false;
      return false;
    }
  }

  var store = readStore();
  if ((store && store.decision) || hasLegacyDecision() || !storageEnabled) return;

  var title = root.querySelector('[data-language-suggestion-title]');
  var copy = root.querySelector('[data-language-suggestion-copy]');
  var stay = root.querySelector('[data-language-suggestion-stay]');
  var switchButton = root.querySelector('[data-language-suggestion-switch]');
  var form = root.querySelector('[data-language-suggestion-form]');
  var close = root.querySelector('[data-language-suggestion-close]');
  var backdrop = root.querySelector('[data-language-suggestion-backdrop]');

  var labels = {
    ru: { stay: 'Остаться на русском', title: 'Перейти на украинский?', copy: 'Украинский интерфейс уже готов. Вы можете остаться на русском или перейти на украинский.', switch: 'Перейти на украинский' },
    en: { stay: 'Stay in English', title: 'Switch to Ukrainian?', copy: 'The Ukrainian interface is ready. You can stay in English or switch to Ukrainian.', switch: 'Switch to Ukrainian' }
  };

  function setCopy() {
    var locale = labels[htmlLanguage] || labels.en;
    if (title) title.textContent = locale.title;
    if (copy) copy.textContent = locale.copy;
    if (stay) stay.textContent = locale.stay;
    if (switchButton) switchButton.textContent = locale.switch;
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
