/*
 * Mono checkout + Monobank Pay — вынесено из main.js (Phase 2.1, 2026-04-18).
 * Модуль грузится через dynamic import ТОЛЬКО когда на странице есть
 * кнопки [data-mono-checkout-trigger] или [data-monobank-pay-trigger],
 * либо после открытия мини-корзины.
 *
 * Экономия: ~530 строк parse/execute на страницах без мono-кнопок
 * (главная, каталог, товар без мono-CTA, профиль и т.д.).
 *
 * Зависимости из main.js читаются через window.__twcMono (инжектит main.js).
 */

import { getCookie } from './shared.js';
import { validateNovaPoshtaSelection } from './nova-poshta-selector.js?v=20260422b';
import { syncUkraineCheckoutPhoneField } from './phone.js?v=20260422c';

function deps() {
  return window.__twcMono || {};
}

function getCheckoutAnalyticsPayload() {
  const el = document.getElementById('checkout-payload');
  if (!el) return null;
  let contents = [];
  let ids = [];
  const rawContents = el.dataset.contents ? decodeURIComponent(el.dataset.contents) : '[]';
  const rawIds = el.dataset.ids ? decodeURIComponent(el.dataset.ids) : '[]';
  try { contents = JSON.parse(rawContents || '[]'); } catch (_) { contents = []; }
  try { ids = JSON.parse(rawIds || '[]'); } catch (_) { ids = []; }
  const value = parseFloat(el.dataset.value || '0');
  const currency = el.dataset.currency || 'UAH';
  let numItems = parseInt(el.dataset.numItems || '0', 10);
  if (Number.isNaN(numItems) || numItems <= 0) {
    numItems = contents.reduce((acc, item) => acc + (item.quantity || 0), 0);
    if (!numItems && ids.length) {
      numItems = ids.length;
    }
  }
  return { contents, content_ids: ids, value, currency, num_items: numItems };
}

function getMonoCheckoutStatus(button) {
  if (!button) return null;
  const explicit = button.getAttribute('data-mono-status');
  if (explicit) {
    try { return document.querySelector(explicit); } catch (_) { return null; }
  }
  const scope = button.closest('[data-mono-status-scope]') || button.closest('.vstack') || button.parentElement;
  if (scope) {
    const el = scope.querySelector('[data-mono-checkout-status]');
    if (el) return el;
  }
  return null;
}

function setMonoCheckoutStatus(statusEl, type, message) {
  if (!statusEl) return;
  statusEl.textContent = message || '';
  statusEl.classList.remove('error', 'success', 'text-danger', 'text-success');
  if (type === 'error') {
    statusEl.classList.remove('text-secondary');
    statusEl.classList.add('error', 'text-danger');
  } else if (type === 'success') {
    statusEl.classList.remove('text-secondary');
    statusEl.classList.add('success', 'text-success');
  } else {
    if (!statusEl.classList.contains('text-secondary')) statusEl.classList.add('text-secondary');
  }
}

function toggleMonoCheckoutLoading(button, isLoading) {
  if (!button) return;
  if (isLoading) {
    button.setAttribute('aria-busy', 'true');
    button.disabled = true;
    button.classList.add('loading');
  } else {
    button.removeAttribute('aria-busy');
    button.disabled = false;
    button.classList.remove('loading');
  }
}

function collectMonoCsrf() {
  if (typeof getCookie === 'function') {
    const cookieToken = getCookie('csrftoken');
    if (cookieToken) return cookieToken;
  }
  const meta = document.querySelector('meta[name="csrf-token"]');
  if (meta && meta.getAttribute) {
    const token = meta.getAttribute('content');
    if (token) return token;
  }
  const input = document.querySelector('[name="csrfmiddlewaretoken"]');
  if (input && 'value' in input && input.value) {
    return input.value;
  }
  return '';
}

function resolveMonoProductContext(button) {
  const context = {
    productId: null,
    size: '',
    qty: 1,
    colorVariantId: null,
    fitOption: ''
  };
  if (!button) return context;

  context.productId = button.getAttribute('data-product-id');

  const rootSelector = button.getAttribute('data-product-root');
  let root = null;
  if (rootSelector) {
    try { root = document.querySelector(rootSelector); } catch (_) { root = null; }
  }
  if (!root) root = button.closest('[data-product-container]');
  const find = (selector) => root ? root.querySelector(selector) : document.querySelector(selector);

  const checkedSize = find('input[name="size"]:checked');
  if (checkedSize) context.size = checkedSize.value;
  if (!context.size) {
    const sizeInput = find('input[name="size"]');
    if (sizeInput) context.size = sizeInput.value;
  }
  context.size = (context.size || '').toString().trim();

  const qtyInput = find('#qty');
  if (qtyInput) {
    const parsed = parseInt(qtyInput.value, 10);
    if (Number.isFinite(parsed) && parsed > 0) context.qty = parsed;
  }

  const colorActive = find('#color-picker .color-swatch.active') || document.querySelector('#color-picker .color-swatch.active');
  if (colorActive) context.colorVariantId = colorActive.getAttribute('data-variant');
  const fitInput = find('input[name="fit_option"]:checked') || document.querySelector('input[name="fit_option"]:checked');
  if (fitInput) context.fitOption = fitInput.value || '';

  return context;
}

