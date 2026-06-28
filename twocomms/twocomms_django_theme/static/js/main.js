import {
  DOMCache,
  debounce,
  scheduleIdle,
  prefersReducedMotion,
  PERF_LITE,
  nextEvt,
  nowTs,
  getCookie,
  escapeHtml
} from './modules/shared.js';
import { PerformanceOptimizer, ImageOptimizer, MobileOptimizer } from './modules/optimizers.js';

// Помечаем, что основной JS инициализирован и можно запускать анимации
document.documentElement.classList.add('js-ready');

function initDeferredInstallPrompts() {
  const boot = () => {
    scheduleIdle(() => {
      if (document.getElementById('web-push-config')) {
        import('./modules/web-push.js')
          .then(({ initWebPush }) => initWebPush())
          .catch(() => { });
      }

      if ('serviceWorker' in navigator) {
        import('./modules/pwa-install.js?v=20260628-conversion-routes')
          .then(({ initPwaInstall }) => initPwaInstall())
          .catch(() => { });
      }
    });
  };

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot, { once: true });
    return;
  }

  boot();
}

initDeferredInstallPrompts();

const ANALYTICS_BRAND_NAME = 'TwoComms';

function isProtectedImageTarget(target) {
  return Boolean(target?.closest?.(
    '[data-twc-image-protected], .home-product-media-link, .product-main-image, .tc-media-hero-img'
  ));
}

function initProtectedImageInteractions() {
  document.addEventListener('contextmenu', (event) => {
    if (isProtectedImageTarget(event.target)) {
      event.preventDefault();
    }
  }, { capture: true });

  document.addEventListener('dragstart', (event) => {
    if (isProtectedImageTarget(event.target)) {
      event.preventDefault();
    }
  }, { capture: true });
}

initProtectedImageInteractions();

function getAnalyticsTrackingContext() {
  if (typeof window.getTrackingContext === 'function') {
    try {
      return window.getTrackingContext() || {};
    } catch (err) {
      if (console && console.debug) {
        console.debug('getTrackingContext error:', err);
      }
    }
  }
  return { fbp: null, fbc: null };
}

function safeGenerateAnalyticsEventId() {
  if (typeof window.generateEventId === 'function') {
    try {
      return window.generateEventId();
    } catch (err) {
      if (console && console.debug) {
        console.debug('generateEventId error:', err);
      }
    }
  }
  return Date.now() + '_' + Math.random().toString(36).substring(2, 11);
}

const GUEST_STORAGE_PREFIX = '_twc_guest_';
const GUEST_STORAGE_FIELDS = ['full_name', 'phone', 'city', 'np_office', 'email'];

function readGuestStorageValue(name) {
  if (!name) {
    return '';
  }
  try {
    if (typeof window.sessionStorage !== 'undefined') {
      return window.sessionStorage.getItem(GUEST_STORAGE_PREFIX + name) || '';
    }
  } catch (_) { }
  return '';
}

function writeGuestStorageValue(name, value) {
  if (!name) {
    return;
  }
  try {
    if (typeof window.sessionStorage === 'undefined') {
      return;
    }
    const key = GUEST_STORAGE_PREFIX + name;
    if (value) {
      window.sessionStorage.setItem(key, value);
    } else {
      window.sessionStorage.removeItem(key);
    }
  } catch (_) { }
}

function attachGuestFormPersistence() {
  const form = document.getElementById('guest-form');
  if (!form) {
    return;
  }
  GUEST_STORAGE_FIELDS.forEach((fieldName) => {
    const input = form.querySelector(`[name="${fieldName}"]`);
    if (!input) {
      return;
    }
    const storedValue = readGuestStorageValue(fieldName);
    if (storedValue && !input.value) {
      input.value = storedValue;
    }
    const handler = () => {
      writeGuestStorageValue(fieldName, (input.value || '').trim());
    };
    input.addEventListener('input', handler);
    input.addEventListener('change', handler);
  });
}

// Обогащение Advanced Matching из форм корзины (auth и guest). Когда
// пользователь вводит/меняет email, телефон, ФИО или город в корзине, мы
// синхронизируем эти значения в #am, чтобы последующие события (AddPaymentInfo,
// InitiateCheckout, Purchase) уходили с максимально полным user_data — даже
// без перезагрузки страницы. Чем больше совпадений, тем выше Event Match
// Quality в Meta Events Manager.
function attachAdvancedMatchingSync() {
  const forms = [document.getElementById('deliveryForm'), document.getElementById('guest-form')].filter(Boolean);
  if (!forms.length) return;
  const am = document.getElementById('am');
  if (!am) return;
  const fieldToAttr = {
    email: 'em',
    phone: 'ph',
    full_name: 'fn',
    city: 'ct',
  };
  forms.forEach((form) => {
    Object.keys(fieldToAttr).forEach((fieldName) => {
      const input = form.querySelector(`[name="${fieldName}"]`);
      if (!input) return;
      const sync = () => {
        const value = (input.value || '').trim();
        if (!value) return;
        const attr = fieldToAttr[fieldName];
        am.dataset[attr] = (fieldName === 'email') ? value.toLowerCase() : value;
      };
      sync();
      input.addEventListener('input', sync);
      input.addEventListener('change', sync);
    });
  });
}

// FIX 2026-06-12: begin_checkout для обычных заказов (наложка / форма).
// Раньше begin_checkout пушился ТОЛЬКО по кнопкам Monobank
// (checkout-mono.js), поэтому заказы через форму доставки вообще не
// давали begin_checkout (4 события против 82 add_to_cart за 30 дней).
// Семантика: пользователь начал заполнять форму доставки в корзине =
// начало оформления. Срабатывает один раз на страницу.
function attachBeginCheckoutOnFormStart() {
  const forms = [document.getElementById('deliveryForm'), document.getElementById('guest-form')].filter(Boolean);
  if (!forms.length) return;
  const readCheckoutPayload = () => {
    const el = document.getElementById('checkout-payload');
    if (!el) return null;
    let contents = [];
    let ids = [];
    try { contents = JSON.parse(decodeURIComponent(el.dataset.contents || '') || '[]'); } catch (_) { contents = []; }
    try { ids = JSON.parse(decodeURIComponent(el.dataset.ids || '') || '[]'); } catch (_) { ids = []; }
    const value = parseFloat(el.dataset.value || '0') || 0;
    const currency = el.dataset.currency || 'UAH';
    let numItems = parseInt(el.dataset.numItems || '0', 10);
    if (Number.isNaN(numItems) || numItems <= 0) {
      numItems = contents.reduce((acc, item) => acc + (item.quantity || 0), 0) || ids.length;
    }
    return { contents, content_ids: ids, value, currency, num_items: numItems };
  };
  let fired = false;
  const fire = () => {
    if (fired) return;
    fired = true;
    try {
      const analytics = readCheckoutPayload();
      if (!analytics || !(analytics.value > 0 || analytics.content_ids.length)) return;
      const eventId = safeGenerateAnalyticsEventId();
      // Не дублируем, если begin_checkout уже ушёл по кнопке Monobank.
      if (window.__twcBeginCheckoutSent) return;
      window.__twcBeginCheckoutSent = true;
      pushBeginCheckoutEvent(analytics, { eventId, eventLabel: 'Checkout form start' });
      if (typeof window.trackEvent === 'function') {
        window.trackEvent('InitiateCheckout', {
          value: analytics.value,
          currency: analytics.currency,
          num_items: analytics.num_items,
          payment_method: 'delivery_form',
          content_ids: analytics.content_ids,
          contents: analytics.contents,
          event_id: eventId,
          __meta: buildMetaWithUserData(eventId),
        });
      }
    } catch (_) { }
  };
  forms.forEach((form) => {
    form.addEventListener('focusin', fire, { once: true });
  });
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', () => {
    try { attachGuestFormPersistence(); } catch (_) { }
    try { attachAdvancedMatchingSync(); } catch (_) { }
    try { attachBeginCheckoutOnFormStart(); } catch (_) { }
  });
} else {
  try { attachGuestFormPersistence(); } catch (_) { }
  try { attachAdvancedMatchingSync(); } catch (_) { }
  try { attachBeginCheckoutOnFormStart(); } catch (_) { }
}

function buildMetaWithUserData(eventId, baseMeta) {
  const meta = Object.assign({}, baseMeta || {});
  if (eventId) {
    meta.event_id = eventId;
  }
  if (typeof window.buildUserDataForEvent !== 'function') {
    return meta;
  }
  try {
    const context = window.buildUserDataForEvent();
    if (!context || typeof context !== 'object') {
      return meta;
    }
    if (context.user_data && Object.keys(context.user_data).length) {
      meta.user_data = context.user_data;
    }
    if (context.external_id) {
      meta.external_id = context.external_id;
    }
    if (context.fbp) {
      meta.fbp = context.fbp;
    }
    if (context.fbc) {
      meta.fbc = context.fbc;
    }
  } catch (err) {
    if (console && console.debug) {
      console.debug('buildUserDataForEvent error:', err);
    }
  }
  return meta;
}

function pushDataLayerEvent(config) {
  if (!config || !config.eventName || !config.eventId || !config.ecommerce || !config.eventModel) {
    return null;
  }
  window.dataLayer = window.dataLayer || [];
  const ctx = getAnalyticsTrackingContext();
  if (!config.eventModel.event_id) {
    config.eventModel.event_id = config.eventId;
  }
  const payload = {
    event: config.eventName,
    event_id: config.eventId,
    fbp: ctx.fbp || null,
    fbc: ctx.fbc || null,
    ecommerce: config.ecommerce,
    eventModel: config.eventModel
  };
  if (config.userData && typeof config.userData === 'object' && Object.keys(config.userData).length) {
    payload.user_data = config.userData;
  }
  if (config.extraFields && typeof config.extraFields === 'object') {
    Object.keys(config.extraFields).forEach(key => {
      const value = config.extraFields[key];
      if (value !== undefined && value !== null) {
        payload[key] = value;
      }
    });
  }
  window.dataLayer.push(payload);
  return payload;
}

