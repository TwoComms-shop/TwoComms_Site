import { getCookie, DOMCache, escapeHtml } from './shared.js';
import { initNovaPoshtaSelectors } from './nova-poshta-selector.js?v=20260801a';
import {
  normalizeUkraineCheckoutPhoneValue,
  syncUkraineCheckoutPhoneField,
  syncUkraineCheckoutPhoneHint
} from './phone.js?v=20260422c';

const getCartLocaleConfig = () => {
  try {
    const node = document.getElementById('cart-locale-config');
    return node ? JSON.parse(node.textContent || '{}') : {};
  } catch (_) {
    return {};
  }
};

const CART_LOCALE = getCartLocaleConfig();
const CART_STRINGS = CART_LOCALE.strings || {};
const cartText = (key) => {
  const value = CART_STRINGS[key];
  return typeof value === 'string' && value ? value : '';
};
const cartInterpolate = (key, values = {}) => {
  let text = cartText(key);
  Object.entries(values).forEach(([name, value]) => {
    text = text.replaceAll(`{${name}}`, String(value));
  });
  return text;
};
const cartUrl = (key) => {
  const value = CART_LOCALE.urls?.[key];
  return typeof value === 'string' && value ? value : '';
};

const CART_EMPTY_TEMPLATE = () => `
  <div class="cart-empty">
    <div class="cart-empty-icon">
      <svg width="64" height="64" viewBox="0 0 24 24" fill="currentColor">
        <path d="M7 18c-1.1 0-1.99.9-1.99 2S5.9 22 7 22s2-.9 2-2-.9-2-2-2zM1 2v2h2l3.6 7.59-1.35 2.45c-.16.28-.25.61-.25.96 0 1.1.9 2 2 2h12v-2H7.42c-.14 0-.25-.11-.25-.25l.03-.12L8.1 13h7.45c.75 0 1.41-.41 1.75-1.03L21.7 4H5.21l-.94-2H1zm16 16c-1.1 0-1.99.9-1.99 2s.89 2 1.99 2 2-.9 2-2-.89-2-1.99-2z"/>
      </svg>
    </div>
    <h2 class="cart-empty-title">${cartText('emptyCart')}</h2>
    <p class="cart-empty-text">${cartText('emptyCartText')}</p>
    <a href="${cartUrl('home')}" class="cart-empty-btn">
      <svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor">
        <path d="M12 2l3.09 6.26L22 9.27l-5 4.87 1.18 6.88L12 17.77l-6.18 3.25L7 14.14 2 9.27l6.91-1.01L12 2z"/>
      </svg>
      ${cartText('continueShopping')}
    </a>
  </div>
`;

const NOOP = () => { };

const getCsrfToken = () =>
  DOMCache.query('meta[name="csrf-token"]')?.getAttribute('content') ||
  DOMCache.query('input[name="csrfmiddlewaretoken"]')?.value ||
  getCookie('csrftoken') ||
  '';

const parseNumber = (raw) => {
  if (raw === null || raw === undefined) {
    return 0;
  }

  const cleaned = String(raw)
    .replace(/\u00A0/g, '') // nbsp
    .replace(/\s/g, '')
    .replace(/,/g, '.')
    .replace(/[^\d.-]/g, '');

  const parsed = Number.parseFloat(cleaned);
  return Number.isFinite(parsed) ? parsed : 0;
};

const formatUAH = (amount) => {
  const value = Number.isFinite(amount) ? amount : 0;
  const isInt = Math.abs(value % 1) < 1e-9;
  const options = isInt
    ? { minimumFractionDigits: 0, maximumFractionDigits: 0 }
    : { minimumFractionDigits: 2, maximumFractionDigits: 2 };
  const locale = CART_LOCALE.intlLocale || document.documentElement.lang || undefined;
  const suffix = CART_LOCALE.currency?.suffix || CART_LOCALE.currency?.code || '';
  return `${value.toLocaleString(locale, options)} ${suffix}`;
};

// Keep color values safe and predictable before placing them in dynamic markup.
const normalizeHexColor = (raw) => {
  const value = String(raw ?? '').trim();
  if (!/^#(?:[\da-f]{3}|[\da-f]{6})$/i.test(value)) {
    return '';
  }
  const hex = value.slice(1).toLowerCase();
  return `#${hex.length === 3 ? hex.split('').map((part) => `${part}${part}`).join('') : hex}`;
};

const renderCartSwatch = (item, colorLabel) => {
  const primary = normalizeHexColor(item.color_primary_hex);
  // A secondary color only has meaning when the primary value is valid too.
  const secondary = primary ? normalizeHexColor(item.color_secondary_hex) : '';
  const classes = ['cart-item-swatch'];
  if (!primary) {
    classes.push('cart-item-swatch--fallback');
  }

  const dataAttrs = primary
    ? ` data-primary="${primary}"${secondary ? ` data-secondary="${secondary}"` : ''}`
    : '';
  const styleVars = primary
    ? ` style="--primary-color:${primary};${secondary ? `--secondary-color:${secondary};` : ''}"`
    : '';
  const label = colorLabel && colorLabel !== '—'
    ? ` role="img" aria-label="${escapeHtml(`${cartText('color')}: ${colorLabel}`)}"`
    : ' aria-hidden="true"';

  return `<span class="${classes.join(' ')}"${dataAttrs}${styleVars}${label}></span>`;
};

const toggleElement = (el, show) => {
  if (!el) {
    return;
  }
  el.classList[show ? 'remove' : 'add']('d-none');
};



