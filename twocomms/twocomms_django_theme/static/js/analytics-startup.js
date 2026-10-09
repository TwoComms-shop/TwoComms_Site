/** Start marketing after parsing, with one owner and bounded load retries. */
(function (win, doc) {
  'use strict';
  if (win.__twcAnalyticsStartup || !doc.documentElement) return;
  win.__twcAnalyticsStartup = true;

  var root = doc.documentElement;
  var startup = doc.currentScript;
  var loaderUrl = startup && startup.getAttribute('data-loader-src');
  var gtmId = root.getAttribute('data-gtm-id');
  var events = ['scroll', 'click', 'touchstart', 'pointerdown', 'keydown'];
  var loader = { attempts: 0, pending: false, loaded: false, failed: false };
  var gtm = { attempts: 0, pending: false, loaded: false };
  win.dataLayer = win.dataLayer || [];
  if (!win.gtag) {
    win.gtag = function () { win.dataLayer.push(arguments); };
  }

  function detachWhenReady() {
    if ((loader.loaded || loader.failed) && (!gtmId || gtm.loaded || win.__twcGtmFailed)) {
      events.forEach(function (event) {
        win.removeEventListener(event, start, true);
      });
    }
  }

  function loadScript(state, url, onload, onfailure) {
    if (state.pending || state.loaded || state.attempts >= 2) return;
    state.pending = true;
    state.attempts++;
    var script = doc.createElement('script');
    script.async = true;
    script.src = url;
    script.onload = function () {
      state.pending = false;
      state.loaded = true;
      if (onload) onload();
      detachWhenReady();
    };
    script.onerror = function () {
      state.pending = false;
      if (script.parentNode) script.parentNode.removeChild(script);
      if (state.attempts < 2) {
        // Retry only a definite load failure, never a slow in-flight script.
        win.setTimeout(function () { loadScript(state, url, onload, onfailure); }, 1500);
      } else if (onfailure) {
        onfailure();
      }
    };
    (doc.head || root).appendChild(script);
  }

  function loadLoader() {
    if (loaderUrl) loadScript(loader, loaderUrl, null, function () {
      loader.failed = true;
      detachWhenReady();
    });
  }

  function loadGTM() {
    if (!gtmId || gtm.pending || gtm.loaded || gtm.attempts >= 2) return;
    if (!gtm.attempts) {
      win.dataLayer.push({ 'gtm.start': Date.now(), event: 'gtm.js' });
    }
    loadScript(gtm, 'https://www.googletagmanager.com/gtm.js?id=' + encodeURIComponent(gtmId),
      function () { win.__twcGtmFailed = false; },
      function () {
        win.__twcGtmFailed = true;
        // The loader supplies direct GA/Clarity only if their GTM owner failed.
        win.dispatchEvent(new win.CustomEvent('twc:gtm-failed'));
        loadLoader();
        detachWhenReady();
      });
  }

  function start() {
    loadGTM();
    loadLoader();
  }

  events.forEach(function (event) {
    win.addEventListener(event, start, { passive: true, capture: true });
  });
  var paidLanding = /[?&](gclid|fbclid|ttclid|wbraid|gbraid|msclkid|utm_source|utm_medium|utm_campaign)=/i.test(win.location.search);
  if (paidLanding) {
    start();
  } else {
    // Include organic visitors who read or bounce without clicking. Analytics
    // must not depend on 25–35 seconds of inactivity to improve an audit score.
    win.setTimeout(loadGTM, 0);
    win.setTimeout(loadLoader, 1000);
  }
})(window, document);