function mapContentsToGaItems(items, currency) {
  return (items || []).map(item => {
    const id = item.id || item.offer_id || item.item_id || '';
    if (!id) {
      return null;
    }
    const qty = Number(item.quantity || item.qty || 1) || 1;
    const price = Number(item.price || item.item_price || item.unit_price || 0) || 0;
    return {
      item_id: String(id),
      item_name: item.item_name || item.name || item.product_title || '',
      item_brand: ANALYTICS_BRAND_NAME,
      item_category: item.item_category || item.product_category || '',
      item_variant: (item.variant || item.size || item.item_variant || '').toString().toUpperCase(),
      price: price,
      quantity: qty,
      currency: currency || item.currency || 'UAH'
    };
  }).filter(Boolean);
}

function mapGaItemsToEventItems(items) {
  return (items || []).map(item => ({
    id: item.item_id,
    name: item.item_name,
    price: item.price,
    quantity: item.quantity
  }));
}

function fetchCartAnalyticsSnapshot() {
  return fetch('/cart/items/', {
    headers: { 'X-Requested-With': 'XMLHttpRequest' },
    cache: 'no-store'
  }).then(response => {
    if (!response.ok) {
      throw new Error('cart snapshot failed');
    }
    return response.json();
  });
}

function pushAddToCartEvent(params) {
  const eventId = params.eventId || safeGenerateAnalyticsEventId();
  const currency = params.currency || 'UAH';
  const value = Number(params.value || params.itemPrice * params.quantity || 0) || 0;
  const baseItem = {
    item_id: params.offerId,
    item_name: params.contentName || '',
    item_brand: ANALYTICS_BRAND_NAME,
    item_category: params.contentCategory || '',
    item_variant: (params.variant || '').toString().toUpperCase(),
    price: Number(params.itemPrice || 0) || 0,
    quantity: Number(params.quantity || 1) || 1,
    currency: currency
  };

  const pushSnapshot = snapshot => {
    const snapshotItems = snapshot && Array.isArray(snapshot.items) ? snapshot.items : [];
    const ecommProdid = snapshotItems
      .map(item => item.offer_id || item.id)
      .filter(Boolean);
    const totalValue = Number(snapshot && snapshot.total) || value;

    pushDataLayerEvent({
      eventName: 'add_to_cart',
      eventId: eventId,
      ecommerce: {
        currency: currency,
        value: value,
        items: [baseItem]
      },
      eventModel: {
        event_id: eventId,
        value: value,
        currency: currency,
        content_name: baseItem.item_name,
        items: [{
          id: baseItem.item_id,
          name: baseItem.item_name,
          price: baseItem.price,
          quantity: baseItem.quantity
        }],
        ecomm_prodid: ecommProdid.length ? ecommProdid : [baseItem.item_id],
        ecomm_pagetype: 'cart',
        ecomm_totalvalue: totalValue
      }
    });
  };

  fetchCartAnalyticsSnapshot()
    .then(pushSnapshot)
    .catch(() => pushSnapshot(null));

  return eventId;
}

function pushBeginCheckoutEvent(analytics, options) {
  if (!analytics) {
    return null;
  }
  const eventId = (options && options.eventId) || safeGenerateAnalyticsEventId();
  const currency = analytics.currency || 'UAH';
  let gaItems = mapContentsToGaItems(analytics.contents || [], currency);
  if (!gaItems.length && Array.isArray(analytics.content_ids)) {
    gaItems = analytics.content_ids.map(id => ({
      item_id: String(id),
      item_name: '',
      item_brand: ANALYTICS_BRAND_NAME,
      item_category: '',
      item_variant: '',
      price: 0,
      quantity: 1,
      currency: currency
    }));
  }
  const ecommProdid = Array.isArray(analytics.content_ids) && analytics.content_ids.length
    ? analytics.content_ids
    : gaItems.map(item => item.item_id);

  pushDataLayerEvent({
    eventName: 'begin_checkout',
    eventId: eventId,
    ecommerce: {
      currency: currency,
      value: analytics.value || 0,
      items: gaItems
    },
    eventModel: {
      event_id: eventId,
      value: analytics.value || 0,
      currency: currency,
      event_category: 'Оформлення замовлення',
      event_label: (options && options.eventLabel) || 'TwoComms',
      items: mapGaItemsToEventItems(gaItems),
      ecomm_prodid: ecommProdid,
      ecomm_pagetype: 'cart',
      ecomm_totalvalue: analytics.value || 0
    }
  });
  return eventId;
}

window.__twcAnalytics = window.__twcAnalytics || {};
Object.assign(window.__twcAnalytics, {
  brand: ANALYTICS_BRAND_NAME,
  safeGenerateEventId: safeGenerateAnalyticsEventId,
  getTrackingContext: getAnalyticsTrackingContext,
  pushDataLayerEvent: pushDataLayerEvent,
  mapContentsToGaItems: mapContentsToGaItems,
  mapGaItemsToEventItems: mapGaItemsToEventItems,
  fetchCartAnalyticsSnapshot: fetchCartAnalyticsSnapshot,
  pushAddToCartEvent: pushAddToCartEvent,
  pushBeginCheckoutEvent: pushBeginCheckoutEvent
});

// Экспонируем helpers глобально, чтобы lazy-модули (favorites.js,
// custom-print-configurator.js и т.п.) могли строить Advanced Matching meta
// без дублирования логики.
window.buildMetaWithUserData = buildMetaWithUserData;
window.safeGenerateAnalyticsEventId = safeGenerateAnalyticsEventId;

// ===== ОПТИМИЗАЦИЯ ПРОИЗВОДИТЕЛЬНОСТИ =====
// Анимации появления
// Оптимизированные настройки IntersectionObserver
const observerOptions = {
  threshold: 0.12,
  rootMargin: '0px 0px -10% 0px',
  passive: true
};

const supportsIO = 'IntersectionObserver' in window;
const io = supportsIO ? new IntersectionObserver(e => {
  e.forEach(t => {
    if (t.isIntersecting) {
      t.target.classList.add('visible');
      io.unobserve(t.target);
    }
  });
}, observerOptions) : null;

document.addEventListener('DOMContentLoaded', () => {
  // Инициализация оптимизации изображений переносится в idle, чтобы не блокировать главный поток
  scheduleIdle(() => {
    ImageOptimizer.init();
    MobileOptimizer.initMobileOptimizations();
  });

  const registerRevealTargets = (scope = document) => {
    const basicTargets = scope.querySelectorAll('.reveal, .reveal-fast');
    if (!supportsIO) {
      basicTargets.forEach(el => el.classList.add('visible'));
      return;
    }
    basicTargets.forEach(el => io.observe(el));
  };
  registerRevealTargets();

  // Стаггер-анимация карточек в гриде — по порядку DOM, без измерений
  const gridObserver = supportsIO ? new IntersectionObserver(entries => {
    entries.forEach(entry => {
      if (!entry.isIntersecting) return;
      const grid = entry.target;
      const ordered = Array.from(grid.querySelectorAll('.stagger-item'));

      // Мягкий каскад без дёрганий: шаг зависит от числа карточек,
      // отключается при reduced motion/перф-лайт
      const count = ordered.length || 1;
      const step = (prefersReducedMotion || PERF_LITE)
        ? 0
        : Math.max(50, Math.min(110, Math.floor(900 / count)));
      try { if (window.equalizeCardHeights) window.equalizeCardHeights(); } catch (_) { }
      ordered.forEach((el, i) => {
        el.style.setProperty('--d', (i * step) + 'ms'); // дублируем задержку в CSS (на всякий)
        const revealCard = () => {
          el.classList.add('visible');

          // Анимация цветов товаров - СТРОГО вместе с карточкой
          const colorDots = el.closest('.product-card-wrap').querySelector('.product-card-dots');
          if (colorDots) {
            // Цвета появляются одновременно с карточкой
            colorDots.classList.add('visible');
            // Анимация отдельных цветовых точек
            const dots = colorDots.querySelectorAll('.color-dot');
            dots.forEach((dot, dotIndex) => {
              setTimeout(() => {
                dot.classList.add('visible');
              }, prefersReducedMotion ? 0 : (dotIndex * 60)); // Быстрая анимация точек
            });
          }
        };
        if (step === 0) {
          revealCard();
        } else {
          setTimeout(revealCard, i * step);
        }
      });

      gridObserver.unobserve(grid);
    });
  }, { threshold: .12, rootMargin: '0px 0px -10% 0px' }) : null;
  const grids = document.querySelectorAll('[data-stagger-grid]');
  if (!supportsIO) {
    grids.forEach(grid => {
      grid.querySelectorAll('.stagger-item').forEach(el => el.classList.add('visible'));
    });
  } else {
    grids.forEach(grid => gridObserver.observe(grid));
  }

  requestAnimationFrame(() => {
    document.documentElement.classList.add('reveal-ready');
    if (!supportsIO || prefersReducedMotion) {
      document.querySelectorAll('.reveal, .reveal-fast, .reveal-stagger, .stagger-item').forEach(el => el.classList.add('visible'));
    }
  });

  // Экспортируем функцию для динамически подгруженных карточек (load more)
  window.registerRevealTargets = registerRevealTargets;
});

// ===== Force hide cart/profile on mobile: handled by CSS (base.html media query
// + Bootstrap d-none d-lg-block on header). JS removed 2026-04-20 — было dead-code:
// toggled class .mobile-hidden, у которого нет CSS-правил; видимость уже
// обеспечивается media query. =====

// ===== Корзина (AJAX) =====

const panelState = () => ({
  userShown: !(DOMCache.get('user-panel') || { classList: { contains: () => true } }).classList.contains('d-none'),
  userMobileShown: !(DOMCache.get('user-panel-mobile') || { classList: { contains: () => true } }).classList.contains('d-none'),
  cartShown: !(miniCartPanel() || { classList: { contains: () => true } }).classList.contains('d-none')
});
function updateCartBadge(count) {
  const normalizedCount = Number.isFinite(Number(count)) ? Number(count) : 0;
  setCartSyncEnabled(normalizedCount > 0);
  const n = String(count || 0);
  const desktop = DOMCache.get('cart-count');
  const mobile = DOMCache.get('cart-count-mobile');

  requestAnimationFrame(() => {
    if (desktop) {
      desktop.textContent = n;
      desktop.classList.add('visible');
    }
    if (mobile) {
      mobile.textContent = n;
      mobile.classList.add('visible');
    }
  });
}
window.updateCartBadge = updateCartBadge;