class CartPageController {
  constructor(root) {
    this.root = root;
    this.cartList = root.querySelector('#cart-list');
    this.cartMainSection = root.querySelector('.cart-main-section');
    this.subtotalValueEl = root.querySelector('#cart-subtotal-value');
    this.itemsLabelEl = root.querySelector('#cart-items-label');
    this.discountRow = root.querySelector('#cart-discount-row');
    this.discountValueEl = root.querySelector('#cart-discount-value');
    this.siteDiscountRow = root.querySelector('#cart-site-discount-row');
    this.siteDiscountValueEl = root.querySelector('#cart-site-discount-value');
    this.payNowAmountEl = root.querySelector('#pay-now-amount');
    this.payNowLabelEl = root.querySelector('#pay-now-label');
    this.prepayRow = root.querySelector('#prepay-remaining-row');
    this.prepayAmountEl = root.querySelector('#prepay-remaining-amount');
    this.prepayNote = root.querySelector('#prepay-note');
    this.payTypeSelect = root.querySelector('#pay_type_auth') || root.querySelector('#pay_type_guest');
    this.monobankPayBtn = root.querySelector('#monobank-pay-btn');
    this.placeOrderBtn = root.querySelector('#placeOrderBtn');
    this.guestOrderBtn = root.querySelector('#guestOrderBtn');
    this.pointsSummary = root.querySelector('#cart-points-summary');
    this.pointsEarnedBox = root.querySelector('#cart-points-earned');
    this.pointsAmountEl = root.querySelector('#cart-points-amount');
    this.pointsNoneBox = root.querySelector('#cart-points-none');
    this.savingsInfoEl = root.querySelector('#cart-savings-info');
    this.savingsSiteLine = root.querySelector('#cart-savings-site');
    this.savingsSiteAmountEl = root.querySelector('#cart-savings-site-amount');
    this.savingsPromoLine = root.querySelector('#cart-savings-promo');
    this.savingsPromoCodeEl = root.querySelector('#cart-savings-promo-code');
    this.savingsPromoAmountEl = root.querySelector('#cart-savings-promo-amount');
    this.savingsTotalEl = root.querySelector('#cart-savings-total');
    this.savingsTotalAmountEl = root.querySelector('#cart-savings-total-amount');
    this.checkoutPayloadEl = root.querySelector('#checkout-payload');
    this.promoAppliedDiscountEl = root.querySelector('.cart-promo-applied-discount');
    this.placeholderImage = this.cartList?.dataset.placeholder || '';
    this.prepayValue = parseNumber(this.payNowAmountEl?.dataset.prepay || 200) || 200;
    this.itemsEndpoint = root.dataset.cartItemsUrl || cartUrl('items');
    this.summaryEndpoint = root.dataset.cartSummaryUrl || cartUrl('summary');
    this.contactUrl = root.dataset.contactUrl || cartUrl('contact');
    this.state = null;
    this.syncTimer = null;
    this.requestController = null;
    this.handleCartEvent = this.handleCartEvent.bind(this);
    this.handleRefreshSummary = this.handleRefreshSummary.bind(this);
  }

  init() {
    this.setupPayTypeControls();
    this.setupCartListeners();
    this.setupRefreshSummaryHook();
    this.setupContactModal();
    this.updateOrderButtonText(this.getCurrentPayType());
    this.updatePaymentSummary(this.getCurrentPayType());
    this.requestSync(0);
  }

  destroy() {
    document.removeEventListener('cartUpdated', this.handleCartEvent);
  }

  handleCartEvent() {
    this.requestSync(80);
  }

  handleRefreshSummary(originalPromise) {
    this.requestSync(0);
    return originalPromise;
  }

  setupCartListeners() {
    document.addEventListener('cartUpdated', this.handleCartEvent);
  }

  setupRefreshSummaryHook() {
    if (typeof window.refreshCartSummary !== 'function') {
      return;
    }
    const originalRefresh = window.refreshCartSummary;
    const controller = this;
    window.refreshCartSummary = function overriddenRefresh(...args) {
      const result = originalRefresh.apply(this, args);
      controller.requestSync(0);
      if (result && typeof result.then === 'function') {
        return result.then((value) => {
          controller.requestSync(0);
          return value;
        });
      }
      return result;
    };
  }

  getCurrentPayType() {
    return this.payTypeSelect?.value || 'online_full';
  }

  setupPayTypeControls() {
    if (!this.payTypeSelect) {
      return;
    }
    this.payTypeSelect.addEventListener('change', () => {
      const payType = this.getCurrentPayType();
      this.updateOrderButtonText(payType);
      this.updatePaymentSummary(payType);
    });
  }

  updateOrderButtonText(payType) {
    const activeBtn = this.placeOrderBtn || this.guestOrderBtn;
    if (!activeBtn) {
      return;
    }
    const textSpan = activeBtn.querySelector('.cart-cta-text') || activeBtn.querySelector('span') || activeBtn;
    let text = '';
    switch (payType) {
      case 'online_full':
        text = cartText('paymentCta');
        break;
      case 'prepay_200':
        text = cartText('prepayCta');
        break;
      default:
        text = this.placeOrderBtn
          ? cartText('orderCta')
          : cartText('guestCta');
    }
    textSpan.textContent = text;
  }

  requestSync(delay) {
    clearTimeout(this.syncTimer);
    this.syncTimer = window.setTimeout(() => {
      this.syncTimer = null;
      this.sync();
    }, delay);
  }

  async sync() {
    if (!this.itemsEndpoint) {
      return;
    }
    if (this.requestController) {
      this.requestController.abort();
    }
    const controller = new AbortController();
    this.requestController = controller;

    try {
      const response = await fetch(this.itemsEndpoint, {
        method: 'GET',
        headers: {
          'X-Requested-With': 'XMLHttpRequest',
          'Cache-Control': 'no-cache',
        },
        cache: 'no-store',
        signal: controller.signal,
      });

      if (!response.ok) {
        throw new Error(`Cart sync failed with status ${response.status}`);
      }

      const data = await response.json();
      if (controller.signal.aborted) {
        return;
      }
      this.requestController = null;

      if (data && data.ok) {
        this.applyState(data);
      }
    } catch (error) {
      if (error.name === 'AbortError') {
        return;
      }
      console.error('Cart sync error:', error);
      this.requestController = null;
    }
  }

  updateBrigadeState(data) {
    let words = {};
    try { words = JSON.parse(document.getElementById('brigade-copy')?.textContent || '{}'); } catch (_) {}
    const hasBrigade = Boolean(data.has_brigade_items);
    const hasCustom = Boolean(data.has_custom_items || data.custom_items?.length);
    const prepayAllowed = data.prepay_allowed !== undefined ? Boolean(data.prepay_allowed) : !hasBrigade && !hasCustom;
    this.root.querySelectorAll('[data-brigade-payment-note]').forEach(el => { el.hidden = !hasBrigade; });
    this.root.querySelectorAll('.pay-method-native').forEach(select => {
      const option = select.querySelector('option[value="prepay_200"]');
      if (option) option.disabled = !prepayAllowed;
      if (!prepayAllowed && select.value === 'prepay_200') {
        select.value = 'online_full';
        select.dispatchEvent(new Event('change', { bubbles: true }));
      }
      const tile = select.closest('.cart-form-group')?.querySelector('[data-pay-tile="prepay_200"]');
      if (tile) {
        tile.classList.toggle('is-disabled', !prepayAllowed);
        tile.setAttribute('aria-disabled', String(!prepayAllowed));
        if (prepayAllowed) delete tile.dataset.payDisabled;
        else tile.dataset.payDisabled = '1';
      }
    });
    const offer = this.root.querySelector('[data-brigade-cart-offer]');
    if (!offer) return;
    offer.hidden = !data.has_225_items;
    const savings = parseNumber(data.brigade_discount_total);
    offer.querySelector('[data-brigade-savings]').hidden = savings <= 0;
    offer.querySelector('[data-brigade-saving-value]').textContent = String(savings);
    offer.querySelector('[data-brigade-cart-title]').textContent = savings > 0 ? words.applied : words.cart_title;
    const tees = Number(data.brigade_tee_qty || 0), hoodies = Number(data.brigade_hoodie_qty || 0);
    offer.querySelector('[data-brigade-cart-intro]').textContent = Number(data.brigade_discount_total) > 0 ? words.automatic : hoodies && !tees ? words.add_tee : tees && !hoodies ? words.add_hoodie : words.automatic;
    offer.querySelector('[data-brigade-choose-tee]').hidden = Boolean(tees && !hoodies);
    offer.querySelector('[data-brigade-choose-hoodie]').hidden = Boolean(hoodies && !tees);
  }

