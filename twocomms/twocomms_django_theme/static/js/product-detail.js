(function () {
  'use strict';

  const prefersReducedMotion = window.matchMedia &&
    window.matchMedia('(prefers-reduced-motion: reduce)').matches;

  document.addEventListener('DOMContentLoaded', initProductDetail);

  function initProductDetail() {
    const root = document.querySelector('[data-pdp]');
    const container = document.getElementById('product-detail-container');
    if (!root || !container) return;

    const state = {
      root,
      container,
      variants: readJsonScript('variant-data', []),
      extraImages: readJsonScript('product-extra-images', []),
      offerIdMap: readOfferMap(container),
      mainImage: document.getElementById('mainProductImage'),
      thumbs: document.getElementById('productThumbnails'),
      video: readJsonScript('product-video', null),
      videoStage: document.getElementById('productVideoStage'),
      videoActive: false,
      viewContentTracked: false,
    };

    initGallery(state);
    initColorSelection(state);
    initSizeSelection(state);
    initFitSelection(root);
    initVariantPriceNote(root);
    applyCurrentVariantMerchandising(state);
    initTabs(root);
    initDescriptionCollapse(root);
    initShare(root, container);
    initZoom(state);
    initVideo(state);
    initStickyAdd(root);
    updateCurrentOfferId(state);
    trackViewContent(state);
    initRecentViewed(state);
  }

  function readJsonScript(id, fallback) {
    const node = document.getElementById(id);
    if (!node) return fallback;
    try {
      const parsed = JSON.parse(node.textContent || '');
      return parsed == null ? fallback : parsed;
    } catch (_) {
      return fallback;
    }
  }

  function readOfferMap(container) {
    const scriptMap = readJsonScript('offer-id-map', null);
    if (scriptMap && typeof scriptMap === 'object') return scriptMap;
    try {
      return JSON.parse(container.getAttribute('data-offer-id-map') || '{}') || {};
    } catch (_) {
      return {};
    }
  }

  function mainImagePayload(image) {
    if (!image) return null;
    const picture = image.closest ? image.closest('picture') : null;
    const avif = picture ? picture.querySelector('source[type="image/avif"]') : null;
    const webp = picture ? picture.querySelector('source[type="image/webp"]') : null;
    const src = image.getAttribute('src') || image.src || '';
    const current = image.currentSrc || src;

    return {
      url: current || src,
      src: current || src,
      original_url: image.getAttribute('data-initial-image') || image.getAttribute('data-original-src') || src || current,
      zoom_url: image.getAttribute('data-zoom') ||
        image.getAttribute('data-original-src') ||
        image.getAttribute('data-initial-image') ||
        src ||
        current,
      thumbnail_url: current || src,
      avif_srcset: avif ? (avif.getAttribute('srcset') || '') : '',
      webp_srcset: webp ? (webp.getAttribute('srcset') || '') : '',
      sizes: image.getAttribute('sizes') ||
        (avif && avif.getAttribute('sizes')) ||
        (webp && webp.getAttribute('sizes')) ||
        '',
      alt: image.getAttribute('alt') || '',
    };
  }

  function normalizeImage(image) {
    if (!image) return null;
    if (typeof image === 'string') {
      const url = image.trim();
      return url ? {
        url,
        src: url,
        originalUrl: url,
        zoomUrl: url,
        thumbnailUrl: url,
        avifSrcset: '',
        webpSrcset: '',
        sizes: '',
        alt: '',
      } : null;
    }

    const url = image.url || image.src || image.webp_url || image.avif_url || image.original_url || '';
    if (!url) return null;

    return {
      url,
      src: url,
      originalUrl: image.original_url || image.originalUrl || image.zoom_url || image.zoomUrl || url,
      zoomUrl: image.zoom_url || image.zoomUrl || image.original_url || image.originalUrl || url,
      thumbnailUrl: image.thumbnail_url || image.thumbnailUrl || url,
      avifSrcset: image.avif_srcset || image.avifSrcset || '',
      webpSrcset: image.webp_srcset || image.webpSrcset || '',
      sizes: image.sizes || '',
      alt: image.alt || image.alt_text || image.altText || '',
    };
  }

  function uniqueImages(images) {
    const seen = new Set();
    return (images || []).map(normalizeImage).filter((image) => {
      if (!image) return false;
      const key = image.originalUrl || image.url;
      if (!key || seen.has(key)) return false;
      seen.add(key);
      return true;
    });
  }

  function baseImages(state) {
    const initial = mainImagePayload(state.mainImage);
    return uniqueImages([initial].concat(state.extraImages || []));
  }

  function activeVariant(state) {
    const active = state.root.querySelector('#color-picker .color-swatch.active');
    if (!active) return null;
    const id = Number(active.getAttribute('data-variant') || 0);
    return (state.variants || []).find((variant) => Number(variant.id) === id) || null;
  }

  function imagesForCurrentSelection(state) {
    const variant = activeVariant(state);
    if (variant && Array.isArray(variant.images) && variant.images.length) {
      return uniqueImages(variant.images);
    }
    return baseImages(state);
  }

  function initGallery(state) {
    if (!state.mainImage || !state.thumbs) return;
    const images = imagesForCurrentSelection(state);
    renderThumbnails(state, images);
    initThumbnailNav(state);
    if (images[0]) {
      setMainImage(state, images[0], { immediate: true });
    }
  }

  function renderThumbnails(state, images) {
    if (!state.thumbs) return;
    state.thumbs.innerHTML = '';

    images.forEach((image, index) => {
      const button = document.createElement('button');
      button.type = 'button';
      button.className = `tc-thumbnail${index === 0 ? ' active' : ''}`;
      button.setAttribute('aria-label', `Фото товару ${index + 1}`);
      button.dataset.image = image.url;

      const img = document.createElement('img');
      img.src = image.thumbnailUrl || image.url;
      img.alt = image.alt || '';
      img.loading = index === 0 ? 'eager' : 'lazy';
      img.decoding = 'async';

      button.appendChild(img);
      button.addEventListener('click', () => {
        state.thumbs.querySelectorAll('.tc-thumbnail').forEach((item) => item.classList.remove('active'));
        button.classList.add('active');
        hideVideo(state);
        setMainImage(state, image);
      });
      state.thumbs.appendChild(button);
    });

    appendVideoThumbnail(state);
  }

  function appendVideoThumbnail(state) {
    if (!state.thumbs || !state.video || !state.video.embed_url) return;

    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'tc-thumbnail tc-thumbnail--video';
    button.setAttribute('aria-label', state.video.title || 'Відео товару');
    button.dataset.videoThumb = '1';

    if (state.video.thumbnail_url) {
      const img = document.createElement('img');
      img.src = state.video.thumbnail_url;
      img.alt = state.video.title || 'Відео товару';
      img.loading = 'lazy';
      img.decoding = 'async';
      img.addEventListener('error', () => {
        img.remove();
        button.classList.add('tc-thumbnail--video-noposter');
      });
      button.appendChild(img);
    } else {
      button.classList.add('tc-thumbnail--video-noposter');
    }

    const badge = document.createElement('span');
    badge.className = 'tc-thumbnail-video-badge';
    badge.setAttribute('aria-hidden', 'true');
    badge.innerHTML = '<svg width="16" height="16" viewBox="0 0 24 24"><path d="M8 5v14l11-7z" fill="currentColor"/></svg>';
    button.appendChild(badge);

    button.addEventListener('click', () => {
      state.thumbs.querySelectorAll('.tc-thumbnail').forEach((item) => item.classList.remove('active'));
      button.classList.add('active');
      showVideo(state);
    });

    state.thumbs.appendChild(button);
  }

  function initThumbnailNav(state) {
    const prev = state.root.querySelector('[data-thumb-prev]');
    const next = state.root.querySelector('[data-thumb-next]');
    if (!prev || !next || prev.dataset.bound === '1') return;
    prev.dataset.bound = '1';
    next.dataset.bound = '1';

    const scrollByThumb = (direction) => {
      if (!state.thumbs) return;
      const firstThumb = state.thumbs.querySelector('.tc-thumbnail');
      const amount = firstThumb ? firstThumb.getBoundingClientRect().width + 12 : 124;
      state.thumbs.scrollBy({ left: direction * amount, behavior: prefersReducedMotion ? 'auto' : 'smooth' });
    };

    prev.addEventListener('click', () => scrollByThumb(-1));
    next.addEventListener('click', () => scrollByThumb(1));
  }

  function setMainImage(state, image, options) {
    const data = normalizeImage(image);
    const url = data ? data.url : '';
    if (!state.mainImage || !url) return;
    const immediate = options && options.immediate;
    if ((state.mainImage.getAttribute('src') === url || state.mainImage.currentSrc === url) && !immediate) {
      state.mainImage.alt = data.alt || state.container.dataset.productTitle || '';
      state.mainImage.setAttribute('data-zoom', data.zoomUrl || url);
      syncResponsiveSources(state.mainImage, data);
      return;
    }

    const apply = () => {
      syncResponsiveSources(state.mainImage, data);
      state.mainImage.src = url;
      state.mainImage.alt = data.alt || state.container.dataset.productTitle || '';
      state.mainImage.setAttribute('data-zoom', data.zoomUrl || url);
      state.mainImage.classList.remove('is-switching');
    };

    if (immediate || prefersReducedMotion) {
      apply();
      return;
    }

    state.mainImage.classList.add('is-switching');
    preloadImage(url)
      .then(() => window.setTimeout(apply, 60))
      .catch(() => window.setTimeout(apply, 90));
  }

  function syncResponsiveSources(image, data) {
    const picture = image && image.closest ? image.closest('picture') : null;
    if (!picture) return;

    const updateSource = (type, srcset) => {
      let source = picture.querySelector(`source[type="${type}"]`);
      if (!srcset) {
        if (source) source.remove();
        return;
      }
      if (!source) {
        source = document.createElement('source');
        source.setAttribute('type', type);
        picture.insertBefore(source, image);
      }
      source.setAttribute('srcset', srcset);
      if (data.sizes) {
        source.setAttribute('sizes', data.sizes);
      } else {
        source.removeAttribute('sizes');
      }
    };

    updateSource('image/avif', data.avifSrcset);
    updateSource('image/webp', data.webpSrcset);

    image.removeAttribute('srcset');
    if (data.sizes) {
      image.setAttribute('sizes', data.sizes);
    } else {
      image.removeAttribute('sizes');
    }
  }

  function preloadImage(url) {
    return new Promise((resolve, reject) => {
      const img = new Image();
      img.onload = () => {
        if (img.decode) {
          img.decode().then(resolve).catch(resolve);
        } else {
          resolve();
        }
      };
      img.onerror = reject;
      img.src = url;
    });
  }

  function initColorSelection(state) {
    const swatches = Array.from(state.root.querySelectorAll('#color-picker .color-swatch'));
    if (!swatches.length) return;

    swatches.forEach((button) => {
      button.addEventListener('click', () => {
        swatches.forEach((item) => item.classList.remove('active'));
        button.classList.add('active');
        state.container.dataset.currentVariant = button.getAttribute('data-variant') || 'default';
        applyCurrentVariantMerchandising(state);
        const images = imagesForCurrentSelection(state);
        renderThumbnails(state, images);
        hideVideo(state);
        if (images[0]) setMainImage(state, images[0]);
        const offerId = updateCurrentOfferId(state);
        trackCustomizeProduct(state, button.getAttribute('data-variant'), offerId);
      });
    });
  }

  function formatVariantPrice(value) {
    const number = Number.parseFloat(String(value == null ? '' : value).replace(',', '.'));
    if (!Number.isFinite(number)) return '';
    return new Intl.NumberFormat('uk-UA', {
      minimumFractionDigits: Number.isInteger(number) ? 0 : 2,
      maximumFractionDigits: 2,
    }).format(number);
  }

  function currentVariantData(state) {
    const id = String(state.container.dataset.currentVariant || '');
    return state.variants.find((variant) => String(variant && variant.id) === id) || null;
  }

  function applyCurrentVariantMerchandising(state) {
    const baseVariant = currentVariantData(state);
    if (!baseVariant) return;

    // Selecting a colour may disable the previously active fit. Resolve the
    // fallback fit first, then use that exact colour × fit merchandising row
    // for title, price and SEO-adjacent buyer copy.
    applyVariantFitRules(state, baseVariant.fit_rules || {});
    const activeFit = String(state.container.dataset.currentFit || '').toLowerCase();
    const fitOverrides = baseVariant.merchandising_by_fit || {};
    const variant = Object.assign({}, baseVariant, fitOverrides[activeFit] || {});

    if (variant.seo_title) document.title = String(variant.seo_title);
    const metaDescription = document.querySelector('meta[name="description"]');
    if (metaDescription && variant.seo_description) {
      metaDescription.setAttribute('content', String(variant.seo_description));
    }
    const metaKeywords = document.querySelector('meta[name="keywords"]');
    if (metaKeywords && variant.seo_keywords) {
      metaKeywords.setAttribute('content', String(variant.seo_keywords));
    }

    const formatted = formatVariantPrice(variant.final_price);
    const title = state.root.querySelector('[data-pdp-product-title]');
    if (title && variant.display_name) title.textContent = variant.display_name;
    if (formatted) {
      state.root.querySelectorAll('[data-pdp-current-price], [data-pdp-sticky-price]').forEach((node) => {
        node.textContent = `${formatted} грн`;
      });
      const addButton = state.root.querySelector('.tc-add-btn[data-add-to-cart]');
      if (addButton) addButton.dataset.productPrice = String(variant.final_price);
      const analytics = document.getElementById('product-analytics-payload');
      if (analytics) analytics.dataset.price = String(variant.final_price);
    }

    const reason = String(variant.price_reason || '').trim();
    const hasAdjustment = Boolean(variant.has_price_adjustment || reason);
    const note = state.root.querySelector('[data-pdp-price-note]');
    if (note) {
      note.classList.toggle('is-hidden', !hasAdjustment);
      const title = note.querySelector('[data-pdp-price-note-title]');
      const copy = note.querySelector('[data-pdp-price-note-copy]');
      if (title) title.textContent = reason || 'Особливість вибраного кольору';
      if (copy) {
        copy.textContent = variant.is_thermo
          ? 'Термохромна тканина реагує на тепло, тому коштує дорожче. Звичайні кольори можуть мати нижчу ціну.'
          : 'Ціна залежить від вибраного кольору та матеріалу.';
      }
    }

    const story = state.root.querySelector('[data-pdp-variant-story]');
    if (story) {
      const storyText = String(variant.thermo_description || variant.marketing_html || '').trim();
      story.classList.toggle('is-hidden', !storyText);
      story.classList.toggle('is-thermo', Boolean(variant.is_thermo));
      const storyTitle = story.querySelector('[data-pdp-variant-story-title]');
      const storyCopy = story.querySelector('[data-pdp-variant-story-text]');
      const storyIcon = story.querySelector('[data-pdp-variant-story-icon]');
      if (storyTitle) storyTitle.textContent = variant.is_thermo ? 'Термохромна тканина' : 'Особливість кольору';
      if (storyCopy) storyCopy.textContent = storyText;
      if (storyIcon) storyIcon.hidden = !variant.is_thermo;
    }

    applyVariantSizeRules(
      state,
      baseVariant.size_rules || [],
      baseVariant.available_sizes_by_fit || {}
    );
    applyVariantSizeGuides(state, baseVariant);
  }

  function applyVariantSizeGuides(state, variant) {
    const guides = variant.size_guides_by_fit && typeof variant.size_guides_by_fit === 'object'
      ? variant.size_guides_by_fit
      : {};
    state.root.querySelectorAll('[data-size-grid-fit]').forEach((card) => {
      const fitCode = card.getAttribute('data-size-grid-fit') || '';
      const guide = guides[fitCode];
      if (!guide || !Array.isArray(guide.columns) || !Array.isArray(guide.rows)) return;

      const colorLabel = card.querySelector('.tc-size-comparison__head small');
      if (colorLabel) colorLabel.textContent = variant.name || '';
      const intro = card.querySelector('.tc-muted-copy');
      if (intro) {
        intro.textContent = guide.intro || '';
        intro.hidden = !guide.intro;
      }
      const table = card.querySelector('.tc-size-table');
      if (!table) return;
      const headRow = table.querySelector('thead tr');
      const body = table.querySelector('tbody');
      if (!headRow || !body) return;

      headRow.replaceChildren(...guide.columns.map((column) => {
        const cell = document.createElement('th');
        cell.scope = 'col';
        cell.textContent = column.label || column.key || '';
        return cell;
      }));
      body.replaceChildren(...guide.rows.map((row) => {
        const tr = document.createElement('tr');
        guide.columns.forEach((column) => {
          const cell = document.createElement('td');
          if (column.key === 'size') cell.className = 'tc-size-table-key';
          cell.textContent = column.key === 'size'
            ? (row.display_size || row.size || '')
            : (row[column.key] || '');
          tr.appendChild(cell);
        });
        return tr;
      }));
    });
  }

  function applyVariantFitRules(state, rules) {
    const selector = state.root.querySelector('[data-fit-selector]');
    if (!selector) return;
    const enabledInputs = [];
    selector.querySelectorAll('input[name="fit_option"]').forEach((input) => {
      const rule = rules[input.value] || {};
      const enabled = rule.is_enabled !== false;
      const label = selector.querySelector(`label[for="${input.id}"]`);
      input.disabled = !enabled;
      if (label) {
        label.hidden = !enabled;
        label.setAttribute('aria-disabled', enabled ? 'false' : 'true');
        if (rule.reason) label.setAttribute('title', rule.reason);
        else label.removeAttribute('title');
      }
      state.root.querySelectorAll(`[data-size-grid-fit="${input.value}"]`).forEach((card) => {
        card.hidden = !enabled;
      });
      if (enabled) enabledInputs.push(input);
    });

    let selected = selector.querySelector('input[name="fit_option"]:checked:not(:disabled)');
    if (!selected && enabledInputs.length) {
      selected = enabledInputs[0];
      selected.checked = true;
    }
    selector.querySelectorAll('[data-fit-option]').forEach((label) => {
      const input = selector.querySelector(`#${label.getAttribute('for')}`);
      label.classList.toggle('active', Boolean(input && input.checked && !input.disabled));
    });
    if (selected) state.container.dataset.currentFit = selected.value || '';
  }

  function applyVariantSizeRules(state, rules, sizeMatrix) {
    const fit = String(state.container.dataset.currentFit || '').toLowerCase();
    const normalizedRules = Array.isArray(rules) ? rules : [];
    const matrix = sizeMatrix && typeof sizeMatrix === 'object' ? sizeMatrix : {};
    const hasFitGrid = Object.prototype.hasOwnProperty.call(matrix, fit);
    const fitGridSizes = new Set((matrix[fit] || []).map((size) => String(size || '').toUpperCase()));
    const enabledInputs = [];
    state.root.querySelectorAll('input[name="size"]').forEach((input) => {
      const size = String(input.value || '').toUpperCase();
      const general = normalizedRules.filter((rule) =>
        String(rule.size || '').toUpperCase() === size && !String(rule.fit_code || '')
      ).pop();
      const specific = normalizedRules.filter((rule) =>
        String(rule.size || '').toUpperCase() === size && String(rule.fit_code || '').toLowerCase() === fit
      ).pop();
      const rule = specific || general;
      const enabledByGrid = !hasFitGrid || fitGridSizes.has(size);
      const enabledByRule = !rule || (rule.is_enabled !== false && (rule.stock == null || Number(rule.stock) > 0));
      const enabled = enabledByGrid && enabledByRule;
      const label = state.root.querySelector(`label[for="${input.id}"]`);
      input.disabled = !enabled;
      if (label) {
        label.classList.toggle('is-unavailable', !enabled);
        label.setAttribute('aria-disabled', enabled ? 'false' : 'true');
        if (rule && rule.note) label.setAttribute('title', rule.note);
        else label.removeAttribute('title');
      }
      if (enabled) enabledInputs.push(input);
    });

    let selected = state.root.querySelector('input[name="size"]:checked:not(:disabled)');
    if (!selected && enabledInputs.length) {
      selected = enabledInputs[0];
      selected.checked = true;
    }
    updateCurrentOfferId(state);
  }

  function initVariantPriceNote(root) {
    const note = root.querySelector('[data-pdp-price-note]');
    const trigger = note && note.querySelector('[data-pdp-price-note-trigger]');
    if (!note || !trigger) return;

    const close = () => {
      note.classList.remove('is-open');
      trigger.setAttribute('aria-expanded', 'false');
    };
    trigger.addEventListener('click', (event) => {
      event.stopPropagation();
      const open = !note.classList.contains('is-open');
      note.classList.toggle('is-open', open);
      trigger.setAttribute('aria-expanded', open ? 'true' : 'false');
    });
    document.addEventListener('click', (event) => {
      if (!note.contains(event.target)) close();
    });
    document.addEventListener('keydown', (event) => {
      if (event.key === 'Escape') close();
    });
  }

  function initSizeSelection(state) {
    selectSizeFromURL();
    state.root.querySelectorAll('input[name="size"]').forEach((input) => {
      input.addEventListener('change', () => {
        const offerId = updateCurrentOfferId(state);
        // Выбор размера — это кастомизация продукта (по аналогии с выбором
        // цвета). Trekаем CustomizeProduct, как рекомендует Meta
        // ("A person selects the color/size of a t-shirt").
        const variantId = state.container.dataset.currentVariant || null;
        trackCustomizeProduct(state, variantId, offerId, {
          option: 'size',
          size: input.value ? String(input.value).toUpperCase() : '',
        });
      });
    });
  }

  function selectSizeFromURL() {
    const sizeParam = new URLSearchParams(window.location.search).get('size');
    if (!sizeParam) return;
    const wanted = sizeParam.toUpperCase();
    const input = Array.from(document.querySelectorAll('input[name="size"]'))
      .find((item) => String(item.value || '').toUpperCase() === wanted);
    if (input && !input.checked) {
      input.checked = true;
      input.dispatchEvent(new Event('change', { bubbles: true }));
    }
  }

  function currentSelection(state) {
    const color = state.root.querySelector('#color-picker .color-swatch.active');
    const sizeInput = state.root.querySelector('input[name="size"]:checked');
    const colorVariantId = color ? color.getAttribute('data-variant') : null;
    const size = sizeInput ? String(sizeInput.value || '').toUpperCase() : '';
    const key = `${colorVariantId || 'default'}:${size}`;
    const fallback = state.container.getAttribute('data-current-offer-id') ||
      state.container.getAttribute('data-default-offer-id') ||
      '';

    return {
      colorVariantId,
      size,
      key,
      offerId: state.offerIdMap[key] || fallback,
    };
  }

  function updateCurrentOfferId(state) {
    const selection = currentSelection(state);
    if (selection.offerId) {
      state.container.setAttribute('data-current-offer-id', selection.offerId);
    }
    return selection.offerId;
  }

  function initFitSelection(root) {
    const selector = root.querySelector('[data-fit-selector]');
    if (!selector) return;

    const options = Array.from(selector.querySelectorAll('[data-fit-option]'));
    const container = document.getElementById('product-detail-container');
    const checked = selector.querySelector('input[name="fit_option"]:checked');
    if (checked && container) {
      container.dataset.currentFit = checked.value || '';
      const label = selector.querySelector(`label[for="${checked.id}"]`);
      if (label) label.classList.add('active');
    }
    selector.querySelectorAll('input[name="fit_option"]').forEach((input) => {
      input.addEventListener('change', () => {
        options.forEach((option) => option.classList.remove('active'));
        const label = selector.querySelector(`label[for="${input.id}"]`);
        if (label) label.classList.add('active');
        if (container) container.dataset.currentFit = input.value || '';

        // Phase 19h (2026-05-10): fit toggle was JS-only — page kept
        // serving the previous fit's bottom SEO copy (Phase 16). Now
        // navigate with ``?fit=<code>`` so the server re-renders with
        // the correct ``fit_code`` (Phase 7.5 then 301-redirects to
        // canonical ``/<color>/<size>/<fit>/``).
        const fitValue = (input.value || '').trim();
        if (!fitValue) return;
        const currentUrl = new URL(window.location.href);
        if (currentUrl.searchParams.get('fit') === fitValue) return;
        currentUrl.searchParams.set('fit', fitValue);
        window.location.assign(currentUrl.toString());
      });
    });
  }

  function initTabs(root) {
    const tabs = Array.from(root.querySelectorAll('[data-pdp-tab]'));
    const panels = Array.from(root.querySelectorAll('[data-pdp-panel]'));
    if (!tabs.length || !panels.length) return;

    const activateTab = (tab, focus) => {
        const target = tab.getAttribute('data-pdp-tab');
        tabs.forEach((item) => {
          const active = item === tab;
          item.classList.toggle('active', active);
          item.setAttribute('aria-selected', active ? 'true' : 'false');
          item.setAttribute('tabindex', active ? '0' : '-1');
        });
        panels.forEach((panel) => {
          const active = panel.getAttribute('data-pdp-panel') === target;
          panel.classList.toggle('active', active);
          panel.hidden = !active;
        });
        tab.scrollIntoView({ block: 'nearest', inline: 'nearest' });
        if (focus) tab.focus();
    };

    tabs.forEach((tab, index) => {
      tab.setAttribute('tabindex', tab.classList.contains('active') ? '0' : '-1');
      tab.addEventListener('click', () => activateTab(tab, false));
      tab.addEventListener('keydown', (event) => {
        const keys = ['ArrowLeft', 'ArrowRight', 'Home', 'End'];
        if (!keys.includes(event.key)) return;
        event.preventDefault();
        let nextIndex = index;
        if (event.key === 'ArrowLeft') nextIndex = (index - 1 + tabs.length) % tabs.length;
        if (event.key === 'ArrowRight') nextIndex = (index + 1) % tabs.length;
        if (event.key === 'Home') nextIndex = 0;
        if (event.key === 'End') nextIndex = tabs.length - 1;
        activateTab(tabs[nextIndex], true);
      });
    });

    root.querySelectorAll('[data-pdp-tab-trigger]').forEach((button) => {
      button.addEventListener('click', () => {
        const target = button.getAttribute('data-pdp-tab-trigger');
        const tab = tabs.find((item) => item.getAttribute('data-pdp-tab') === target);
        if (tab) activateTab(tab, true);
      });
    });
  }

  function initDescriptionCollapse(root) {
    const collapse = root.querySelector('[data-pdp-description-collapse]');
    if (!collapse) return;

    const content = collapse.querySelector('[data-pdp-description-content]');
    const toggle = collapse.querySelector('[data-pdp-description-toggle]');
    if (!content || !toggle) return;

    let measureRaf = null;

    const collapsedHeight = () => {
      const value = window.getComputedStyle(collapse).getPropertyValue('--tc-desc-collapsed-height');
      const parsed = Number.parseFloat(value);
      return Number.isFinite(parsed) && parsed > 0 ? parsed : 278;
    };

    const toggleLabel = toggle.querySelector('.tc-desc-more__label');
    const setToggleText = (expanded) => {
      const text = expanded
        ? (toggle.dataset.lessLabel || 'Згорнути')
        : (toggle.dataset.moreLabel || 'Показати більше');
      if (toggleLabel) {
        toggleLabel.textContent = text;
      } else {
        toggle.textContent = text;
      }
    };

    const updateState = () => {
      const limit = collapsedHeight();
      const fullHeight = content.scrollHeight;
      const isCollapsible = fullHeight > limit + 18;
      const isExpanded = collapse.classList.contains('is-expanded');

      collapse.classList.toggle('is-collapsible', isCollapsible);
      toggle.hidden = !isCollapsible;

      if (!isCollapsible) {
        collapse.classList.remove('is-collapsed', 'is-expanded');
        toggle.setAttribute('aria-expanded', 'false');
        content.style.removeProperty('max-height');
        setToggleText(false);
        return;
      }

      collapse.style.setProperty('--tc-desc-expanded-height', `${fullHeight}px`);
      collapse.classList.toggle('is-collapsed', !isExpanded);
      toggle.setAttribute('aria-expanded', isExpanded ? 'true' : 'false');
      setToggleText(isExpanded);
    };

    const scheduleUpdate = () => {
      if (measureRaf) window.cancelAnimationFrame(measureRaf);
      measureRaf = window.requestAnimationFrame(() => {
        measureRaf = null;
        updateState();
      });
    };

    toggle.addEventListener('click', () => {
      const isExpanded = toggle.getAttribute('aria-expanded') === 'true';
      collapse.style.setProperty('--tc-desc-expanded-height', `${content.scrollHeight}px`);
      collapse.classList.toggle('is-expanded', !isExpanded);
      collapse.classList.toggle('is-collapsed', isExpanded);
      toggle.setAttribute('aria-expanded', isExpanded ? 'false' : 'true');
      setToggleText(!isExpanded);
    });

    scheduleUpdate();
    window.addEventListener('resize', scheduleUpdate, { passive: true });
    if (window.ResizeObserver) {
      const observer = new ResizeObserver(scheduleUpdate);
      observer.observe(content);
    }
    if (!prefersReducedMotion) {
      window.setTimeout(scheduleUpdate, 180);
    }
  }

  function initShare(root, container) {
    const buttons = root.querySelectorAll('[data-share-action], [data-share-copy]');
    if (!buttons.length) return;

    buttons.forEach((button) => {
      if (button.dataset.shareBound === '1') return;
      button.dataset.shareBound = '1';
      button.addEventListener('click', async () => {
        const action = button.getAttribute('data-share-action') || 'copy';
        const shareData = buildShareData(root, container);

        if (action === 'native') {
          const shared = await tryNativeShare(shareData);
          if (shared) {
            trackShareAction('native', shareData.url);
            pulseShareButton(button, 'is-shared');
            return;
          }
          await copyShareUrl(button, shareData.url);
          return;
        }

        if (action === 'copy') {
          await copyShareUrl(button, shareData.url);
          return;
        }

        if (action === 'telegram') {
          trackShareAction(action, shareData.url);
          openTelegramShare(shareData);
          return;
        }

        const providerUrl = buildProviderShareUrl(action, shareData);
        if (!providerUrl) return;
        trackShareAction(action, shareData.url);
        openShareTarget(providerUrl);
      });
    });
  }

  function buildShareData(root, container) {
    const title = decodeDataValue(container.getAttribute('data-product-title')) || cleanDocumentTitle();
    const category = decodeDataValue(container.getAttribute('data-product-category'));
    const text = category ? `${title} — ${category} від TwoComms` : `${title} — TwoComms`;
    return {
      title,
      text,
      url: buildShareUrl(root, container),
    };
  }

  function buildShareUrl(root, container) {
    const canonical = document.querySelector('link[rel="canonical"]');
    const rawUrl = (canonical && canonical.href) ||
      container.getAttribute('data-share-url') ||
      window.location.href;
    let url;
    try {
      url = new URL(rawUrl, window.location.href);
    } catch (_) {
      return window.location.href;
    }

    url.hash = '';
    url.search = '';

    const sizeInput = root.querySelector('input[name="size"]:checked');
    const size = sizeInput ? String(sizeInput.value || '').toUpperCase() : '';
    if (size) url.searchParams.set('size', size);

    const color = root.querySelector('#color-picker .color-swatch.active');
    const colorId = color ? color.getAttribute('data-variant') : '';
    if (colorId && colorId !== 'default') url.searchParams.set('color', colorId);

    const fitInput = root.querySelector('input[name="fit_option"]:checked');
    const fit = fitInput ? String(fitInput.value || '').trim() : '';
    if (fit) url.searchParams.set('fit', fit);

    return url.toString();
  }

  async function tryNativeShare(shareData) {
    if (!navigator.share) return false;
    const payload = {
      title: shareData.title,
      text: shareData.text,
      url: shareData.url,
    };

    try {
      if (navigator.canShare && !navigator.canShare(payload)) return false;
    } catch (_) { }

    try {
      await navigator.share(payload);
      return true;
    } catch (_) {
      return false;
    }
  }

  function buildProviderShareUrl(action, shareData) {
    const url = encodeURIComponent(shareData.url);
    const text = encodeURIComponent(shareData.text);

    if (action === 'telegram') {
      return `https://t.me/share/url?url=${url}&text=${text}`;
    }
    if (action === 'facebook') {
      return `https://www.facebook.com/sharer/sharer.php?u=${url}`;
    }
    if (action === 'x') {
      return `https://twitter.com/intent/tweet?url=${url}&text=${text}`;
    }
    return '';
  }

  function openTelegramShare(shareData) {
    const webUrl = buildProviderShareUrl('telegram', shareData);
    if (!isLikelyMobile()) {
      openShareTarget(webUrl);
      return;
    }

    const appUrl = `tg://msg_url?url=${encodeURIComponent(shareData.url)}&text=${encodeURIComponent(shareData.text)}`;
    window.location.href = appUrl;
    window.setTimeout(() => {
      if (document.visibilityState === 'visible') {
        window.location.href = webUrl;
      }
    }, 850);
  }

  function isLikelyMobile() {
    return window.matchMedia && window.matchMedia('(max-width: 767.98px), (pointer: coarse)').matches;
  }

  function openShareTarget(url) {
    const popup = window.open(url, '_blank', 'width=760,height=620');
    if (popup && typeof popup.focus === 'function') {
      try {
        popup.opener = null;
      } catch (_) { }
      popup.focus();
      return;
    }
    window.location.href = url;
  }

  async function copyShareUrl(button, url) {
    const copied = await copyToClipboard(url);
    if (!copied) return;
    trackShareAction('copy', url);
    pulseShareButton(button, 'is-copied');
  }

  function pulseShareButton(button, className) {
    button.classList.add(className);
    window.setTimeout(() => button.classList.remove(className), 1400);
  }

  function trackShareAction(method, url) {
    try {
      window.dataLayer = window.dataLayer || [];
      window.dataLayer.push({
        event: 'share_product',
        event_id: makeEventId(),
        method,
        product_url: url,
      });
    } catch (_) { }
  }

  function cleanDocumentTitle() {
    return String(document.title || 'TwoComms').replace(/\s*[|—-]\s*TwoComms.*$/i, '').trim() || 'TwoComms';
  }

  function decodeDataValue(value) {
    if (!value) return '';
    return String(value)
      .replace(/\\u([0-9a-fA-F]{4})/g, (_, code) => String.fromCharCode(Number.parseInt(code, 16)))
      .replace(/\\x27/g, "'")
      .replace(/\\"/g, '"')
      .replace(/\\\\/g, '\\')
      .trim();
  }

  async function copyToClipboard(value) {
    try {
      if (navigator.clipboard && window.isSecureContext) {
        await navigator.clipboard.writeText(value);
        return true;
      }
    } catch (_) { }

    const input = document.createElement('textarea');
    input.value = value;
    input.setAttribute('readonly', '');
    input.style.position = 'fixed';
    input.style.top = '-1000px';
    document.body.appendChild(input);
    input.select();
    let ok = false;
    try {
      ok = document.execCommand('copy');
    } catch (_) {
      ok = false;
    }
    input.remove();
    return ok;
  }

  function initZoom(state) {
    const buttons = state.root.querySelectorAll('[data-gallery-zoom]');
    if (!state.mainImage) return;

    buttons.forEach((button) => {
      button.addEventListener('click', () => openLightbox(state.mainImage.getAttribute('data-zoom') || state.mainImage.src, state.mainImage.alt));
    });
    state.mainImage.addEventListener('click', () => {
      openLightbox(state.mainImage.getAttribute('data-zoom') || state.mainImage.currentSrc || state.mainImage.src, state.mainImage.alt);
    });
    state.mainImage.setAttribute('tabindex', '0');
    state.mainImage.addEventListener('keydown', (event) => {
      if (event.key === 'Enter' || event.key === ' ') {
        event.preventDefault();
        openLightbox(state.mainImage.getAttribute('data-zoom') || state.mainImage.currentSrc || state.mainImage.src, state.mainImage.alt);
      }
    });
  }

  function initVideo(state) {
    if (!state.video || !state.video.embed_url || !state.videoStage) return;
    const openButtons = state.root.querySelectorAll('[data-video-open]');
    openButtons.forEach((button) => {
      button.addEventListener('click', () => {
        const videoThumb = state.thumbs && state.thumbs.querySelector('[data-video-thumb]');
        if (videoThumb) {
          state.thumbs.querySelectorAll('.tc-thumbnail').forEach((item) => item.classList.remove('active'));
          videoThumb.classList.add('active');
        }
        showVideo(state);
      });
    });
  }

  function buildVideoFacade(state) {
    // Lazy-load: показываем постер с кнопкой play, iframe грузим по клику,
    // чтобы не тянуть YouTube до того, как пользователь реально захотел видео.
    const facade = document.createElement('div');
    facade.className = 'tc-video-facade';

    if (state.video.thumbnail_url) {
      const poster = document.createElement('img');
      poster.className = 'tc-video-facade__poster';
      poster.src = state.video.thumbnail_url;
      poster.alt = state.video.title || '';
      poster.loading = 'lazy';
      poster.decoding = 'async';
      // Если постер не загрузился (нет hqdefault / сетевой сбой) — убираем
      // битую картинку, остаётся чистый градиентный фон сцены с play-кнопкой.
      poster.addEventListener('error', () => {
        poster.remove();
        facade.classList.add('tc-video-facade--no-poster');
      });
      facade.appendChild(poster);
    } else {
      facade.classList.add('tc-video-facade--no-poster');
    }

    const play = document.createElement('button');
    play.type = 'button';
    play.className = 'tc-video-facade__play';
    play.setAttribute('aria-label', state.video.title || 'Відтворити відео');
    play.innerHTML = '<svg width="34" height="34" viewBox="0 0 24 24" aria-hidden="true"><path d="M8 5v14l11-7z" fill="currentColor"/></svg>';
    facade.appendChild(play);

    const caption = document.createElement('span');
    caption.className = 'tc-video-facade__caption';
    caption.textContent = state.video.title || 'Відео товару';
    facade.appendChild(caption);

    const mountIframe = () => {
      const iframe = document.createElement('iframe');
      const sep = state.video.embed_url.indexOf('?') === -1 ? '?' : '&';
      iframe.src = `${state.video.embed_url}${sep}autoplay=1&rel=0&modestbranding=1&playsinline=1`;
      iframe.title = state.video.title || 'Відео товару';
      iframe.allow = 'accelerometer; autoplay; clipboard-write; encrypted-media; gyroscope; picture-in-picture; web-share';
      iframe.setAttribute('allowfullscreen', '');
      iframe.loading = 'lazy';
      iframe.className = 'tc-video-frame';
      state.videoStage.innerHTML = '';
      state.videoStage.appendChild(iframe);
    };

    facade.addEventListener('click', mountIframe);
    return facade;
  }

  function showVideo(state) {
    if (!state.video || !state.video.embed_url || !state.videoStage) return;
    if (!state.videoActive) {
      state.videoStage.innerHTML = '';
      state.videoStage.appendChild(buildVideoFacade(state));
    }
    state.videoActive = true;
    state.videoStage.hidden = false;
    state.videoStage.setAttribute('aria-hidden', 'false');
    if (state.root) state.root.classList.add('is-video-active');
  }

  function hideVideo(state) {
    if (!state.videoStage || !state.videoActive) return;
    state.videoActive = false;
    state.videoStage.hidden = true;
    state.videoStage.setAttribute('aria-hidden', 'true');
    // Полностью выгружаем iframe, чтобы остановить воспроизведение.
    state.videoStage.innerHTML = '';
    if (state.root) state.root.classList.remove('is-video-active');
  }

  function openLightbox(src, alt) {
    if (!src) return;
    let scale = 1;
    let translateX = 0;
    let translateY = 0;
    let dragging = false;
    let startX = 0;
    let startY = 0;
    let pointerId = null;

    const overlay = document.createElement('div');
    overlay.className = 'tc-lightbox';
    overlay.setAttribute('role', 'dialog');
    overlay.setAttribute('aria-modal', 'true');
    overlay.setAttribute('aria-label', 'Перегляд фото товару');

    const toolbar = document.createElement('div');
    toolbar.className = 'tc-lightbox-toolbar';

    const makeButton = (label, text, className) => {
      const button = document.createElement('button');
      button.type = 'button';
      button.className = className || '';
      button.setAttribute('aria-label', label);
      button.textContent = text;
      return button;
    };

    const zoomOut = makeButton('Зменшити фото', '−', 'tc-lightbox-tool');
    const zoomReset = makeButton('Повернути масштаб 100%', '100%', 'tc-lightbox-tool tc-lightbox-reset');
    const zoomIn = makeButton('Збільшити фото', '+', 'tc-lightbox-tool');
    const close = makeButton('Закрити фото', '×', 'tc-lightbox-close');

    toolbar.appendChild(zoomOut);
    toolbar.appendChild(zoomReset);
    toolbar.appendChild(zoomIn);
    toolbar.appendChild(close);

    const viewport = document.createElement('div');
    viewport.className = 'tc-lightbox-viewport';

    const img = document.createElement('img');
    img.src = src;
    img.alt = alt || '';
    img.draggable = false;

    viewport.appendChild(img);
    overlay.appendChild(toolbar);
    overlay.appendChild(viewport);
    document.body.appendChild(overlay);
    document.body.style.overflow = 'hidden';

    const clamp = (value, min, max) => Math.min(max, Math.max(min, value));
    const applyTransform = () => {
      if (scale <= 1) {
        translateX = 0;
        translateY = 0;
      }
      img.style.transform = `translate3d(${translateX}px, ${translateY}px, 0) scale(${scale})`;
      zoomReset.textContent = `${Math.round(scale * 100)}%`;
      overlay.classList.toggle('is-zoomed', scale > 1);
    };
    const setScale = (nextScale) => {
      scale = clamp(nextScale, 1, 3.5);
      applyTransform();
    };

    const remove = () => {
      overlay.classList.remove('is-visible');
      window.setTimeout(() => {
        overlay.remove();
        document.body.style.overflow = '';
      }, prefersReducedMotion ? 0 : 160);
      document.removeEventListener('keydown', onKeydown);
    };
    const onKeydown = (event) => {
      if (event.key === 'Escape') remove();
      if (event.key === '+' || event.key === '=') setScale(scale + 0.25);
      if (event.key === '-') setScale(scale - 0.25);
      if (event.key === '0') setScale(1);
    };

    overlay.addEventListener('click', (event) => {
      if (event.target === overlay) remove();
    });
    close.addEventListener('click', remove);
    zoomIn.addEventListener('click', () => setScale(scale + 0.35));
    zoomOut.addEventListener('click', () => setScale(scale - 0.35));
    zoomReset.addEventListener('click', () => setScale(1));
    viewport.addEventListener('dblclick', () => setScale(scale > 1 ? 1 : 2));
    viewport.addEventListener('wheel', (event) => {
      event.preventDefault();
      setScale(scale + (event.deltaY < 0 ? 0.18 : -0.18));
    }, { passive: false });
    viewport.addEventListener('pointerdown', (event) => {
      if (scale <= 1) return;
      dragging = true;
      pointerId = event.pointerId;
      startX = event.clientX - translateX;
      startY = event.clientY - translateY;
      viewport.setPointerCapture(pointerId);
    });
    viewport.addEventListener('pointermove', (event) => {
      if (!dragging || scale <= 1) return;
      translateX = event.clientX - startX;
      translateY = event.clientY - startY;
      applyTransform();
    });
    const stopDrag = () => {
      dragging = false;
      pointerId = null;
    };
    viewport.addEventListener('pointerup', stopDrag);
    viewport.addEventListener('pointercancel', stopDrag);
    document.addEventListener('keydown', onKeydown);
    close.focus({ preventScroll: true });
    window.requestAnimationFrame(() => overlay.classList.add('is-visible'));
  }

  function initRecentViewed(state) {
    const panel = document.querySelector('[data-recent-viewed-panel]');
    if (!panel || !state.container) return;

    const storageKey = 'twc_recent_products_v1';
    const canonical = document.querySelector('link[rel="canonical"]');
    const url = canonical && canonical.href ? canonical.href : window.location.href.split('#')[0];
    const productId = String(state.container.dataset.productId || '');
    const currentImage = normalizeImage(mainImagePayload(state.mainImage));
    const current = {
      id: productId,
      title: decodeDataValue(state.container.getAttribute('data-product-title')) || cleanDocumentTitle(),
      category: decodeDataValue(state.container.getAttribute('data-product-category')) || '',
      price: (document.getElementById('product-analytics-payload') && document.getElementById('product-analytics-payload').dataset.price) || '',
      image: currentImage ? (currentImage.thumbnailUrl || currentImage.url) : '',
      url,
    };

    let items = [];
    try {
      items = JSON.parse(window.localStorage.getItem(storageKey) || '[]') || [];
    } catch (_) {
      items = [];
    }

    const previous = items.filter((item) => item && String(item.id) !== productId && item.url && item.title).slice(0, 4);
    renderRecentViewed(panel, previous);

    if (productId && current.url && current.title) {
      const nextItems = [current].concat(previous).slice(0, 8);
      try {
        window.localStorage.setItem(storageKey, JSON.stringify(nextItems));
      } catch (_) { }
    }
  }

  function renderRecentViewed(panel, items) {
    if (!items.length) {
      panel.hidden = true;
      return;
    }
    const list = panel.querySelector('[data-recent-viewed-list]');
    if (!list) return;
    list.innerHTML = items.map((item) => `
      <a class="tc-recent-card" href="${escapeAttribute(item.url)}">
        <span class="tc-recent-image">
          ${item.image ? `<img src="${escapeAttribute(item.image)}" alt="${escapeAttribute(item.title)} — переглянутий товар TwoComms" loading="lazy" width="112" height="140">` : ''}
        </span>
        <span class="tc-recent-copy">
          <strong>${escapeHtml(item.title)}</strong>
          ${item.category ? `<small>${escapeHtml(item.category)}</small>` : ''}
          ${item.price ? `<b>${escapeHtml(item.price)} грн</b>` : ''}
        </span>
      </a>
    `).join('');
    panel.hidden = false;
  }

  function escapeHtml(value) {
    return String(value || '')
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  }

  function escapeAttribute(value) {
    return escapeHtml(value).replace(/`/g, '&#96;');
  }

  function initStickyAdd(root) {
    const stickyBar = root.querySelector('#productStickyMobile');
    const stickyButton = root.querySelector('[data-pdp-sticky-add]');
    const mainButton = root.querySelector('.tc-add-btn[data-add-to-cart]');
    if (!stickyButton || !mainButton) return;

    stickyButton.addEventListener('click', () => {
      mainButton.click();
    });

    if (!stickyBar) return;

    const mediaPanel = root.querySelector('.tc-gallery-card') || root.querySelector('.tc-media-stage');
    const purchaseBar = root.querySelector('#product-action-bar');
    const mobileQuery = window.matchMedia('(max-width: 767.98px)');

    const updateStickyVisibility = () => {
      if (!mobileQuery.matches) {
        stickyBar.classList.remove('is-visible');
        return;
      }

      const mediaBottom = mediaPanel ? mediaPanel.getBoundingClientRect().bottom : 0;
      const purchaseRect = purchaseBar ? purchaseBar.getBoundingClientRect() : null;
      const purchaseVisible = purchaseRect && purchaseRect.top < window.innerHeight - 110 && purchaseRect.bottom > 0;
      const hasPassedMedia = mediaBottom < 88;
      stickyBar.classList.toggle('is-visible', hasPassedMedia && !purchaseVisible);
    };

    updateStickyVisibility();
    window.addEventListener('scroll', updateStickyVisibility, { passive: true });
    window.addEventListener('resize', updateStickyVisibility);
    if (mobileQuery.addEventListener) {
      mobileQuery.addEventListener('change', updateStickyVisibility);
    }
  }

  function trackCustomizeProduct(state, variantId, offerId, extra) {
    try {
      if (!window.trackEvent || !offerId) return;
      const payload = document.getElementById('product-analytics-payload');
      const price = parsePrice(payload && payload.dataset.price);
      const eventData = {
        content_ids: [offerId],
        content_type: 'product',
        variant_id: variantId || null,
        value: price,
        currency: 'UAH',
      };
      if (extra && typeof extra === 'object') {
        if (extra.option) eventData.customization = extra.option;
        if (extra.size) eventData.size = extra.size;
      }
      window.trackEvent('CustomizeProduct', eventData);
    } catch (_) { }
  }

  function trackViewContent(state) {
    window.setTimeout(() => {
      if (state.viewContentTracked) return;
      const payload = document.getElementById('product-analytics-payload');
      // FIX 2026-06-12: раньше тут был ранний выход `!window.trackEvent`,
      // из-за которого view_item НЕ попадал даже в dataLayer, если
      // analytics-loader.js ещё не загрузился (он грузится только после
      // первого user interaction). GA4 недосчитывал view_item (76 view_item
      // против 82 add_to_cart за 30 дней). dataLayer-push не требует
      // trackEvent — GTM обрабатывает буфер при загрузке; Meta ViewContent
      // уходит через trackEvent-стаб с очередью (см. base.html + loader).
      if (!payload) return;

      const selection = currentSelection(state);
      const offerId = selection.offerId || payload.dataset.id;
      const price = parsePrice(payload.dataset.price);
      const title = payload.dataset.title || state.container.getAttribute('data-product-title') || '';
      const category = payload.dataset.category || state.container.getAttribute('data-product-category') || '';
      const eventId = makeEventId();
      const trackingCtx = (typeof window.getTrackingContext === 'function' && window.getTrackingContext()) || {};
      const item = {
        item_id: offerId,
        item_name: title,
        item_brand: 'TwoComms',
        item_category: category || '',
        item_variant: selection.size || '',
        price,
        quantity: 1,
        currency: 'UAH',
      };

      state.viewContentTracked = true;
      window.dataLayer = window.dataLayer || [];
      window.dataLayer.push({
        event: 'view_item',
        event_id: eventId,
        fbp: trackingCtx.fbp || null,
        fbc: trackingCtx.fbc || null,
        ecommerce: {
          currency: 'UAH',
          value: price,
          items: [item],
        },
        eventModel: {
          event_id: eventId,
          value: price,
          currency: 'UAH',
          content_name: title,
          items: [{ id: offerId, name: title, price, quantity: 1 }],
          ecomm_prodid: [offerId],
          ecomm_pagetype: 'product',
          ecomm_totalvalue: price,
        },
      });

      const metaOptions = { event_id: eventId };
      try {
        if (typeof window.buildUserDataForEvent === 'function') {
          const ctx = window.buildUserDataForEvent();
          if (ctx && typeof ctx === 'object') {
            if (ctx.user_data && Object.keys(ctx.user_data).length) metaOptions.user_data = ctx.user_data;
            if (ctx.external_id) metaOptions.external_id = ctx.external_id;
            if (ctx.fbp) metaOptions.fbp = ctx.fbp;
            if (ctx.fbc) metaOptions.fbc = ctx.fbc;
          }
        }
      } catch (_) { }

      if (typeof window.trackEvent !== 'function') return;
      window.trackEvent('ViewContent', {
        content_ids: [offerId],
        content_name: title,
        content_type: 'product',
        content_category: category,
        value: price,
        currency: 'UAH',
        contents: [{
          id: offerId,
          quantity: 1,
          item_price: price,
        }],
        event_id: eventId,
        __meta: metaOptions,
      });
    }, 240);
  }

  function parsePrice(value) {
    const parsed = Number.parseFloat(value || '0');
    return Number.isFinite(parsed) && parsed > 0 ? parsed : 0;
  }

  function makeEventId() {
    if (window.__twcAnalytics && typeof window.__twcAnalytics.safeGenerateEventId === 'function') {
      return window.__twcAnalytics.safeGenerateEventId();
    }
    if (typeof window.generateEventId === 'function') {
      return window.generateEventId();
    }
    return `${Date.now()}_${Math.random().toString(36).slice(2, 11)}`;
  }
})();