function readGuestVal(name) {
  const fn = deps().readGuestStorageValue;
  return typeof fn === 'function' ? fn(name) : '';
}

function addProductToCartForMono(button) {
  const context = resolveMonoProductContext(button);
  const productId = context.productId;
  if (!productId) return Promise.resolve();

  const body = new URLSearchParams();
  body.append('product_id', String(productId));
  body.append('size', (context.size || 'S').toUpperCase());
  body.append('qty', String(context.qty));
  if (context.colorVariantId) body.append('color_variant_id', context.colorVariantId);
  if (context.fitOption) body.append('fit_option', context.fitOption);

  const csrfToken = collectMonoCsrf();

  return fetch('/cart/add/', {
    method: 'POST',
    headers: {
      'X-CSRFToken': csrfToken || '',
      'X-Requested-With': 'XMLHttpRequest',
      'Content-Type': 'application/x-www-form-urlencoded;charset=UTF-8'
    },
    body
  })
    .then(r => {
      if (!r.ok) throw new Error('Не вдалося додати товар до кошика. Спробуйте ще раз.');
      return r.json();
    })
    .then(data => {
      if (!(data && data.ok)) {
        const message = data && data.error ? data.error : 'Не вдалося додати товар до кошика. Спробуйте ще раз.';
        throw new Error(message);
      }
      try { if (typeof data.count === 'number' && window.updateCartBadge) window.updateCartBadge(data.count); } catch (_) { }
      try {
        document.dispatchEvent(new CustomEvent('cartUpdated', {
          detail: { action: 'add', productId: productId, source: 'mono-checkout' }
        }));
      } catch (_) { }
      try { if (window.refreshMiniCart) window.refreshMiniCart(); } catch (_) { }
      try { if (window.refreshCartSummary) window.refreshCartSummary(); } catch (_) { }
      return data;
    });
}

function requestMonoCheckout() {
  const csrfToken = collectMonoCsrf();
  const guestForm = document.getElementById('guest-form');
  let payload = {};
  const getAnyVal = (name) => {
    const fromForm = guestForm && guestForm.querySelector(`[name="${name}"]`);
    if (fromForm && 'value' in fromForm) return (fromForm.value || '').trim();
    const anywhere = document.querySelector(`[name="${name}"]`);
    if (anywhere && 'value' in anywhere) {
      return (anywhere.value || '').trim();
    }
    return readGuestVal(name);
  };
  if (guestForm || document.querySelector('[name="full_name"]') || document.querySelector('[name="phone"]')) {
    payload = {
      full_name: getAnyVal('full_name'),
      phone: getAnyVal('phone'),
      city: getAnyVal('city'),
      np_office: getAnyVal('np_office'),
      np_settlement_ref: getAnyVal('np_settlement_ref'),
      np_city_ref: getAnyVal('np_city_ref'),
      np_city_token: getAnyVal('np_city_token'),
      np_warehouse_ref: getAnyVal('np_warehouse_ref'),
      np_warehouse_token: getAnyVal('np_warehouse_token'),
      pay_type: getAnyVal('pay_type') || 'online_full',
      email: getAnyVal('email')
    };
  }
  return fetch('/cart/monobank/quick/', {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'X-CSRFToken': csrfToken || '',
      'X-Requested-With': 'XMLHttpRequest'
    },
    credentials: 'same-origin',
    body: JSON.stringify(payload)
  }).then(response => response.json().then(data => ({ data, status: response.status, ok: response.ok })).catch(() => ({ data: null, status: response.status, ok: false })));
}

