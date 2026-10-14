const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const assert = require('node:assert/strict');

const root = path.resolve(__dirname, '..');
const template = fs.readFileSync(path.join(root, 'twocomms/twocomms_django_theme/templates/pages/ig_checkout.html'), 'utf8');
const cssPath = path.join(root, 'twocomms/twocomms_django_theme/static/css/instagram-checkout.css');
const jsPath = path.join(root, 'twocomms/twocomms_django_theme/static/js/instagram-checkout.js');
const viewPath = path.join(root, 'twocomms/storefront/views/ig_checkout.py');
const selectorPath = path.join(root, 'twocomms/twocomms_django_theme/static/js/modules/nova-poshta-selector.js');
const bridgePath = path.join(root, 'twocomms/twocomms_django_theme/static/js/modules/nova-poshta-form-bridge.js');
const botPath = path.join(root, 'twocomms/management/services/instagram_bot.py');
const brandKnowledgePath = path.join(root, 'twocomms/management/bot_knowledge/brand.md');

test('checkout template uses isolated assets and semantic delivery controls', () => {
  assert.match(template, /instagram-checkout\.css/);
  assert.match(template, /instagram-checkout\.js/);
  assert.match(template, /data-np-form/);
  assert.match(template, /data-np-form="instagram"/);
  assert.match(template, /data-payment-submit/);
  assert.match(template, /data-payment-amount/);
  assert.match(template, /data-payment-trust/);
  assert.match(
    template,
    /\{% if payment_url %\}[\s\S]*?data-payment-rail[\s\S]*?data-payment-continue[\s\S]*?data-payment-trust[\s\S]*?\{% endif %\}/,
  );
  assert.match(
    template,
    /data-checkout-state-banner[\s\S]*?\{% if payment_url %\}[\s\S]*?data-countdown[\s\S]*?\{% endif %\}/,
  );
  assert.match(template, /data-countdown-ring/);
  assert.match(template, /data-countdown[^>]*role="timer"[^>]*aria-live="off"/);
  assert.match(template, /data-expiry-status[^>]*role="status"[^>]*aria-live="polite"/);
  assert.match(template, /data-direct-help/);
  assert.match(template, /data-checkout-exit/);
  assert.match(template, /class="ig-language-switcher"/);
  assert.match(template, /language_options/);
  assert.match(template, /language\.url/);
  assert.match(template, /img\/lang\/ptn\.png/);
  assert.match(template, /data-exit-dialog/);
  assert.match(template, /data-header-share/);
  assert.match(template, /data-price-details/);
  assert.match(template, /data-price-dialog/);
  assert.match(template, /data-share-card/);
  assert.match(template, /data-expired-title/);
  assert.match(template, /data-expired-body/);
  assert.match(template, /data-share-dialog/);
  assert.match(template, /class="ig-section-head"/);
  assert.match(template, /data-product-fact/);
  assert.match(template, /data-field-icon/);
  assert.match(template, /data-field-complete/);
  assert.match(template, /data-delivery-time/);
  assert.match(template, /id="ig-city"[^>]*role="combobox"[^>]*aria-haspopup="listbox"/);
  assert.match(template, /id="ig-warehouse"[^>]*role="combobox"[^>]*aria-haspopup="listbox"/);
  assert.match(template, /data-np-kind-toggle role="group"/);
  assert.match(template, /data-kind="all" aria-pressed="true"/);
  assert.match(template, /name="email"[^>]*aria-describedby/);
  assert.match(template, /name="email"[^>]*aria-required="false"/);
  assert.match(template, /name="email"[^>]*data-optional-field/);
  assert.doesNotMatch(template, /name="email"[^>]*\srequired(?:\s|=|>)/);
  assert.match(template, /\{% if customer_name %\}<h1[^>]*>\{\{ copy\.greeting \}\}, <strong>\{\{ customer_name \}\}<\/strong>/);
  assert.match(template, /data-cart-unit-price[\s\S]*item\.unit_price/);
  assert.match(template, /aria-label="\{\{ copy\.analytics_consent_label \}\}"/);
  assert.doesNotMatch(template, /<style[\s>]/i);
  assert.doesNotMatch(template, /<script>(?!\s*\{\{)/i);
});

test('existing invoice continuation is disabled when proposal time expires', () => {
  const source = fs.readFileSync(jsPath, 'utf8');

  assert.match(source, /const paymentContinue = document\.querySelector\("\[data-payment-continue\]"\)/);
  assert.match(source, /paymentContinue\.removeAttribute\("href"\)/);
  assert.match(source, /paymentContinue\.setAttribute\("aria-disabled", "true"\)/);
  assert.match(source, /const checkoutStateBanner = document\.querySelector\("\[data-checkout-state-banner\]"\)/);
  assert.match(source, /checkoutStateBanner\.classList\.add\("ig-state--expired"\)/);
});

test('checkout CSS restores the approved C3 Brand Night surfaces and restrained motion', () => {
  assert.equal(fs.existsSync(cssPath), true);
  const css = fs.readFileSync(cssPath, 'utf8');
  const panelRule = [...css.matchAll(/\.ig-checkout-panel\s*\{([\s\S]*?)\}/g)]
    .find((match) => /animation:/.test(match[1]));
  const panelFadeKeyframes = css.match(/@keyframes\s+ig-panel-fade\s*\{([\s\S]*?)\n\}/);
  assert.ok(panelRule, 'checkout panel rule must exist');
  assert.ok(panelFadeKeyframes, 'checkout panel fade keyframes must exist');
  assert.match(css, /aspect-ratio:\s*4\s*\/\s*5/);
  assert.match(css, /env\(safe-area-inset-bottom\)/);
  assert.match(css, /prefers-reduced-motion:\s*reduce/);
  assert.match(css, /min-height:\s*48px/);
  assert.match(css, /\.ig-kind button\s*\{[^}]*min-width:\s*44px/s);
  assert.match(css, /\.ig-language-switcher__link\s*\{[^}]*min-height:\s*44px/s);
  assert.match(css, /scroll-padding-bottom/);
  assert.match(panelRule[1], /animation:\s*ig-panel-fade/);
  assert.doesNotMatch(panelRule[1], /ig-settle/);
  assert.doesNotMatch(
    panelFadeKeyframes[1],
    /transform\s*:/,
    'a transformed payment-rail ancestor breaks viewport-fixed positioning',
  );
  assert.match(css, /color-scheme:\s*dark/);
  assert.match(css, /--ig-bg:\s*#080b10/);
  assert.match(css, /--ig-panel:\s*#111721/);
  assert.match(css, /--ig-field:\s*#0d131c/);
  assert.match(css, /--ig-gold:\s*#f3a43d/);
  assert.match(css, /radial-gradient\(/);
  assert.match(css, /linear-gradient\(/);
  assert.match(css, /\.ig-checkout-panel\s*\{[^}]*background:[^}]*linear-gradient/s);
  assert.match(css, /\.ig-payment-rail\s*\{[^}]*position:\s*fixed/s);
  assert.match(css, /\.ig-share-card button\s*\{[^}]*min-height:\s*44px/s);
  assert.match(css, /\.ig-direct-help a\s*\{[^}]*min-height:\s*44px/s);
  assert.match(css, /\.ig-footer a\s*\{[^}]*min-width:\s*44px[^}]*min-height:\s*44px/s);
  assert.match(css, /\.is-field-focused\s+\.ig-payment-rail\s+\.ig-payment-rail__trust\s*\{[^}]*display:\s*none/s);
  assert.match(css, /--ig-action:\s*#ff6b2b/);
  assert.match(css, /--ig-accent-text:\s*#ffd09a/);
  assert.match(css, /\.ig-brand__accent\s*\{[^}]*color:\s*var\(--ig-accent-text\)/s);
  assert.match(css, /@media \(max-width:\s*350px\)[\s\S]*?\.ig-language-switcher__link > span:last-child\s*\{[^}]*display:\s*none/s);
  assert.match(css, /@media \(max-width:\s*350px\)[\s\S]*?\.ig-language-switcher__link\s*\{[^}]*min-width:\s*44px[^}]*min-height:\s*44px/s);
  assert.match(css, /@media \(max-width:\s*350px\)[\s\S]*?\.ig-icon-button\s*\{[^}]*width:\s*44px[^}]*height:\s*44px/s);
  assert.doesNotMatch(css, /@media \(max-width:\s*350px\)[\s\S]*?\.ig-language-switcher__link img[^}]*display:\s*none/s);
  assert.match(css, /\.ig-field input::placeholder\s*\{[^}]*color:\s*#706b65/s);
  assert.match(css, /\.ig-consent\s*\{[^}]*position:\s*static/s);
  assert.match(css, /\.has-cart-controls \.ig-payment-rail\s*\{[^}]*position:\s*static/s);
  assert.doesNotMatch(css, /max-height:\s*650px/);
  assert.doesNotMatch(css, /\.is-field-focused\s+\.ig-payment-rail\s*\{[^}]*(display:\s*none|visibility:\s*hidden|opacity:\s*0|transform:)/s);
  assert.doesNotMatch(css, /animation[^;]*infinite/i);
});

test('checkout JS copies fresh share URL and refreshes stale revisions', () => {
  assert.equal(fs.existsSync(jsPath), true);
  const source = fs.readFileSync(jsPath, 'utf8');
  assert.match(source, /data-share-url/);
  assert.match(source, /navigator\.clipboard/);
  assert.match(source, /visibilitychange/);
  assert.match(source, /TwoCommsNovaPoshta/);
  assert.match(source, /stopImmediatePropagation\(\)/);
  assert.match(source, /image\.complete && image\.naturalWidth === 0/);
  assert.match(source, /const readJsonResponse = async \(response\)/);
  assert.match(source, /paymentFallbackErrors/);
  assert.match(source, /const paymentErrorField = \(code\) =>[\s\S]*promo_invalid:\s*'promo_code'/);
  assert.match(source, /showFormError\(paymentErrorMessage\(errorCode\), paymentErrorField\(errorCode\)\)/);
  assert.match(source, /focusFirstInvalid/);
  assert.match(source, /form\.checkValidity\(\)/);
  assert.doesNotMatch(source, /form\.reportValidity\(\)/);
  assert.match(source, /paymentErrorMessage\(invalidName\)/);
  assert.match(source, /scrollMarginBottom/);
  assert.match(source, /countdown-ring/);
  assert.match(source, /expireCheckout/);
  assert.match(source, /showModal|showModal\(\)/);
  assert.match(source, /event\.target === exitDialog[\s\S]*exitDialog\.close\(\)/);
  assert.match(source, /data-price-dialog/);
  assert.match(source, /data-share-dialog/);
  assert.match(source, /showCheckoutDialog/);
  assert.match(source, /navigator\.share/);
  assert.match(source, /classList\.toggle\(\s*"is-field-focused"/);
  assert.match(source, /error\?\.name === "AbortError"/);
  assert.match(source, /const setFieldError = \(field, message\) =>/);
  assert.match(source, /deliverySelectionsAreSigned/);
  assert.match(source, /data-expiry-status/);
  assert.match(source, /priceDetails\.disabled\s*=\s*true/);
  assert.match(source, /priceDetails\.removeAttribute\(["']aria-haspopup["']\)/);
  assert.match(source, /serverErrorField/);
  assert.match(source, /serverErrorPending && serverErrorField === field/);
  assert.doesNotMatch(source, /errorBox\.textContent = error\.message/);
  assert.doesNotMatch(source, /offer\/a\/\$\{/);
  assert.doesNotMatch(source, /console\.(log|debug|info)/);
});

test('checkout expiry closes transient dialogs and revokes every share action', () => {
  const source = fs.readFileSync(jsPath, 'utf8');
  const expiryBlock = source.match(/const expireCheckout = \(\) => \{([\s\S]*?)\n  \};/);

  assert.ok(expiryBlock, 'expireCheckout must remain an explicit state transition');
  assert.match(expiryBlock[1], /closeCheckoutDialog\(priceDialog\)/);
  assert.match(expiryBlock[1], /closeCheckoutDialog\(shareDialog\)/);
  assert.match(expiryBlock[1], /querySelectorAll\("\[data-share-open\]"\)/);
  assert.match(expiryBlock[1], /querySelectorAll\("\[data-share-url\]"\)/);
  assert.match(expiryBlock[1], /removeAttribute\("data-share-url"\)/);
  assert.match(expiryBlock[1], /data-state-icon[\s\S]*setAttribute\("href", "#ig-icon-alert"\)/);
  assert.match(expiryBlock[1], /directHelp[\s\S]*classList\.add\("is-priority"\)/);
  assert.doesNotMatch(expiryBlock[1], /directHelp[\s\S]*\.focus\(/);
  assert.match(source, /if \(checkoutExpired\) return;[\s\S]*showCheckoutDialog\(shareDialog, trigger\)/);
  assert.match(source, /if \(checkoutExpired \|\| !button\.dataset\.shareUrl\) return/);
  assert.match(source, /button\.disabled = checkoutExpired/);
});

test('server field errors survive untrusted Nova Poshta restoration events', () => {
  const source = fs.readFileSync(jsPath, 'utf8');

  assert.match(source, /let serverErrorPending = Boolean\(/);
  assert.match(source, /serverErrorPending && serverErrorField === field/);
  assert.match(source, /if \(!event\?\.isTrusted\)/);
  assert.match(source, /queueMicrotask\(syncServerError\)/);
  assert.match(source, /serverErrorPending = false/);
  assert.match(source, /requiredField\.getAttribute\("aria-invalid"\) !== "true"/);
});

test('terminal states reserve the success check for ready and paid outcomes', () => {
  const css = fs.readFileSync(cssPath, 'utf8');

  assert.match(template, /data-state-icon="success"[\s\S]*href="#ig-icon-check-badge"/);
  assert.match(template, /data-state-icon="progress"[\s\S]*href="#ig-icon-timer"/);
  assert.match(template, /data-state-icon="attention"[\s\S]*href="#ig-icon-alert"/);
  assert.match(css, /\.ig-state--superseded[\s\S]*border-color:/);
  assert.match(
    css,
    /\.ig-state--superseded \.ig-state__icon[\s\S]*color:\s*var\(--ig-gold\)/,
  );
});

test('checkout copy keeps optional email and consent labels localized', () => {
  const source = fs.readFileSync(viewPath, 'utf8');
  assert.match(source, /"uk": \{[\s\S]*?"optional": "Необов'язково"/);
  assert.match(source, /"ru": \{[\s\S]*?"optional": "Необязательно"/);
  assert.match(source, /"en": \{[\s\S]*?"optional": "Not required"/);
  assert.match(source, /"uk": \{[\s\S]*?"email_hint": "Email не є обов'язковим\./);
  assert.match(source, /"ru": \{[\s\S]*?"email_hint": "Email не обязателен\./);
  assert.match(source, /"en": \{[\s\S]*?"email_hint": "Email is optional\./);
  assert.match(source, /"uk": \{[\s\S]*?"analytics_consent_label": "Налаштування аналітики"/);
  assert.match(source, /"ru": \{[\s\S]*?"analytics_consent_label": "Настройки аналитики"/);
  assert.match(source, /"en": \{[\s\S]*?"analytics_consent_label": "Analytics preferences"/);
});

test('assisted-checkout bot and knowledge copy keep receipt email optional', () => {
  const bot = fs.readFileSync(botPath, 'utf8');
  const brandKnowledge = fs.readFileSync(brandKnowledgePath, 'utf8');

  assert.match(bot, /email для чека за бажанням/);
  assert.match(bot, /email для чека по желанию/);
  assert.match(bot, /an optional receipt email/);
  assert.doesNotMatch(bot, /вводить Нову Пошту та email для чека/);
  assert.match(bot, /Не збирай email, ПІБ, телефон, місто/);
  assert.match(brandKnowledge, /email для чека за бажанням/);
  assert.doesNotMatch(brandKnowledge, /вказати доставку та email для чека, а потім/);
});

test('Nova Poshta settlement metadata follows the checkout locale', () => {
  const selector = fs.readFileSync(selectorPath, 'utf8');
  assert.match(selector, /scope\.matches\?\.\('\[data-np-form\]'\)[\s\S]*forms\.push\(scope\)/);
  assert.match(selector, /function localizeSettlementType\(value, locale\)/);
  assert.match(selector, /localizeSettlementType\(current\.settlement_type, this\.locale\)/);
  assert.match(selector, /input\.removeAttribute\('aria-activedescendant'\)/);
  assert.match(selector, /item\.setAttribute\('aria-pressed', isActive \? 'true' : 'false'\)/);
  assert.match(selector, /params\.set\('locale', this\.locale\)/);
  assert.doesNotMatch(selector, /payload\.error \|\| this\.copy\.searchDisabled/);
  assert.match(selector, /ru:\s*\{[\s\S]*?city:\s*'Город'/);
  assert.match(selector, /en:\s*\{[\s\S]*?city:\s*'City'/);
  assert.match(selector, /button\.tabIndex\s*=\s*-1/);
  assert.match(selector, /closest\?\.\('\.ig-field'/);
  assert.match(
    selector,
    /clearWarehouseSelection\(options = \{\}\)[\s\S]*options\.preserveInput[\s\S]*dispatchEvent\(new Event\('change', \{ bubbles: true \}\)\)/,
  );
});

test('Nova Poshta validation keeps the field-specific error and one described node', () => {
  const source = fs.readFileSync(jsPath, 'utf8');
  const bridge = fs.readFileSync(bridgePath, 'utf8');

  assert.match(
    bridge,
    /function firstInvalidNovaPoshtaField\(form\)[\s\S]*aria-invalid=['"]true['"][\s\S]*np_office/,
  );
  assert.match(bridge, /field: firstInvalidNovaPoshtaField\(form\)/);
  assert.doesNotMatch(source, /result\.field === ['"]delivery['"] \? ['"]city['"]/);
  assert.match(source, /querySelector\(['"]\[data-field-error\], \.cart-form-error['"]\)/);
});

test('checkout trust copy describes the Monobank handoff without a fake badge', () => {
  const source = fs.readFileSync(viewPath, 'utf8');
  assert.match(source, /"uk": \{[\s\S]*?"secure_payment": "Дані картки вводяться на захищеній сторінці Monobank"/);
  assert.match(source, /"ru": \{[\s\S]*?"secure_payment": "Данные карты вводятся на защищенной странице Monobank"/);
  assert.match(source, /"en": \{[\s\S]*?"secure_payment": "Card details are entered on Monobank's secure page"/);
});

test('delivery benefit precedes totals and keeps consent bound to the native form', () => {
  const css = fs.readFileSync(cssPath, 'utf8');
  assert.ok(template.indexOf('data-cart-feedback') < template.indexOf('data-delivery-benefit'));
  assert.ok(template.indexOf('data-delivery-benefit') < template.indexOf('data-cart-totals'));
  assert.match(template, /role="progressbar"[^>]*aria-valuemin="0"[^>]*aria-valuemax="100"/);
  assert.match(template, /data-delivery-benefit-update/);
  assert.match(template, /name="delivery_policy_ack"[^>]*form="[^"]*ig-checkout-delivery-form/);
  assert.match(template, /name="delivery_policy_digest"[^>]*form="[^"]*ig-checkout-delivery-form/);
  assert.match(template, /data-delivery-benefit-meter\{% if not cart_state\.free_delivery or not cart_state\.can_edit %\} hidden/);
  assert.match(template, /cart_state\.free_delivery and not cart_state\.can_edit[\s\S]*Доставку оплачуєте окремо Новій пошті/);
  assert.match(css, /\.ig-delivery-benefit[\s\S]*min-width:\s*0/);
  assert.match(css, /@media \(prefers-reduced-motion:\s*reduce\)[\s\S]*\.ig-delivery-benefit__track span\s*\{\s*transition:\s*none/);
});

test('editable basket copy explains native edits while read-only offers retain their original terms', () => {
  assert.match(template, /\{% if cart_state\.can_edit %\}[\s\S]*Ваш кошик можна змінити[\s\S]*\{% else %\}\{\{ copy\.fixed_price_title \}\}/);
  assert.match(template, /Вашу корзину можно изменить/);
  assert.match(template, /You can update your basket/);
  assert.match(template, /data-direct-help[\s\S]*Кількість змінюйте кнопками \+\/−[\s\S]*\{% else %\}\{\{ copy\.direct_help \}\}/);
  assert.match(template, /Other changes — in Direct/);
  assert.match(template, /id="ig-price-title"[\s\S]*Оновлюйте кошик тут[\s\S]*\{% else %\}\{\{ copy\.price_dialog_title \}\}/);
});

test('paid summary separates goods value from the factual amount received on this invoice', () => {
  assert.match(template, /ig-totals__final[\s\S]*\{% if checkout_state == 'paid' %\}[\s\S]*Вартість замовлення[\s\S]*\{% else %\}\{\{ copy\.total \}\}/);
  assert.match(template, /Стоимость заказа/);
  assert.match(template, /Order total/);
  assert.match(template, /\{% if checkout_state == 'paid' and paid_via_invoice %\}[\s\S]*data-paid-via-invoice[\s\S]*Сплачено за цим рахунком[\s\S]*\{\{ paid_via_invoice \}\}/);
  assert.match(template, /Оплачено по этому счёту/);
  assert.match(template, /Paid on this invoice/);
  assert.match(template, /data-cart-charge-row\{% if[^%]*checkout_state == 'paid' %\} hidden/);
});