const CART_SYNC_HINT_KEY = 'twc-sync-cart';
const FAVORITES_SYNC_HINT_KEY = 'twc-sync-favs';

function persistSyncHint(key, enabled) {
  try {
    if (!window.localStorage) return;
    if (enabled) {
      window.localStorage.setItem(key, '1');
    } else {
      window.localStorage.removeItem(key);
    }
  } catch (_) { }
}

function setCartSyncEnabled(enabled) {
  const normalized = !!enabled;
  window.__TC_SYNC_CART = normalized;
  persistSyncHint(CART_SYNC_HINT_KEY, normalized);
}
window.setCartSyncEnabled = setCartSyncEnabled;

function setFavoritesSyncEnabled(enabled) {
  const normalized = !!enabled;
  window.__TC_SYNC_FAVS = normalized;
  persistSyncHint(FAVORITES_SYNC_HINT_KEY, normalized);
}
window.setFavoritesSyncEnabled = setFavoritesSyncEnabled;

function refreshCartSummary() {
  return fetch('/cart/summary/', {
    headers: { 'X-Requested-With': 'XMLHttpRequest' },
    cache: 'no-store'
  })
    .then(r => r.ok ? r.json() : null)
    .then(d => { if (d && d.ok && typeof d.count === 'number') { updateCartBadge(d.count); } })
    .catch(() => { });
}
window.refreshCartSummary = refreshCartSummary;

// Функция для обновления счетчика избранного
function updateFavoritesBadge(count) {
  const normalizedCount = Number.isFinite(Number(count)) ? Number(count) : 0;
  setFavoritesSyncEnabled(normalizedCount > 0);
  const n = String(count || 0);
  const desktop = DOMCache.get('favorites-count');
  const mobile = DOMCache.get('favorites-count-mobile');
  const mini = DOMCache.get('favorites-count-mini');
  const favoritesWrapper = DOMCache.query('.favorites-icon-wrapper');
  const mobileIcon = DOMCache.query('a[href*="favorites"] .bottom-nav-icon');

  // Используем requestAnimationFrame для избежания принудительной компоновки
  requestAnimationFrame(() => {
    // Обновляем десктопный счетчик
    if (desktop) {
      desktop.textContent = n;
      desktop.classList.add('visible');

      if (count > 0) {
        // Добавляем класс для анимации когда есть товары
        if (favoritesWrapper) {
          favoritesWrapper.classList.add('has-items');
        }
      } else {
        // Убираем класс когда нет товаров
        if (favoritesWrapper) {
          favoritesWrapper.classList.remove('has-items');
        }
      }
    }

    // Обновляем мобильный счетчик
    if (mobile) {
      mobile.textContent = n;

      if (count > 0) {
        mobile.classList.add('visible');
        if (mobileIcon) {
          mobileIcon.classList.add('has-items');
        }
      } else {
        mobile.classList.remove('visible');
        if (mobileIcon) {
          mobileIcon.classList.remove('has-items');
        }
      }
    }

    // Обновляем счетчик в минипрофиле
    if (mini) {
      mini.textContent = n;

      if (count > 0) {
        mini.classList.add('visible');
      } else {
        mini.classList.remove('visible');
      }
    }
  });
}

// Применение цветов свотчей (включая комбинированные) по data-* атрибутам
function applySwatchColors(root) {
  try {
    const scope = root || document;
    const list = scope.querySelectorAll('.cart-item-swatch, .swatch, .order-item-swatch, .color-dot, .featured-color-dot');
    list.forEach(function (el) {
      const primary = el.getAttribute('data-primary') || '';
      const secondary = el.getAttribute('data-secondary') || '';

      // Используем requestAnimationFrame для избежания принудительной компоновки
      requestAnimationFrame(() => {
        // Устанавливаем CSS-переменные для комбинированных цветов
        if (primary) el.style.setProperty('--primary-color', primary);
        if (secondary && secondary !== 'None') {
          el.style.setProperty('--secondary-color', secondary);
        } else {
          el.style.removeProperty('--secondary-color');
        }

        // Устанавливаем прямой background-color для .swatch элементов
        if (el.classList.contains('swatch') && primary) {
          el.style.backgroundColor = primary;
        }
      });
    });
  } catch (_) { }
}

// Мини‑корзина с кэшированием
function miniCartPanel() {
  if (window.innerWidth < 576) {
    return DOMCache.get('mini-cart-panel-mobile');
  } else {
    return DOMCache.get('mini-cart-panel');
  }
}
// Небольшая защита от мгновенного закрытия при переключении панелей
let uiGuardUntil = 0;
let suppressGlobalCloseUntil = 0;
let suppressNextDocPointerdownUntil = 0; // блокируем ближайший pointerdown от документа (клик по тогглеру)
function showAnimatedPanel(panel) {
  if (!panel) return;
  panel.classList.remove('hiding');
  panel.classList.remove('d-none');
  panel.removeAttribute('inert');
  panel.setAttribute('aria-hidden', 'false');
  const commitOpen = () => {
    panel.classList.add('show');
  };
  if ('requestAnimationFrame' in window) {
    requestAnimationFrame(() => {
      requestAnimationFrame(commitOpen);
    });
  } else {
    setTimeout(commitOpen, 0);
  }
}
function openMiniCart(opts = {}) {
  const { skipRefresh = false } = opts;
  const id = nextEvt();
  const panel = miniCartPanel(); if (!panel) return;
  // Оп‑токен: любое новое действие отменяет старые таймауты/слушатели
  panel._opId = (panel._opId || 0) + 1; const opId = panel._opId;
  if (panel._hideTimeout) { clearTimeout(panel._hideTimeout); panel._hideTimeout = null; }
  panel.classList.remove('hiding');
  // Закрываем открытый мини‑профиль (desktop/mobile), если он был открыт
  [DOMCache.get('user-panel'), DOMCache.get('user-panel-mobile')]
    .forEach(up => { if (up && !up.classList.contains('d-none')) { up.classList.remove('show'); setTimeout(() => up.classList.add('d-none'), 200); } });
  panel.classList.remove('d-none', 'hiding');
  // Мобильный полноэкранный режим
  if (window.innerWidth < 576) {
    panel.classList.add('position-fixed', 'top-0', 'start-0', 'vw-100', 'vh-100', 'rounded-0');
    panel.style.right = '';
    panel.style.top = '0';
  } else {
    panel.classList.remove('position-fixed', 'top-0', 'start-0', 'vw-100', 'vh-100', 'rounded-0');
    panel.style.right = '0';
    panel.style.top = 'calc(100% + 8px)';
  }
  const markOpened = () => {
    panel.classList.add('show');
    uiGuardUntil = Date.now() + 220;
    suppressGlobalCloseUntil = Date.now() + 180;
  };
  if ('requestAnimationFrame' in window) {
    requestAnimationFrame(markOpened);
  } else {
    markOpened();
  }
  if (!skipRefresh) refreshMiniCart();
}
function closeMiniCart(reason) {
  const id = nextEvt();
  const panel = miniCartPanel(); if (!panel) return;
  panel._opId = (panel._opId || 0) + 1; const opId = panel._opId;
  panel.classList.remove('show');
  panel.classList.add('hiding');
  const hideAfter = setTimeout(() => {
    if (opId !== panel._opId) return; // Уже было другое действие
    panel.classList.add('d-none');
    panel.classList.remove('hiding');
  }, 260);
  panel._hideTimeout = hideAfter;
  // если есть transitionend — ускорим скрытие
  panel.addEventListener('transitionend', function onTrEnd(e) {
    if (e.target !== panel) return;
    panel.removeEventListener('transitionend', onTrEnd);
    if (opId !== panel._opId) return; // Не актуально
    clearTimeout(hideAfter);
    panel.classList.add('d-none');
    panel.classList.remove('hiding');
  });
}
function toggleMiniCart() {
  const panel = miniCartPanel(); if (!panel) return;
  if (panel.classList.contains('d-none') || !panel.classList.contains('show')) openMiniCart(); else closeMiniCart();
}

// Mono/Monobank checkout — extracted to modules/checkout-mono.js (Phase 2.1).
// The module is loaded lazily on first detection of a mono/monobank trigger.
window.__twcMono = {
  readGuestStorageValue: readGuestStorageValue,
  buildMetaWithUserData: buildMetaWithUserData,
  safeGenerateAnalyticsEventId: safeGenerateAnalyticsEventId,
};
let __monoModulePromise = null;
function __loadMonoModule() {
  if (!__monoModulePromise) {
    __monoModulePromise = import('./modules/checkout-mono.js?v=20260428-pdp-fit-v2').catch((err) => {
      __monoModulePromise = null;
      throw err;
    });
  }
  return __monoModulePromise;
}
function bindMonoCheckout(scope) {
  const root = scope || document;
  if (!root || typeof root.querySelector !== 'function') return;
  if (!root.querySelector('[data-mono-checkout-trigger]')) return;
  __loadMonoModule().then((m) => { try { m.bindMonoCheckout(root); } catch (_) {} }).catch(() => {});
}
function bindMonobankPay(scope) {
  const root = scope || document;
  if (!root || typeof root.querySelector !== 'function') return;
  if (!root.querySelector('[data-monobank-pay-trigger]')) return;
  __loadMonoModule().then((m) => { try { m.bindMonobankPay(root); } catch (_) {} }).catch(() => {});
}


let miniCartFetchController = null;
let miniCartFetchSeq = 0;

