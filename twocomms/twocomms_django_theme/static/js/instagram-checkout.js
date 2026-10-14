(() => {
  "use strict";

  const root = document.querySelector("[data-ig-checkout]");
  if (!root) return;

  const locale = String(document.documentElement.lang || "uk").split("-")[0].toLowerCase();
  const fallbackErrors = {
    uk: "Не вдалося перевірити Нову пошту. Оновіть сторінку.",
    ru: "Не удалось проверить Новую почту. Обновите страницу.",
    en: "We could not verify Nova Poshta. Refresh the page.",
  };
  const paymentFallbackErrors = {
    uk: {
      expired: "Термін дії пропозиції завершився.",
      unavailable: "Цю пропозицію більше не можна оплатити.",
      in_progress: "Платіж уже створюється. Зачекайте кілька секунд.",
      provider_ambiguous: "Банк ще перевіряє платіж. Не повторюйте оплату — ми звіримо статус і повідомимо вас у Direct.",
      full_name: "Вкажіть ім'я та прізвище.",
      phone: "Вкажіть коректний український номер телефону.",
      email: "Перевірте email для чека.",
      city: "Оберіть місто зі списку Нової пошти.",
      np_office: "Оберіть відділення або поштомат зі списку Нової пошти.",
      promo_unavailable: "Промокод для цієї пропозиції недоступний.",
      promo_invalid: "Промокод недійсний або вже використаний.",
      promo_requires_account: "Цей промокод доступний лише в особистому кабінеті.",
      catalog_changed: "Товар або його умови змінилися. Попросіть бота оновити пропозицію.",
      invalid_amount: "Сума замовлення має бути більшою за нуль.",
      item_unavailable: "Один із товарів більше недоступний.",
      empty_items: "У пропозиції немає товарів.",
      default: "Не вдалося створити платіж. Спробуйте ще раз.",
    },
    ru: {
      expired: "Срок действия предложения истек.",
      unavailable: "Это предложение больше нельзя оплатить.",
      in_progress: "Платеж уже создается. Подождите несколько секунд.",
      provider_ambiguous: "Банк еще проверяет платеж. Не повторяйте оплату — мы сверим статус и сообщим вам в Direct.",
      full_name: "Укажите имя и фамилию.",
      phone: "Укажите корректный украинский номер телефона.",
      email: "Проверьте email для чека.",
      city: "Выберите город из списка Новой почты.",
      np_office: "Выберите отделение или почтомат из списка Новой почты.",
      promo_unavailable: "Промокод для этого предложения недоступен.",
      promo_invalid: "Промокод недействителен или уже использован.",
      promo_requires_account: "Этот промокод доступен только в личном кабинете.",
      catalog_changed: "Товар или его условия изменились. Попросите бота обновить предложение.",
      invalid_amount: "Сумма заказа должна быть больше нуля.",
      item_unavailable: "Один из товаров больше недоступен.",
      empty_items: "В предложении нет товаров.",
      default: "Не удалось создать платеж. Попробуйте еще раз.",
    },
    en: {
      expired: "This offer has expired.",
      unavailable: "This offer can no longer be paid.",
      in_progress: "A payment is already being created. Please wait a few seconds.",
      provider_ambiguous: "The bank is still checking this payment. Do not pay again; we will verify it and message you in Direct.",
      full_name: "Enter your first and last name.",
      phone: "Enter a valid Ukrainian phone number.",
      email: "Check the receipt email.",
      city: "Choose a city from the Nova Poshta list.",
      np_office: "Choose a branch or locker from the Nova Poshta list.",
      promo_unavailable: "A promo code is not available for this offer.",
      promo_invalid: "The promo code is invalid or already used.",
      promo_requires_account: "This promo code is available only in an account.",
      catalog_changed: "An item or its terms changed. Ask the bot for an updated offer.",
      invalid_amount: "The order total must be greater than zero.",
      item_unavailable: "One of the items is no longer available.",
      empty_items: "This offer has no items.",
      default: "We could not create the payment. Please try again.",
    },
  };
  const prefersReducedMotion = window.matchMedia?.("(prefers-reduced-motion: reduce)")?.matches;

  const readJsonResponse = async (response) => {
    const contentType = String(response.headers?.get("content-type") || "").toLowerCase();
    if (!contentType.includes("application/json")) return {};
    try {
      const payload = await response.json();
      return payload && typeof payload === "object" && !Array.isArray(payload) ? payload : {};
    } catch (_error) {
      return {};
    }
  };

  const paymentErrorMessage = (code) => {
    const copy = paymentFallbackErrors[locale] || paymentFallbackErrors.uk;
    const safeCode = typeof code === "string" ? code : "";
    return copy[safeCode] || copy.default;
  };

  const paymentErrorField = (code) => ({
    promo_invalid: 'promo_code',
    promo_unavailable: 'promo_code',
    promo_requires_account: 'promo_code',
  }[code] || code);

  const trackCheckoutEvent = (name, eventId, extra = {}) => {
    if (!eventId || window.__twcAnalyticsConsent !== true || typeof window.trackEvent !== "function") return;
    window.trackEvent(name, {
      event_id: eventId,
      value: Number(root.dataset.analyticsValue || 0),
      currency: root.dataset.analyticsCurrency || "UAH",
      ...extra,
      __meta: { event_id: eventId },
    });
  };

  // The bearer entry redirects to a clean URL before this runs. Crawlers do
  // not execute this browser-only bridge, and repeated loads reuse the event id.
  trackCheckoutEvent("ViewContent", document.documentElement.dataset.viewContentEventId);
  trackCheckoutEvent("Purchase", document.documentElement.dataset.purchaseEventId);

  const csrfToken = () =>
    document.querySelector('[name="csrfmiddlewaretoken"]')?.value || "";

  const setActionLabel = (button, value) => {
    const label = button?.querySelector("[data-action-label]");
    if (label) label.textContent = value;
  };

  const paymentRail = document.querySelector("[data-payment-rail]");
  const paymentButton = document.querySelector("[data-payment-submit]");
  const paymentContinue = document.querySelector("[data-payment-continue]");
  const checkoutForm = document.querySelector("[data-np-form]");
  let checkoutExpired = false;
  const shareDialog = document.querySelector("[data-share-dialog]");
  const priceDialog = document.querySelector("[data-price-dialog]");
  const priceDetails = document.querySelector("[data-price-details]");
  const checkoutStateBanner = document.querySelector("[data-checkout-state-banner]");
  const cartProducts = document.querySelector("[data-cart-products]");
  const cartFeedback = document.querySelector("[data-cart-feedback]");
  const cartStatus = document.querySelector("[data-cart-status]");
  const cartRetry = document.querySelector("[data-cart-retry]");
  const cartCopy = ({
    uk: { saving: "Зберігаємо зміни…", saved: "Зміни збережено. Сума оновлена.", review: "Зміни збережено. Менеджер перевірить нові умови перед оплатою.", unknown: "Перевіряємо, чи збережено зміни. Оплата тимчасово недоступна. Перевірте збереження перед наступною зміною.", failed: "Не вдалося зберегти зміни. Кошик не змінено.", stale: "Кошик уже оновлено. Перевірте актуальні товари й суму перед оплатою.", blocked: "Зараз змінювати це замовлення не можна. Актуальний стан можна уточнити в Direct.", pendingPrice: "Після погодження", empty: "У кошику немає товарів. Додати товари допоможемо в Direct." },
    ru: { saving: "Сохраняем изменения…", saved: "Изменения сохранены. Сумма обновлена.", review: "Изменения сохранены. Менеджер проверит новые условия перед оплатой.", unknown: "Проверяем, сохранились ли изменения. Оплата временно недоступна. Проверьте сохранение перед следующим изменением.", failed: "Не удалось сохранить изменения. Корзина не изменена.", stale: "Корзина уже обновлена. Проверьте актуальные товары и сумму перед оплатой.", blocked: "Сейчас изменить этот заказ нельзя. Актуальный статус можно уточнить в Direct.", pendingPrice: "После согласования", empty: "В корзине нет товаров. Добавить товары поможем в Direct." },
    en: { saving: "Saving changes…", saved: "Changes saved. The total is updated.", review: "Changes saved. A manager will confirm the updated terms before payment.", unknown: "Checking whether your changes were saved. Payment is temporarily unavailable. Check saved changes before editing again.", failed: "Changes could not be saved. Your basket is unchanged.", stale: "Your basket has already been updated. Check the current items and total before paying.", blocked: "This order cannot be edited right now. Contact us in Direct for its current status.", pendingPrice: "After confirmation", empty: "Your basket is empty. We can help you add items in Direct." },
  }[locale] || null) || {
    saving: "Saving changes…", saved: "Changes saved.", review: "Changes saved. Awaiting manager confirmation.", unknown: "Checking saved changes. Payment is unavailable.", failed: "Changes could not be saved.", stale: "Basket updated. Check the current total.", blocked: "This order cannot be edited right now.", pendingPrice: "After confirmation", empty: "Your basket is empty.",
  };
  const deliveryCopy = ({
    uk: { free: "Ваша доставка безкоштовна", remaining: "До безкоштовної доставки — ще", goods: "Товари після знижок", threshold: "безкоштовно від", gained: "Тепер доставка безкоштовна — її оплачує TwoComms.", lost: "Кошик оновлено. Доставка тепер оплачується окремо Новій пошті; підтвердьте ці умови, щоб продовжити.", covered: "Доставку оплачує TwoComms", manual: "Доставка за погодженими умовами", manualDetail: "Погоджені з вами умови зберігаються окремо від акції на безкоштовну доставку.", review: "Умови доставки уточнюємо", reviewDetail: "Менеджер перевірить оновлений кошик і підтвердить доставку перед оплатою.", ack: "Підтвердьте нові умови доставки нижче, щоб продовжити з цими товарами." },
    ru: { free: "Ваша доставка бесплатная", remaining: "До бесплатной доставки — ещё", goods: "Товары после скидок", threshold: "бесплатно от", gained: "Теперь доставка бесплатная — её оплачивает TwoComms.", lost: "Корзина обновлена. Доставка теперь оплачивается отдельно Новой почте; подтвердите эти условия, чтобы продолжить.", covered: "Доставку оплачивает TwoComms", manual: "Доставка по согласованным условиям", manualDetail: "Согласованные с вами условия сохраняются отдельно от акции на бесплатную доставку.", review: "Условия доставки уточняем", reviewDetail: "Менеджер проверит обновлённую корзину и подтвердит доставку перед оплатой.", ack: "Подтвердите новые условия доставки ниже, чтобы продолжить с этими товарами." },
    en: { free: "Your delivery is free", remaining: "Free delivery is within reach — add", goods: "Items after discounts", threshold: "free from", gained: "Your delivery is now free — TwoComms covers it.", lost: "Your basket is updated. Delivery is now paid separately to Nova Poshta; confirm these terms to continue.", covered: "TwoComms covers delivery", manual: "Delivery under your agreed terms", manualDetail: "Your agreed terms are separate from the free delivery promotion.", review: "Confirming your delivery terms", reviewDetail: "A manager will review your updated basket and confirm delivery before payment.", ack: "Confirm the updated delivery terms below to continue with these items." },
  }[locale] || null);
  let cartState = null;
  try {
    const initialCart = JSON.parse(document.getElementById("ig-cart-state")?.textContent || "null");
    if (initialCart && Array.isArray(initialCart.items)) cartState = initialCart;
  } catch (_error) { /* Missing capability data must not grant edit permission. */ }
  let cartSaving = false;
  let cartNeedsReload = false;
  let removedCartFocus = false;
  let pendingCartOperation = null;
  let paymentCreating = false;
  let previousDeliveryPolicy = null;
  const operationId = () => window.crypto?.randomUUID?.() || "";
  const cartStorageKey = `twc-cart-amendment:${root.dataset.proposalUrl || window.location.pathname}`;
  const rememberCartOperation = (operation) => {
    try {
      if (operation) window.sessionStorage.setItem(cartStorageKey, JSON.stringify(operation));
      else window.sessionStorage.removeItem(cartStorageKey);
    } catch (_error) { /* Exact replay still works in memory when storage is disabled. */ }
  };
  try {
    const remembered = JSON.parse(window.sessionStorage.getItem(cartStorageKey) || "null");
    if (remembered?.action === "cart_amendment" && typeof remembered.operation_id === "string"
      && ["quantity", "remove"].includes(remembered.operation) && typeof remembered.line_id === "string"
      && Number.isInteger(remembered.expected_revision) && typeof remembered.expected_digest === "string") {
      pendingCartOperation = remembered;
    }
  } catch (_error) { /* Storage is optional. */ }

  const showCartStatus = (message, kind = "") => {
    if (cartStatus) cartStatus.textContent = message;
    if (cartFeedback) {
      cartFeedback.hidden = !message;
      cartFeedback.classList.toggle("is-error", kind === "error");
      cartFeedback.classList.toggle("is-review", kind === "review");
    }
    if (cartRetry) cartRetry.hidden = !pendingCartOperation || cartSaving;
  };
  const cartCanPay = () => !cartSaving && !cartNeedsReload && !pendingCartOperation && !paymentCreating
    && (!cartState || cartState.can_pay === true) && !checkoutExpired;
  const syncCartCapabilities = () => {
    const canEdit = cartState?.can_edit === true && !checkoutExpired && !cartSaving
      && !cartNeedsReload && !pendingCartOperation && !paymentCreating;
    cartProducts?.setAttribute("aria-busy", cartSaving ? "true" : "false");
    cartProducts?.querySelectorAll("[data-cart-action]").forEach((button) => {
      const row = button.closest("[data-cart-line]");
      const item = cartState?.items.find((line) => String(line.line_id) === row?.dataset.cartLine);
      const quantity = Number(item?.quantity);
      button.disabled = !canEdit || !item
        || (button.dataset.cartAction === "decrease" && quantity <= 1)
        || (button.dataset.cartAction === "increase" && quantity >= 50);
    });
    if (paymentButton) paymentButton.disabled = !cartCanPay();
    paymentRail?.classList.toggle("is-cart-blocked", !cartCanPay());
    if (cartRetry) cartRetry.disabled = cartSaving || paymentCreating || checkoutExpired;
    renderDeliveryPolicy(cartState, true);
  };
  const validCartState = (value) => value && Array.isArray(value.items)
    && Number.isInteger(value.revision) && typeof value.digest === "string"
    && typeof value.can_edit === "boolean" && typeof value.can_pay === "boolean";
  const renderDeliveryPolicy = (state, quiet = false) => {
    const card = document.querySelector("[data-delivery-benefit]");
    if (!card || !deliveryCopy || !state) return;
    const candidate = state.free_delivery;
    const policy = candidate?.schema === "shipping-policy.v1" && candidate.automatic === true
      && candidate.authority === "automatic_threshold" && typeof candidate.eligible === "boolean"
      && Number.isInteger(candidate.progress) && candidate.progress >= 0 && candidate.progress <= 100
      && [candidate.remaining, candidate.threshold, candidate.basis_total].every((value) => typeof value === "string" && Number.isFinite(Number(value)))
      ? candidate : null;
    const money = (value) => `${new Intl.NumberFormat(locale === "uk" ? "uk-UA" : locale === "ru" ? "ru-UA" : "en-GB", {
      maximumFractionDigits: 2,
    }).format(Number(value))} ${policy?.currency || "UAH"}`;
    const title = card.querySelector("[data-delivery-benefit-title]");
    const detail = card.querySelector("[data-delivery-benefit-detail]");
    const meter = card.querySelector("[data-delivery-benefit-meter]");
    const update = card.querySelector("[data-delivery-benefit-update]");
    const ackWrap = card.querySelector("[data-delivery-policy-ack-wrap]");
    const ack = card.querySelector("[data-delivery-policy-ack]");
    const digest = card.querySelector("[data-delivery-policy-digest]");
    const pending = state.review_needed === true;
    const editable = state.can_edit === true && !paymentCreating && !checkoutExpired;
    const recipientCopy = { uk: "Доставку оплачуєте окремо Новій пошті", ru: "Доставку оплачиваете отдельно Новой почте", en: "You pay Nova Poshta for delivery separately" };
    card.hidden = !policy && !pending && !card.dataset.manualPayer;
    card.classList.toggle("is-achieved", policy?.eligible === true);
    card.classList.toggle("is-review", pending);
    if (meter) meter.hidden = !policy || !editable;
    if (title) title.textContent = policy ? policy.eligible ? deliveryCopy.free
      : editable ? `${deliveryCopy.remaining} ${money(policy.remaining)}` : recipientCopy[locale] || recipientCopy.en
      : pending ? deliveryCopy.review : card.dataset.manualPayer === "merchant" ? deliveryCopy.covered : deliveryCopy.manual;
    if (detail) detail.textContent = policy ? `${deliveryCopy.goods}: ${money(policy.basis_total)}${editable ? ` · ${deliveryCopy.threshold} ${money(policy.threshold)}` : ""}`
      : pending ? deliveryCopy.reviewDetail : deliveryCopy.manualDetail;
    if (policy) {
      const bar = card.querySelector("[data-delivery-benefit-bar]");
      if (bar) bar.style.width = `${policy.progress}%`;
      const progress = card.querySelector("[data-delivery-benefit-progress]");
      progress?.setAttribute("aria-valuenow", String(policy.progress));
      progress?.setAttribute("aria-valuetext", `${title?.textContent || ""}. ${detail?.textContent || ""}`);
      const threshold = card.querySelector("[data-delivery-benefit-threshold]");
      if (threshold) threshold.textContent = money(policy.threshold);
    }
    const requiresAck = policy?.ack_required === true && typeof policy.ack_digest === "string" && state.can_pay === true;
    if (ackWrap) ackWrap.hidden = !requiresAck;
    if (ack) {
      if (!requiresAck || digest?.value !== policy.ack_digest) ack.checked = false;
      ack.required = requiresAck;
      ack.disabled = !requiresAck;
      if (!requiresAck) ack.removeAttribute("aria-invalid");
    }
    if (digest) {
      digest.value = policy?.ack_digest || "";
      // Every automatic policy is bound to the displayed economics, even
      // when delivery is free and no new consent checkbox is needed.
      digest.disabled = !policy;
    }
    if (update) {
      if (!editable) update.textContent = "";
      else if (policy?.ack_required) update.textContent = deliveryCopy.lost;
      else if (!quiet) update.textContent = policy?.eligible && previousDeliveryPolicy?.eligible === false ? deliveryCopy.gained : "";
    }
    if (!quiet) previousDeliveryPolicy = policy;
  };
  const applyCartState = (state) => {
    if (!validCartState(state)) return false;
    const changed = cartState && (cartState.digest !== state.digest || cartState.revision !== state.revision);
    cartState = state;
    renderDeliveryPolicy(state);
    const renderedLines = new Set([...cartProducts?.querySelectorAll("[data-cart-line]") || []].map((row) => row.dataset.cartLine));
    // A concurrent bot update can add a line. Reload the server-rendered
    // product facts before allowing payment for any item not visible here.
    cartNeedsReload = state.items.some((item) => !renderedLines.has(String(item.line_id)));
    document.documentElement.dataset.proposalRevision = String(state.revision);
    const money = (value) => value === null || value === undefined || value === ""
      ? cartCopy.pendingPrice : `${value} ${state.currency || "UAH"}`;
    const setText = (selector, value) => {
      document.querySelectorAll(selector).forEach((node) => { node.textContent = value; });
    };
    cartProducts?.querySelectorAll("[data-cart-line]").forEach((row) => {
      const item = state.items.find((line) => String(line.line_id) === row.dataset.cartLine);
      if (!item) {
        if (row.contains?.(document.activeElement)) removedCartFocus = true;
        row.remove();
        return;
      }
      row.querySelectorAll("[data-cart-quantity], [data-cart-stepper-quantity]").forEach((node) => { node.textContent = String(item.quantity); });
      const amount = row.querySelector("[data-cart-line-total]");
      if (amount) amount.textContent = money(item.line_total);
      const unitPrice = row.querySelector("[data-cart-unit-price]");
      if (unitPrice) {
        unitPrice.textContent = `${item.quantity} × ${money(item.unit_price)}`;
        unitPrice.hidden = Number(item.quantity) <= 1 || item.unit_price === null;
      }
    });
    const count = document.querySelector("[data-cart-count]");
    if (count) count.textContent = `${state.items.length} ${state.items.length === 1 ? count.dataset.singular : count.dataset.plural}`;
    setText("[data-cart-total]", money(state.total));
    setText("[data-cart-catalog-total]", money(state.catalog_total));
    setText("[data-cart-discount]", `−${money(state.discount)}`);
    setText("[data-cart-charge-now]", money(state.charge_now));
    document.querySelectorAll("[data-cart-discount-row]").forEach((node) => { node.hidden = !(Number(state.discount) > 0); });
    const isPrepayment = state.is_prepayment === true;
    document.querySelectorAll("[data-cart-charge-row]").forEach((node) => { node.hidden = !isPrepayment || root.dataset.checkoutState === "paid"; });
    const paymentAmount = state.payment_preview || isPrepayment ? state.charge_now : state.total;
    setText("[data-payment-amount] strong", money(paymentAmount));
    const paymentAmountLabels = document.querySelector("[data-payment-amount]")?.dataset;
    if (paymentAmountLabels) setText("[data-payment-amount] span", isPrepayment ? paymentAmountLabels.chargeLabel : paymentAmountLabels.totalLabel);
    root.dataset.analyticsValue = String(paymentAmount || "0");
    root.dataset.analyticsCurrency = state.currency || "UAH";
    document.querySelectorAll('[name="expected_revision"]').forEach((field) => { field.value = String(state.revision); });
    document.querySelectorAll('[name="expected_digest"]').forEach((field) => { field.value = state.digest; });
    if (changed) {
      const terms = checkoutForm?.elements.namedItem("assisted_terms_accepted");
      if (terms) { terms.checked = false; terms.dispatchEvent(new Event("change", { bubbles: true })); }
    }
    if (state.review_needed || !state.can_pay) {
      priceDetails?.setAttribute("aria-disabled", "true");
      if (priceDetails) priceDetails.disabled = true;
      closeCheckoutDialog(priceDialog);
      if (priceDetails) priceDetails.hidden = true;
    } else if (priceDetails) {
      priceDetails.hidden = false;
      priceDetails.disabled = false;
      priceDetails.removeAttribute("aria-disabled");
    }
    syncCartCapabilities();
    if (cartNeedsReload) window.location.reload();
    return true;
  };
  const submitCartOperation = async () => {
    if (!pendingCartOperation || cartSaving || paymentCreating || checkoutExpired) return;
    cartSaving = true;
    showCartStatus(cartCopy.saving);
    syncCartCapabilities();
    try {
      const body = new FormData();
      Object.entries(pendingCartOperation).forEach(([key, value]) => body.set(key, String(value)));
      const response = await fetch(root.dataset.proposalUrl || window.location.href, {
        method: "POST", body, credentials: "same-origin", cache: "no-store",
        headers: { Accept: "application/json", "X-CSRFToken": csrfToken() },
      });
      const payload = await readJsonResponse(response);
      if (typeof payload.redirect === "string" && payload.redirect.startsWith("/")) {
        pendingCartOperation = null;
        rememberCartOperation(null);
        cartNeedsReload = true;
        window.location.assign(payload.redirect);
        return;
      }
      const state = payload.cart_state || payload.current;
      const authoritative = applyCartState(state);
      if (!authoritative || response.status >= 500 || payload.error === "operation_unknown") {
        showCartStatus(cartCopy.unknown, "error");
        return;
      }
      pendingCartOperation = null;
      rememberCartOperation(null);
      if (state.items.length === 0) showCartStatus(cartCopy.empty, "review");
      else if (state.review_needed) {
        showCartStatus(cartCopy.review, "review");
        window.location.reload();
      }
      else if (!response.ok) showCartStatus(response.status === 409 ? cartCopy.stale : cartCopy.failed, "error");
      else showCartStatus(cartCopy.saved);
    } catch (_error) {
      // A lost response may follow a committed operation. Keep its exact CAS
      // and UUID, and block new edits/payment until that operation is replayed.
      showCartStatus(cartCopy.unknown, "error");
    } finally {
      cartSaving = false;
      syncCartCapabilities();
      if (cartRetry) cartRetry.hidden = !pendingCartOperation;
      if (removedCartFocus) {
        cartStatus?.focus?.({ preventScroll: true });
        removedCartFocus = false;
      }
    }
  };
  cartProducts?.addEventListener("click", (event) => {
    const button = event.target.closest("[data-cart-action]");
    if (!button || button.disabled || cartState?.can_edit !== true || cartSaving
      || pendingCartOperation || paymentCreating || checkoutExpired) return;
    const row = button.closest("[data-cart-line]");
    const item = cartState.items.find((line) => String(line.line_id) === row?.dataset.cartLine);
    const id = operationId();
    if (!item || !id) { showCartStatus(cartCopy.blocked, "error"); return; }
    const remove = button.dataset.cartAction === "remove";
    pendingCartOperation = {
      action: "cart_amendment", operation_id: id, operation: remove ? "remove" : "quantity",
      line_id: String(item.line_id), expected_revision: cartState.revision,
      expected_digest: cartState.digest, expected_selection_revision: cartState.selection_revision || 0,
    };
    if (!remove) pendingCartOperation.quantity = Number(item.quantity) + (button.dataset.cartAction === "increase" ? 1 : -1);
    rememberCartOperation(pendingCartOperation);
    void submitCartOperation();
  });
  cartRetry?.addEventListener("click", () => { void submitCartOperation(); });
  if (cartProducts?.querySelector("[data-cart-controls]")) root.classList.add("has-cart-controls");
  syncCartCapabilities();
  renderDeliveryPolicy(cartState);
  document.querySelector("[data-delivery-policy-ack]")?.addEventListener("change", (event) => {
    const ack = event.target;
    if (ack.checked) {
      ack.removeAttribute("aria-invalid");
      const error = checkoutForm?.querySelector("[data-form-error]");
      if (error?.textContent === deliveryCopy?.ack) error.hidden = true;
    }
  });
  checkoutForm?.elements.namedItem("promo_code")?.addEventListener("input", () => {
    const ack = document.querySelector("[data-delivery-policy-ack]");
    if (ack) ack.checked = false;
  });
  if (pendingCartOperation) showCartStatus(cartCopy.unknown, "error");
  let dialogTrigger = null;

  const showCheckoutDialog = (dialog, trigger) => {
    if (!dialog) return;
    dialogTrigger = trigger || document.activeElement;
    if (typeof dialog.showModal === "function") {
      dialog.showModal();
    } else {
      dialog.setAttribute("open", "");
    }
    window.setTimeout(() => {
      dialog.querySelector("button, a[href], input")?.focus({ preventScroll: true });
    }, 0);
  };

  const restoreDialogFocus = () => {
    dialogTrigger?.focus?.({ preventScroll: true });
    dialogTrigger = null;
  };

  const closeCheckoutDialog = (dialog) => {
    if (!dialog || (!dialog.open && !dialog.hasAttribute("open"))) return;
    if (typeof dialog.close === "function") {
      dialog.close();
    } else {
      dialog.removeAttribute("open");
    }
  };

  const bindCheckoutDialog = (dialog) => {
    dialog?.addEventListener("click", (event) => {
      if (event.target === dialog) dialog.close?.();
    });
    dialog?.addEventListener("close", restoreDialogFocus);
  };

  bindCheckoutDialog(priceDialog);
  bindCheckoutDialog(shareDialog);
  document.querySelectorAll("[data-price-details]").forEach((trigger) => {
    trigger.addEventListener("click", () => {
      if (!checkoutExpired) showCheckoutDialog(priceDialog, trigger);
    });
  });
  document.querySelectorAll("[data-share-open]").forEach((trigger) => {
    trigger.addEventListener("click", () => {
      if (checkoutExpired) return;
      if (shareDialog) shareDialog.dataset.preferNative = trigger.hasAttribute("data-share-native") ? "true" : "false";
      showCheckoutDialog(shareDialog, trigger);
    });
  });

  const writeClipboard = async (value) => {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(value);
      return;
    }
    const field = document.createElement("textarea");
    field.value = value;
    field.setAttribute("readonly", "");
    field.style.position = "fixed";
    field.style.opacity = "0";
    document.body.appendChild(field);
    field.select();
    document.execCommand("copy");
    field.remove();
  };

  document.querySelectorAll("[data-share-url]").forEach((button) => {
    button.addEventListener("click", async () => {
      if (checkoutExpired || !button.dataset.shareUrl) return;
      const original = button.dataset.shareLabel || button.textContent.trim();
      button.disabled = true;
      try {
        const response = await fetch(button.dataset.shareUrl, {
          method: "POST",
          credentials: "same-origin",
          cache: "no-store",
          headers: {
            "X-CSRFToken": csrfToken(),
            Accept: "application/json",
          },
        });
        const payload = await readJsonResponse(response);
        if (!response.ok || !payload.url) throw new Error("share_unavailable");
        if (checkoutExpired) return;
        const preferNativeShare = shareDialog?.dataset.preferNative === "true";
        if (preferNativeShare && navigator.share) {
          await navigator.share({ url: payload.url });
        } else {
          await writeClipboard(payload.url);
        }
        setActionLabel(button, button.dataset.shareDone || original);
      } catch (error) {
        setActionLabel(
          button,
          error?.name === "AbortError"
            ? original
            : (button.dataset.shareError || original),
        );
      } finally {
        window.setTimeout(() => {
          setActionLabel(button, original);
          button.disabled = checkoutExpired;
        }, 1800);
      }
    });
  });

  const countdown = document.querySelector("[data-countdown]");
  const expiryStatus = document.querySelector("[data-expiry-status]");
  const countdownWrap = document.querySelector("[data-countdown-wrap]");
  const countdownRing = document.querySelector("[data-countdown-ring]");
  const expiresAt = Date.parse(root.dataset.expiresAt || "");
  const createdAt = Date.parse(root.dataset.createdAt || "");
  const countdownDuration = Number.isFinite(expiresAt)
    ? Math.max(1000, expiresAt - (Number.isFinite(createdAt) ? createdAt : expiresAt - 25 * 60 * 1000))
    : 0;
  const ringCircumference = 2 * Math.PI * 17;
  if (countdownRing) {
    countdownRing.style.strokeDasharray = `${ringCircumference}`;
    countdownRing.style.strokeDashoffset = "0";
  }

  const expireCheckout = () => {
    if (checkoutExpired) return;
    checkoutExpired = true;
    syncCartCapabilities();
    root.classList.add("is-expired");
    root.dataset.checkoutState = "expired";
    countdownWrap?.classList.add("is-expired");
    dialogTrigger = null;
    closeCheckoutDialog(priceDialog);
    closeCheckoutDialog(shareDialog);
    if (priceDetails) {
      priceDetails.classList.remove("ig-state--ready");
      priceDetails.classList.add("ig-state--expired");
      priceDetails.querySelector(".ig-state__copy strong").textContent = priceDetails.dataset.expiredTitle || "";
      priceDetails.querySelector(".ig-state__copy small").textContent = priceDetails.dataset.expiredBody || "";
      priceDetails.disabled = true;
      priceDetails.removeAttribute("aria-haspopup");
      priceDetails.removeAttribute("aria-controls");
      const stateIcon = priceDetails.querySelector("[data-state-icon]");
      stateIcon?.setAttribute("data-state-icon", "attention");
      stateIcon?.querySelector("use")?.setAttribute("href", "#ig-icon-alert");
    }
    if (checkoutStateBanner) {
      checkoutStateBanner.classList.remove("ig-state--pending", "ig-state--locked");
      checkoutStateBanner.classList.add("ig-state--expired");
      checkoutStateBanner.querySelector(".ig-state__copy strong").textContent = checkoutStateBanner.dataset.expiredTitle || "";
      checkoutStateBanner.querySelector(".ig-state__copy small").textContent = checkoutStateBanner.dataset.expiredBody || "";
      const stateIcon = checkoutStateBanner.querySelector("[data-state-icon]");
      stateIcon?.setAttribute("data-state-icon", "attention");
      stateIcon?.querySelector("use")?.setAttribute("href", "#ig-icon-alert");
    }
    document.querySelectorAll("[data-share-open]").forEach((trigger) => {
      trigger.disabled = true;
      trigger.setAttribute("aria-disabled", "true");
    });
    document.querySelectorAll("[data-share-url]").forEach((button) => {
      button.disabled = true;
      button.removeAttribute("data-share-url");
    });
    if (paymentRail) paymentRail.classList.add("is-expired");
    if (paymentButton) {
      paymentButton.disabled = true;
      setActionLabel(paymentButton, countdown?.dataset.expiredLabel || "Час завершився");
    }
    if (paymentContinue) {
      paymentContinue.removeAttribute("href");
      paymentContinue.setAttribute("aria-disabled", "true");
      paymentContinue.setAttribute("tabindex", "-1");
      setActionLabel(paymentContinue, countdown?.dataset.expiredLabel || "Час завершився");
    }
    checkoutForm?.querySelectorAll("input, button, select, textarea").forEach((field) => {
      if (field !== paymentButton) field.disabled = true;
    });
    if (countdown) {
      countdown.textContent = "00:00";
      countdown.setAttribute("aria-label", countdown.dataset.expiredLabel || "00:00");
    }
    if (expiryStatus && !expiryStatus.textContent) {
      expiryStatus.textContent = countdown?.dataset.expiredLabel || "00:00";
    }
    const directHelp = root.querySelector("[data-direct-help]");
    directHelp?.classList.add("is-priority");
  };

  if (countdown && Number.isFinite(expiresAt)) {
    const renderCountdown = () => {
      const remaining = Math.max(0, expiresAt - Date.now());
      const remainingSeconds = Math.ceil(remaining / 1000);
      const minutes = Math.floor(remainingSeconds / 60);
      const seconds = remainingSeconds % 60;
      const progress = countdownDuration ? Math.min(1, remaining / countdownDuration) : 0;
      countdownRing?.style.setProperty("stroke-dashoffset", `${ringCircumference * (1 - progress)}`);
      root.style.setProperty("--countdown-progress", `${progress}`);
      countdownWrap?.classList.toggle("is-expiring", remaining > 0 && remaining <= 5 * 60 * 1000);
      if (!remaining) {
        expireCheckout();
        return false;
      }
      countdown.textContent = `${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}`;
      return true;
    };
    renderCountdown();
    const countdownTimer = window.setInterval(() => {
      if (!renderCountdown()) window.clearInterval(countdownTimer);
    }, 1000);
    window.addEventListener("beforeunload", () => window.clearInterval(countdownTimer), { once: true });
  }

  if (["pending", "amendment_review"].includes(root.dataset.checkoutState) && root.dataset.statusUrl) {
    const waitingForReview = root.dataset.checkoutState === "amendment_review";
    const generationExpiresAt = Date.parse(root.dataset.generationExpiresAt || "");
    let generationExpiryTimer;
    if (Number.isFinite(generationExpiresAt)) {
      const remaining = Math.max(0, generationExpiresAt - Date.now() + 500);
      generationExpiryTimer = window.setTimeout(() => window.location.reload(), remaining);
      window.addEventListener(
        "beforeunload",
        () => window.clearTimeout(generationExpiryTimer),
        { once: true },
      );
    }
    let polling = false;
    let pollCount = 0;
    const pollDelays = [3000, 5000, 8000, 12000, 18000, 25000, 30000, 30000];
    const pollStatus = async () => {
      if (polling || document.visibilityState !== "visible" || (!waitingForReview && pollCount >= pollDelays.length)) return;
      pollCount += 1;
      polling = true;
      try {
        const response = await fetch(root.dataset.statusUrl, {
          credentials: "same-origin",
          cache: "no-store",
          headers: { Accept: "application/json" },
        });
        if (!response.ok) return;
        const payload = await readJsonResponse(response);
        if (["verified", "amendment_approved"].includes(payload.state) && payload.redirect) {
          window.location.assign(payload.redirect);
          return;
        }
        if (payload.state && ["failed", "expired", "cancellation_ambiguous", "reissue"].includes(payload.state)) {
          window.location.reload();
        }
      } catch (_error) {
        // A later poll retries without exposing payment or recipient data.
      } finally {
        polling = false;
      }
    };
    let pendingTimer;
    const schedulePoll = () => {
      if (!waitingForReview && pollCount >= pollDelays.length) return;
      pendingTimer = window.setTimeout(async () => {
        await pollStatus();
        schedulePoll();
      }, pollDelays[pollCount] || 60000);
    };
    schedulePoll();
    window.addEventListener("beforeunload", () => window.clearTimeout(pendingTimer), { once: true });
  }

  document.querySelectorAll("[data-product-image]").forEach((image) => {
    const enableFallback = () => {
      image.closest("[data-product-media]")?.classList.add("is-fallback");
    };
    image.addEventListener("error", enableFallback);
    if (image.complete && image.naturalWidth === 0) enableFallback();
  });

  const form = checkoutForm;
  if (form) {
    const errorBox = form.querySelector("[data-form-error]");
    const paymentLabel = paymentButton?.querySelector("[data-action-label]");
    const defaultPaymentLabel = paymentLabel?.textContent || "";
    let submitted = false;
    const serverErrorFieldName = document.documentElement.dataset.formErrorField || "";
    const serverErrorField = serverErrorFieldName
      ? form.elements.namedItem(serverErrorFieldName)
      : null;
    let serverErrorPending = Boolean(
      serverErrorField instanceof HTMLElement
      && serverErrorField.getAttribute("aria-invalid") === "true",
    );
    const validationSummaryCopy = {
      uk: "Перевірте виділене поле, щоб продовжити.",
      ru: "Проверьте выделенное поле, чтобы продолжить.",
      en: "Check the highlighted field to continue.",
    };

    const invalidField = (fieldName = "") => {
      const namedField = fieldName ? form.elements.namedItem(fieldName) : null;
      return namedField instanceof HTMLElement
        ? namedField
        : form.querySelector("input[aria-invalid='true'], input:invalid, select:invalid, textarea:invalid");
    };

    const setFieldError = (field, message) => {
      if (!(field instanceof HTMLElement)) return;
      const wrapper = field.closest(".ig-field");
      if (!wrapper) return;
      let fieldError = wrapper.querySelector("[data-field-error], .cart-form-error");
      if (!fieldError) {
        fieldError = document.createElement("small");
        wrapper.appendChild(fieldError);
      }
      fieldError.classList.add("ig-field-error");
      fieldError.dataset.fieldError = "";
      if (!fieldError.id) {
        fieldError.id = `${field.id || field.getAttribute("name") || "ig-field"}-error`;
      }
      fieldError.textContent = message || validationSummaryCopy[locale] || validationSummaryCopy.uk;
      fieldError.hidden = false;
      field.setAttribute("aria-invalid", "true");
      const describedBy = new Set(
        String(field.getAttribute("aria-describedby") || "").split(/\s+/).filter(Boolean),
      );
      describedBy.add(fieldError.id);
      field.setAttribute("aria-describedby", [...describedBy].join(" "));
    };

    const clearFieldError = (field) => {
      if (!(field instanceof HTMLElement)) return;
      const fieldError = field.closest(".ig-field")?.querySelector("[data-field-error], .cart-form-error");
      if (fieldError) fieldError.hidden = true;
      if (field.checkValidity?.()) field.removeAttribute("aria-invalid");
    };

    const showFormError = (message, fieldName = "") => {
      if (errorBox) {
        errorBox.textContent = message || validationSummaryCopy[locale] || validationSummaryCopy.uk;
        errorBox.hidden = false;
      }
      setFieldError(invalidField(fieldName), message);
      focusFirstInvalid(fieldName);
    };

    const focusFirstInvalid = (fieldName = "") => {
      const field = invalidField(fieldName);
      if (!(field instanceof HTMLElement)) {
        errorBox?.focus({ preventScroll: true });
        return;
      }
      field.closest("details")?.setAttribute("open", "");
      field.setAttribute("aria-invalid", "true");
      if (errorBox?.id) {
        const describedBy = new Set(
          String(field.getAttribute("aria-describedby") || "").split(/\s+/).filter(Boolean),
        );
        describedBy.add(errorBox.id);
        field.setAttribute("aria-describedby", [...describedBy].join(" "));
      }
      field.style.scrollMarginBottom = `${(paymentRail?.offsetHeight || 0) + 24}px`;
      field.scrollIntoView({
        block: "center",
        behavior: prefersReducedMotion ? "auto" : "smooth",
      });
      window.setTimeout(() => field.focus({ preventScroll: true }), prefersReducedMotion ? 0 : 220);
    };

    const syncServerError = () => {
      if (!serverErrorPending) return;
      const fieldName = serverErrorFieldName;
      if (!fieldName) return;
      const field = form.elements.namedItem(fieldName);
      if (!field || typeof field.setAttribute !== "function") return;
      field.setAttribute("aria-invalid", "true");
      if (errorBox?.id) {
        const describedBy = new Set(
          String(field.getAttribute("aria-describedby") || "")
            .split(/\s+/)
            .filter(Boolean),
        );
        describedBy.add(errorBox.id);
        field.setAttribute("aria-describedby", [...describedBy].join(" "));
      }
    };
    syncServerError();
    if (document.documentElement.dataset.formErrorField && errorBox && !errorBox.hidden) {
      window.setTimeout(() => focusFirstInvalid(document.documentElement.dataset.formErrorField), 0);
    }

    const setFocusedState = () => {
      root.classList.toggle(
        "is-field-focused",
        Boolean(form.querySelector("input:focus")),
      );
    };
    form.addEventListener("focusin", setFocusedState);
    form.addEventListener("focusout", () => window.setTimeout(setFocusedState, 0));

    form.querySelectorAll("input:not([type='hidden'])").forEach((field) => {
      const wrapper = field.closest(".ig-field");
      const syncCompletion = (event) => {
        if (serverErrorPending && serverErrorField === field) {
          if (!event?.isTrusted) {
            if (typeof window.queueMicrotask === "function") {
              window.queueMicrotask(syncServerError);
            } else {
              Promise.resolve().then(syncServerError);
            }
            return;
          }
          serverErrorPending = false;
        }
        const optionalEmpty = !field.required && !field.value.trim();
        if (field.value.trim()) clearFieldError(field);
        wrapper?.classList.toggle(
          "is-complete",
          !optionalEmpty && field.value.trim() !== "" && field.checkValidity(),
        );
        const requiredFields = [...form.querySelectorAll("input[required]")];
        const deliverySelectionsAreSigned = () => {
          const value = (selector) => form.querySelector(selector)?.value?.trim() || "";
          return Boolean(
            (value("[data-np-settlement-ref]") || value("[data-np-city-ref]"))
            && value("[data-np-city-token]")
            && value("[data-np-warehouse-ref]")
            && value("[data-np-warehouse-token]"),
          );
        };
        const isPaymentReady = requiredFields.length > 0
          && requiredFields.every((requiredField) => (
            requiredField.checkValidity()
            && requiredField.value.trim()
            && requiredField.getAttribute("aria-invalid") !== "true"
          ))
          && deliverySelectionsAreSigned();
        const wasPaymentReady = paymentRail?.classList.contains("is-payment-ready");
        paymentRail?.classList.toggle("is-payment-ready", isPaymentReady);
        if (isPaymentReady && !wasPaymentReady && !prefersReducedMotion) {
          paymentRail?.classList.remove("has-readiness-sheen");
          void paymentRail?.offsetWidth;
          paymentRail?.classList.add("has-readiness-sheen");
          window.setTimeout(() => paymentRail?.classList.remove("has-readiness-sheen"), 700);
        }
      };
      field.addEventListener("input", syncCompletion);
      field.addEventListener("change", syncCompletion);
      syncCompletion();
    });

    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      event.stopImmediatePropagation();
      if (submitted) return;
      if (errorBox) {
        errorBox.hidden = true;
        errorBox.textContent = "";
      }

      if (checkoutExpired) {
        showFormError(paymentErrorMessage("expired"));
        return;
      }
      if (!cartCanPay()) {
        showCartStatus(pendingCartOperation ? cartCopy.unknown : cartState?.review_needed ? cartCopy.review : cartCopy.blocked, "review");
        return;
      }
      if (!form.checkValidity()) {
        const firstInvalid = invalidField();
        const invalidName = firstInvalid?.getAttribute("name") || "default";
        showFormError(invalidName === "delivery_policy_ack" ? deliveryCopy?.ack : paymentErrorMessage(invalidName), invalidName);
        return;
      }
      const bridge = window.TwoCommsNovaPoshta;
      if (!bridge?.validateForm) {
        showFormError(fallbackErrors[locale] || fallbackErrors.uk);
        return;
      }

      // Reserve the action before asynchronous delivery validation. A second
      // click or a basket edit must not race with invoice creation.
      submitted = true;
      paymentCreating = true;
      syncCartCapabilities();
      let result;
      try {
        result = await bridge.validateForm(form);
      } catch (_error) {
        submitted = false;
        paymentCreating = false;
        syncCartCapabilities();
        showFormError(fallbackErrors[locale] || fallbackErrors.uk);
        return;
      }
      if (!result.ok) {
        submitted = false;
        paymentCreating = false;
        syncCartCapabilities();
        showFormError(result.message, result.field);
        return;
      }
      if (checkoutExpired || pendingCartOperation || cartNeedsReload || cartState?.can_pay === false) {
        submitted = false;
        paymentCreating = false;
        syncCartCapabilities();
        showFormError(paymentErrorMessage(checkoutExpired ? "expired" : "unavailable"));
        return;
      }

      const initiateEventId = document.documentElement.dataset.initiateCheckoutEventId;
      trackCheckoutEvent("InitiateCheckout", initiateEventId, {
        num_items: document.querySelectorAll(".ig-item").length,
      });
      if (paymentButton) {
        paymentButton.disabled = true;
        setActionLabel(
          paymentButton,
          paymentButton.dataset.loadingLabel || defaultPaymentLabel,
        );
      }
      let paymentResponseKnown = false;
      try {
        const paymentBody = new FormData(form);
        if (cartState) {
          paymentBody.set("expected_revision", String(cartState.revision));
          paymentBody.set("expected_digest", cartState.digest);
        }
        const response = await fetch(form.action, {
          method: "POST",
          body: paymentBody,
          credentials: "same-origin",
          cache: "no-store",
          headers: { Accept: "application/json" },
        });
        const payload = await readJsonResponse(response);
        paymentResponseKnown = Boolean(payload.invoice_url || typeof payload.error === "string");
        if (typeof payload.redirect === "string" && payload.redirect.startsWith("/")) {
          cartNeedsReload = true;
          window.location.assign(payload.redirect);
          return;
        }
        if (validCartState(payload.cart_state || payload.current)) applyCartState(payload.cart_state || payload.current);
        if (!response.ok || !payload.invoice_url) {
          throw new Error(typeof payload.error === "string" ? payload.error : "payment_unavailable");
        }
        trackCheckoutEvent("AddPaymentInfo", payload.add_payment_event_id, {
          value: Number(payload.value || root.dataset.analyticsValue || 0),
        });
        submitted = true;
        window.location.assign(payload.invoice_url);
      } catch (error) {
        const errorCode = error instanceof Error ? error.message : "";
        const uncertain = !paymentResponseKnown || ["provider_ambiguous", "in_progress", "operation_unknown"].includes(errorCode);
        submitted = uncertain;
        paymentCreating = uncertain;
        syncCartCapabilities();
        if (paymentButton && !uncertain) setActionLabel(paymentButton, defaultPaymentLabel);
        if (errorBox) {
          if (uncertain) showFormError(paymentErrorMessage("provider_ambiguous"));
          else if (errorCode === "delivery_policy_ack") showFormError(deliveryCopy?.ack, "delivery_policy_ack");
          else if (errorCode === "delivery_policy_changed") {
            const needsAck = cartState?.free_delivery?.ack_required === true;
            const changedCopy = { uk: "Умови доставки оновлено. Перевірте їх перед оплатою.", ru: "Условия доставки обновлены. Проверьте их перед оплатой.", en: "Delivery terms are updated. Check them before paying." };
            showFormError(needsAck ? deliveryCopy?.ack : changedCopy[locale] || changedCopy.en, needsAck ? "delivery_policy_ack" : "");
          }
          else showFormError(paymentErrorMessage(errorCode), paymentErrorField(errorCode));
        }
      }
    });
  }

  const reissueForm = document.querySelector("[data-generation-reissue-form]");
  let reissueSubmitted = false;
  reissueForm?.addEventListener("submit", (event) => {
    if (reissueSubmitted || cartSaving || pendingCartOperation || checkoutExpired) {
      event.preventDefault();
      return;
    }
    reissueSubmitted = true;
    paymentCreating = true;
    syncCartCapabilities();
    const button = reissueForm.querySelector("[data-generation-reissue-submit]");
    if (button) {
      button.disabled = true;
      button.textContent = button.dataset.loadingLabel || button.textContent;
    }
  });

  const exitDialog = document.querySelector("[data-exit-dialog]");
  let exitTrigger = null;
  document.querySelectorAll("[data-checkout-exit]").forEach((trigger) => {
    trigger.addEventListener("click", (event) => {
      if (!exitDialog) return;
      event.preventDefault();
      exitTrigger = trigger;
      showCheckoutDialog(exitDialog, trigger);
      exitDialog.querySelector("[data-checkout-exit-cancel]")?.focus();
    });
  });
  exitDialog?.querySelector("[data-checkout-exit-confirm]")?.addEventListener("click", () => {
    const target = exitTrigger?.getAttribute("href") || "/";
    exitDialog.close?.();
    window.location.assign(target);
  });
  exitDialog?.addEventListener("close", () => {
    exitTrigger?.focus({ preventScroll: true });
    exitTrigger = null;
    dialogTrigger = null;
  });
  exitDialog?.addEventListener("click", (event) => {
    if (event.target === exitDialog) exitDialog.close();
  });

  const initializeNovaPoshta = () => {
    window.TwoCommsNovaPoshta?.initScope?.(root);
  };
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", initializeNovaPoshta, { once: true });
  } else {
    initializeNovaPoshta();
  }

  let revisionCheckRunning = false;
  document.addEventListener("visibilitychange", async () => {
    if (document.visibilityState !== "visible" || revisionCheckRunning) return;
    revisionCheckRunning = true;
    try {
      const response = await fetch(root.dataset.proposalUrl || window.location.href, {
        credentials: "same-origin",
        cache: "no-store",
        headers: { Accept: "text/html" },
      });
      if (!response.ok) return;
      const html = await response.text();
      const page = new DOMParser().parseFromString(html, "text/html").documentElement;
      const current = document.documentElement.dataset.proposalRevision;
      const next = page.dataset.proposalRevision;
      const currentState = document.documentElement.dataset.checkoutState;
      const nextState = page.dataset.checkoutState;
      if ((next && next !== current) || (nextState && nextState !== currentState)) {
        window.location.reload();
      }
    } catch (_error) {
      // The next visibility change safely retries without exposing proposal data.
    } finally {
      revisionCheckRunning = false;
    }
  });
})();