  applyState(data) {
    this.state = data;
    this.updateBrigadeState(data);

    this.renderItems(
      Array.isArray(data.items) ? data.items : [],
      Array.isArray(data.custom_items) ? data.custom_items : []
    );
    this.updateSummary(data);
    this.updatePaymentSummary(this.getCurrentPayType(), data);
    this.toggleCheckoutAvailability(data);
    this.updatePoints(data);
    this.updateCheckoutPayload(data);
    this.updatePromoDiscount(data);

    if (typeof window.updateCartBadge === 'function') {
      const badgeCount = data.items_count ?? data.cart_count ?? 0;
      window.updateCartBadge(badgeCount);
    }
  }

  renderItems(items, customItems) {
    if (!this.cartMainSection) {
      return;
    }

    customItems = Array.isArray(customItems) ? customItems : [];

    if (!items.length && !customItems.length) {
      this.cartMainSection.innerHTML = CART_EMPTY_TEMPLATE();
      return;
    }

    if (!this.cartList) {
      const container = document.createElement('div');
      container.className = 'cart-items-container';
      container.id = 'cart-list';
      this.cartMainSection.appendChild(container);
      this.cartList = container;
    }

    const placeholder = this.placeholderImage || '';
    const customRows = customItems.map((ci) => this.renderCustomItem(ci)).join('');
    const regularRows = items.map((item) => this.renderItem(item, placeholder)).join('');
    this.cartList.innerHTML = customRows + regularRows;
  }

