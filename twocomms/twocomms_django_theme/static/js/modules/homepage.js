import { prefersReducedMotion } from './shared.js';

function parseNumber(value, fallback = 1) {
  const parsed = parseInt(value, 10);
  return Number.isNaN(parsed) ? fallback : parsed;
}

function buildPageHref(basePath, page) {
  const safeBasePath = basePath || window.location.pathname || '/';
  if (page <= 1) {
    return safeBasePath;
  }
  return `${safeBasePath}?page=${page}`;
}

// Phase 17a — preserve the active language prefix when calling AJAX endpoints
// declared inside ``i18n_patterns`` (e.g. ``/load-more-products/``). Without
// this prefix the request resolves to the default Ukrainian locale even when
// the user has switched to ``/ru/`` or ``/en/``, which causes the load-more
// button to return UA content on translated pages.
function getLanguagePrefix() {
  try {
    const path = window.location.pathname || '/';
    const match = path.match(/^\/(ru|en)(?:\/|$)/);
    if (match) {
      return `/${match[1]}`;
    }
  } catch (_) {}
  return '';
}

function isMobilePaginationViewport() {
  return typeof window !== 'undefined' && window.matchMedia('(max-width: 768px)').matches;
}

function resetPaginationMobileScale(showcase, scrollContainer) {
  showcase?.style.removeProperty('--pagination-mobile-scale');
  showcase?.style.removeProperty('--pagination-mobile-height');
  showcase?.style.removeProperty('--pagination-mobile-offset');
  scrollContainer?.classList.remove('is-scaled');
}

function syncPaginationLayout(navElement) {
  if (!navElement) return false;
  const scrollContainer = navElement.closest('.pagination-rail');
  if (!scrollContainer) return false;

  const showcase = scrollContainer.closest('.pagination-showcase');
  const paginationList = navElement.querySelector('.pagination-premium');

  if (showcase && paginationList) {
    resetPaginationMobileScale(showcase, scrollContainer);

    const isMobileViewport = isMobilePaginationViewport();
    if (isMobileViewport) {
      const railStyles = window.getComputedStyle(scrollContainer);
      const railPaddingX = parseFloat(railStyles.paddingLeft || '0') + parseFloat(railStyles.paddingRight || '0');
      const railPaddingY = parseFloat(railStyles.paddingTop || '0') + parseFloat(railStyles.paddingBottom || '0');
      const mobileVisualInset = 16;
      const availableWidth = Math.max(0, scrollContainer.clientWidth - railPaddingX - mobileVisualInset);
      const naturalWidth = Math.ceil(paginationList.scrollWidth);
      const naturalHeight = Math.ceil(paginationList.offsetHeight);

      if (availableWidth > 0 && naturalWidth > availableWidth + 1) {
        const scale = Math.min(1, availableWidth / naturalWidth);
        const scaledWidth = naturalWidth * scale;
        showcase.style.setProperty('--pagination-mobile-scale', scale.toFixed(4));
        showcase.style.setProperty('--pagination-mobile-offset', `${Math.max(0, Math.floor((availableWidth - scaledWidth) / 2))}px`);
        showcase.style.setProperty('--pagination-mobile-height', `${Math.ceil(naturalHeight * scale + railPaddingY + 6)}px`);
        scrollContainer.classList.add('is-scaled');
        scrollContainer.classList.remove('is-scrollable');
        scrollContainer.scrollLeft = 0;
        return false;
      }
    }
  }

  const needsScroll = Math.ceil(scrollContainer.scrollWidth - scrollContainer.clientWidth) > 4;
  scrollContainer.classList.toggle('is-scrollable', needsScroll);
  return needsScroll;
}