function requestMonoCheckoutSingleProduct(button) {
  const csrfToken = collectMonoCsrf();
  const context = resolveMonoProductContext(button);

  if (!context.productId) {
    return Promise.resolve({ data: { success: false, error: 'Товар недоступний.' }, status: 400, ok: false });
  }

  const payload = {
    product_id: context.productId,
    size: (context.size || 'S').toUpperCase(),
    qty: context.qty,
    single_product: true
  };
  if (context.colorVariantId) payload.color_variant_id = context.colorVariantId;
  if (context.fitOption) payload.fit_option = context.fitOption;
  const guestForm = document.getElementById('guest-form');
  const getAnyVal = (name) => {
    const fromForm = guestForm && guestForm.querySelector(`[name="${name}"]`);
    if (fromForm && 'value' in fromForm) return (fromForm.value || '').trim();
    const anywhere = document.querySelector(`[name="${name}"]`);
    if (anywhere && 'value' in anywhere) {
      return (anywhere.value || '').trim();
    }
    return readGuestVal(name);
  };
  if (guestForm || document.querySelector('[name="full_name"]') || document.querySelector('[name="phone"]')) {
    payload.full_name = getAnyVal('full_name');
    payload.phone = getAnyVal('phone');
    payload.city = getAnyVal('city');
    payload.np_office = getAnyVal('np_office');
    payload.np_settlement_ref = getAnyVal('np_settlement_ref');
    payload.np_city_ref = getAnyVal('np_city_ref');
    payload.np_city_token = getAnyVal('np_city_token');
    payload.np_warehouse_ref = getAnyVal('np_warehouse_ref');
    payload.np_warehouse_token = getAnyVal('np_warehouse_token');
    payload.pay_type = getAnyVal('pay_type') || 'online_full';
    payload.email = getAnyVal('email');
  }

  return fetch('/cart/monobank/quick/', {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'X-CSRFToken': csrfToken || '',
      'X-Requested-With': 'XMLHttpRequest'
    },
    credentials: 'same-origin',
    body: JSON.stringify(payload)
  }).then(response => response.json().then(data => ({ data, status: response.status, ok: response.ok })).catch(() => ({ data: null, status: response.status, ok: false })));
}

function startMonoCheckout(button, statusEl, options) {
  const opts = options || {};
  setMonoCheckoutStatus(statusEl, '', '');
  toggleMonoCheckoutLoading(button, true);

  const triggerType = button.getAttribute('data-mono-checkout-trigger');
  const isSingleProduct = triggerType === 'product';

  let requestPromise;
  if (isSingleProduct) {
    requestPromise = requestMonoCheckoutSingleProduct(button);
  } else {
    const ensureProduct = opts.ensureProduct ? addProductToCartForMono(button) : Promise.resolve();
    requestPromise = ensureProduct.then(() => requestMonoCheckout());
  }

  return requestPromise
    .then(result => {
      const data = result.data || {};
      if (result.ok && data.success && data.redirect_url) {
        setMonoCheckoutStatus(statusEl, 'success', 'Відкриваємо mono checkout…');
        const analytics = getCheckoutAnalyticsPayload();
        try {
          if (window.trackEvent && analytics) {
            const buildMeta = deps().buildMetaWithUserData;
            // W2-9 (AN-012): event_id ТОЛЬКО серверный (детерминированный по заказу),
            // random-fallback рвал дедуп-пару Pixel↔CAPI → двойной счёт AddPaymentInfo.
            const eventId = data.add_payment_event_id || null;
            const meta = buildMeta && eventId ? buildMeta(eventId) : {};
            window.trackEvent('AddPaymentInfo', {
              value: analytics.value,
              currency: analytics.currency,
              num_items: analytics.num_items,
              payment_method: 'monobank',
              content_type: analytics.content_ids && analytics.content_ids.length > 1 ? 'product_group' : 'product',
              content_ids: analytics.content_ids,
              contents: analytics.contents,
              event_id: eventId,
              __meta: meta
            });
          }
        } catch (_) { }
        window.location.href = data.redirect_url;
        return;
      }
      let message = (data && data.error) ? data.error : 'Не вдалося створити платіж. Спробуйте ще раз.';
      if (result.status === 401) {
        message = 'Увійдіть, щоб скористатися mono checkout.';
      }
      setMonoCheckoutStatus(statusEl, 'error', message);
      throw new Error(message);
    })
    .catch(err => {
      if (err && err.message) {
        setMonoCheckoutStatus(statusEl, 'error', err.message);
      }
    })
    .finally(() => {
      toggleMonoCheckoutLoading(button, false);
    });
}