function refreshMiniCart() {
  const panel = miniCartPanel(); if (!panel) return Promise.resolve();
  const content = panel.querySelector('#mini-cart-content') || panel.querySelector('#mini-cart-content-mobile') || panel;
  content.innerHTML = "<div class='text-secondary small'>Завантаження…</div>";

  if (typeof AbortController !== 'undefined') {
    if (miniCartFetchController) {
      try { miniCartFetchController.abort(); } catch (_) { }
    }
    miniCartFetchController = new AbortController();
  } else {
    miniCartFetchController = null;
  }

  const currentSeq = ++miniCartFetchSeq;
  const controller = miniCartFetchController;

  return fetch('/cart/mini/', {
    headers: { 'X-Requested-With': 'XMLHttpRequest' },
    cache: 'no-store',
    signal: controller ? controller.signal : undefined
  })
    .then(r => r.text())
    .then(html => {
      if (currentSeq !== miniCartFetchSeq) return;
      content.innerHTML = html;
      try { applySwatchColors(content); } catch (_) { }
      try { bindMonoCheckout(content); } catch (_) { }
    })
    .catch(err => {
      if (controller && err && err.name === 'AbortError') return;
      if (currentSeq !== miniCartFetchSeq) return;
      content.innerHTML = "<div class='text-danger small'>Не вдалося завантажити кошик</div>";
    })
    .finally(() => {
      if (controller && miniCartFetchController === controller) {
        miniCartFetchController = null;
      }
    });
}
window.refreshMiniCart = refreshMiniCart;
window.openMiniCart = openMiniCart;
window.closeMiniCart = closeMiniCart;
window.toggleMiniCart = toggleMiniCart;

// Делегированный обработчик для кнопки «Продовжити покупки» внутри мини-корзины.
// Inline onclick="closeMiniCart(...)" не работает, потому что main.js загружается
// как ES-модуль (область видимости изолирована). Через data-атрибут — работает
// и в исходной разметке, и при динамической перерисовке через refreshMiniCart.
document.addEventListener('click', function (e) {
  const btn = e.target && e.target.closest && e.target.closest('[data-mini-cart-continue]');
  if (!btn) return;
  e.preventDefault();
  try { closeMiniCart('continue'); } catch (_) {}
});