  renderCustomItem(ci) {
    const leadNumber = ci.lead_number
      ? ` · ${escapeHtml(cartInterpolate('leadNumber', { number: ci.lead_number }))}`
      : '';
    const placements = ci.placements_display ? `
            <div class="cart-item-detail">
              <span class="cart-item-label">${cartText('placement')}:</span>
              <span class="cart-item-value">${escapeHtml(ci.placements_display)}</span>
            </div>` : '';
    const productLabel = ci.product_label ? `
            <div class="cart-item-detail">
              <span class="cart-item-label">${cartText('product')}:</span>
              <span class="cart-item-value">${escapeHtml(ci.product_label)}</span>
            </div>` : '';
    const sizeMode = ci.size_mode_label ? `
            <div class="cart-item-detail">
              <span class="cart-item-label">${cartText('sizeMode')}:</span>
              <span class="cart-item-value">${escapeHtml(ci.size_mode_label)}</span>
            </div>` : '';
    const sizeBreakdown = ci.size_breakdown_display ? `
            <div class="cart-item-detail">
              <span class="cart-item-label">${cartText('sizes')}:</span>
              <span class="cart-item-value">${escapeHtml(ci.size_breakdown_display)}</span>
            </div>` : '';
    const color = ci.color ? `
            <div class="cart-item-detail">
              <span class="cart-item-label">${cartText('color')}:</span>
              <span class="cart-item-value">${escapeHtml(ci.color)}</span>
            </div>` : '';
    const fit = ci.fit_label ? `
            <div class="cart-item-detail">
              <span class="cart-item-label">${cartText('cut')}:</span>
              <span class="cart-item-value">${escapeHtml(ci.fit_label)}</span>
            </div>` : '';
    const fabric = ci.fabric_label ? `
            <div class="cart-item-detail">
              <span class="cart-item-label">${cartText('fabric')}:</span>
              <span class="cart-item-value">${escapeHtml(ci.fabric_label)}</span>
            </div>` : '';
    const serviceKind = ci.service_kind_label ? `
            <div class="cart-item-detail">
              <span class="cart-item-label">${cartText('service')}:</span>
              <span class="cart-item-value">${escapeHtml(ci.service_kind_label)}</span>
            </div>` : '';
    const fileTriage = ci.file_triage_label ? `
            <div class="cart-item-detail">
              <span class="cart-item-label">${cartText('filePreparation')}:</span>
              <span class="cart-item-value">${escapeHtml(ci.file_triage_label)}</span>
            </div>` : '';
    const addOns = Array.isArray(ci.add_on_labels) && ci.add_on_labels.length ? `
            <div class="cart-item-detail">
              <span class="cart-item-label">${cartText('additional')}:</span>
              <span class="cart-item-value">${escapeHtml(ci.add_on_labels.join(', '))}</span>
            </div>` : '';
    const placementNote = ci.placement_note ? `
            <div class="cart-item-detail">
              <span class="cart-item-label">${cartText('placementComment')}:</span>
              <span class="cart-item-value">${escapeHtml(ci.placement_note)}</span>
            </div>` : '';
    const gift = ci.gift_enabled ? `
            <div class="cart-item-detail">
              <span class="cart-item-label">${cartText('gift')}:</span>
              <span class="cart-item-value">${escapeHtml(ci.gift_text || cartText('giftText'))}</span>
            </div>` : '';
    const b2bDiscount = parseNumber(ci.b2b_discount_per_unit) > 0 && ci.mode === 'brand' ? `
            <div class="cart-item-detail">
              <span class="cart-item-label">${cartText('b2bDiscount')}:</span>
              <span class="cart-item-value">-${formatUAH(parseNumber(ci.b2b_discount_per_unit))} / ${cartText('perItem')}</span>
            </div>` : '';

    let moderationBadge = '';
    if (ci.is_pending) {
      moderationBadge = `<span class="cart-item-moderation-badge cart-item-moderation-badge--pending"><span class="cart-item-status-spinner" aria-hidden="true"></span>${ci.is_draft ? cartText('pendingToManager') : cartText('pendingManager')}</span>`;
    } else if (ci.is_approved) {
      moderationBadge = `<span class="cart-item-moderation-badge cart-item-moderation-badge--approved">${cartText('approved')}</span>`;
    } else if (ci.is_rejected) {
      moderationBadge = `<span class="cart-item-moderation-badge cart-item-moderation-badge--rejected">${cartText('rejected')}</span>`;
    }
    const managerNote = ci.manager_note ? `<div class="cart-item-manager-note">${cartText('managerComment')}: ${escapeHtml(ci.manager_note)}</div>` : '';
    const priceNote = ci.pending_price_note || ci.payment_note || '';
    const totalValue = ci.included_in_payment
      ? `<span class="cart-item-total-value">${formatUAH(parseNumber(ci.line_total))}</span>`
      : `<span class="cart-item-total-value cart-item-total-value--muted">${cartText('afterApproval')}</span>${parseNumber(ci.final_total) > 0 ? `<div class="cart-item-total-note">${cartText('approximately')} ${formatUAH(parseNumber(ci.final_total))}</div>` : ''}`;
    const managerLink = ci.show_manager_contact
      ? `<a href="https://t.me/twocomms" target="_blank" rel="noopener noreferrer" class="cart-item-manager-link" aria-label="${cartText('managerContact')}"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M21 11.5a8.38 8.38 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.38 8.38 0 0 1-3.8-.9L3 21l1.9-5.7a8.38 8.38 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.38 8.38 0 0 1 3.8-.9h.5a8.48 8.48 0 0 1 8 8v.5Z"/><path d="M8 12h.01M12 12h.01M16 12h.01"/></svg><span class="cart-action-label">${cartText('managerContact')}</span></a>`
      : '';

    return `
      <div class="cart-item cart-item--custom" data-custom-key="${escapeHtml(ci.key || '')}" data-lead-id="${escapeHtml(String(ci.lead_id || ''))}">
        <div class="cart-item-sparks">
          <div class="cart-item-spark cart-item-spark-1"></div>
          <div class="cart-item-spark cart-item-spark-2"></div>
          <div class="cart-item-spark cart-item-spark-3"></div>
        </div>

        <div class="cart-item-image cart-item-image--custom" aria-hidden="true">
          <svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">
            <path d="M12 2l2.4 4.86 5.37.78-3.88 3.78.91 5.35L12 14.9l-4.8 2.52.91-5.35L4.23 8.3l5.37-.78L12 2z"/>
            <path d="M9 14h6"/>
          </svg>
        </div>

        <div class="cart-item-info">
          <div class="cart-item-custom-badge">
            <svg width="12" height="12" viewBox="0 0 24 24" fill="currentColor">
              <path d="M12 2l3.09 6.26L22 9.27l-5 4.87 1.18 6.88L12 17.77l-6.18 3.25L7 14.14 2 9.27l6.91-1.01L12 2z"/>
            </svg>
            ${cartText('customPrint')}${leadNumber}
          </div>
          <div class="cart-item-moderation" data-status="${escapeHtml(ci.moderation_status || '')}">
            ${moderationBadge}
            ${managerNote}
          </div>
          <h3 class="cart-item-title">${escapeHtml(ci.label || cartText('customProduct'))}</h3>
          <div class="cart-item-details">${productLabel}${placements}
            <div class="cart-item-detail">
              <span class="cart-item-label">${cartText('quantity')}:</span>
              <span class="cart-item-value">${Number(ci.quantity || 1)}</span>
            </div>${sizeMode}${sizeBreakdown}${fit}${fabric}${color}${serviceKind}${fileTriage}${addOns}${placementNote}${gift}${b2bDiscount}
          </div>
          <div class="cart-item-price">
            <span class="cart-item-price-label">${escapeHtml(ci.price_caption || cartText('price'))}:</span>
            <span class="cart-item-price-value">
              <span class="cart-item-price-current">${formatUAH(parseNumber(ci.unit_total))} / ${cartText('perItem')}</span>
            </span>
            ${priceNote ? `<div class="cart-item-price-note">${escapeHtml(priceNote)}</div>` : ''}
          </div>
        </div>

        <div class="cart-item-actions">
          <div class="cart-item-total">
            <span class="cart-item-total-label">${cartText('total')}:</span>
            ${totalValue}
          </div>
          ${managerLink}
          <button type="button" class="cart-item-remove-btn" data-custom-remove data-key="${escapeHtml(ci.key || '')}" data-lead-id="${escapeHtml(String(ci.lead_id || ''))}" aria-label="${cartText('remove')}">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor">
              <path d="M6 19c0 1.1.9 2 2 2h8c1.1 0 2-.9 2-2V7H6v12zM19 4h-3.5l-1-1h-5l-1 1H5v2h14V4z"/>
            </svg>
            <span class="cart-action-label">${cartText('remove')}</span>
          </button>
        </div>
      </div>
    `;
  }

