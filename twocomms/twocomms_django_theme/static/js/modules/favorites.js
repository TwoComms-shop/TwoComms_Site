/**
 * Favorites + notifications — lazy-mounted (Phase 2.1 ext).
 *
 * Реальная логика:
 *   - toggleFavorite(productId, button)  — POST /favorites/toggle/<id>/ + AddToWishlist / RemoveFromWishlist
 *   - checkFavoriteStatus(productId, button) — GET /favorites/check/<id>/ + проставить is-favorite класс
 *   - showNotification(message, type)   — toast-уведомление в правом нижнем углу
 *
 * main.js экспонирует тонкие глобальные стабы `window.toggleFavorite`
 * и т.п., которые динамически импортируют этот модуль при первом вызове.
 * Таким образом `onclick="toggleFavorite(...)"` из шаблонов продолжает работать,
 * но код favorites не парсится/не выполняется до реального взаимодействия.
 */

import { escapeHtml, getCookie } from './shared.js';

/**
 * Собирает данные товара (offer_id, цена, название, категория) для событий
 * AddToWishlist / RemoveFromWishlist. Раньше цена читалась прямо с
 * .favorite-btn, где атрибута data-product-price нет (он на родительской
 * карточке .card.product или в #product-detail-container), поэтому value
 * всегда падал на заглушку 0.01. Теперь ищем данные на ближайшей карточке,
 * затем на PDP-контейнере, и только потом fallback.
 */
function collectWishlistProductData(productId, button) {
  let price = 0;
  let title = '';
  let category = '';
  let offerId = '';

  const card = button && button.closest ? button.closest('.card.product, [data-product-id]') : null;
  const pdp = document.getElementById('product-detail-container');
  const addBtn = document.querySelector('[data-add-to-cart="' + productId + '"]');

  const readFrom = (el) => {
    if (!el) return;
    if (!price) {
      const raw = parseFloat(el.getAttribute('data-product-price') || el.getAttribute('data-price') || '0');
      if (Number.isFinite(raw) && raw > 0) price = raw;
    }
    if (!title) title = el.getAttribute('data-product-title') || el.getAttribute('data-product-name') || '';
    if (!category) category = el.getAttribute('data-product-category') || '';
  };

  readFrom(button);
  readFrom(card);
  readFrom(addBtn);
  // PDP: цена лежит в #product-analytics-payload, заголовок — в контейнере.
  const payload = document.getElementById('product-analytics-payload');
  if (payload && pdp && String(pdp.getAttribute('data-product-id')) === String(productId)) {
    if (!price) {
      const raw = parseFloat(payload.dataset.price || '0');
      if (Number.isFinite(raw) && raw > 0) price = raw;
    }
    if (!title) title = pdp.getAttribute('data-product-title') || '';
    if (!category) category = pdp.getAttribute('data-product-category') || '';
    const currentOffer = pdp.getAttribute('data-current-offer-id');
    if (currentOffer) offerId = currentOffer;
  }

  if (!offerId && card) offerId = card.getAttribute('data-default-offer-id') || '';
  if (!offerId) return null;
  if (!Number.isFinite(price) || price <= 0) price = 0.01;

  return { offerId, price, title, category };
}

export function toggleFavorite(productId, button) {
  if (!button) return;
  button.classList.add('loading');

  fetch(`/favorites/toggle/${productId}/`, {
    method: 'POST',
    headers: {
      'X-CSRFToken': getCookie('csrftoken'),
      'Content-Type': 'application/json',
    },
  })
    .then((response) => response.json())
    .then((data) => {
      if (data.success) {
        if (data.is_favorite) {
          button.classList.add('is-favorite');
          try {
            if (window.trackEvent) {
              const info = collectWishlistProductData(productId, button);
              if (!info) return;
              const meta = (typeof window.buildMetaWithUserData === 'function')
                ? window.buildMetaWithUserData(undefined)
                : undefined;
              const payload = {
                content_ids: [info.offerId],
                content_type: 'product',
                value: info.price,
                currency: 'UAH',
                num_items: 1,
                contents: [{ id: info.offerId, quantity: 1, item_price: info.price }],
              };
              if (info.title) payload.content_name = info.title;
              if (info.category) payload.content_category = info.category;
              if (meta) payload.__meta = meta;
              window.trackEvent('AddToWishlist', payload);
            }
          } catch (_) { }
        } else {
          button.classList.remove('is-favorite');
          try {
            if (window.trackEvent) {
              const info = collectWishlistProductData(productId, button);
              if (!info) return;
              const meta = (typeof window.buildMetaWithUserData === 'function')
                ? window.buildMetaWithUserData(undefined)
                : undefined;
              const payload = {
                content_ids: [info.offerId],
                content_type: 'product',
                value: info.price,
                currency: 'UAH',
                num_items: 1,
                contents: [{ id: info.offerId, quantity: 1, item_price: info.price }],
              };
              if (info.title) payload.content_name = info.title;
              if (info.category) payload.content_category = info.category;
              if (meta) payload.__meta = meta;
              window.trackEvent('RemoveFromWishlist', payload);
            }
          } catch (_) { }
        }

        if (data.favorites_count !== undefined) {
          if (typeof window.updateFavoritesBadge === 'function') {
            window.updateFavoritesBadge(data.favorites_count);
          }
        }
        showNotification(data.message, 'success');
      } else {
        showNotification(data.message || 'Помилка', 'error');
      }
    })
    .catch((error) => {
      console.error('Error:', error);
      showNotification('Помилка з\'єднання', 'error');
    })
    .finally(() => {
      button.classList.remove('loading');
    });
}

export function checkFavoriteStatus(productId, button) {
  if (!button) return;
  fetch(`/favorites/check/${productId}/`)
    .then((response) => response.json())
    .then((data) => {
      if (data.is_favorite) button.classList.add('is-favorite');
      else button.classList.remove('is-favorite');
    })
    .catch((error) => {
      console.error('Error checking favorite status:', error);
    });
}

export function showNotification(message, type = 'info') {
  const notification = document.createElement('div');
  notification.className = `notification notification-${type}`;
  notification.innerHTML = `
    <div class="notification-content">
      <span class="notification-message">${escapeHtml(message)}</span>
      <button class="notification-close" onclick="this.parentElement.parentElement.remove();">
        <svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor">
          <path d="M19 6.41L17.59 5 12 10.59 6.41 5 5 6.41 10.59 12 5 17.59 6.41 19 12 13.41 17.59 19 19 17.59 13.41 12z"/>
        </svg>
      </button>
    </div>
  `;
  document.body.appendChild(notification);
  setTimeout(() => { notification.classList.add('show'); }, 100);
  setTimeout(() => {
    notification.classList.remove('show');
    setTimeout(() => {
      if (notification.parentElement) notification.remove();
    }, 300);
  }, 3000);
}