// Обновляем сводку при загрузке - DEFERRED до после LCP
document.addEventListener('DOMContentLoaded', () => {
  try { bindMonoCheckout(document); } catch (_) { }
  try { bindMonobankPay(document); } catch (_) { }
  const docEl = document.documentElement;
  const routeName = (docEl.dataset.routeName || '').toLowerCase();
  const deviceClass = (docEl.dataset.deviceClass || '').toLowerCase();
  const pageShell = (docEl.dataset.pageShell || '').toLowerCase();
  const isMarketingShell = pageShell === 'marketing';
  const cartBootEvents = routeName === 'home'
    ? ['click', 'pointerdown', 'keydown']
    : (isMarketingShell
      ? ['click', 'pointerdown', 'keydown']
      : ['scroll', 'click', 'touchstart', 'pointerdown', 'keydown']);
  const cartBootDelay = routeName === 'home'
    ? (deviceClass === 'low' ? 3500 : 2500)
    : (isMarketingShell
      ? (deviceClass === 'low' ? 6500 : 4500)
      : 2000);
  
  // PERF: Delay cart/favorites requests until AFTER LCP (1.5s) or user interaction
  // This is critical for mobile PageSpeed TBT score
  const loadCartData = () => {
    // Guard against multiple calls
    if (window.__cartDataLoaded) return;
    window.__cartDataLoaded = true;
    
    // Remove interaction listeners
    cartBootEvents.forEach(evt => {
      window.removeEventListener(evt, loadCartData, { passive: true, capture: true });
    });
    
    scheduleIdle(() => {
      // SSR-маркер: если cart пуст на сервере, не дёргаем /cart/summary/
      if (window.__TC_SYNC_CART !== false) {
        refreshCartSummary();
      }
      if (window.__TC_SYNC_FAVS !== false) {
        fetch('/favorites/count/', { headers: { 'X-Requested-With': 'XMLHttpRequest' } })
          .then(r => r.ok ? r.json() : null)
          .then(d => {
            if (d && d.count !== undefined) {
              updateFavoritesBadge(d.count);
            }
          })
          .catch(() => { });
      }
    });
  };
  
  // Load on user interaction (more aggressive for PageSpeed)
  cartBootEvents.forEach(evt => {
    window.addEventListener(evt, loadCartData, { passive: true, capture: true, once: true });
  });
  
  // Fallback: home gets a longer quiet window to keep LCP clean on weaker devices.
  setTimeout(loadCartData, cartBootDelay);

  // Применим цвета для свотчей на текущей странице
  scheduleIdle(function () { try { applySwatchColors(document); } catch (_) { } });

  // Перемещаем галерею товара в левую колонку и синхронизируем миниатюры
  scheduleIdle(function () {
    const galleryBlock = document.querySelector('.product-gallery-block');
    const carouselEl = document.getElementById('productCarousel');
    if (!(galleryBlock && carouselEl)) return;

    // Функция: есть ли у элемента класс вида col-*
    const hasColClass = (el) => {
      if (!el || !el.classList) return false;
      return Array.from(el.classList).some(c => c.startsWith('col-'));
    };

    // 1) Найдём ближайшую к галерее колонку Bootstrap и её левую "соседнюю" колонку
    let currentCol = galleryBlock.closest('*');
    while (currentCol && !hasColClass(currentCol)) currentCol = currentCol.parentElement;

    let leftCol = null;
    if (currentCol && currentCol.parentElement) {
      // Ищем предыдущий элемент-колонку в пределах той же строки
      let prev = currentCol.previousElementSibling;
      while (prev && !hasColClass(prev)) prev = prev.previousElementSibling;
      if (prev && hasColClass(prev)) leftCol = prev;
    }

    // Если нашли левую колонку — вставим туда галерею и удалим старое изображение
    if (leftCol) {
      const oldImgWrap = leftCol.querySelector('.ratio, img');
      // Вставим галерею в начало левой колонки
      leftCol.insertBefore(galleryBlock, leftCol.firstChild);
      if (oldImgWrap) oldImgWrap.remove();
      // Удалим возможные старые полоски миниатюр в этой колонке (не входящие в нашу галерею)
      Array.from(leftCol.children)
        .filter(el => el !== galleryBlock && !galleryBlock.contains(el) && el.querySelectorAll && el.querySelectorAll('img').length >= 2)
        .forEach(el => el.remove());
    } else {
      // 2) Альтернативная цель: конкретные селекторы
      const tryLeft = document.querySelector('.row .col-12.col-md-5') || document.querySelector('.row .col-md-6');
      if (tryLeft) {
        const old = tryLeft.querySelector('.ratio, img');
        tryLeft.insertBefore(galleryBlock, tryLeft.firstChild);
        if (old) old.remove();
        // Удалим возможные старые полоски миниатюр (дубликаты) в колонке
        Array.from(tryLeft.children)
          .filter(el => el !== galleryBlock && !galleryBlock.contains(el) && el.querySelectorAll && el.querySelectorAll('img').length >= 2)
          .forEach(el => el.remove());
      } else {
        // 3) Фолбэк: заменить контейнер #mainImage, если он есть
        const mainImg = document.getElementById('mainImage');
        let targetHost = null;
        if (mainImg) {
          targetHost = mainImg.closest('.ratio') ? mainImg.closest('.ratio').parentElement : mainImg.parentElement;
        }
        if (targetHost && targetHost.parentElement) {
          targetHost.parentElement.replaceChild(galleryBlock, targetHost);
        }
      }
    }

    // Синхронизация активной миниатюры (подсветка рамкой)
    const thumbButtons = Array.from(document.querySelectorAll('.thumb[data-bs-target="#productCarousel"]'));
    const setActiveThumb = (idx) => {
      thumbButtons.forEach(b => {
        const to = parseInt(b.getAttribute('data-bs-slide-to') || '-1', 10);
        b.classList.toggle('active', to === idx);
      });
    };
    setActiveThumb(0);
    try {
      carouselEl.addEventListener('slid.bs.carousel', (ev) => {
        if (typeof ev.to === 'number') { setActiveThumb(ev.to); }
      });
    } catch (_) { }
    thumbButtons.forEach(b => {
      b.addEventListener('click', () => {
        const to = parseInt(b.getAttribute('data-bs-slide-to') || '-1', 10);
        if (to >= 0) setActiveThumb(to);
      });
    });
  });

  // Переміщення блоку «Кольори»: спочатку ПЕРЕД кнопками «Опис/Розмірна сітка», потім фолбеки
  scheduleIdle(function () {
    const card = document.getElementById('color-picker-card');
    if (!card) return;
    if (card.dataset.placed === '1') return;

    const placeBefore = (node) => {
      if (node && node.parentElement) {
        node.parentElement.insertBefore(card, node);
        card.dataset.placed = '1';
        return true;
      }
      return false;
    };
    const placeAfter = (node) => {
      if (node && node.parentElement) {
        if (node.nextSibling) node.parentElement.insertBefore(card, node.nextSibling);
        else node.parentElement.appendChild(card);
        card.dataset.placed = '1';
        return true;
      }
      return false;
    };

    // A) ПЕРВОЕ: ищем строку с кнопками (id, по .toggle-chip, по тексту)
    let togglesRow = document.getElementById('desc-size-toggles');

    if (!togglesRow) {
      const chips = Array.from(document.querySelectorAll('.toggle-chip'));
      if (chips.length) {
        // общий контейнер для чипов
        let cont = chips[0];
        while (cont && cont.parentElement && cont.tagName !== 'DIV') { cont = cont.parentElement; }
        togglesRow = cont || chips[0].parentElement;
      }
    }
    if (!togglesRow) {
      const btns = Array.from(document.querySelectorAll('button, a, .btn')).filter(el => {
        const t = (el.textContent || '').trim().toLowerCase();
        return t.includes('опис') || t.includes('розмірна сітка') || t.includes('розмірна') || t.includes('size');
      });
      if (btns.length) {
        let cont = btns[0];
        while (cont && cont.parentElement && cont.tagName !== 'DIV') { cont = cont.parentElement; }
        togglesRow = cont || btns[0].parentElement;
      }
    }
    if (togglesRow && placeBefore(togglesRow)) return;

    // B) ВТОРОЕ: если строка кнопок не найдена — ищем контроль размера и ставим ПОСЛЕ него
    const sizeCtrl = document.querySelector('[data-size-picker]') ||
      document.querySelector('select[name="size"]') ||
      document.querySelector('select[name*="size" i]') ||
      document.querySelector('[name="size"]');
    if (sizeCtrl && placeAfter(sizeCtrl)) return;

    // C) ФОЛБЕК: перед панелями опису/сітки
    const panels = document.querySelector('.panel-wrap');
    if (panels && placeBefore(panels)) return;

    // D) DOM может обновляться — наблюдаем и вставляем как только появится строка кнопок
    const observer = new MutationObserver(() => {
      if (card.dataset.placed === '1') { observer.disconnect(); return; }
      const row = document.getElementById('desc-size-toggles') ||
        (document.querySelector('.toggle-chip') && document.querySelector('.toggle-chip').closest('div'));
      if (row && placeBefore(row)) { observer.disconnect(); }
    });
    observer.observe(document.body, { childList: true, subtree: true });
  });

  // Переносимо блоки з «Кольори» у «Новинках» всередину самої карточки (щоб анімація була єдиною)
  (function () {
    const dotsList = Array.from(document.querySelectorAll('.product-card-dots'));
    dotsList.forEach(dots => {
      // Находим ближайшую карточку (попередній сусід включає card)
      let card = dots.previousElementSibling;
      if (card && !card.classList.contains('card')) card = card.closest('.card');
      if (card) {
        card.style.position = card.style.position || 'relative';
        card.appendChild(dots);
      }
    });
  });

  // Тогглер мини‑корзины (и по id, и по data-атрибуту)
  const bindCartToggle = (el) => {
    if (!el) return;
    if (el.dataset.uiBoundCart === '1') return;
    el.dataset.uiBoundCart = '1';
    el.addEventListener('pointerdown', (e) => { suppressNextDocPointerdownUntil = Date.now() + 250; }, { passive: true });
    el.addEventListener('click', (e) => { e.preventDefault(); e.stopPropagation(); toggleMiniCart(); });
  };
  bindCartToggle(document.getElementById('cart-toggle'));
  bindCartToggle(document.getElementById('cart-toggle-mobile'));
  document.querySelectorAll('[data-cart-toggle]').forEach(bindCartToggle);

  // Пользовательская панель (десктоп)
  const userToggle = document.getElementById('user-toggle');
  const userPanel = document.getElementById('user-panel');
  if (userToggle && userPanel) {
    const openUser = () => { const id = nextEvt(); userPanel._opId = (userPanel._opId || 0) + 1; const opId = userPanel._opId; if (userPanel._hideTimeout) { clearTimeout(userPanel._hideTimeout); userPanel._hideTimeout = null; } showAnimatedPanel(userPanel); };
    const closeUser = (reason) => { const id = nextEvt(); userPanel._opId = (userPanel._opId || 0) + 1; const opId = userPanel._opId; userPanel.classList.remove('show'); userPanel.classList.add('hiding'); const t = setTimeout(() => { if (opId !== userPanel._opId) return; userPanel.classList.add('d-none'); userPanel.classList.remove('hiding'); userPanel.setAttribute('inert', ''); userPanel.setAttribute('aria-hidden', 'true'); }, 220); userPanel._hideTimeout = t; userPanel.addEventListener('transitionend', function onEnd(e) { if (e.target !== userPanel) return; userPanel.removeEventListener('transitionend', onEnd); if (opId !== userPanel._opId) return; clearTimeout(t); userPanel.classList.add('d-none'); userPanel.classList.remove('hiding'); userPanel.setAttribute('inert', ''); userPanel.setAttribute('aria-hidden', 'true'); }); };
    if (!userToggle.dataset.uiBoundUser) {
      userToggle.dataset.uiBoundUser = '1';
      userToggle.addEventListener('pointerdown', (e) => { suppressNextDocPointerdownUntil = Date.now() + 250; }, { passive: true });
      userToggle.addEventListener('click', (e) => { const id = nextEvt(); e.preventDefault(); e.stopPropagation(); if (Date.now() < uiGuardUntil) { return; } const cartOpen = miniCartPanel() && !miniCartPanel().classList.contains('d-none'); if (cartOpen) closeMiniCart('userToggle'); if (userPanel.classList.contains('d-none') || !userPanel.classList.contains('show')) { openUser(); } else { closeUser('userToggle'); } suppressGlobalCloseUntil = Date.now() + 220; });
    }
    document.addEventListener('pointerdown', (e) => { const id = nextEvt(); const tgt = e.target; const state = panelState(); const supNext = Date.now() < suppressNextDocPointerdownUntil; const supGlob = Date.now() < suppressGlobalCloseUntil; const outside = !userPanel.contains(tgt) && !userToggle.contains(tgt); if (supNext || supGlob) return; if (userPanel.classList.contains('d-none')) return; if (outside) { closeUser('docOutside'); } }, { passive: true });
    const uc = document.querySelector('[data-user-close]'); if (uc) { uc.addEventListener('click', (e) => { e.preventDefault(); closeUser(); }); }
    document.addEventListener('keydown', (e) => { if (e.key === 'Escape') closeUser(); });
  }

  // Пользовательская панель (мобильная)
  const userToggleMobile = document.getElementById('user-toggle-mobile');
  const userPanelMobile = document.getElementById('user-panel-mobile');
  if (userToggleMobile && userPanelMobile) {
    const openUserMobile = () => { const id = nextEvt(); userPanelMobile._opId = (userPanelMobile._opId || 0) + 1; const opId = userPanelMobile._opId; if (userPanelMobile._hideTimeout) { clearTimeout(userPanelMobile._hideTimeout); userPanelMobile._hideTimeout = null; } showAnimatedPanel(userPanelMobile); };
    const closeUserMobile = (reason) => { const id = nextEvt(); userPanelMobile._opId = (userPanelMobile._opId || 0) + 1; const opId = userPanelMobile._opId; userPanelMobile.classList.remove('show'); userPanelMobile.classList.add('hiding'); const t = setTimeout(() => { if (opId !== userPanelMobile._opId) return; userPanelMobile.classList.add('d-none'); userPanelMobile.classList.remove('hiding'); userPanelMobile.setAttribute('inert', ''); userPanelMobile.setAttribute('aria-hidden', 'true'); }, 220); userPanelMobile._hideTimeout = t; userPanelMobile.addEventListener('transitionend', function onEnd(e) { if (e.target !== userPanelMobile) return; userPanelMobile.removeEventListener('transitionend', onEnd); if (opId !== userPanelMobile._opId) return; clearTimeout(t); userPanelMobile.classList.add('d-none'); userPanelMobile.classList.remove('hiding'); userPanelMobile.setAttribute('inert', ''); userPanelMobile.setAttribute('aria-hidden', 'true'); }); };
    if (!userToggleMobile.dataset.uiBoundUser) {
      userToggleMobile.dataset.uiBoundUser = '1';
      userToggleMobile.addEventListener('pointerdown', (e) => { suppressNextDocPointerdownUntil = Date.now() + 250; }, { passive: true });
      userToggleMobile.addEventListener('click', (e) => { const id = nextEvt(); e.preventDefault(); e.stopPropagation(); if (Date.now() < uiGuardUntil) { return; } const cartOpen = miniCartPanel() && !miniCartPanel().classList.contains('d-none'); if (cartOpen) closeMiniCart('userToggleMobile'); if (userPanelMobile.classList.contains('d-none') || !userPanelMobile.classList.contains('show')) { openUserMobile(); } else { closeUserMobile('userToggleMobile'); } suppressGlobalCloseUntil = Date.now() + 220; });
    }
    document.addEventListener('pointerdown', (e) => { const id = nextEvt(); const tgt = e.target; const state = panelState(); const supNext = Date.now() < suppressNextDocPointerdownUntil; const supGlob = Date.now() < suppressGlobalCloseUntil; const outside = !userPanelMobile.contains(tgt) && !userToggleMobile.contains(tgt); if (supNext || supGlob) return; if (userPanelMobile.classList.contains('d-none')) return; if (outside) { closeUserMobile('docOutside'); } }, { passive: true });
    const ucMobile = userPanelMobile.querySelector('[data-user-close-mobile]'); if (ucMobile) { ucMobile.addEventListener('click', (e) => { e.preventDefault(); closeUserMobile(); }); }
    document.addEventListener('keydown', (e) => { if (e.key === 'Escape') closeUserMobile(); });
  }

  // Кнопка закрытия мини‑кошика
  const hookClose = () => {
    const c = document.querySelector('[data-cart-close]');
    const cMobile = document.querySelector('[data-cart-close-mobile]');
    if (c) { c.addEventListener('click', (e) => { e.preventDefault(); closeMiniCart(); }); }
    if (cMobile) { cMobile.addEventListener('click', (e) => { e.preventDefault(); closeMiniCart(); }); }
  };
  hookClose();

  // Закрытие по клику снаружи
  document.addEventListener('pointerdown', (e) => {
    const id = nextEvt();
    const supNext = Date.now() < suppressNextDocPointerdownUntil;
    if (supNext) { return; }
    const supGuard = Date.now() < uiGuardUntil;
    const supGlob = Date.now() < suppressGlobalCloseUntil;
    const panel = miniCartPanel();
    const toggle = window.innerWidth < 576 ?
      document.getElementById('cart-toggle-mobile') :
      document.getElementById('cart-toggle') || document.querySelector('[data-cart-toggle]');
    if (!panel) return;
    if (panel.classList.contains('d-none')) return;
    const tgt = e.target;
    const outside = !panel.contains(tgt) && (!toggle || !toggle.contains(tgt));
    if (supGuard || supGlob) return;
    if (outside) {
      closeMiniCart('docOutside');
    }
  }, { passive: true });
  // Закрытие по ESC
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape') closeMiniCart(); });

  // Адаптация при ресайзе
  window.addEventListener('resize', debounce(() => {
    const panel = miniCartPanel();
    if (panel && !panel.classList.contains('d-none')) {
      // пересчитать позиционирование, сохраняя анимационные классы
      const wasShown = panel.classList.contains('show');
      if (wasShown) panel.classList.remove('show');
      // режим позиционирования
      if (window.innerWidth < 576) {
        panel.classList.add('position-fixed', 'top-0', 'start-0', 'vw-100', 'vh-100', 'rounded-0');
        panel.style.right = '';
        panel.style.top = '0';
      } else {
        panel.classList.remove('position-fixed', 'top-0', 'start-0', 'vw-100', 'vh-100', 'rounded-0');
        panel.style.right = '0';
        panel.style.top = 'calc(100% + 8px)';
      }
      if (wasShown) {
        if ('requestAnimationFrame' in window) {
          requestAnimationFrame(() => panel.classList.add('show'));
        } else {
          panel.classList.add('show');
        }
      }
    }
  }, 150));

  // ===== Мобильное нижнее меню: скрытие/показ по скроллу, фокусу и свайпу =====
  //
  // Phase 22h (2026-05-13) — refactored. The previous implementation gated
  // hide-on-scroll behind `!isSmallScreen && deviceClass === 'high'`, which
  // disabled the dock on every actual mobile device after commit f8320893
  // ("calm mobile dock"). User-visible result: the dock stayed pinned and
  // obscured content while scrolling. Rewritten with three goals:
  //   1. Enable on ALL mobile widths (≤ 991 px, matching the CSS breakpoint).
  //   2. Replace the momentum/cooldown/threshold machinery with a clean
  //      direction-change accumulator — instant feedback, no flicker, no
  //      jank. Always-show rules at the top and near the bottom keep the
  //      dock available where users actually need it.
  //   3. Keep focus-on-input hiding, touch swipe gestures and the
  //      first-session hint-wiggle — those are good UX patterns.
  (function () {
    const bottomNav = document.querySelector('.bottom-nav');
    if (!bottomNav) return;

    // Media-query the breakpoint instead of hard-coding 768 px so a single
    // source of truth (the CSS `@media (min-width: 992px)` that hides the
    // dock on desktop) drives the JS too. matchMedia is live — if the user
    // resizes (Chrome DevTools mobile emulator), the listener flips state.
    const mq = window.matchMedia('(max-width: 991.98px)');

    // Tunables. Kept named so the next maintainer can grok the UX intent
    // without re-deriving thresholds from scroll math.
    const SHOW_AT_TOP_PX      = 80;   // Always visible within first viewport.
    const HIDE_NEAR_BOTTOM_PX = 260;  // Phase 22i — hide near footer so the
                                      // language switcher and footer links
                                      // are not covered by the dock. 260 px
                                      // ≈ height of footer brand block +
                                      // language switcher (so the dock
                                      // disappears the moment the user
                                      // starts to see the footer signature).
    const HIDE_AFTER_DOWN_PX  = 24;   // Cumulative scroll-down delta to hide.
    const SHOW_AFTER_UP_PX    = 12;   // Cumulative scroll-up delta to reveal.
    const MICRO_NOISE_PX      = 1;    // Sub-pixel jitter is ignored.
    const HINT_WIGGLE_DELAY   = 1200; // First-load nudge after LCP settles.

    let hidden = false;
    let hintShown = sessionStorage.getItem('bottom-nav-hint') === '1';
    let lastScrollY = PerformanceOptimizer.getScrollY();
    let downAccum = 0;
    let upAccum = 0;
    let touchStartY = null;
    let touchStartX = null;

    const setHidden = (value) => {
      if (hidden === value) return;
      hidden = value;
      // No batching: a single classList toggle inside an rAF tick is
      // already paint-coalesced, and batching through the optimizer
      // delayed the toggle by another frame (visible lag on real devices).
      bottomNav.classList.toggle('bottom-nav--hidden', hidden);
    };

    const showHint = () => {
      if (hintShown || prefersReducedMotion || PERF_LITE) {
        hintShown = true;
        return;
      }
      bottomNav.classList.add('hint-wiggle');
      setTimeout(() => bottomNav.classList.remove('hint-wiggle'), 950);
      sessionStorage.setItem('bottom-nav-hint', '1');
      hintShown = true;
    };

    const resetAccumulators = () => {
      downAccum = 0;
      upAccum = 0;
    };

    const handleScroll = (currentY) => {
      const dy = currentY - lastScrollY;
      lastScrollY = currentY;
      if (Math.abs(dy) < MICRO_NOISE_PX) return;

      // Hard overrides — these always win against momentum.
      if (currentY <= SHOW_AT_TOP_PX) {
        resetAccumulators();
        setHidden(false);
        return;
      }
      // Phase 22i — when the user reaches the footer area, hide the dock so
      // the language switcher and footer links are accessible. This inverts
      // the previous "always show near bottom" rule which obscured them.
      const docH = document.documentElement.scrollHeight;
      const viewH = window.innerHeight;
      if (currentY + viewH >= docH - HIDE_NEAR_BOTTOM_PX) {
        resetAccumulators();
        setHidden(true);
        return;
      }

      // Direction-change accumulator. The first scroll in a new direction
      // resets the opposite accumulator, so a user who scrolls down 200 px
      // then up 15 px sees the dock instantly — no momentum-debt to pay off.
      if (dy > 0) {
        downAccum += dy;
        upAccum = 0;
        if (!hidden && downAccum >= HIDE_AFTER_DOWN_PX) setHidden(true);
      } else {
        upAccum -= dy;
        downAccum = 0;
        if (hidden && upAccum >= SHOW_AFTER_UP_PX) setHidden(false);
      }
    };

    const attachScrollListener = () => {
      // PerformanceOptimizer.onScrollChange is already rAF-throttled inside
      // initScrollOptimization() and shared with other features, so we plug
      // into it instead of adding a second scroll listener.
      PerformanceOptimizer.onScrollChange = (currentY) => handleScroll(currentY);
      PerformanceOptimizer.initScrollOptimization();
    };

    const detachScrollListener = () => {
      // No formal teardown API in the optimizer; setting a no-op is enough
      // because no other feature shares this exact callback at the moment.
      PerformanceOptimizer.onScrollChange = () => {};
      setHidden(false);
      resetAccumulators();
    };

    const syncMode = () => {
      if (mq.matches) {
        bottomNav.dataset.scrollMode = 'adaptive';
        attachScrollListener();
        setTimeout(showHint, HINT_WIGGLE_DELAY);
      } else {
        bottomNav.dataset.scrollMode = 'desktop';
        detachScrollListener();
      }
    };

    if (typeof mq.addEventListener === 'function') {
      mq.addEventListener('change', syncMode);
    } else if (typeof mq.addListener === 'function') {
      mq.addListener(syncMode); // Safari < 14
    }
    syncMode();

    // Focus on a text input — hide so the on-screen keyboard doesn't fight
    // the dock for space. Restore on blur and re-show the hint if the user
    // has never seen it.
    const isTextField = (el) => el && (
      el.tagName === 'INPUT' || el.tagName === 'TEXTAREA' || el.isContentEditable
    );
    document.addEventListener('focusin', (e) => {
      if (isTextField(e.target)) setHidden(true);
    });
    document.addEventListener('focusout', (e) => {
      if (isTextField(e.target)) {
        setHidden(false);
        if (mq.matches) showHint();
      }
    });

    // Explicit vertical swipe ON the dock itself — this lets users tuck it
    // away or pull it back deliberately. Threshold is intentionally high
    // (40 px + 2× horizontal) so a horizontal tab-swap inside the dock
    // never collapses it accidentally.
    const onTouchStart = (e) => {
      const t = e.touches ? e.touches[0] : e;
      touchStartY = t.clientY;
      touchStartX = t.clientX;
    };
    const onTouchEnd = (e) => {
      if (touchStartY == null) return;
      const t = (e.changedTouches && e.changedTouches[0]) || e;
      const dy = (t.clientY - touchStartY) || 0;
      const dx = (t.clientX - touchStartX) || 0;
      touchStartY = touchStartX = null;
      if (Math.abs(dy) > 40 && Math.abs(dy) > Math.abs(dx) * 2) {
        setHidden(dy > 0);
        showHint();
      }
    };
    bottomNav.addEventListener('touchstart', onTouchStart, { passive: true });
    bottomNav.addEventListener('touchend', onTouchEnd, { passive: true });
  })();
});

