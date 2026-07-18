/**
 * UI fallback globals: QtyInc/QtyDec, AddToCart, CartRemove*, cleanCart,
 * updatePaymentSummary, dlog. Вызываются из inline onclick="" на
 * product_detail / cart страницах.
 *
 * Вынесено из inline <script> в base.html (~22 KB HTML) → отдельный
 * кэшируемый static. Grузится с defer — гарантированно выполняется до
 * того, как пользователь кликнет по любой кнопке, потому что defer
 * выполняется до DOMContentLoaded.
 */
/* Fallback: глобальные функции, если основной JS не заинициализирован */
(function () {
  var dlog = window.dlog || function () {
    if (window.console && console.debug) {
      try { console.debug.apply(console, arguments); } catch (_) { console.log.apply(console, arguments); }
    }
  };
  window.dlog = dlog;

  // UI debugging disabled for production
  function getCookie(name) {
    var m = document.cookie.match('(^|;)\\s*' + name + '\\s*=\\s*([^;]+)');
    var result = m ? decodeURIComponent(m.pop()) : '';
    return result;
  }
  if (typeof window.QtyInc !== 'function') {
    window.QtyInc = function (selector) {
      var input = typeof selector === 'string' ? document.querySelector(selector) : selector;
      if (!input) { return; }
      var v = parseInt(input.value || '1', 10) || 1; input.value = Math.max(1, v + 1);
    };
  }
  if (typeof window.QtyDec !== 'function') {
    window.QtyDec = function (selector) {
      var input = typeof selector === 'string' ? document.querySelector(selector) : selector;
      if (!input) { return; }
      var v = parseInt(input.value || '1', 10) || 1; input.value = Math.max(1, v - 1);
    };
  }
  if (typeof window.CartRemoveKey !== 'function') {
    window.CartRemoveKey = function (key, el) {
      // Получаем CSRF токен из мета-тега или cookie
      var csrfToken = getCookie('csrftoken') ||
        document.querySelector('meta[name="csrf-token"]')?.getAttribute('content') ||
        document.querySelector('input[name="csrfmiddlewaretoken"]')?.value ||
        getCookie('csrftoken');

      var body = new URLSearchParams({ key: key });

      fetch('/cart/remove/', {
        method: 'POST',
        headers: {
          'X-CSRFToken': csrfToken,
          'X-Requested-With': 'XMLHttpRequest',
          'Content-Type': 'application/x-www-form-urlencoded;charset=UTF-8'
        },
        body: body
      }).then(function (r) {
        if (!r.ok) {
          throw new Error('Network response was not ok: ' + r.status);
        }
        return r.json();
      }).then(function (d) {
        if (d && d.ok) {
          try { if (window.updateCartBadge) window.updateCartBadge(d.count); } catch (_) { }

          // Отправляем событие обновления корзины для страницы корзины
          try {
            document.dispatchEvent(new CustomEvent('cartUpdated', { detail: { action: 'remove', key: key } }));
          } catch (_) { }

          // Удаляем элемент из DOM
          if (el) {
            var row = el.closest('[data-cart-row]') || el.closest('.d-flex');
            if (row && row.parentElement) {
              row.style.opacity = '0';
              row.style.transform = 'translateX(-20px)';
              row.style.transition = 'all 0.3s ease';
              setTimeout(function () {
                if (row.parentElement) row.parentElement.removeChild(row);
              }, 300);
            }
          }

          // Обновляем мини-корзину
          var miniPromise = null;
          try { if (window.refreshMiniCart) miniPromise = window.refreshMiniCart(); } catch (_) { }
          var reopen = function () { try { if (window.openMiniCart) { window.openMiniCart({ skipRefresh: true }); } } catch (_) { } };
          if (miniPromise && miniPromise.finally) {
            miniPromise.then(reopen).catch(reopen).finally(function () {
              try { if (window.refreshCartSummary) window.refreshCartSummary(); } catch (_) { }
            });
          } else {
            try { if (window.refreshCartSummary) window.refreshCartSummary(); } catch (_) { }
            reopen();
          }

          // Обновляем UI страницы корзины БЕЗ перезагрузки
          var cartList = document.getElementById('cart-list');
          if (cartList) {
            var cartContainerEl = document.querySelector('.cart-page-container');
            var cartController = cartContainerEl && cartContainerEl.__cartController;
            if (cartController && typeof cartController.requestSync === 'function') {
              cartController.requestSync(0);
              return;
            }
            // Обновляем итоговую сумму товаров (subtotal)
            var summaryValue = document.querySelector('.cart-summary-row:first-child .cart-summary-value');
            var summaryLabel = document.querySelector('.cart-summary-row:first-child .cart-summary-label');
            var payNowAmount = document.getElementById('pay-now-amount');

            // Обновляем subtotal (сумма товаров без скидки)
            if (summaryValue && d.subtotal !== undefined) {
              summaryValue.textContent = parseFloat(d.subtotal).toFixed(2) + ' грн';
            }
            if (summaryLabel && d.count !== undefined) {
              summaryLabel.textContent = 'Товари (' + d.count + '):';
            }

            // Обновляем блок скидки промокода если есть
            var discountRow = document.querySelector('.cart-summary-discount');
            if (d.discount !== undefined && d.discount > 0) {
              // Показываем блок скидки если его нет
              if (!discountRow) {
                // Находим место для вставки (после суммы товаров, перед итогом)
                var summaryRows = document.querySelectorAll('.cart-checkout-summary .cart-summary-row');
                if (summaryRows.length > 0) {
                  var discountHtml = '<div class="cart-summary-row cart-summary-discount"><span class="cart-summary-label"><div class="cart-discount-label"><svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor"><path d="M12 2l3.09 6.26L22 9.27l-5 4.87 1.18 6.88L12 17.77l-6.18 3.25L7 14.14 2 9.27l6.91-1.01L12 2z"/></svg>Знижка промокоду</div></span><span class="cart-summary-value">-' + parseFloat(d.discount).toFixed(2) + ' грн</span></div>';
                  summaryRows[0].insertAdjacentHTML('afterend', discountHtml);
                }
              } else {
                // Обновляем существующий блок
                var discountValue = discountRow.querySelector('.cart-summary-value');
                if (discountValue) {
                  discountValue.textContent = '-' + parseFloat(d.discount).toFixed(2) + ' грн';
                }
              }
            } else {
              // Скрываем блок скидки если скидки нет
              if (discountRow) {
                discountRow.remove();
              }
            }

            // Обновляем итоговую сумму (total)
            if (payNowAmount && d.total !== undefined) {
              // КРИТИЧНО: Используем Number чтобы избежать проблем с форматированием
              var totalValue = Number(parseFloat(d.total));
              if (isNaN(totalValue)) totalValue = 0;

              // ВАЖНО: Обновляем только data-total атрибут, НЕ текст напрямую
              // Используем toFixed(2) для сохранения в data-атрибуте, но как число для расчетов
              payNowAmount.setAttribute('data-total', totalValue.toFixed(2));

              // Обновляем paymentSummary для корректного пересчета предоплаты
              if (window.paymentSummary) {
                window.paymentSummary.total = totalValue;
              }

              // КРИТИЧНО: ВСЕГДА вызываем updatePaymentSummary для правильного отображения суммы
              // Она сама решит что показывать: 200 грн при prepay_200 или полную сумму
              // Используем setTimeout чтобы гарантировать что updatePaymentSummary выполнится ПОСЛЕ всех обновлений
              setTimeout(function () {
                var payTypeSelect = document.getElementById('pay_type_auth') || document.getElementById('pay_type_guest');
                if (payTypeSelect && typeof window.updatePaymentSummary === 'function') {
                  // Используем глобальную функцию и текущее значение селекта
                  window.updatePaymentSummary(payTypeSelect.value || 'online_full');
                } else if (typeof window.updatePaymentSummary === 'function') {
                  // Если селект еще не загружен, используем значение по умолчанию
                  window.updatePaymentSummary('online_full');
                }
              }, 50);
            }

            // Если корзина пуста - показываем сообщение
            if (d.count === 0) {
              setTimeout(function () {
                var mainSection = document.querySelector('.cart-main-section');
                if (mainSection) {
                  mainSection.innerHTML = '<div class="cart-empty"><div class="cart-empty-icon"><svg width="64" height="64" viewBox="0 0 24 24" fill="currentColor"><path d="M7 18c-1.1 0-1.99.9-1.99 2S5.9 22 7 22s2-.9 2-2-.9-2-2-2zM1 2v2h2l3.6 7.59-1.35 2.45c-.16.28-.25.61-.25.96 0 1.1.9 2 2 2h12v-2H7.42c-.14 0-.25-.11-.25-.25l.03-.12L8.1 13h7.45c.75 0 1.41-.41 1.75-1.03L21.7 4H5.21l-.94-2H1zm16 16c-1.1 0-1.99.9-1.99 2s.89 2 1.99 2 2-.9 2-2-.9-2-2-2z"/></svg></div><h2 class="cart-empty-title">Кошик порожній</h2><p class="cart-empty-text">Додайте товари до кошика, щоб зробити замовлення</p><a href="/" class="cart-empty-btn"><svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor"><path d="M12 2l3.09 6.26L22 9.27l-5 4.87 1.18 6.88L12 17.77л-6.18 3.25Л7 14.14 2 9.27л6.91-1.01L12 2z"/></svg>Перейти до покупок</a></div>';
                }
              }, 350);
            }
          }

          try {
            if (window.trackEvent) {
              var rowEl = el && el.closest ? el.closest('[data-cart-row]') : null;
              if (!rowEl) {
                rowEl = document.querySelector('[data-cart-row][data-key="' + key + '"]');
              }
              var offerId = rowEl ? rowEl.getAttribute('data-offer-id') : null;
              if (offerId) {
                var payload = {
                  content_ids: [offerId],
                  content_type: 'product',
                  contents: [{
                    id: offerId,
                    quantity: 1
                  }]
                };
                window.trackEvent('RemoveFromCart', payload);
              } else {
                if (console && console.debug) {
                  console.debug('RemoveFromCart: offer_id not found for key', key);
                }
              }
            }
          } catch (_) { }
        }
      }).catch(function (error) {
        // Тихая обработка ошибок - перезагружаем только если на странице корзины
        if (document.getElementById('cart-list')) {
          location.reload();
        }
      });
    };
  }
  if (typeof window.CustomCartRemoveKey !== 'function') {
    window.CustomCartRemoveKey = function (key, el, leadId) {
      var csrfToken = getCookie('csrftoken') ||
        document.querySelector('meta[name="csrf-token"]')?.getAttribute('content') ||
        document.querySelector('input[name="csrfmiddlewaretoken"]')?.value ||
        getCookie('csrftoken');

      var body = new URLSearchParams();
      if (key) {
        body.append('key', key);
      }
      if (leadId) {
        body.append('lead_id', leadId);
      }

      fetch('/custom-print/remove/', {
        method: 'POST',
        headers: {
          'X-CSRFToken': csrfToken,
          'X-Requested-With': 'XMLHttpRequest',
          'Content-Type': 'application/x-www-form-urlencoded;charset=UTF-8'
        },
        body: body
      }).then(function (r) {
        if (!r.ok) {
          throw new Error('Network response was not ok: ' + r.status);
        }
        return r.json();
      }).then(function (d) {
        if (d && d.ok) {
          if (el) {
            var row = el.closest('.cart-item--custom') || el.closest('.mini-cart-custom-row') || el.closest('.d-flex');
            if (row) {
              row.style.opacity = '0';
              row.style.transform = 'translateX(-20px)';
              row.style.transition = 'all 0.3s ease';
            }
          }

          try {
            document.dispatchEvent(new CustomEvent('cartUpdated', {
              detail: { action: 'remove_custom', key: key, lead_id: leadId || null }
            }));
          } catch (_) { }

          var miniPromise = null;
          try { if (window.refreshMiniCart) miniPromise = window.refreshMiniCart(); } catch (_) { }
          var reopen = function () { try { if (window.openMiniCart) { window.openMiniCart({ skipRefresh: true }); } } catch (_) { } };
          if (miniPromise && miniPromise.finally) {
            miniPromise.then(reopen).catch(reopen).finally(function () {
              try { if (window.refreshCartSummary) window.refreshCartSummary(); } catch (_) { }
            });
          } else {
            try { if (window.refreshCartSummary) window.refreshCartSummary(); } catch (_) { }
            reopen();
          }

          var cartContainerEl = document.querySelector('.cart-page-container');
          var cartController = cartContainerEl && cartContainerEl.__cartController;
          if (cartController && typeof cartController.requestSync === 'function') {
            cartController.requestSync(0);
          } else if (document.getElementById('cart-list')) {
            window.location.reload();
          }
        }
      }).catch(function () {
        if (document.getElementById('cart-list')) {
          window.location.reload();
        } else {
          try { if (window.refreshMiniCart) window.refreshMiniCart(); } catch (_) { }
        }
      });
      return false;
    };
  }
  if (typeof window.CustomCartRemove !== 'function') {
    window.CustomCartRemove = function (el) {
      if (!el) { return false; }
      return window.CustomCartRemoveKey(
        el.getAttribute('data-key') || '',
        el,
        el.getAttribute('data-lead-id') || ''
      );
    };
  }
  if (typeof window.CartRemove !== 'function') {
    window.CartRemove = function (pid, size, el) { return window.CartRemoveKey(String(pid) + ':' + (size || ''), el); };
  }

  if (typeof window.cleanCart !== 'function') {
    window.cleanCart = function () {
      if (confirm('Ви впевнені, що хочете очистити кошик?')) {
        var csrfToken = getCookie('csrftoken') ||
          document.querySelector('meta[name="csrf-token"]')?.getAttribute('content') ||
          document.querySelector('input[name="csrfmiddlewaretoken"]')?.value ||
          getCookie('csrftoken');

        var cartContainer = document.querySelector('.cart-page-container');
        var clearUrl = cartContainer?.dataset.cartClearUrl || '/cart/clean/';

        fetch(clearUrl, {
          method: 'POST',
          headers: {
            'X-CSRFToken': csrfToken,
            'X-Requested-With': 'XMLHttpRequest',
            'Content-Type': 'application/x-www-form-urlencoded;charset=UTF-8'
          }
        }).then(function (r) {
          if (!r.ok) {
            throw new Error('Network response was not ok');
          }
          return r.json();
        }).then(function (d) {
          if (d && d.ok) {
            location.reload();
          }
        }).catch(function (error) {
          // Тихая обработка - перезагружаем в любом случае
          location.reload();
        });
      }
    };
  }
  if (typeof window.AddToCart !== 'function') {
    window.AddToCart = function (btn) {
      var productId = btn.getAttribute('data-add-to-cart');
      var sizeInput = document.querySelector('input[name="size"]:checked');
      var size = (sizeInput ? sizeInput.value : 'S') || 'S';
      var wrap = btn.closest('[data-qty]') || document;
      var qtyInput = wrap.querySelector('input[type="number"]#qty') || wrap.querySelector('[data-qty] input[type="number"]') || document.querySelector('#qty') || (btn.parentElement ? btn.parentElement.querySelector('input[type="number"]') : null);
      var qty = Math.max(1, parseInt(qtyInput && qtyInput.value ? qtyInput.value : '1', 10));

      // Получаем выбранный цвет
      var colorVariantId = null;
      var activeColorSwatch = document.querySelector('#color-picker .color-swatch.active');
      if (activeColorSwatch) {
        colorVariantId = activeColorSwatch.getAttribute('data-variant');
      }

      // Получаем CSRF токен
      var csrfToken = getCookie('csrftoken') ||
        document.querySelector('meta[name="csrf-token"]')?.getAttribute('content') ||
        document.querySelector('input[name="csrfmiddlewaretoken"]')?.value ||
        getCookie('csrftoken');

      var body = new URLSearchParams({ product_id: String(productId), size: String(size).toUpperCase(), qty: String(qty) });
      if (colorVariantId) {
        body.append('color_variant_id', colorVariantId);
      }
      var fitInput = document.querySelector('input[name="fit_option"]:checked');
      if (fitInput && fitInput.value) {
        body.append('fit_option', fitInput.value);
      }

      fetch('/cart/add/', {
        method: 'POST',
        headers: {
          'X-CSRFToken': csrfToken,
          'X-Requested-With': 'XMLHttpRequest',
          'Content-Type': 'application/x-www-form-urlencoded;charset=UTF-8'
        },
        body: body
      }).then(function (r) {
        if (r.status === 403) {
          // Self-heal застарілого host-only csrftoken і повторна спроба.
          try { document.cookie = 'csrftoken=; expires=Thu, 01 Jan 1970 00:00:00 GMT; path=/'; } catch (_) { }
          return fetch('/cart/add/', {
            method: 'POST',
            headers: {
              'X-CSRFToken': getCookie('csrftoken') || csrfToken,
              'X-Requested-With': 'XMLHttpRequest',
              'Content-Type': 'application/x-www-form-urlencoded;charset=UTF-8'
            },
            body: body
          });
        }
        return r;
      }).then(function (r) {
        if (!r.ok) {
          throw new Error('Network response was not ok');
        }
        return r.json();
      }).then(function (d) {
        if (d && d.ok) {
          try {
            if (window.updateCartBadge) window.updateCartBadge(d.count);
            document.dispatchEvent(new CustomEvent('cartUpdated', {
              detail: { action: 'add', productId: productId, source: 'ui-fallback' }
            }));
            if (window.refreshMiniCart) window.refreshMiniCart();
            if (window.openMiniCart) window.openMiniCart();
            if (document.querySelector('.cart-page-container')) {
              try {
                if (window.refreshCartPage) { window.refreshCartPage(); }
                else { window.location.reload(); }
              } catch (_) { window.location.reload(); }
            }
          } catch (_) { }
          try {
            if (window.__twcTrackAddToCart) {
              window.__twcTrackAddToCart(d, btn, qty);
            }
          } catch (trackErr) {
            if (window.console && console.debug) {
              console.debug('AddToCart tracking error:', trackErr);
            }
          }
          // AddToCart tracking handled by main.js to avoid duplication
        } else {
          console.error('Add to cart failed:', d);
        }
      }).catch(function (error) {
        console.error('Add to cart error:', error);
        alert('Помилка при додаванні товару до кошика. Спробуйте ще раз.');
      });
      return false;
    };
  }
})();

// Применяем CSS переменные для комбинированных цветов в корзине и заказах
document.addEventListener('DOMContentLoaded', function () {
  document.querySelectorAll('.swatch[data-secondary]:not([data-secondary=""])').forEach(function (swatch) {
    var primary = swatch.getAttribute('data-primary');
    var secondary = swatch.getAttribute('data-secondary');

    if (secondary && secondary !== '') {
      swatch.style.setProperty('--primary-color', primary);
      swatch.style.setProperty('--secondary-color', secondary);
    }
  });
});