function syncPaginationViewport(navElement, currentPage) {
  if (!navElement) return;
  const scrollContainer = navElement.closest('.pagination-rail');
  const activeItem = navElement.querySelector(`.page-item-number[data-page="${currentPage}"]`);
  if (!scrollContainer || !activeItem) return;

  const needsScroll = syncPaginationLayout(navElement);
  if (!needsScroll) {
    scrollContainer.scrollLeft = 0;
    return;
  }

  const scrollToActive = () => {
    const itemRect = activeItem.getBoundingClientRect();
    const containerRect = scrollContainer.getBoundingClientRect();
    const maxScroll = scrollContainer.scrollWidth - scrollContainer.clientWidth;

    if (maxScroll <= 0) {
      return;
    }

    let targetLeft = scrollContainer.scrollLeft + (itemRect.left - containerRect.left) - ((containerRect.width - itemRect.width) / 2);
    targetLeft = Math.max(0, Math.min(targetLeft, maxScroll));

    if (currentPage <= 2) {
      targetLeft = 0;
    }

    const totalPages = parseNumber(navElement.dataset.totalPages || currentPage, currentPage);
    if (currentPage >= totalPages - 1) {
      targetLeft = maxScroll;
    }

    scrollContainer.scrollTo({
      left: targetLeft,
      behavior: prefersReducedMotion ? 'auto' : 'smooth',
    });
  };

  requestAnimationFrame(scrollToActive);
}

function updatePaginationNav(navElement, currentPage) {
  if (!navElement) return;
  const totalPages = parseNumber(navElement.dataset.totalPages || currentPage, currentPage);
  const basePath = navElement.dataset.basePath || window.location.pathname || '/';

  navElement.dataset.currentPage = String(currentPage);

  navElement.querySelectorAll('.page-item-number').forEach((item) => {
    const targetPage = parseNumber(item.dataset.page, null);
    if (!targetPage) return;
    const link = item.querySelector('.page-link');
    item.classList.toggle('active', targetPage === currentPage);
    if (link) {
      if (targetPage === currentPage) {
        link.setAttribute('aria-current', 'page');
      } else {
        link.removeAttribute('aria-current');
      }
    }
  });

  const prevLink = navElement.querySelector('[data-page-nav="prev"]');
  const nextLink = navElement.querySelector('[data-page-nav="next"]');
  const prevItem = prevLink?.closest('.page-item');
  const nextItem = nextLink?.closest('.page-item');

  if (prevItem && prevLink) {
    const disabled = currentPage <= 1;
    prevItem.classList.toggle('disabled', disabled);
    prevLink.setAttribute('href', disabled ? '#' : buildPageHref(basePath, currentPage - 1));
    prevLink.setAttribute('aria-disabled', String(disabled));
  }

  if (nextItem && nextLink) {
    const disabled = currentPage >= totalPages;
    nextItem.classList.toggle('disabled', disabled);
    nextLink.setAttribute('href', disabled ? '#' : buildPageHref(basePath, currentPage + 1));
    nextLink.setAttribute('aria-disabled', String(disabled));
  }

  syncPaginationViewport(navElement, currentPage);
}

function animateNewCards(container) {
  const allCards = container.querySelectorAll('.product-card-wrap');
  const newCards = Array.from(allCards).slice(-8);
  newCards.forEach((card, index) => {
    if (prefersReducedMotion) {
      card.style.opacity = '';
      card.style.transform = '';
      card.style.filter = '';
      card.style.transition = '';
      return;
    }
    card.classList.remove('reveal-stagger', 'stagger-item', 'reveal-fast', 'reveal');
    const childCards = card.querySelectorAll('.card');
    childCards.forEach(childCard => childCard.classList.remove('reveal-stagger', 'stagger-item', 'reveal-fast', 'reveal'));
    card.style.opacity = '0';
    card.style.transform = 'translateY(24px) scale(0.94)';
    card.style.filter = 'blur(10px)';
    card.style.transition = 'none';
    setTimeout(() => {
      card.style.transition = 'all 560ms cubic-bezier(0.2, 0.8, 0.2, 1)';
      setTimeout(() => {
        card.style.opacity = '1';
        card.style.transform = 'translateY(0) scale(1)';
        card.style.filter = 'blur(0)';
      }, 50);
    }, index * 120);
  });
}