  renderItem(item, placeholder) {
    const imageUrl = item.image_url || placeholder || '';
    const size = item.size || '—';
    const qty = Number(item.qty || 0);
    const unitPrice = parseNumber(item.unit_price);
    const originalUnitPrice = parseNumber(
      item.original_unit_price !== undefined ? item.original_unit_price : item.unit_price
    );
    const lineTotal = parseNumber(item.line_total);
    const hasSiteDiscount = originalUnitPrice > unitPrice + 0.009;
    const points = Number(item.points_reward || 0) * qty;
    const colorLabel = item.color_label || '—';
    const hasColor = Boolean(item.color_variant_id);
    const colorSwatch = hasColor ? renderCartSwatch(item, colorLabel) : '';
    const fitLabel = item.fit_option_label || item.fit_label || '';
    let priceHtml = hasSiteDiscount
      ? `<span class="cart-item-price-old">${formatUAH(originalUnitPrice)}</span><span class="cart-item-price-current">${formatUAH(unitPrice)}</span>`
      : `<span class="cart-item-price-current">${formatUAH(unitPrice)}</span>`;

    const bp = item.brigade_pricing;
    if (bp && Number(bp.discounted_qty) > 0) {
      priceHtml = `<span class="cart-item-price-current">${Number(bp.discounted_qty)} × ${formatUAH(parseNumber(bp.offer_unit_price))}</span>`;
      if (Number(bp.regular_qty) > 0) priceHtml += `<span class="brigade-line-offer">+ ${Number(bp.regular_qty)} × ${formatUAH(parseNumber(bp.standalone_unit_price))}</span>`;
      priceHtml += `<span class="brigade-line-offer">225 · −${formatUAH(parseNumber(bp.discount_amount))}</span>`;
    }

    return `
      <div class="cart-item" data-cart-row data-key="${escapeHtml(item.key)}" data-offer-id="${escapeHtml(item.offer_id || '')}">
        <div class="cart-item-sparks">
          <div class="cart-item-spark cart-item-spark-1"></div>
          <div class="cart-item-spark cart-item-spark-2"></div>
          <div class="cart-item-spark cart-item-spark-3"></div>
        </div>
        <div class="cart-item-image">
          <img src="${imageUrl}" alt="${escapeHtml(item.product_title || cartText('itemProductAlt'))}" class="cart-item-img" width="80" height="80">
          <div class="cart-item-image-glow"></div>
        </div>
        <div class="cart-item-info">
          <h3 class="cart-item-title">${escapeHtml(item.product_title || '')}</h3>
          <div class="cart-item-details">
            <div class="cart-item-detail">
              <span class="cart-item-label">${cartText('size')}:</span>
              <span class="cart-item-value">${escapeHtml(size)}</span>
            </div>
            <div class="cart-item-detail cart-item-detail--qty">
              <span class="cart-item-label">${cartText('quantity')}:</span>
              <div class="cart-qty-stepper">
                <button type="button" class="cart-qty-btn" data-qty-dec aria-label="${cartText('decrease')}"${qty <= 1 ? ' disabled' : ''}>
                  <svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M19 13H5v-2h14v2z"/></svg>
                </button>
                <span class="cart-qty-value" data-qty-value>${qty}</span>
                <button type="button" class="cart-qty-btn" data-qty-inc aria-label="${cartText('increase')}">
                  <svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M19 13h-6v6h-2v-6H5v-2h6V5h2v6h6v2z"/></svg>
                </button>
              </div>
            </div>
            ${fitLabel ? `
            <div class="cart-item-detail">
              <span class="cart-item-label">${cartText('fit')}:</span>
              <span class="cart-item-value">${escapeHtml(fitLabel)}</span>
            </div>` : ''}
            ${hasColor ? `
            <div class="cart-item-detail">
              <span class="cart-item-label">${cartText('color')}:</span>
              <div class="cart-item-color d-flex align-items-center gap-2">
                ${colorSwatch}
                <span class="cart-item-color-name">${escapeHtml(colorLabel)}</span>
              </div>
            </div>` : ''}
          </div>
          <div class="cart-item-price">
            <span class="cart-item-price-label">${cartText('price')}:</span>
            <span class="cart-item-price-value">${priceHtml}</span>
          </div>
          ${points > 0 ? `
          <div class="cart-item-points">
            <div class="cart-item-points-icon">
              <svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor">
                <path d="M12 2l3.09 6.26L22 9.27l-5 4.87 1.18 6.88L12 17.77l-6.18 3.25L7 14.14 2 9.27l6.91-1.01L12 2z"/>
              </svg>
            </div>
            <span class="cart-item-points-text">${cartInterpolate('pointsEarned', { points })}</span>
          </div>` : ''}
        </div>
        <div class="cart-item-actions">
          <div class="cart-item-total">
            <span class="cart-item-total-label">${cartText('total')}:</span>
            <span class="cart-item-total-value">${formatUAH(lineTotal)}</span>
          </div>
          <button type="button" class="cart-item-remove-btn" data-key="${escapeHtml(item.key)}" aria-label="${cartText('remove')}">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor">
              <path d="M6 19c0 1.1.9 2 2 2h8c1.1 0 2-.9 2-2V7H6v12zM19 4h-3.5l-1-1h-5l-1 1H5v2h14V4z"/>
            </svg>
            <span class="cart-action-label">${cartText('remove')}</span>
          </button>
        </div>
      </div>
    `;
  }

  updateSummary(data) {
    // Используем original_subtotal (сумма без скидок) для отображения суммы товаров
    const subtotal = parseNumber(data.original_subtotal || data.subtotal);
    if (this.subtotalValueEl) {
      this.subtotalValueEl.textContent = formatUAH(subtotal);
    }

    const itemsCount = Number.isFinite(data.items_count)
      ? Number(data.items_count)
      : Array.isArray(data.items)
        ? data.items.reduce((acc, item) => acc + Number(item.qty || 0), 0)
        : 0;

    if (this.itemsLabelEl) {
      this.itemsLabelEl.textContent = cartInterpolate('itemsCount', { count: itemsCount });
    }

    const promoDiscount = parseNumber(data.discount);
    const hasPromoDiscount = promoDiscount > 0;
    if (this.discountRow) {
      toggleElement(this.discountRow, hasPromoDiscount);
    }
    if (this.discountValueEl) {
      this.discountValueEl.textContent = hasPromoDiscount
        ? `-${formatUAH(promoDiscount)}`
        : `-${formatUAH(0)}`;
    }

    const siteDiscount = parseNumber(data.site_discount_total);
    const hasSiteDiscount = siteDiscount > 0;
    if (this.siteDiscountRow) {
      toggleElement(this.siteDiscountRow, hasSiteDiscount);
    }
    if (this.siteDiscountValueEl) {
      this.siteDiscountValueEl.textContent = hasSiteDiscount
        ? `-${formatUAH(siteDiscount)}`
        : `-${formatUAH(0)}`;
    }

    const totalSavingsRaw = parseNumber(data.total_savings);
    const savingsTotal = totalSavingsRaw > 0 ? totalSavingsRaw : siteDiscount + promoDiscount;
    const hasSavings = hasSiteDiscount || hasPromoDiscount;

    if (this.savingsInfoEl) {
      toggleElement(this.savingsInfoEl, hasSavings);
    }
    if (this.savingsSiteLine) {
      toggleElement(this.savingsSiteLine, hasSiteDiscount);
    }
    if (this.savingsSiteAmountEl) {
      this.savingsSiteAmountEl.textContent = formatUAH(siteDiscount);
    }
    if (this.savingsPromoLine) {
      toggleElement(this.savingsPromoLine, hasPromoDiscount);
    }
    if (this.savingsPromoAmountEl) {
      this.savingsPromoAmountEl.textContent = formatUAH(promoDiscount);
    }
    if (this.savingsPromoCodeEl) {
      this.savingsPromoCodeEl.textContent = data.applied_promo || '';
    }
    if (this.savingsTotalEl) {
      toggleElement(this.savingsTotalEl, hasSavings);
    }
    if (this.savingsTotalAmountEl) {
      this.savingsTotalAmountEl.textContent = formatUAH(Math.max(savingsTotal, 0));
    }
  }