// Runtime diagnostics removed for production

// ===== Авто-оптимизация тяжёлых эффектов без изменения вида =====
document.addEventListener('DOMContentLoaded', function () {
  // На `data-device-class="low"` те же эффекты уже глобально killed статическим CSS
  // (Phase 3.2). Runtime-релаксация тут лишь включала бы их обратно с inline !important,
  // перекрывая CSS. Поэтому на low — просто выходим.
  const _dc = (document.documentElement.dataset.deviceClass || '').toLowerCase();
  if (_dc === 'low') return;
  if (!(PERF_LITE || prefersReducedMotion)) return;
  scheduleIdle(() => {
    const candidateSelectors = [
      '.hero.bg-hero', '.bottom-nav', '#mini-cart-panel-mobile', '#user-panel-mobile',
      '.featured-bg-unified', '.categories-bg-unified', '.card.product',
      '[class*="particles" i]', '[class*="spark" i]', '[class*="glow" i]'
    ];
    const unique = new Set();
    const candidates = [];
    candidateSelectors.forEach(sel => {
      document.querySelectorAll(sel).forEach(el => {
        if (!el || unique.has(el)) return; unique.add(el); candidates.push(el);
      });
    });

    const heavyMeta = [];
    const metaMap = new Map();
    candidates.forEach(el => {
      try {
        const cs = DOMCache.getComputedStyle(el);
        const hasBackdrop = (cs.backdropFilter && cs.backdropFilter !== 'none') || (cs.webkitBackdropFilter && cs.webkitBackdropFilter !== 'none');
        const hasBlur = (cs.filter || '').includes('blur');
        const hasBigShadow = (cs.boxShadow || '').includes('px');
        const isAnimatedInf = (cs.animationIterationCount || '').includes('infinite');
        if (hasBackdrop || hasBlur || hasBigShadow || isAnimatedInf) {
          const meta = { el, hasBackdrop, hasInfiniteAnim: isAnimatedInf };
          heavyMeta.push(meta);
          metaMap.set(el, meta);
        }
      } catch (_) { }
    });

    const animatedHeavyMeta = heavyMeta.filter(({ hasInfiniteAnim }) => hasInfiniteAnim);
    if (!animatedHeavyMeta.length) return;

    let relaxTimer = null;
    let relaxed = false;
    function relaxHeavy() {
      if (relaxed) return; relaxed = true;
      animatedHeavyMeta.forEach(({ el, hasInfiniteAnim }) => {
        try {
          if (hasInfiniteAnim) {
            el.style.setProperty('animation-play-state', 'paused', 'important');
          }
        } catch (_) { }
      });
    }
    function restoreHeavy() {
      if (!relaxed) return; relaxed = false;
      animatedHeavyMeta.forEach(({ el }) => {
        try {
          el.style.removeProperty('animation-play-state');
        } catch (_) { }
      });
    }
    function onScroll() {
      relaxHeavy();
      if (relaxTimer) clearTimeout(relaxTimer);
      relaxTimer = setTimeout(restoreHeavy, 350);
    }
    window.addEventListener('scroll', onScroll, { passive: true });

    if ('IntersectionObserver' in window) {
      const io = new IntersectionObserver(entries => {
        PerformanceOptimizer.batchDOMOperations(
          entries.map(entry => () => {
            const meta = metaMap.get(entry.target);
            if (!meta || !meta.hasInfiniteAnim) return;
            try {
              entry.target.style.setProperty('animation-play-state', entry.isIntersecting ? 'running' : 'paused', 'important');
            } catch (_) { }
          })
        );
      }, { threshold: 0.05 });
      animatedHeavyMeta.forEach(({ el }) => { try { io.observe(el); } catch (_) { } });
    }
  });
});