function revealColorDots(container, options = {}) {
  if (!container) return;
  const shouldAnimate = options.animate !== false;
  const cards = container.querySelectorAll('.product-card-wrap');
  cards.forEach((card) => {
    if (card.dataset.colorDotsReady === '1') return;
    const dotsWrap = card.querySelector('.product-card-dots');
    if (!dotsWrap) return;
    dotsWrap.classList.add('visible');
    const dots = dotsWrap.querySelectorAll('.color-dot');
    dots.forEach((dot, idx) => {
      if (dot.classList.contains('visible')) return;
      if (prefersReducedMotion || !shouldAnimate) {
        dot.classList.add('visible');
      } else {
        setTimeout(() => dot.classList.add('visible'), idx * 60);
      }
    });
    card.dataset.colorDotsReady = '1';
  });
}

function delay(ms) {
  return new Promise(resolve => setTimeout(resolve, ms));
}

function nextAnimationFrame() {
  return new Promise(resolve => requestAnimationFrame(resolve));
}

function buildAbsolutePageUrl(basePath, page) {
  return new URL(buildPageHref(basePath, page), window.location.origin).href;
}

function setHeadLink(rel, href) {
  const selector = `link[rel="${rel}"]`;
  let link = document.head.querySelector(selector);
  if (!href) {
    link?.remove();
    return;
  }
  if (!link) {
    link = document.createElement('link');
    link.setAttribute('rel', rel);
    document.head.appendChild(link);
  }
  link.setAttribute('href', href);
}

function syncPaginationHead(basePath, currentPage, totalPages) {
  const canonical = document.head.querySelector('link[rel="canonical"]');
  if (canonical) {
    canonical.setAttribute('href', buildAbsolutePageUrl(basePath, currentPage));
  }
  setHeadLink('prev', currentPage > 1 ? buildAbsolutePageUrl(basePath, currentPage - 1) : null);
  setHeadLink('next', currentPage < totalPages ? buildAbsolutePageUrl(basePath, currentPage + 1) : null);
}

function replaceBrowserUrl(basePath, page, replace = false) {
  if (!window.history || typeof window.history.pushState !== 'function') return;
  const nextUrl = buildPageHref(basePath, page);
  const currentUrl = `${window.location.pathname}${window.location.search}`;
  const state = { homepagePage: page };
  if (nextUrl === currentUrl) {
    window.history.replaceState(state, '', nextUrl);
    return;
  }
  if (replace) {
    window.history.replaceState(state, '', nextUrl);
  } else {
    window.history.pushState(state, '', nextUrl);
  }
}

function scrollToProductsStart(section) {
  if (!section) return;
  const header = document.querySelector('.navbar, header[role="banner"], .site-header');
  const headerHeight = header ? Math.ceil(header.getBoundingClientRect().height) : 0;
  const targetTop = Math.max(0, window.scrollY + section.getBoundingClientRect().top - headerHeight - 16);
  window.scrollTo({
    top: targetTop,
    behavior: prefersReducedMotion ? 'auto' : 'smooth',
  });
}

async function renderProductsHtml(container, html, mode) {
  if (mode !== 'replace') {
    container.insertAdjacentHTML('beforeend', html);
    return;
  }
  if (prefersReducedMotion) {
    container.innerHTML = html;
    revealColorDots(container, { animate: false });
    return;
  }
  container.classList.add('is-page-exiting');
  await delay(110);
  container.innerHTML = html;
  revealColorDots(container, { animate: false });
  container.classList.remove('is-page-exiting');
  container.classList.add('is-page-entering');
  // Give the browser a paint boundary for the enter state without forcing a
  // synchronous layout read. The second frame reliably starts the transition.
  await nextAnimationFrame();
  await nextAnimationFrame();
  container.classList.add('is-page-entered');
  await delay(430);
  container.classList.remove('is-page-entering', 'is-page-entered');
}

