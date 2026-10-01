/* Preview browsing is independent of the saved configuration and studio steps. */
(() => {
  const hero = document.querySelector('[data-mobile-hero]');
  if (!hero) return;
  const buttons = [...hero.querySelectorAll('[data-hero-garment]')];
  const pictures = [...hero.querySelectorAll('[data-hero-picture]')];
  const status = hero.querySelector('[data-hero-status]');
  let request = 0;

  buttons.forEach((button) => {
    button.addEventListener('click', async () => {
      const version = ++request;
      const picture = pictures.find((item) => item.dataset.heroPicture === button.dataset.heroGarment);
      if (!picture) return;
      const source = picture.querySelector('source');
      const img = picture.querySelector('img');
      if (source.dataset.srcset) source.srcset = source.dataset.srcset;
      buttons.forEach((item) => item.removeAttribute('aria-busy'));
      button.setAttribute('aria-busy', 'true');
      try {
        await img.decode();
        if (version !== request) return;
        pictures.forEach((item) => {
          item.classList.toggle('is-active', item === picture);
          item.setAttribute('aria-hidden', String(item !== picture));
        });
        buttons.forEach((item) => item.setAttribute('aria-pressed', String(item === button)));
        status.textContent = img.alt;
      } catch {
        if (version === request) status.textContent = status.dataset.errorMessage;
      } finally {
        if (version === request) button.removeAttribute('aria-busy');
      }
    });
  });
})();