// Делегирование клика "добавить в корзину"
function getQtyFromTrigger(btn) {
  if (!btn || typeof btn.closest !== 'function') {
    return null;
  }
  let scope = btn.closest('[data-qty]');
  let qtyInput = scope ? scope.querySelector('input[type="number"]') : null;
  if (!qtyInput) {
    qtyInput = document.getElementById('qty');
  }
  if (!qtyInput) {
    return null;
  }
  const parsed = parseInt(qtyInput.value || '1', 10);
  if (!Number.isNaN(parsed) && parsed > 0) {
    return parsed;
  }
  return null;
}

function trackAddToCartAnalytics(serverData, triggerButton, fallbackQty) {
  try {
    if (!window.trackEvent || !serverData || !serverData.ok) {
      return;
    }
    const analyticsItem = serverData.item || null;
    const productId = (analyticsItem && analyticsItem.product_id) || (triggerButton && triggerButton.getAttribute && triggerButton.getAttribute('data-add-to-cart'));
    if (!productId) {
      return;
    }
    const productContainer = document.getElementById('product-detail-container');
    const productAnalyticsEl = document.getElementById('product-analytics-payload');
    let offerId = analyticsItem && analyticsItem.offer_id;
    if (!offerId && productContainer) {
      offerId = productContainer.getAttribute('data-current-offer-id');
    }
    if (!offerId) {
      offerId = 'TC-' + String(productId) + '-default-S';
    }

    let quantity = Number(analyticsItem && analyticsItem.quantity);
    if (!Number.isFinite(quantity) || quantity <= 0) {
      if (typeof fallbackQty === 'number' && fallbackQty > 0) {
        quantity = fallbackQty;
      } else {
        const qtyFromDom = getQtyFromTrigger(triggerButton);
        if (qtyFromDom) {
          quantity = qtyFromDom;
        }
      }
    }
    if (!Number.isFinite(quantity) || quantity <= 0) {
      quantity = 1;
    }

    let fallbackPrice = triggerButton ? parseFloat(triggerButton.getAttribute('data-product-price') || '0') : NaN;
    if ((!Number.isFinite(fallbackPrice) || fallbackPrice <= 0) && productAnalyticsEl) {
      const datasetPrice = parseFloat(productAnalyticsEl.dataset.price || '0');
      if (Number.isFinite(datasetPrice) && datasetPrice >= 0) {
        fallbackPrice = datasetPrice;
      }
    }
    let itemPrice = Number(analyticsItem && analyticsItem.item_price);
    if (!Number.isFinite(itemPrice) || itemPrice < 0) {
      itemPrice = Number.isFinite(fallbackPrice) && fallbackPrice >= 0 ? fallbackPrice : 0;
    }

    let value = Number(analyticsItem && analyticsItem.value);
    if (!Number.isFinite(value) || value < 0) {
      value = itemPrice * quantity;
    }
    const currency = (analyticsItem && analyticsItem.currency) || 'UAH';

    let contentName = analyticsItem && analyticsItem.product_title;
    if (!contentName && productAnalyticsEl) {
      contentName = productAnalyticsEl.dataset.title || '';
    }
    if (!contentName && triggerButton) {
      contentName = triggerButton.getAttribute('data-product-name') || '';
    }

    let contentCategory = analyticsItem && analyticsItem.product_category;
    if (!contentCategory && productAnalyticsEl) {
      contentCategory = productAnalyticsEl.dataset.category || '';
    }
    if (!contentCategory && triggerButton) {
      contentCategory = triggerButton.getAttribute('data-product-category') || '';
    }

    let variantLabel = (analyticsItem && analyticsItem.size) ? analyticsItem.size : '';
    if (!variantLabel) {
      const selectedSize = document.querySelector('input[name="size"]:checked');
      if (selectedSize && selectedSize.value) {
        variantLabel = selectedSize.value;
      }
    }
    const fitLabel = (analyticsItem && (analyticsItem.fit_option_label || analyticsItem.fit_label)) || '';
    if (fitLabel) {
      variantLabel = variantLabel ? `${variantLabel} / ${fitLabel}` : fitLabel;
    }
    const payload = {
      content_ids: [offerId],
      content_type: 'product',
      value: value,
      currency: currency,
      num_items: quantity,
      contents: [{
        id: offerId,
        quantity: quantity,
        item_price: itemPrice
      }]
    };

    if (contentName) {
      payload.content_name = contentName;
      payload.contents[0].item_name = contentName;
    }
    if (contentCategory) {
      payload.content_category = contentCategory;
      payload.contents[0].item_category = contentCategory;
    }
    payload.contents[0].brand = 'TwoComms';
    const eventId = safeGenerateAnalyticsEventId();
    payload.event_id = eventId;
    payload.__meta = buildMetaWithUserData(eventId, payload.__meta);

    window.trackEvent('AddToCart', payload);
    try {
      if (window.__twcAnalytics && typeof window.__twcAnalytics.pushAddToCartEvent === 'function') {
        window.__twcAnalytics.pushAddToCartEvent({
          eventId,
          offerId,
          contentName,
          contentCategory,
          currency,
          itemPrice,
          quantity,
          value,
          variant: variantLabel
        });
      }
    } catch (err) {
      if (console && console.debug) {
        console.debug('AddToCart dataLayer error:', err);
      }
    }
  } catch (err) {
    if (console && console.debug) {
      console.debug('AddToCart analytics error:', err);
    }
  }
}
window.__twcTrackAddToCart = trackAddToCartAnalytics;

document.addEventListener('click', (e) => {
  const btn = e.target.closest('[data-add-to-cart]');
  if (!btn) return;
  // Если на кнопке есть inline-обработчик (AddToCart), не дублируем запрос
  if (btn.hasAttribute('onclick')) return;
  e.preventDefault();
  const productId = btn.getAttribute('data-add-to-cart');
  const sizeInput = document.querySelector('input[name="size"]:checked');
  const size = sizeInput ? sizeInput.value : '';
  const qtyFromDom = getQtyFromTrigger(btn);
  const qty = qtyFromDom && qtyFromDom > 0 ? qtyFromDom : 1;

  // Получаем выбранный цвет
  let colorVariantId = null;
  const activeColorSwatch = document.querySelector('#color-picker .color-swatch.active');
  if (activeColorSwatch) {
    colorVariantId = activeColorSwatch.getAttribute('data-variant');
  }

  const normalizedSize = size ? String(size).toUpperCase() : '';
  const body = new URLSearchParams({ product_id: productId, size: normalizedSize, qty: String(qty) });
  if (colorVariantId) {
    body.append('color_variant_id', colorVariantId);
  }
  const fitInput = document.querySelector('input[name="fit_option"]:checked');
  if (fitInput && fitInput.value) {
    body.append('fit_option', fitInput.value);
  }

  // Открываем мини-корзину сразу, чтобы пользователь видел текущее состояние
  try { openMiniCart({ skipRefresh: true }); } catch (_) { }

  // Add с одноразовым self-heal при CSRF 403 (застарілий host-only cookie).
  const addToCartRequest = (isRetry) => fetch('/cart/add/', {
    method: 'POST',
    headers: {
      'X-CSRFToken': getCookie('csrftoken'),
      'X-Requested-With': 'XMLHttpRequest',
      'Content-Type': 'application/x-www-form-urlencoded;charset=UTF-8'
    },
    body
  }).then(r => {
    if (r.status === 403 && !isRetry) {
      // Прибираємо застарілий host-only csrftoken і пробуємо ще раз.
      try { document.cookie = 'csrftoken=; expires=Thu, 01 Jan 1970 00:00:00 GMT; path=/'; } catch (_) { }
      return addToCartRequest(true);
    }
    if (!r.ok) { return Promise.reject(new Error('HTTP ' + r.status)); }
    return r.json();
  });

  const handleAddFailure = () => {
    // Ніколи не лишаємо міні-кошик у стані нескінченного завантаження:
    // оновлюємо його реальним станом (refreshMiniCart прибере спінер).
    try {
      const p = refreshMiniCart();
      if (p && p.then) { p.then(() => { try { openMiniCart({ skipRefresh: true }); } catch (_) { } }).catch(() => { }); }
    } catch (_) { }
    btn.classList.add('btn-danger');
    setTimeout(() => btn.classList.remove('btn-danger'), 600);
  };

  addToCartRequest(false)
    .then(d => {
      if (d && d.ok) {
        if (typeof d.count === 'number') { updateCartBadge(d.count); }
        const miniUpdate = refreshMiniCart();
        miniUpdate
          .then(() => { openMiniCart({ skipRefresh: true }); })
          .catch(() => { openMiniCart({ skipRefresh: true }); })
          .finally(() => { refreshCartSummary(); });

        // Отправляем событие обновления корзины для страницы корзины
        try {
          document.dispatchEvent(new CustomEvent('cartUpdated', { detail: { action: 'add', productId: productId } }));
        } catch (_) { }

        // Небольшой визуальный отклик
        btn.classList.add('btn-success');
        setTimeout(() => btn.classList.remove('btn-success'), 400);

        trackAddToCartAnalytics(d, btn, qty);
      } else {
        handleAddFailure();
      }
    })
    .catch(() => {
      handleAddFailure();
    });
});