  updatePaymentSummary(payType, state = this.state) {
    if (!this.payNowAmountEl || !state) {
      return;
    }

    const total = Math.max(parseNumber(state.approved_total ?? state.total ?? state.grand_total), 0);
    const isPrepay = payType === 'prepay_200';
    const payNow = isPrepay ? Math.min(this.prepayValue, total) : total;
    const remaining = isPrepay ? Math.max(total - payNow, 0) : 0;

    this.payNowAmountEl.dataset.total = total.toFixed(2);
    this.payNowAmountEl.textContent = formatUAH(payNow);

    if (this.payNowLabelEl) {
      this.payNowLabelEl.textContent = isPrepay
        ? cartText('payNowPrepay')
        : cartText('payNow');
    }

    toggleElement(this.prepayRow, isPrepay);
    toggleElement(this.prepayNote, isPrepay);
    if (this.prepayAmountEl) {
      this.prepayAmountEl.textContent = formatUAH(remaining);
    }

    window.paymentSummary = {
      total,
      prepay: this.prepayValue,
      pay_now: payNow,
      remaining,
    };
  }

  toggleCheckoutAvailability(state = this.state) {
    const payableTotal = Math.max(parseNumber(state?.approved_total ?? state?.total ?? state?.grand_total), 0);
    const hasPayableItems = payableTotal > 0.009;
    [this.monobankPayBtn, this.placeOrderBtn, this.guestOrderBtn].forEach((button) => {
      if (button) {
        button.disabled = !hasPayableItems;
      }
    });
  }

  updatePoints(data) {
    if (!this.pointsSummary) {
      return;
    }
    const points = Number(data.total_points || 0);
    toggleElement(this.pointsEarnedBox, points > 0);
    toggleElement(this.pointsNoneBox, points <= 0);

    if (this.pointsAmountEl) {
      this.pointsAmountEl.textContent = cartInterpolate('pointsEarned', { points: `+${points}` });
    }
  }

  updateCheckoutPayload(data) {
    if (!this.checkoutPayloadEl) {
      return;
    }

    const items = Array.isArray(data.items) ? data.items : [];
    const ids = items.map((item) => item.offer_id).filter(Boolean);
    const contents = items.map((item) => ({
      id: item.offer_id,
      quantity: item.qty || 0,
      item_price: parseNumber(item.unit_price),
      item_name: item.product_title || '',
      item_category: item.product_category || '',
      brand: 'TwoComms',
    }));

    const encodedIds = encodeURIComponent(JSON.stringify(ids));
    const encodedContents = encodeURIComponent(JSON.stringify(contents));
    const total = parseNumber(data.approved_total ?? data.total ?? data.grand_total);
    const itemsCount = contents.reduce((acc, item) => acc + Number(item.quantity || 0), 0);

    this.checkoutPayloadEl.setAttribute('data-ids', encodedIds);
    this.checkoutPayloadEl.setAttribute('data-contents', encodedContents);
    this.checkoutPayloadEl.setAttribute('data-value', total.toFixed(2));
    this.checkoutPayloadEl.setAttribute('data-num-items', String(itemsCount));
  }

  updatePromoDiscount(data) {
    if (!this.promoAppliedDiscountEl) {
      return;
    }
    const discount = parseNumber(data.discount);
    if (discount > 0) {
      this.promoAppliedDiscountEl.textContent = `${cartText('discount')}: -${formatUAH(discount)}`;
    } else {
      this.promoAppliedDiscountEl.textContent = '';
    }
  }

  setupContactModal() {
    // Модальное окно находится в {% block modals %}, который рендерится ВНЕ .cart-page-container
    const modal = document.querySelector('#contactManagerModal');
    // Кнопки находятся внутри .cart-page-container
    const triggers = this.root.querySelectorAll('.btn-contact-manager');
    if (!modal || !triggers.length) {
      return;
    }

    const closeBtn = modal.querySelector('.contact-modal-close');
    const form = modal.querySelector('#contactManagerForm');
    const body = document.body;
    const closeModal = () => {
      modal.classList.remove('modal-active');
      body.classList.remove('contact-modal-open');
      modal.setAttribute('aria-hidden', 'true');
      setTimeout(() => {
        modal.style.display = 'none';
        body.style.overflow = '';
      }, 250);
    };

    const fillFromForms = () => {
      const deliveryForm = this.root.querySelector('#deliveryForm');
      const guestForm = this.root.querySelector('#guest-form');
      const sourceForm = deliveryForm || guestForm;
      if (!sourceForm) {
        return;
      }

      const fullName = sourceForm.querySelector('[name="full_name"]')?.value || '';
      const phone = sourceForm.querySelector('[name="phone"]')?.value || '';

      const fullNameInput = modal.querySelector('#contactModalFullName');
      const phoneInput = modal.querySelector('#contactModalPhone');

      if (fullNameInput) {
        fullNameInput.value = fullName;
      }
      if (phoneInput) {
        phoneInput.value = phone;
      }
    };

    const openModal = () => {
      fillFromForms();
      modal.style.display = 'flex';
      modal.setAttribute('aria-hidden', 'false');
      body.classList.add('contact-modal-open');
      body.style.overflow = 'hidden';
      window.requestAnimationFrame(() => modal.classList.add('modal-active'));
    };

    triggers.forEach((btn) => {
      btn.addEventListener('click', (event) => {
        event.preventDefault();
        openModal();
      });
    });

    if (closeBtn) {
      closeBtn.addEventListener('click', (event) => {
        event.preventDefault();
        closeModal();
      });
    }

    modal.addEventListener('click', (event) => {
      if (event.target === modal) {
        closeModal();
      }
    });

    if (form) {
      form.addEventListener('submit', async (event) => {
        event.preventDefault();

        const formData = new FormData(form);
        const submitBtn = form.querySelector('button[type="submit"]');
        const originalText = submitBtn ? submitBtn.textContent : '';

        if (submitBtn) {
          submitBtn.disabled = true;
          submitBtn.textContent = cartText('contactSending');
        }

        try {
          const response = await fetch(this.contactUrl, {
            method: 'POST',
            headers: { 'X-CSRFToken': getCsrfToken() },
            body: formData,
          });
          const data = await response.json();
          if (data.success) {
            alert(`✅ ${cartText('contactSuccess')}`);
            try {
              if (window.trackEvent) {
                const eventId = (typeof window.safeGenerateAnalyticsEventId === 'function')
                  ? window.safeGenerateAnalyticsEventId()
                  : String(Date.now());
                const meta = (typeof window.buildMetaWithUserData === 'function')
                  ? window.buildMetaWithUserData(eventId)
                  : { event_id: eventId };
                window.trackEvent('Lead', {
                  content_name: 'Cart consultation request',
                  content_category: 'consultation',
                  currency: 'UAH',
                  event_id: eventId,
                  __meta: meta,
                });
              }
            } catch (_) { }
            form.reset();
            closeModal();
          } else {
            alert(`❌ ${cartInterpolate('contactError', { message: data.error || cartText('genericError') })}`);
          }
        } catch (error) {
          console.error('Contact manager error:', error);
          alert(`❌ ${cartText('contactConnectionError')}`);
        } finally {
          if (submitBtn) {
            submitBtn.disabled = false;
            submitBtn.textContent = originalText;
          }
        }
      });
    }
  }
}