export function bindMonoCheckout(scope) {
  const root = scope || document;
  const buttons = root.querySelectorAll('[data-mono-checkout-trigger]');

  buttons.forEach((button) => {
    if (!button || button.dataset.monoCheckoutBound === '1') return;
    button.dataset.monoCheckoutBound = '1';
    const statusEl = getMonoCheckoutStatus(button);
    const triggerType = button.getAttribute('data-mono-checkout-trigger');
    button.addEventListener('click', (event) => {
      event.preventDefault();
      if (button.disabled) return;
      const options = { ensureProduct: triggerType === 'product' };
      startMonoCheckout(button, statusEl, options);
      const analytics = getCheckoutAnalyticsPayload();
      if (analytics) {
        try {
          const safeGen = deps().safeGenerateAnalyticsEventId;
          const buildMeta = deps().buildMetaWithUserData;
          const eventId = safeGen ? safeGen() : String(Date.now());
          try {
            // FIX 2026-06-12: не дублируем begin_checkout, если он уже ушёл
            // при старте заполнения формы доставки (см. main.js).
            if (!window.__twcBeginCheckoutSent && window.__twcAnalytics && typeof window.__twcAnalytics.pushBeginCheckoutEvent === 'function') {
              window.__twcBeginCheckoutSent = true;
              window.__twcAnalytics.pushBeginCheckoutEvent(analytics, {
                eventId,
                eventLabel: triggerType === 'product' ? 'Mono checkout product' : 'Mono checkout cart'
              });
            }
          } catch (helperErr) {
            if (console && console.debug) {
              console.debug('begin_checkout dataLayer error:', helperErr);
            }
          }
          if (window.trackEvent) {
            const meta = buildMeta ? buildMeta(eventId) : {};
            window.trackEvent('InitiateCheckout', {
              value: analytics.value,
              currency: analytics.currency,
              num_items: analytics.num_items,
              payment_method: 'monobank',
              content_ids: analytics.content_ids,
              contents: analytics.contents,
              event_id: eventId,
              __meta: meta
            });
          }
        } catch (_) { }
      }
    });
  });
}

function requestMonobankPay(retryAttempt = 0) {
  const csrfToken = collectMonoCsrf();
  const guestForm = document.getElementById('guest-form');
  let payload = {};
  const getAnyVal = (name) => {
    const fromForm = guestForm && guestForm.querySelector(`[name="${name}"]`);
    if (fromForm && 'value' in fromForm) return (fromForm.value || '').trim();
    const anywhere = document.querySelector(`[name="${name}"]`);
    return anywhere && 'value' in anywhere ? (anywhere.value || '').trim() : '';
  };
  const getPayType = () => {
    if (guestForm) {
      const guestPayType = document.getElementById('pay_type_guest');
      if (guestPayType && guestPayType.value) {
        return guestPayType.value.trim();
      }
    }
    const authPayType = document.getElementById('pay_type_auth');
    if (authPayType && authPayType.value) {
      return authPayType.value.trim();
    }
    return 'online_full';
  };
  if (guestForm || document.querySelector('[name="full_name"]') || document.querySelector('[name="phone"]')) {
    payload = {
      full_name: getAnyVal('full_name'),
      phone: getAnyVal('phone'),
      city: getAnyVal('city'),
      np_office: getAnyVal('np_office'),
      np_settlement_ref: getAnyVal('np_settlement_ref'),
      np_city_ref: getAnyVal('np_city_ref'),
      np_city_token: getAnyVal('np_city_token'),
      np_warehouse_ref: getAnyVal('np_warehouse_ref'),
      np_warehouse_token: getAnyVal('np_warehouse_token'),
      pay_type: getPayType(),
      email: getAnyVal('email')
    };
  }
  const effectivePayType = getPayType();

  if (!payload || typeof payload !== 'object') {
    payload = {};
  }
  payload.pay_type = effectivePayType;

  try {
    if (window.getTrackingContext && typeof window.getTrackingContext === 'function') {
      const tracking = window.getTrackingContext();
      if (tracking && typeof tracking === 'object') {
        if ('event_id' in tracking) {
          delete tracking.event_id;
        }
        if ('lead_event_id' in tracking) {
          delete tracking.lead_event_id;
        }
      }
      payload.tracking = tracking;
    }
  } catch (_) { }

  return fetch('/cart/monobank/create-invoice/', {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'X-CSRFToken': csrfToken || '',
      'X-Requested-With': 'XMLHttpRequest'
    },
    credentials: 'same-origin',
    body: JSON.stringify(payload)
  }).then(response => response.json().then(data => ({ data, status: response.status, ok: response.ok })).catch(() => ({ data: null, status: response.status, ok: false })))
    .then(result => {
      const data = result.data || {};
      if (result.status === 409 && data.in_progress && retryAttempt < 20) {
        const delay = Math.max(250, Math.min(Number(data.retry_after_ms) || 500, 1500));
        return new Promise(resolve => window.setTimeout(resolve, delay))
          .then(() => requestMonobankPay(retryAttempt + 1));
      }
      return result;
    });
}

