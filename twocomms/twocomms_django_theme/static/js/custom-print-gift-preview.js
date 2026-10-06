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
    let lastBox = false, lastCertificate = false, lastMessageMode = 'blank';

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
    unwrap?.addEventListener('click', () => setUnwrapped(!scene?.classList.contains('is-unwrapped')));
    seal?.addEventListener('click', () => setUnwrapped(!scene?.classList.contains('is-unwrapped')));
    flip?.addEventListener('click', () => showBack(!card?.classList.contains('is-back')));
    function sync({ boxEnabled, certificateEnabled, messageMode }) {
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
