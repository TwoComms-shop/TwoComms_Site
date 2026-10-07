(function (global) {
  const mounted = new WeakMap();

  function mount(root) {
    if (!root) return null;
    if (mounted.has(root)) return mounted.get(root);
    const scene = root.querySelector('[data-gift-package-scene]');
    const unwrap = root.querySelector('[data-gift-unwrap]');
    const seal = root.querySelector('[data-gift-seal-control]');
    const card = root.querySelector('[data-gift-card-preview]');
    const flip = root.querySelector('[data-gift-card-flip]');
    const cardTap = root.querySelector('[data-gift-card-tap]');
    let lastBox = false, lastCertificate = false, lastMessageMode = 'blank';
    const reduced = () => global.matchMedia?.('(prefers-reduced-motion: reduce)')?.matches;
    if (global.IntersectionObserver) {
      const observer = new global.IntersectionObserver((entries) => entries.forEach(({ target, isIntersecting }) => target.classList.toggle('is-hint-visible', isIntersecting)), { threshold: .35 });
      if (scene) observer.observe(scene);
      if (card) observer.observe(card);
    } else {
      scene?.classList.add('is-hint-visible'); card?.classList.add('is-hint-visible');
    }

    // Move only after an explicit reveal, never in response to typing. Keep the
    // option header and first visual together when the screen is tall enough.
    function reveal(target, alignStart = false) {
      if (!target || target.hidden) return;
      const viewport = root.querySelector('[data-step-viewport]');
      if (!viewport?.contains(target) || !root.classList.contains('is-studio-active')) return;
      const bounds = viewport.getBoundingClientRect(), rect = target.getBoundingClientRect();
      const height = bounds.height - 20;
      const delta = alignStart || rect.height > height || rect.top < bounds.top + 10
        ? rect.top - bounds.top - 10 : rect.bottom > bounds.bottom - 10 ? rect.bottom - bounds.bottom + 10 : 0;
      if (delta) viewport.scrollTo({ top: viewport.scrollTop + delta, behavior: reduced() ? 'auto' : 'smooth' });
    }

    function setUnwrapped(open) {
      scene?.classList.toggle('is-unwrapped', !!open);
      unwrap?.setAttribute('aria-pressed', String(!!open));
      seal?.setAttribute('aria-pressed', String(!!open));
      seal?.setAttribute('aria-label', open ? 'Загорнути zip-пакет у папір знову' : 'Розгорнути папір і побачити zip-пакет');
      const label = root.querySelector('[data-gift-unwrap-label]');
      if (label) label.textContent = open ? 'Загорнути знову' : 'Зазирнути під папір';
      const caption = root.querySelector('[data-gift-package-caption]');
      if (caption) caption.textContent = open
        ? 'Під папером — прозорий zip-пакет із застібкою та одягом. Картка, якщо оберете, лежатиме зверху.'
        : 'Папір з наклейкою обгортає zip-пакет з одягом. Картка лежатиме зверху, якщо її оберете.';
    }

    function showBack(back) {
      card?.classList.toggle('is-back', !!back);
      flip?.setAttribute('aria-pressed', String(!!back));
      cardTap?.setAttribute('aria-label', back ? 'Переглянути лицьовий бік картки' : 'Переглянути зворот картки');
      const label = root.querySelector('[data-gift-card-flip-label]');
      if (label) label.textContent = back ? 'Переглянути лицьовий бік' : 'Переглянути зворот';
      for (const [selector, hidden] of [['[data-gift-card-front]', !!back], ['[data-gift-card-back]', !back]]) {
        const face = root.querySelector(selector);
        face?.setAttribute('aria-hidden', String(hidden));
        if (face) face.inert = hidden;
      }
    }

    // These controls only inspect packaging. They never select an option, add
    // a charge, submit an order or delay navigation. CSS owns cancellable motion.
    const inspectPackage = () => setUnwrapped(!scene?.classList.contains('is-unwrapped'));
    const inspectCard = () => showBack(!card?.classList.contains('is-back'));
    unwrap?.addEventListener('click', inspectPackage);
    seal?.addEventListener('click', inspectPackage);
    flip?.addEventListener('click', inspectCard);
    cardTap?.addEventListener('click', inspectCard);
    card?.addEventListener('click', (event) => {
      if (!event.target.closest('button, [data-gift-card-preview-text]')) inspectCard();
    });
    root.querySelector('[data-gift-show-box]')?.addEventListener('click', () => {
      const button = root.querySelector('[data-gift-toggle]');
      reveal(button, true);
      button?.focus({ preventScroll: true });
    });
    root.querySelectorAll('.cp-gift-option-toggle').forEach((button) => button.addEventListener('click', () => {
      global.requestAnimationFrame?.(() => {
        if (button.getAttribute('aria-pressed') !== 'true') { reveal(button); return; }
        const panel = root.querySelector(`#${button.getAttribute('aria-controls')}`);
        // Scroll to the start of the visual, leaving the option header nearby.
        const target = panel?.querySelector('[data-gift-group-box-note]:not([hidden]), [data-gift-preview], [data-gift-exterior-preview], [data-gift-card-preview], .cp-gift-delivery-methods') || panel;
        reveal(target, true);
      });
    }));
    root.querySelectorAll('[data-gift-card-mode], [data-gift-content]').forEach((button) => button.addEventListener('click', () => {
      global.requestAnimationFrame?.(() => {
        const target = button.dataset.giftCardMode === 'write' ? root.querySelector('[data-gift-card-message-field]')
          : button.dataset.giftContent ? root.querySelector(button.dataset.giftContent === 'image' ? '[data-gift-image-field]' : '[data-gift-message-field]') : card;
        reveal(target);
      });
    }));
    function sync({ boxEnabled, certificateEnabled, wrappingEnabled, messageMode }) {
      if (!boxEnabled || !lastBox) setUnwrapped(false);
      if (!certificateEnabled) showBack(false);
      else if (!lastCertificate || messageMode !== lastMessageMode) showBack(messageMode === 'write' || lastCertificate);
      lastBox = !!boxEnabled; lastCertificate = !!certificateEnabled; lastMessageMode = messageMode;
    }
    const api = { setUnwrapped, showBack, sync, reset: () => { setUnwrapped(false); showBack(false); } };
    api.reset();
    mounted.set(root, api);
    return api;
  }

  global.CustomPrintGiftPreview = { mount };
})(globalThis);