export function initHomepagePagination() {
  const boot = () => {
    const productsContainer = document.getElementById('products-container');
    const loadMoreBtn = document.getElementById('load-more-btn');
    const loadMoreContainer = document.getElementById('load-more-container');
    const getPaginationShell = () => document.getElementById('home-pagination-shell');
    const getPaginationNav = () => document.getElementById('home-pagination-nav');

    if (!productsContainer) {
      return;
    }

    const getTotalPages = () => {
      const paginationNav = getPaginationNav();
      const dataValue = loadMoreContainer?.dataset.totalPages || paginationNav?.dataset.totalPages || '1';
      return parseNumber(dataValue, 1);
    };

    const getCurrentPage = () => {
      const paginationNav = getPaginationNav();
      return parseNumber(loadMoreContainer?.dataset.currentPage || paginationNav?.dataset.currentPage || '1', 1);
    };

    const setTotalPages = (value) => {
      const paginationNav = getPaginationNav();
      if (loadMoreContainer) {
        loadMoreContainer.dataset.totalPages = String(value);
      }
      if (paginationNav) {
        paginationNav.dataset.totalPages = String(value);
      }
    };

    const setCurrentPage = (value) => {
      const paginationNav = getPaginationNav();
      if (loadMoreContainer) {
        loadMoreContainer.dataset.currentPage = String(value);
      }
      if (paginationNav) {
        paginationNav.dataset.currentPage = String(value);
      }
      updatePaginationNav(paginationNav, value);
    };

    const updateLoadMoreState = (currentPage, totalPages) => {
      const hasMore = currentPage < totalPages;
      if (!loadMoreBtn || !loadMoreContainer) {
        return hasMore;
      }
      if (hasMore) {
        loadMoreBtn.dataset.page = String(currentPage + 1);
        loadMoreBtn.disabled = false;
        loadMoreContainer.style.display = '';
      } else {
        loadMoreBtn.dataset.page = '';
        loadMoreContainer.style.display = 'none';
      }
      return hasMore;
    };

    const syncUI = () => {
      const totalPages = getTotalPages();
      const currentPage = getCurrentPage();
      const paginationNav = getPaginationNav();
      if (paginationNav) {
        paginationNav.dataset.totalPages = String(totalPages);
        paginationNav.dataset.currentPage = String(currentPage);
      }
      updatePaginationNav(paginationNav, currentPage);
      updateLoadMoreState(currentPage, totalPages);
    };

    let pageRequestController = null;
    let pageRequestToken = 0;

    const setPagingBusy = (busy, mode = 'append') => {
      const btnText = loadMoreBtn?.querySelector('.btn-text');
      const btnSpinner = loadMoreBtn?.querySelector('.btn-spinner');
      const paginationNav = getPaginationNav();
      productsContainer.setAttribute('aria-busy', busy ? 'true' : 'false');
      productsContainer.classList.toggle('is-page-loading', busy);
      paginationNav?.classList.toggle('is-page-fetching', busy);
      paginationNav?.setAttribute('aria-busy', busy ? 'true' : 'false');
      if (loadMoreBtn && btnText && btnSpinner) {
        btnText.classList.add('d-none');
        btnSpinner.classList.remove('d-none');
      }
      if (loadMoreBtn) {
        loadMoreBtn.disabled = busy;
      }
    };

    const finishPagingBusy = () => {
      const btnText = loadMoreBtn?.querySelector('.btn-text');
      const btnSpinner = loadMoreBtn?.querySelector('.btn-spinner');
      const paginationNav = getPaginationNav();
      productsContainer.setAttribute('aria-busy', 'false');
      productsContainer.classList.remove('is-page-loading', 'is-page-exiting', 'is-page-entering', 'is-page-entered');
      paginationNav?.classList.remove('is-page-fetching');
      paginationNav?.setAttribute('aria-busy', 'false');
      if (loadMoreBtn && btnText && btnSpinner) {
        btnText.classList.remove('d-none');
        btnSpinner.classList.add('d-none');
      }
    };

    const runAfterProductsRender = (mode = 'append') => {
      if (mode === 'append') {
        animateNewCards(productsContainer);
      }
      revealColorDots(productsContainer, { animate: mode === 'append' });
    };

    const loadPage = (pageNumber, options = {}) => {
      const mode = options.mode === 'replace' ? 'replace' : 'append';
      const targetPage = parseNumber(pageNumber, getCurrentPage());
      const basePath = getPaginationNav()?.dataset.basePath || window.location.pathname || '/';
      const fallbackUrl = options.fallbackUrl || buildPageHref(basePath, targetPage);
      const shouldUpdateHistory = options.updateHistory === true;
      const shouldScroll = options.scrollToProducts === true;

      if (mode === 'append' && (!loadMoreBtn || !loadMoreContainer || loadMoreBtn.disabled)) {
        return Promise.resolve();
      }

      if (pageRequestController) {
        pageRequestController.abort();
      }
      pageRequestController = typeof AbortController !== 'undefined' ? new AbortController() : null;
      const requestToken = pageRequestToken + 1;
      pageRequestToken = requestToken;
      setPagingBusy(true, mode);

      return fetch(`${getLanguagePrefix()}/load-more-products/?page=${encodeURIComponent(targetPage)}`, {
        credentials: 'same-origin',
        headers: {
          Accept: 'application/json',
          'X-Requested-With': 'XMLHttpRequest',
        },
        signal: pageRequestController?.signal,
      })
        .then(response => {
          if (!response.ok) {
            throw new Error(`Homepage page fetch failed: ${response.status}`);
          }
          return response.json();
        })
        .then(async data => {
          if (requestToken !== pageRequestToken) return;
          if (data && data.html && data.html.trim() !== '') {
            await renderProductsHtml(productsContainer, data.html, mode);
            const paginationShell = getPaginationShell();
            if (paginationShell && typeof data.pagination_html === 'string' && data.pagination_html.trim() !== '') {
              paginationShell.innerHTML = data.pagination_html;
            }
            const totalPages = parseNumber(data.total_pages, getTotalPages());
            const currentPage = parseNumber(data.current_page, targetPage);
            setTotalPages(totalPages);
            setCurrentPage(currentPage);
            const hasMore = updateLoadMoreState(currentPage, totalPages);
            const paginationNav = getPaginationNav();
            if (!hasMore && paginationNav) {
              updatePaginationNav(paginationNav, currentPage);
            }
            syncPaginationHead(basePath, currentPage, totalPages);
            if (shouldUpdateHistory) {
              replaceBrowserUrl(basePath, currentPage, false);
            }
            if (shouldScroll) {
              scrollToProductsStart(document.getElementById('new-products-section'));
            }
            runAfterProductsRender(mode);
          }
        })
        .catch(error => {
          if (error && error.name === 'AbortError') return;
          if (mode === 'replace') {
            window.location.assign(fallbackUrl);
          }
        })
        .finally(() => {
          if (requestToken === pageRequestToken) {
            finishPagingBusy();
            updateLoadMoreState(getCurrentPage(), getTotalPages());
            pageRequestController = null;
          }
        });
    };

    if (loadMoreBtn) {
      loadMoreBtn.addEventListener('click', () => {
        const nextPage = parseNumber(loadMoreBtn.dataset.page, getCurrentPage() + 1);
        loadPage(nextPage, { mode: 'append' });
      });
    }

    const paginationShell = getPaginationShell();
    if (paginationShell) {
      paginationShell.addEventListener('click', event => {
        const link = event.target?.closest?.('a.page-link[href]');
        if (!link) return;
        const item = link.closest('.page-item');
        if (item?.classList.contains('disabled') || item?.classList.contains('active')) return;

        let targetUrl;
        try {
          targetUrl = new URL(link.getAttribute('href'), window.location.href);
        } catch (_) {
          return;
        }
        if (targetUrl.origin !== window.location.origin || targetUrl.pathname !== window.location.pathname) {
          return;
        }

        const targetPage = parseNumber(targetUrl.searchParams.get('page'), 1);
        if (targetPage === getCurrentPage()) {
          event.preventDefault();
          return;
        }

        event.preventDefault();
        loadPage(targetPage, {
          mode: 'replace',
          updateHistory: true,
          scrollToProducts: true,
          fallbackUrl: targetUrl.href,
        });
      });
    }

    window.addEventListener('popstate', () => {
      const targetPage = parseNumber(new URL(window.location.href).searchParams.get('page'), 1);
      loadPage(targetPage, {
        mode: 'replace',
        updateHistory: false,
        scrollToProducts: false,
        fallbackUrl: window.location.href,
      });
    });

    let resizeFrame = 0;
    window.addEventListener('resize', () => {
      window.cancelAnimationFrame(resizeFrame);
      resizeFrame = window.requestAnimationFrame(() => {
        updatePaginationNav(getPaginationNav(), getCurrentPage());
      });
    }, { passive: true });

    syncUI();
    revealColorDots(productsContainer);
  };

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot, { once: true });
  } else {
    boot();
  }
}