function initPromoVault() {
  const vault = document.querySelector('[data-promo-vault]');
  const form = vault?.querySelector('[data-promo-vault-form]');
  const input = vault?.querySelector('.promo-vault-input');
  const submit = vault?.querySelector('.promo-vault-submit');
  const status = vault?.querySelector('[data-promo-vault-status]');
  const applied = vault?.querySelector('[data-promo-vault-applied]');
  const appliedCode = vault?.querySelector('[data-promo-vault-code]');
  const appliedDiscount = vault?.querySelector('[data-promo-vault-discount]');
  const remove = vault?.querySelector('[data-promo-vault-remove]');
  if (!vault || !form || !input || !submit || !status || !applied || !remove || vault.dataset.initialized === '1') {
    return;
  }
  vault.dataset.initialized = '1';

  let requestController = null;
  let animationTimers = [];
  let gearAngle = 0;
  let previousLength = input.value.length;
  const reduceMotion = window.matchMedia?.('(prefers-reduced-motion: reduce)').matches;
  const animationTimeline = {
    bolt1: 1800,
    bolt2: 2250,
    bolt3: 2800,
    open: 3300,
    reveal: 4200,
    close: 5450,
    relock3: 6350,
    relock2: 6550,
    relock1: 6750,
    wheelReset: 6900,
    finish: 7800,
  };
  const animationClasses = [
    'is-loading', 'is-error', 'is-unlocking', 'is-bolt-1', 'is-bolt-2',
    'is-bolt-3', 'is-open', 'is-revealing', 'is-closing', 'is-relocking',
    'is-shiver-1', 'is-shiver-2', 'is-shiver-3', 'is-success',
  ];
  const clearAnimationTimers = () => {
    animationTimers.forEach((timer) => window.clearTimeout(timer));
    animationTimers = [];
  };
  const scheduleAnimation = (delay, callback) => {
    animationTimers.push(window.setTimeout(callback, delay));
  };
  const clearStates = () => vault.classList.remove(...animationClasses);
  const shiver = (level) => {
    vault.classList.remove('is-shiver-1', 'is-shiver-2', 'is-shiver-3');
    void vault.offsetWidth;
    vault.classList.add(`is-shiver-${level}`);
  };
  const renderError = (message) => {
    clearAnimationTimers();
    clearStates();
    // Force a layout read so repeated invalid attempts replay the reference's jam animation.
    void vault.offsetWidth;
    vault.classList.add('is-error');
    status.textContent = message || cartText('promoInvalid');
    input.setAttribute('aria-invalid', 'true');
    input.focus({ preventScroll: true });
  };

  input.addEventListener('focus', () => vault.classList.add('is-focus'));
  input.addEventListener('blur', () => vault.classList.remove('is-focus'));
  input.addEventListener('input', () => {
    const delta = Math.max(-4, Math.min(4, input.value.length - previousLength));
    previousLength = input.value.length;
    if (!delta) return;
    gearAngle += delta * 45;
    vault.style.setProperty('--gear-a', `${gearAngle}deg`);
    vault.style.setProperty('--gear-b', `${22 - (gearAngle * 4 / 3)}deg`);
    vault.style.setProperty('--gear-c', `${8 + (gearAngle * 1.6)}deg`);
  });
  const finishSuccess = (data, code) => {
    clearAnimationTimers();
    clearStates();
    vault.classList.add('is-success');
    input.removeAttribute('aria-invalid');
    status.textContent = cartText('promoApplied');
    applied.classList.remove('d-none');
    if (appliedCode) appliedCode.textContent = data.promo_code || code;
    if (appliedDiscount) appliedDiscount.textContent = `${cartText('discount')}: ${formatUAH(parseNumber(data.discount))}`;
  };

  remove.addEventListener('click', async () => {
    if (remove.disabled) return;
    remove.disabled = true;
    remove.setAttribute('aria-busy', 'true');
    status.textContent = cartText('promoRemoving');
    try {
      const response = await fetch(vault.dataset.removeUrl || cartUrl('promoRemove'), {
        method: 'POST',
        headers: {
          'Content-Type': 'application/x-www-form-urlencoded;charset=UTF-8',
          'X-CSRFToken': getCsrfToken(),
          'X-Requested-With': 'XMLHttpRequest',
        },
      });
      let data = {};
      try { data = await response.json(); } catch (_error) { data = {}; }
      if (!response.ok || !data.success) {
        renderError(data.error || cartText('promoRemoveError'));
        return;
      }
      clearStates();
      input.value = '';
      applied.classList.add('d-none');
      if (appliedCode) appliedCode.textContent = '';
      if (appliedDiscount) appliedDiscount.textContent = '';
      status.textContent = data.message || cartText('promoRemoved');
      document.dispatchEvent(new CustomEvent('cartUpdated'));
    } catch (_error) {
      renderError(cartText('promoRemoveNetworkError'));
    } finally {
      remove.disabled = false;
      remove.removeAttribute('aria-busy');
    }
  });

  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    if (vault.classList.contains('is-loading') || vault.classList.contains('is-unlocking') || vault.classList.contains('is-closing')) {
      return;
    }
    const code = (input.value || '').trim();
    if (!code) {
      renderError(cartText('promoRequired'));
      return;
    }

    clearAnimationTimers();
    clearStates();
    vault.classList.add('is-loading');
    status.textContent = cartText('promoChecking');
    input.removeAttribute('aria-invalid');
    input.disabled = true;
    submit.disabled = true;
    submit.setAttribute('aria-busy', 'true');
    requestController?.abort();
    requestController = new AbortController();

    try {
      const response = await fetch(vault.dataset.applyUrl || cartUrl('promoApply'), {
        method: 'POST',
        headers: {
          'Content-Type': 'application/x-www-form-urlencoded;charset=UTF-8',
          'X-CSRFToken': getCsrfToken(),
          'X-Requested-With': 'XMLHttpRequest',
        },
        body: `promo_code=${encodeURIComponent(code)}`,
        signal: requestController.signal,
      });
      let data = {};
      try { data = await response.json(); } catch (_error) { data = {}; }
      if (!response.ok || !data.success) {
        renderError(data.error || data.message || (response.status === 429
          ? cartText('promoRetryLimit')
          : cartText('promoInvalid')));
        return;
      }

      // Сесія вже оновлена сервером: синхронізуємо суми одразу, не чекаючи завершення декорації.
      document.dispatchEvent(new CustomEvent('cartUpdated'));
      clearStates();
      vault.classList.add('is-unlocking');
      status.textContent = cartText('promoAccepted');
      if (reduceMotion) {
        finishSuccess(data, code);
      } else {
        scheduleAnimation(animationTimeline.bolt1, () => {
          vault.classList.add('is-bolt-1');
          shiver(1);
          status.textContent = cartText('promoFirstLock');
        });
        scheduleAnimation(animationTimeline.bolt2, () => {
          vault.classList.add('is-bolt-2');
          shiver(2);
          status.textContent = cartText('promoSecondLock');
        });
        scheduleAnimation(animationTimeline.bolt3, () => {
          vault.classList.add('is-bolt-3');
          shiver(3);
          status.textContent = cartText('promoAllLocks');
        });
        scheduleAnimation(animationTimeline.open, () => {
          vault.classList.add('is-open');
          status.textContent = cartText('promoOpeningDoor');
        });
        scheduleAnimation(animationTimeline.reveal, () => {
          vault.classList.add('is-revealing');
          status.textContent = cartText('promoFound');
        });
        scheduleAnimation(animationTimeline.close, () => {
          vault.classList.add('is-closing');
          status.textContent = cartText('promoClosingDoor');
        });
        scheduleAnimation(animationTimeline.relock3, () => {
          vault.classList.remove('is-bolt-3');
          shiver(1);
        });
        scheduleAnimation(animationTimeline.relock2, () => {
          vault.classList.remove('is-bolt-2');
          shiver(1);
        });
        scheduleAnimation(animationTimeline.relock1, () => {
          vault.classList.remove('is-bolt-1');
          shiver(2);
        });
        scheduleAnimation(animationTimeline.wheelReset, () => {
          vault.classList.add('is-relocking');
        });
        scheduleAnimation(animationTimeline.finish, () => finishSuccess(data, code));
      }
    } catch (error) {
      if (error.name !== 'AbortError') {
        renderError(cartText('promoNetworkError'));
      }
    } finally {
      requestController = null;
      input.disabled = false;
      submit.disabled = false;
      submit.removeAttribute('aria-busy');
    }
  });
}

