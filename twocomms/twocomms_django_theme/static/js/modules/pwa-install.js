const DISMISS_UNTIL_KEY = 'twc-pwa-install-dismissed-until';
const INSTALLED_KEY = 'twc-pwa-installed';
const PROMPT_DELAY_MS = 9000;
const EXCLUDED_PATH_PREFIXES = [
  '/admin/',
  '/api/',
  '/accounts/',
  '/oauth/',
  '/social/',
  '/orders/',
  '/cart/',
  '/checkout/',
  '/push/',
];
const CONVERSION_ROUTE_NAMES = new Set([
  'blog',
  'blog_category',
  'blog_post',
  'custom_print',
]);

let deferredPrompt = null;
let promptTimer = null;
let registerPromise = null;

function isStandalone() {
  try {
    return window.matchMedia('(display-mode: standalone)').matches || window.navigator.standalone === true;
  } catch (_) {
    return window.navigator.standalone === true;
  }
}

function getDisplayMode() {
  if ((document.referrer || '').startsWith('android-app://')) {
    return 'twa';
  }
  if (window.navigator.standalone === true) {
    return 'standalone-ios';
  }

  const modes = ['browser', 'standalone', 'minimal-ui', 'fullscreen', 'window-controls-overlay'];
  for (const mode of modes) {
    try {
      if (window.matchMedia(`(display-mode: ${mode})`).matches) {
        return mode;
      }
    } catch (_) {
      return 'unknown';
    }
  }

  return 'unknown';
}

function getPlatformLabel() {
  const ua = navigator.userAgent || '';
  if (/android/i.test(ua)) {
    return 'телефон';
  }
  if (/mac os x/i.test(ua)) {
    return 'Mac';
  }
  if (/windows/i.test(ua)) {
    return "комп'ютер";
  }
  return 'пристрій';
}

function isDismissed() {
  try {
    const until = parseInt(window.localStorage.getItem(DISMISS_UNTIL_KEY) || '0', 10);
    return Number.isFinite(until) && until > Date.now();
  } catch (_) {
    return false;
  }
}

function dismissPrompt(days = 14) {
  try {
    window.localStorage.setItem(
      DISMISS_UNTIL_KEY,
      String(Date.now() + days * 24 * 60 * 60 * 1000)
    );
  } catch (_) { }
}

function markInstalled() {
  try {
    window.localStorage.setItem(INSTALLED_KEY, '1');
    window.localStorage.removeItem(DISMISS_UNTIL_KEY);
  } catch (_) { }
}

function wasInstalled() {
  try {
    return window.localStorage.getItem(INSTALLED_KEY) === '1';
  } catch (_) {
    return false;
  }
}

function isExcludedPath() {
  return EXCLUDED_PATH_PREFIXES.some((prefix) => window.location.pathname.startsWith(prefix));
}

function isConversionRoute() {
  const routeName = document.documentElement.getAttribute('data-route-name') || '';
  if (CONVERSION_ROUTE_NAMES.has(routeName)) {
    return true;
  }

  const path = window.location.pathname || '';
  return (
    path === '/custom-print/' ||
    path.includes('/custom-print/') ||
    path === '/blog/' ||
    path.includes('/blog/')
  );
}

function hasAnotherPromptOpen() {
  return Boolean(
    document.querySelector('[data-web-push-prompt]') ||
    document.querySelector('[data-pwa-install-prompt]')
  );
}

function shouldShowPrompt() {
  if (!deferredPrompt || isStandalone() || wasInstalled()) {
    return false;
  }
  if (document.visibilityState === 'hidden' || isDismissed() || isExcludedPath()) {
    return false;
  }
  if (isConversionRoute()) {
    return false;
  }
  if (hasAnotherPromptOpen()) {
    return false;
  }
  return true;
}

function pushAnalytics(eventName, extra = {}) {
  try {
    window.dataLayer = window.dataLayer || [];
    window.dataLayer.push({
      event: eventName,
      pwa_event_name: eventName,
      display_mode: getDisplayMode(),
      route_name: document.documentElement.getAttribute('data-route-name') || '',
      device_class: document.documentElement.getAttribute('data-device-class') || '',
      path: window.location.pathname,
      standalone: isStandalone(),
      ...extra,
    });
  } catch (_) { }
}

function readServiceWorkerUrl() {
  const configNode = document.getElementById('web-push-config');
  if (!configNode) {
    return '/sw.js';
  }

  try {
    const config = JSON.parse(configNode.textContent || '{}');
    return config.serviceWorkerUrl || '/sw.js';
  } catch (_) {
    return '/sw.js';
  }
}

function ensureAppShellWorker() {
  if (!window.isSecureContext || !('serviceWorker' in navigator)) {
    return Promise.resolve(null);
  }

  if (!registerPromise) {
    registerPromise = navigator.serviceWorker.register(readServiceWorkerUrl(), {
      scope: '/',
      updateViaCache: 'none',
    }).catch((error) => {
      registerPromise = null;
      throw error;
    });
  }

  return registerPromise;
}

function removePrompt() {
  const existing = document.querySelector('[data-pwa-install-prompt]');
  if (existing) {
    existing.remove();
  }
}