// =====================================================================
// Phase 2.1 — Extracted from main.js: home-only UI блоки.
// Импортируется динамически только на главной (см. main.js:lazy-load).
// =====================================================================

/**
 * "Рекомендовано" collapsible toggle — локальный state в localStorage.
 */
export function initFeaturedToggle() {
  const featuredToggle = document.getElementById('featuredToggle') || document.getElementById('featured-toggle');
  const featuredContent = document.getElementById('featured-content');
  if (!featuredToggle || !featuredContent) return;

  const getState = () => {
    const collapsedKey = localStorage.getItem('featuredCollapsed');
    const hiddenKey = localStorage.getItem('featured-hidden');
    if (collapsedKey !== null) return collapsedKey === 'true';
    if (hiddenKey !== null) return hiddenKey === 'true';
    return false;
  };
  const setState = (collapsed) => {
    localStorage.setItem('featuredCollapsed', collapsed ? 'true' : 'false');
    localStorage.setItem('featured-hidden', collapsed ? 'true' : 'false');
  };
  const applyState = (collapsed) => {
    featuredContent.style.display = collapsed ? 'none' : 'block';
    featuredContent.classList.toggle('collapsed', collapsed);
    featuredToggle.classList.toggle('collapsed', collapsed);
    const hint = featuredToggle.querySelector('.toggle-hint-text') || featuredToggle.querySelector('.toggle-text');
    if (hint) hint.textContent = collapsed ? 'Показати' : 'Сховати';
    const icon = featuredToggle.querySelector('.toggle-icon svg');
    if (icon) icon.style.transform = collapsed ? 'rotate(180deg)' : 'rotate(0deg)';
    featuredToggle.setAttribute('aria-expanded', collapsed ? 'false' : 'true');
  };

  applyState(getState());
  featuredToggle.addEventListener('click', function () {
    featuredToggle.classList.add('pulsing');
    setTimeout(() => featuredToggle.classList.remove('pulsing'), 600);
    const collapsedNext = featuredContent.style.display !== 'none';
    applyState(collapsedNext);
    setState(collapsedNext);
  });
}