function setupCartValidation(form) {
  if (!form) {
    return;
  }
  const inputs = form.querySelectorAll('input, select');

  const markError = (field, msg) => {
    field.classList.add('cart-form-input-error');
    const wrap = field.closest('.cart-form-group') || field.parentElement;
    if (!wrap) {
      return;
    }
    let err = wrap.querySelector('.cart-form-error');
    if (!err) {
      err = document.createElement('div');
      err.className = 'cart-form-error';
      wrap.appendChild(err);
    }
    err.textContent = msg;
    err.style.display = 'block';
  };

  const clearError = (field) => {
    field.classList.remove('cart-form-input-error');
    const wrap = field.closest('.cart-form-group') || field.parentElement;
    if (!wrap) {
      return;
    }
    const err = wrap.querySelector('.cart-form-error');
    if (err) {
      err.style.display = 'none';
    }
  };

  const validate = (field) => {
    const value = (field.value || '').trim();
    clearError(field);

    if (field.hasAttribute('required') && !value) {
      markError(field, cartText('required'));
      return false;
    }

    if (value && field.name === 'phone') {
      const normalized = normalizeUkraineCheckoutPhoneValue(value);
      if (!normalized) {
        markError(field, cartText('invalidPhone'));
        return false;
      }
      if (field.value !== normalized) {
        syncUkraineCheckoutPhoneField(field);
      }
    }
    return true;
  };

  inputs.forEach((input) => {
    if (input.name === 'phone') {
      syncUkraineCheckoutPhoneHint(input);
    }
    input.addEventListener('input', () => {
      clearError(input);
      if (input.name === 'phone') {
        syncUkraineCheckoutPhoneHint(input);
      }
    });
    input.addEventListener('blur', () => {
      if (input.name === 'phone') {
        syncUkraineCheckoutPhoneHint(input);
      }
      validate(input);
    });
  });

  form.addEventListener('submit', (event) => {
    let ok = true;
    inputs.forEach((input) => {
      if (!validate(input)) {
        ok = false;
      }
    });
    if (!ok) {
      event.preventDefault();
      const first = form.querySelector('.cart-form-input-error');
      if (first) {
        first.scrollIntoView({ behavior: 'smooth', block: 'center' });
        first.focus();
      }
      return;
    }
    // W1-14 (NEW-514): защита от double-submit — блокируем повторный сабмит
    // формы, пока идёт первый запрос (дабл-клик / F5 при медленном ответе).
    if (form.dataset.submitting === '1') {
      event.preventDefault();
      return;
    }
    form.dataset.submitting = '1';
    const submitButtons = form.querySelectorAll('button[type="submit"], input[type="submit"]');
    submitButtons.forEach((btn) => {
      btn.disabled = true;
      btn.setAttribute('aria-busy', 'true');
    });
    // Страховка: если навигации не случилось (ошибка сети), вернуть кнопку.
    window.setTimeout(() => {
      form.dataset.submitting = '';
      submitButtons.forEach((btn) => {
        btn.disabled = false;
        btn.removeAttribute('aria-busy');
      });
    }, 15000);
  });
}

export function initCartInteractions() {
  initPromoVault();

  document.addEventListener('click', (event) => {
    const btn = event.target.closest?.('.cart-item-remove-btn');
    if (!btn) {
      return;
    }
    if (btn.hasAttribute('data-custom-remove') && typeof window.CustomCartRemoveKey === 'function') {
      event.preventDefault();
      window.CustomCartRemoveKey(
        btn.getAttribute('data-key') || '',
        btn,
        btn.getAttribute('data-lead-id') || ''
      );
      return;
    }
    const key = btn.getAttribute('data-key');
    if (key && typeof window.CartRemoveKey === 'function') {
      event.preventDefault();
      window.CartRemoveKey(key, btn);
    }
  });

  setupCartValidation(document.getElementById('guest-form'));
  setupCartValidation(document.getElementById('deliveryForm'));

  const root = document.querySelector('.cart-page-container');
  if (root) {
    initNovaPoshtaSelectors(root);
  }
  if (root) {
    if (root.__cartController) {
      root.__cartController.destroy();
    }
    const controller = new CartPageController(root);
    root.__cartController = controller;
    controller.init();
  }
}