function updatePromptState(message, tone = 'default') {
  const card = document.querySelector('[data-pwa-install-prompt]');
  if (!card) {
    return;
  }
  const textNode = card.querySelector('[data-pwa-install-copy]');
  if (textNode) {
    textNode.textContent = message;
  }
  card.setAttribute('data-tone', tone);
}

function maybeNotify(message, type = 'info') {
  if (typeof window.showNotification === 'function') {
    try {
      window.showNotification(message, type);
    } catch (_) { }
  }
}

function buildPromptCard() {
  const platformLabel = getPlatformLabel();
  const wrapper = document.createElement('aside');
  wrapper.className = 'web-push-prompt';
  wrapper.setAttribute('data-pwa-install-prompt', '1');
  wrapper.setAttribute('data-tone', 'install');
  wrapper.innerHTML = `
    <div class="web-push-prompt__glow"></div>
    <div class="web-push-prompt__content">
      <div class="web-push-prompt__icon" aria-hidden="true">App</div>
      <div class="web-push-prompt__text">
        <div class="web-push-prompt__title">Встановити TwoComms як застосунок?</div>
        <p class="web-push-prompt__copy" data-pwa-install-copy>
          Додайте TwoComms на ${platformLabel}, щоб відкривати магазин швидше, у standalone-режимі та без зайвих елементів браузера.
        </p>
      </div>
      <div class="web-push-prompt__actions">
        <button type="button" class="web-push-prompt__btn web-push-prompt__btn--ghost" data-pwa-install-dismiss>
          Пізніше
        </button>
        <button type="button" class="web-push-prompt__btn web-push-prompt__btn--primary" data-pwa-install-enable>
          Встановити
        </button>
      </div>
    </div>
  `;
  return wrapper;
}

async function openInstallPrompt() {
  if (!deferredPrompt) {
    return;
  }

  deferredPrompt.prompt();
  const choice = await deferredPrompt.userChoice.catch(() => null);
  const outcome = choice && choice.outcome ? choice.outcome : 'unknown';

  pushAnalytics('pwa_install_prompt_result', {
    install_outcome: outcome,
    install_platform: choice && choice.platform ? choice.platform : 'unknown',
  });

  if (outcome === 'accepted') {
    markInstalled();
    updatePromptState('TwoComms встановлюється. Після завершення відкрийте його з домашнього екрана або Dock.', 'success');
    maybeNotify('TwoComms додано до встановлення.', 'success');
    setTimeout(removePrompt, 2200);
  } else {
    dismissPrompt(21);
    updatePromptState('Встановлення відкладено. Ви зможете повернутися до цього пізніше.', 'warning');
    maybeNotify('Встановлення застосунку відкладено.', 'info');
    setTimeout(removePrompt, 2600);
  }

  deferredPrompt = null;
}

function showInstallPrompt() {
  if (!shouldShowPrompt()) {
    return;
  }

  removePrompt();
  const card = buildPromptCard();
  document.body.appendChild(card);
  pushAnalytics('pwa_install_prompt_shown');

  const dismissButton = card.querySelector('[data-pwa-install-dismiss]');
  dismissButton?.addEventListener('click', () => {
    dismissPrompt(14);
    pushAnalytics('pwa_install_prompt_dismissed');
    removePrompt();
  });

  const installButton = card.querySelector('[data-pwa-install-enable]');
  installButton?.addEventListener('click', async () => {
    installButton.disabled = true;
    updatePromptState('Підготовка системного вікна встановлення…', 'loading');
    try {
      await openInstallPrompt();
    } catch (_) {
      updatePromptState('Не вдалося відкрити системне вікно встановлення.', 'warning');
      installButton.disabled = false;
    }
  });
}

function schedulePrompt() {
  if (promptTimer || !shouldShowPrompt()) {
    return;
  }

  promptTimer = window.setTimeout(() => {
    promptTimer = null;
    showInstallPrompt();
  }, PROMPT_DELAY_MS);
}

function clearPromptTimer() {
  if (promptTimer) {
    window.clearTimeout(promptTimer);
    promptTimer = null;
  }
}

function trackLaunchIfNeeded() {
  const params = new URLSearchParams(window.location.search);
  const source = params.get('source');
  if (isStandalone() || source === 'pwa' || getDisplayMode() !== 'browser') {
    pushAnalytics('pwa_launch', { launch_source: source || getDisplayMode() });
  }
}

export function initPwaInstall() {
  if (!('serviceWorker' in navigator) || typeof window === 'undefined') {
    return;
  }

  ensureAppShellWorker().catch(() => undefined);
  trackLaunchIfNeeded();

  window.addEventListener('beforeinstallprompt', (event) => {
    event.preventDefault();
    deferredPrompt = event;
    pushAnalytics('pwa_install_available');
    schedulePrompt();
  });

  window.addEventListener('appinstalled', () => {
    markInstalled();
    clearPromptTimer();
    removePrompt();
    pushAnalytics('pwa_installed');
  });

  try {
    window.matchMedia('(display-mode: standalone)').addEventListener('change', () => {
      pushAnalytics('pwa_display_mode_changed', { display_mode: getDisplayMode() });
    });
  } catch (_) { }

  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible') {
      schedulePrompt();
    }
  });
}