// ====== PRODUCT DETAIL: цвета и галерея (lazy — modules/product-gallery.js) ======
document.addEventListener('DOMContentLoaded', function () {
  if (!document.getElementById('productCarousel')) return;
  if (!document.getElementById('variant-data') || !document.getElementById('color-picker')) return;
  import('./modules/product-gallery.js')
    .then((m) => { try { m.initProductGallery(); } catch (_) {} })
    .catch(() => {});
});

// ====== CONTACTS: показать телефон ======
document.addEventListener('DOMContentLoaded', function () {
  const btn = document.getElementById('show-phone-btn'); const phone = document.getElementById('phone-number'); if (btn && phone) { btn.addEventListener('click', () => { phone.style.display = 'inline-block'; btn.style.display = 'none'; }); }
});

// ===== ИЗБРАННЫЕ ТОВАРЫ: глобальные стабы + lazy-loader (Phase 2.1 ext) =====
// Реальная реализация живёт в modules/favorites.js и грузится по первому же
// клику heart-кнопки или первому IO-событию для .favorite-btn в viewport.
// Inline `onclick="toggleFavorite(...)"` в шаблонах продолжает работать:
// стабы сохраняют синхронный вызов, затем dynamic-import + делегация.
window.updateFavoritesBadge = updateFavoritesBadge;

let __favModulePromise = null;
function __loadFavModule() {
  if (!__favModulePromise) {
    __favModulePromise = import('./modules/favorites.js').catch((err) => {
      __favModulePromise = null;
      throw err;
    });
  }
  return __favModulePromise;
}

function toggleFavorite(productId, button) {
  if (button) button.classList.add('loading');
  __loadFavModule()
    .then((m) => { try { m.toggleFavorite(productId, button); } catch (_) { if (button) button.classList.remove('loading'); } })
    .catch(() => { if (button) button.classList.remove('loading'); });
}

function checkFavoriteStatus(productId, button) {
  __loadFavModule()
    .then((m) => { try { m.checkFavoriteStatus(productId, button); } catch (_) {} })
    .catch(() => {});
}

function showNotification(message, type = 'info') {
  __loadFavModule()
    .then((m) => { try { m.showNotification(message, type); } catch (_) {} })
    .catch(() => {});
}
window.toggleFavorite = toggleFavorite;
window.checkFavoriteStatus = checkFavoriteStatus;
window.showNotification = showNotification;

// Инициализация статуса избранного — ленивая проверка только для видимых кнопок.
document.addEventListener('DOMContentLoaded', function () {
  const favoriteButtons = document.querySelectorAll('.favorite-btn');
  if (!favoriteButtons.length) return;
  if ('IntersectionObserver' in window) {
    const io = new IntersectionObserver((entries) => {
      entries.forEach((entry) => {
        if (!entry.isIntersecting) return;
        const button = entry.target;
        const productId = button.getAttribute('data-product-id');
        if (productId) { checkFavoriteStatus(productId, button); }
        io.unobserve(button);
      });
    }, { root: null, rootMargin: '100px 0px', threshold: 0.01 });
    favoriteButtons.forEach((btn) => io.observe(btn));
  } else {
    favoriteButtons.forEach((button) => {
      const productId = button.getAttribute('data-product-id');
      if (productId) { checkFavoriteStatus(productId, button); }
    });
  }
});

// ===== ФУНКЦИОНАЛЬНОСТЬ СВОРАЧИВАНИЯ КАТЕГОРИЙ =====

// Поиск в шапке
document.addEventListener('DOMContentLoaded', function () {
  const headerSearch = document.querySelector('form[role="search"] input[name="q"]');
  if (headerSearch) {
    headerSearch.addEventListener('search', function () {
      const term = (headerSearch.value || '').trim();
      if (term) { try { if (window.trackEvent) { window.trackEvent('Search', { search_string: term }); } } catch (_) { } }
    });
    headerSearch.form && headerSearch.form.addEventListener('submit', function () {
      const term = (headerSearch.value || '').trim();
      if (term) { try { if (window.trackEvent) { window.trackEvent('Search', { search_string: term }); } } catch (_) { } }
    });
  }
});

// Трекинг выбора отделения НП (поле np_office в корзине/чекауте)
document.addEventListener('input', function (e) {
  const el = e.target;
  if (!el || el.name !== 'np_office') return;
  const val = (el.value || '').trim();
  if (val && val.length >= 3) {
    try { if (window.trackEvent) { window.trackEvent('FindLocation', { query: val }); } } catch (_) { }
  }
});

// ViewContent на листингах — по клику на любую область карточки
document.addEventListener('click', function (e) {
  try {
    const card = e.target.closest && e.target.closest('.card.product');
    if (!card) return;
    const pid = card.getAttribute('data-product-id');
    const title = card.getAttribute('data-product-title');
    const price = card.getAttribute('data-product-price');
    const category = card.getAttribute('data-product-category');

    if (pid && window.trackEvent) {
      // Для карточек в каталоге используем базовый offer_id (default color, size S)
      const offerId = 'TC-' + pid + '-default-S';
      const priceNum = price ? parseFloat(price) : undefined;

      const eventId = safeGenerateAnalyticsEventId();
      const meta = buildMetaWithUserData(eventId);
      window.trackEvent('ViewContent', {
        content_ids: [offerId],
        content_type: 'product',
        content_name: title,
        content_category: category,
        value: priceNum,
        currency: 'UAH',
        contents: [{
          id: offerId,
          item_name: title,
          item_category: category,
          brand: ANALYTICS_BRAND_NAME,
          quantity: 1,
          item_price: priceNum
        }],
        event_id: eventId,
        __meta: meta
      });

      // GA4 select_item — дополняет ViewContent данными о позиции в списке
      // (для воронки каталог → товар). Только в dataLayer (Meta-аналога нет).
      try {
        const cards = Array.prototype.slice.call(document.querySelectorAll('.card.product'));
        const position = cards.indexOf(card);
        const listName = (document.documentElement.getAttribute('data-route-name') || 'catalog');
        window.dataLayer = window.dataLayer || [];
        window.dataLayer.push({
          event: 'select_item',
          event_id: eventId,
          ecommerce: {
            item_list_name: listName,
            items: [{
              item_id: offerId,
              item_name: title || '',
              item_brand: ANALYTICS_BRAND_NAME,
              item_category: category || '',
              price: priceNum || 0,
              index: position >= 0 ? position + 1 : undefined,
              quantity: 1,
              currency: 'UAH'
            }]
          }
        });
      } catch (_) { }
    }
  } catch (_) { }
});

// GA4 view_item_list — один push при загрузке листинга каталога/главной.
// Даёт полную ecommerce-воронку (показ списка → select_item → view_item →
// add_to_cart → begin_checkout → purchase). Только dataLayer (у Meta нет
// стандартного события списка; слать массовый ViewContent нежелательно).
(function () {
  function pushViewItemList() {
    try {
      if (window.__twcViewItemListSent) return;
      const cards = Array.prototype.slice.call(document.querySelectorAll('.card.product'));
      if (!cards.length) return;
      window.__twcViewItemListSent = true;
      const listName = (document.documentElement.getAttribute('data-route-name') || 'catalog');
      const items = cards.slice(0, 50).map(function (card, idx) {
        const pid = card.getAttribute('data-product-id');
        if (!pid) return null;
        const offerId = 'TC-' + pid + '-default-S';
        const price = parseFloat(card.getAttribute('data-product-price') || '0') || 0;
        return {
          item_id: offerId,
          item_name: card.getAttribute('data-product-title') || '',
          item_brand: ANALYTICS_BRAND_NAME,
          item_category: card.getAttribute('data-product-category') || '',
          price: price,
          index: idx + 1,
          quantity: 1,
          currency: 'UAH'
        };
      }).filter(Boolean);
      if (!items.length) return;
      window.dataLayer = window.dataLayer || [];
      window.dataLayer.push({
        event: 'view_item_list',
        event_id: safeGenerateAnalyticsEventId(),
        ecommerce: {
          item_list_name: listName,
          items: items
        }
      });
    } catch (_) { }
  }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', function () { scheduleIdle(pushViewItemList); }, { once: true });
  } else {
    scheduleIdle(pushViewItemList);
  }
})();

// Опрос монтируется page-scoped импортом (index.html). Дубль init удалён ради экономии startup JS.

// Phase 22b (2026-05-13) — preloaded modules (homepage, product-media) import
// IMMEDIATELY after DOMContentLoaded instead of inside scheduleIdle. Lighthouse
// mobile reported the dynamic imports sitting at 5.3-5.4 s in the critical
// chain because requestIdleCallback was stalling for ~5 s on slow CPUs even
// though `<link rel=modulepreload>` had already cached the bytes. Cart module
// stays under scheduleIdle since it is route-scoped (cart-page only) and
// doesn't share Lighthouse's home/PDP critical chain.
document.addEventListener('DOMContentLoaded', () => {
  if (document.querySelector('.product-card-wrap') || document.getElementById('productCarousel')) {
    import('./modules/product-media.js')
      .then(({ initProductMedia }) => initProductMedia())
      .catch(() => { });
  }
  if (
    document.getElementById('load-more-btn') ||
    document.getElementById('products-container') ||
    document.getElementById('featuredToggle') ||
    document.getElementById('featured-toggle') ||
    document.getElementById('categoriesToggle')
  ) {
    import('./modules/homepage.js?v=20260515-i18n-loadmore')
      .then(({ initHomepage }) => initHomepage())
      .catch(() => { });
  }
  scheduleIdle(() => {
    if (document.querySelector('.cart-page-container') || document.getElementById('promo-code-input')) {
      import('./modules/cart.js?v=20260605-cart-qty')
        .then(({ initCartInteractions }) => initCartInteractions())
        .catch(() => { });
    }
  });
});