function startMonobankPay(button, statusEl) {
  setMonoCheckoutStatus(statusEl, '', '');
  toggleMonoCheckoutLoading(button, true);
  const deliveryForm = document.getElementById('deliveryForm');
  const guestForm = document.getElementById('guest-form');
  const sourceForm = guestForm || deliveryForm;

  return Promise.resolve()
    .then(async () => {
      if (!sourceForm) {
        return;
      }
      const phoneField = sourceForm.querySelector('[name="phone"]');
      if (phoneField && !syncUkraineCheckoutPhoneField(phoneField)) {
        phoneField.focus();
        throw new Error('Вкажіть коректний український номер. Можна без +380.');
      }
      const isValid = await validateNovaPoshtaSelection(sourceForm, { showErrors: true });
      if (!isValid) {
        throw new Error('Оберіть місто та пункт доставки зі списку Нової пошти.');
      }
    })
    .then(() => requestMonobankPay())
    .then(result => {
      const data = result.data || {};
      if (result.ok && data.success && data.payment_complete && data.redirect_url) {
        window.location.href = data.redirect_url;
        return;
      }
      if (result.ok && data.success && data.invoice_url) {
        setMonoCheckoutStatus(statusEl, 'success', 'Відкриваємо платіжну сторінку…');
        const analytics = getCheckoutAnalyticsPayload();
        try {
          if (window.trackEvent && analytics) {
            const buildMeta = deps().buildMetaWithUserData;
            // W2-9 (AN-012): только серверный event_id — см. комментарий выше.
            const eventId = data.add_payment_event_id || null;
            const meta = buildMeta && eventId ? buildMeta(eventId) : {};
            window.trackEvent('AddPaymentInfo', {
              value: analytics.value,
              currency: analytics.currency,
              num_items: analytics.num_items,
              payment_method: 'monobank_pay',
              content_type: analytics.content_ids && analytics.content_ids.length > 1 ? 'product_group' : 'product',
              content_ids: analytics.content_ids,
              contents: analytics.contents,
              event_id: eventId,
              __meta: meta
            });
          }
        } catch (_) { }
        window.location.href = data.invoice_url;
        return;
      }
      let message = (data && data.error) ? data.error : 'Не вдалося створити платіж. Спробуйте ще раз.';
      if (result.status === 401) {
        message = 'Увійдіть, щоб скористатися онлайн оплатою.';
      }
      setMonoCheckoutStatus(statusEl, 'error', message);
      throw new Error(message);
    })
    .catch(err => {
      if (err && err.message) {
        setMonoCheckoutStatus(statusEl, 'error', err.message);
      }
    })
    .finally(() => {
      toggleMonoCheckoutLoading(button, false);
    });
}

export function bindMonobankPay(scope) {
  const root = scope || document;
  root.querySelectorAll('[data-monobank-pay-trigger]').forEach((button) => {
    if (!button || button.dataset.monobankPayBound === '1') return;
    button.dataset.monobankPayBound = '1';
    const statusEl = getMonoCheckoutStatus(button);
    button.addEventListener('click', (event) => {
      event.preventDefault();
      if (button.disabled) return;
      startMonobankPay(button, statusEl);
      const analytics = getCheckoutAnalyticsPayload();
      if (analytics) {
        try {
          const safeGen = deps().safeGenerateAnalyticsEventId;
          const buildMeta = deps().buildMetaWithUserData;
          const eventId = safeGen ? safeGen() : String(Date.now());
          try {
            // FIX 2026-06-12: не дублируем begin_checkout, если он уже ушёл
            // при старте заполнения формы доставки (см. main.js).
            if (!window.__twcBeginCheckoutSent && window.__twcAnalytics && typeof window.__twcAnalytics.pushBeginCheckoutEvent === 'function') {
              window.__twcBeginCheckoutSent = true;
              window.__twcAnalytics.pushBeginCheckoutEvent(analytics, {
                eventId,
                eventLabel: 'Monobank Pay'
              });
            }
          } catch (helperErr) {
            if (console && console.debug) {
              console.debug('begin_checkout dataLayer error:', helperErr);
            }
          }
          if (window.trackEvent) {
            const meta = buildMeta ? buildMeta(eventId) : {};
            window.trackEvent('InitiateCheckout', {
              value: analytics.value,
              currency: analytics.currency,
              num_items: analytics.num_items,
              payment_method: 'monobank_pay',
              content_ids: analytics.content_ids,
              contents: analytics.contents,
              event_id: eventId,
              __meta: meta
            });
          }
        } catch (_) { }
      }
    });
  });
}
