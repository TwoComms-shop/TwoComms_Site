import { MobileOptimizer } from './optimizers.js';
import { prefersReducedMotion } from './shared.js';

function preloadImage(src) {
  return new Promise((resolve, reject) => {
    const img = new Image();
    img.onload = () => resolve(img);
    img.onerror = reject;
    img.src = src;
  });
}

function getColorImageUrl(colorDot) {
  const imageUrl = colorDot.getAttribute('data-image-url');
  if (imageUrl) {
    return imageUrl;
  }
  const title = colorDot.getAttribute('title');
  if (title) {
    return null;
  }
  return null;
}

function animateImageChange(img, newSrc) {
  return new Promise((resolve) => {
    img.classList.add('switching');
    preloadImage(newSrc).then(() => {
      const picture = img.closest('picture');
      if (picture) {
        const sources = picture.querySelectorAll('source');
        sources.forEach(source => {
          const srcset = source.getAttribute('srcset');
          if (srcset) {
            const baseUrl = newSrc.replace(/\/[^\/]+\.(jpg|jpeg|png)$/, '');
            const fileName = newSrc.match(/\/([^\/]+)\.(jpg|jpeg|png)$/);
            if (fileName) {
              const baseName = fileName[1];
              const type = source.getAttribute('type');
              let newSrcset;
              if (type === 'image/avif') {
                newSrcset = `${baseUrl}/optimized/${baseName}.avif`;
              } else if (type === 'image/webp') {
                newSrcset = `${baseUrl}/optimized/${baseName}.webp`;
              } else {
                newSrcset = newSrc;
              }
              source.setAttribute('srcset', newSrcset);
            }
          }
        });
        img.src = newSrc;
      } else {
        img.src = newSrc;
      }
      requestAnimationFrame(() => {
        img.classList.remove('switching');
        resolve();
      });
    }).catch(() => {
      img.classList.remove('switching');
      resolve();
    });
  });
}

function formatVariantPrice(raw) {
  const value = Number(raw);
  if (!Number.isFinite(value)) return '';
  return `${new Intl.NumberFormat('uk-UA', { maximumFractionDigits: 0 }).format(value)} грн`;
}

function updateCardMerchandising(productCard, colorDot) {
  const price = productCard.querySelector('[data-card-price]');
  const explain = productCard.querySelector('[data-price-explain]');
  const explainCopy = productCard.querySelector('[data-price-explain-copy]');
  const exactPrice = formatVariantPrice(colorDot.getAttribute('data-variant-price'));
  const isThermo = colorDot.getAttribute('data-is-thermo') === '1';
  const reason = (colorDot.getAttribute('data-price-reason') || '').trim();
  const variantUrl = colorDot.getAttribute('data-variant-url') || '';

  if (price && exactPrice) price.textContent = exactPrice;
  if (explain && explainCopy) {
    const copy = reason || (isThermo
      ? 'Термохромна тканина реагує на тепло, змінює відтінок і коштує дорожче за звичайну.'
      : 'Для цього кольору діє звичайна ціна без доплати за термохромну тканину.');
    explainCopy.textContent = copy;
    explain.classList.toggle('is-hidden', !reason && !isThermo);
    explain.classList.remove('is-open');
    const trigger = explain.querySelector('[data-price-explain-trigger]');
    if (trigger) trigger.setAttribute('aria-expanded', 'false');
  }
  if (variantUrl) {
    productCard.querySelectorAll('[data-product-card-link]').forEach((link) => {
      link.setAttribute('href', variantUrl);
    });
    productCard.setAttribute('data-product-url', variantUrl);
  }
}

function handleColorDotClick(e) {
  const colorDot = e.target.closest ? e.target.closest('.color-dot') : null;
  if (!colorDot) {
    return;
  }
  e.stopPropagation();
  const productCardWrap = colorDot.closest('.product-card-wrap');
  const productCard = productCardWrap ? productCardWrap.querySelector('.card.product') : null;
  if (!productCard) {
    return;
  }
  const allDots = productCardWrap.querySelectorAll('.color-dot');
  allDots.forEach(dot => {
    dot.classList.remove('active');
    dot.classList.add('switching');
  });
  requestAnimationFrame(() => {
    colorDot.classList.remove('switching');
    colorDot.classList.add('active');
  });
  updateCardMerchandising(productCard, colorDot);

  const mainImage =
    productCard.querySelector('.home-product-media picture img') ||
    productCard.querySelector('.home-product-media .product-main-image') ||
    productCard.querySelector('.product-main-image') ||
    productCard.querySelector('.ratio picture img') ||
    productCard.querySelector('.ratio .product-main-image') ||
    productCard.querySelector('.ratio img');
  if (!mainImage) {
    return;
  }
  const newImageUrl = getColorImageUrl(colorDot, productCard);
  if (!newImageUrl) {
    return;
  }
  if (mainImage.src !== newImageUrl) {
    animateImageChange(mainImage, newImageUrl);
  }
}

function handlePriceExplainClick(event) {
  const trigger = event.target.closest && event.target.closest('[data-price-explain-trigger]');
  if (!trigger) return;
  event.preventDefault();
  event.stopPropagation();
  const explain = trigger.closest('[data-price-explain]');
  if (!explain) return;
  const next = !explain.classList.contains('is-open');
  document.querySelectorAll('[data-price-explain].is-open').forEach((item) => {
    item.classList.remove('is-open');
    const button = item.querySelector('[data-price-explain-trigger]');
    if (button) button.setAttribute('aria-expanded', 'false');
  });
  explain.classList.toggle('is-open', next);
  trigger.setAttribute('aria-expanded', next ? 'true' : 'false');
}

function closePriceExplainers(event) {
  if (event.target.closest && event.target.closest('[data-price-explain]')) return;
  document.querySelectorAll('[data-price-explain].is-open').forEach((item) => {
    item.classList.remove('is-open');
    const trigger = item.querySelector('[data-price-explain-trigger]');
    if (trigger) trigger.setAttribute('aria-expanded', 'false');
  });
}

export function forceShowAllImages() {}

function revealColorDots() {
  const colorDots = document.querySelectorAll('.color-dot');
  colorDots.forEach((dot, index) => {
    requestAnimationFrame(() => {
      setTimeout(() => {
        dot.classList.add('visible');
      }, index * 60);
    });
  });
}

export function initProductMedia() {
  document.addEventListener('click', handleColorDotClick, { passive: false });
  document.addEventListener('click', handlePriceExplainClick, { passive: false });
  document.addEventListener('click', closePriceExplainers, { passive: true });

  const onReady = () => {
    MobileOptimizer.initMobileOptimizations();
    revealColorDots();
  };
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', onReady);
  } else {
    onReady();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', forceShowAllImages);
  } else {
    forceShowAllImages();
  }
  window.addEventListener('load', forceShowAllImages);
}