/**
 * Categories collapsible — локальный state в localStorage.
 */
export function initCategoriesToggle() {
  const categoriesToggle = document.getElementById('categoriesToggle');
  const categoriesContainer = document.getElementById('categoriesContainer');
  const toggleText = categoriesToggle?.querySelector('.toggle-text');
  if (!categoriesToggle || !categoriesContainer || !toggleText) return;

  const isCollapsedInit = localStorage.getItem('categories-collapsed') === 'true';
  if (isCollapsedInit) {
    categoriesContainer.classList.add('collapsed');
    categoriesToggle.classList.add('collapsed');
    toggleText.textContent = 'Розгорнути';
  }

  categoriesToggle.addEventListener('click', function () {
    const isCollapsed = categoriesContainer.classList.contains('collapsed');
    categoriesToggle.classList.add('pulsing');
    setTimeout(() => categoriesToggle.classList.remove('pulsing'), 600);

    if (isCollapsed) {
      categoriesContainer.classList.remove('collapsed');
      categoriesToggle.classList.remove('collapsed');
      toggleText.textContent = 'Згорнути';
      localStorage.setItem('categories-collapsed', 'false');
      categoriesContainer.style.display = 'block';
      requestAnimationFrame(() => {
        requestAnimationFrame(() => {
          categoriesContainer.classList.add('expanding');
        });
      });
      setTimeout(() => {
        categoriesContainer.classList.remove('expanding');
      }, 800);
    } else {
      categoriesContainer.classList.add('collapsing');
      categoriesToggle.classList.add('collapsed');
      toggleText.textContent = 'Розгорнути';
      localStorage.setItem('categories-collapsed', 'true');
      setTimeout(() => {
        categoriesContainer.classList.remove('collapsing');
        categoriesContainer.classList.add('collapsed');
        categoriesContainer.style.display = 'none';
      }, 800);
    }
  });
}

/**
 * Unified entrypoint: вызывается lazy-loader'ом в main.js на home page.
 */
export function initHomepage() {
  initFeaturedToggle();
  initCategoriesToggle();
  initHomepagePagination();
}
