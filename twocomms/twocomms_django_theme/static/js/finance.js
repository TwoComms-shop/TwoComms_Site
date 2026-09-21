/* TwoComms Finance — shell interactions (Block 1 scaffold).
   Бургер-меню, мобільний сайдбар і заглушки швидких дій.
   Модалки доходу/витрати/переказу під'єднуються у Блоці 3. */
(function () {
    'use strict';

    document.addEventListener('DOMContentLoaded', function () {
        var body = document.body;
        var burger = document.getElementById('fin-burger');
        var sidebar = document.getElementById('fin-sidebar');
        var backdrop = document.getElementById('fin-sidebar-backdrop');
        var settingsPanel = document.getElementById('fin-settings-panel');
        var settingsContent = settingsPanel ? settingsPanel.querySelector('.fin-settings-panel__content') : null;
        // ── Жести шухляд (edge-swipe drawer) ────────────────────────────
        // Відкриття: палець стартує в крайовій зоні екрана (ліва → бургер,
        // права → налаштування) і тягне всередину. Закриття: коли шухляда
        // відкрита, тягнемо у зворотний бік. Пороги навмисно прощаючі — щоб
        // жест надійно спрацьовував навіть при діагональному русі великим
        // пальцем (типова проблема «не завжди відкривається»).
        var EDGE_ZONE = 40;            // px від краю екрана — звідки можна починати відкривати
        var ACTIVATE_PX = 6;           // горизонтальний зсув, щоб «схопити» жест
        var AXIS_RATIO = 0.7;          // горизонталь має бути ≥0.7× вертикалі (прощає діагональ)
        var VERTICAL_CANCEL = 30;      // більший вертикальний дрейф дозволено до скасування
        var OPEN_COMMIT_RATIO = 0.22;  // відкрити, якщо протягнули ≥22% ширини
        var CLOSE_COMMIT_RATIO = 0.28; // закрити, якщо відвели назад ≥28% ширини
        var FLING_VELOCITY = 0.25;     // px/ms — «кидок» завершує дію незалежно від відстані
        var MIN_OPEN_PX = 28;
        var MIN_CLOSE_PX = 40;
        var SETTLE_MS = 320;
        var drawerDrag = null;

        function hapticTick() {
            // М'який тактильний відгук при завершенні жесту (де підтримується).
            try { if (navigator.vibrate) navigator.vibrate(8); } catch (e) {}
        }

        function isMobileDrawerViewport() {
            return window.matchMedia ? window.matchMedia('(max-width: 900px)').matches : window.innerWidth <= 900;
        }

        function setSidebarOpen(open) {
            if (open && !isMobileDrawerViewport()) return;
            if (open && window.FinanceSettings && typeof window.FinanceSettings.close === 'function') {
                window.FinanceSettings.close();
            }
            body.classList.toggle('fin-sidebar-open', open);
            body.classList.toggle('fin-menu-open', open);
            body.classList.toggle('fin-any-drawer-open', open || body.classList.contains('fin-settings-open'));
            if (burger) burger.setAttribute('aria-expanded', open ? 'true' : 'false');
        }

        function openSettingsDrawer() {
            setSidebarOpen(false);
            if (window.FinanceSettings && typeof window.FinanceSettings.open === 'function') {
                window.FinanceSettings.open();
            }
        }

        function closeSidebarDrawer() {
            setSidebarOpen(false);
        }

        function closeSettingsDrawer() {
            if (window.FinanceSettings && typeof window.FinanceSettings.close === 'function') {
                window.FinanceSettings.close();
            }
        }

        function viewportWidth() {
            return window.visualViewport ? window.visualViewport.width : window.innerWidth;
        }

        function isControlGestureTarget(target) {
            return !!(target && target.closest(
                'a, button, input, select, textarea, label, [role="button"], [contenteditable="true"]'
            ));
        }

        function clamp(value, min, max) {
            return Math.min(Math.max(value, min), max);
        }

        function drawerWidth(side) {
            var width = 0;
            if (side === 'left' && sidebar) {
                width = sidebar.getBoundingClientRect().width;
            } else if (side === 'right' && settingsContent) {
                width = settingsContent.getBoundingClientRect().width;
            }
            return width || Math.min(420, viewportWidth() * 0.92);
        }

        function setDrawerProgress(side, progress, width) {
            var safeWidth = width || drawerWidth(side);
            var nextProgress = clamp(progress, 0, safeWidth);
            var ratio = safeWidth ? clamp(nextProgress / safeWidth, 0, 1) : 0;
            if (side === 'left') {
                body.style.setProperty('--fin-left-drawer-progress', nextProgress + 'px');
                body.style.setProperty('--fin-left-drawer-ratio', ratio.toFixed(3));
            } else {
                body.style.setProperty('--fin-right-drawer-progress', nextProgress + 'px');
                body.style.setProperty('--fin-right-drawer-ratio', ratio.toFixed(3));
            }
            return nextProgress;
        }

        function clearDrawerProgress(side) {
            if (!side || side === 'left') {
                body.style.removeProperty('--fin-left-drawer-progress');
                body.style.removeProperty('--fin-left-drawer-ratio');
                body.classList.remove('fin-sidebar-peeking');
            }
            if (!side || side === 'right') {
                body.style.removeProperty('--fin-right-drawer-progress');
                body.style.removeProperty('--fin-right-drawer-ratio');
                body.classList.remove('fin-settings-peeking');
            }
        }

        function hideSettingsPreview() {
            if (!settingsPanel || body.classList.contains('fin-settings-open')) return;
            window.setTimeout(function () {
                if (
                    settingsPanel &&
                    !body.classList.contains('fin-settings-open') &&
                    !body.classList.contains('fin-settings-peeking')
                ) {
                    settingsPanel.hidden = true;
                }
            }, SETTLE_MS);
        }

        function currentDrawerSide() {
            if (body.classList.contains('fin-sidebar-open')) return 'left';
            if (body.classList.contains('fin-settings-open')) return 'right';
            return null;
        }

        // Готуємо DOM до «підглядання» шухляди під час перетягування.
        function prepareDrawerPreview(side, mode) {
            if (side === 'right' && settingsPanel) {
                settingsPanel.hidden = false;
                if (mode === 'open') settingsPanel.classList.remove('is-open');
            }
            if (side === 'left' && window.FinanceSettings && typeof window.FinanceSettings.close === 'function') {
                window.FinanceSettings.close();
            }
            body.classList.add('fin-drawer-dragging');
            body.classList.toggle('fin-sidebar-peeking', side === 'left');
            body.classList.toggle('fin-settings-peeking', side === 'right');
            return drawerWidth(side);
        }

        function releasePointerCapture() {
            if (
                drawerDrag &&
                drawerDrag.captureTarget &&
                typeof drawerDrag.captureTarget.releasePointerCapture === 'function'
            ) {
                try {
                    drawerDrag.captureTarget.releasePointerCapture(drawerDrag.pointerId);
                } catch (err) {}
            }
        }

        function resetDrawerDrag() {
            var side = drawerDrag ? drawerDrag.side : null;
            releasePointerCapture();
            drawerDrag = null;
            body.classList.remove('fin-drawer-dragging');
            clearDrawerProgress(side);
            if (side === 'right') hideSettingsPreview();
        }

        function settleDrawerRelease(side, progress, width, afterSettle) {
            setDrawerProgress(side, progress, width);
            // Знімаємо клас перетягування → вмикається CSS-transition і
            // шухляда плавно доїжджає до кінцевого стану (open/close).
            body.classList.remove('fin-drawer-dragging');
            if (typeof afterSettle === 'function') afterSettle();
            clearDrawerProgress(side);
            if (side === 'right') hideSettingsPreview();
        }

        // Активуємо перетягування: фіксуємо ширину, вмикаємо peek-стан.
        function activateDrag(e) {
            drawerDrag.active = true;
            drawerDrag.width = prepareDrawerPreview(drawerDrag.side, drawerDrag.mode);
            if (drawerDrag.mode === 'close') {
                setDrawerProgress(drawerDrag.side, drawerDrag.width, drawerDrag.width);
            } else {
                setDrawerProgress(drawerDrag.side, 0, drawerDrag.width);
            }
            if (e.target && typeof e.target.setPointerCapture === 'function') {
                try {
                    e.target.setPointerCapture(e.pointerId);
                    drawerDrag.captureTarget = e.target;
                } catch (err) {}
            }
        }

        // Поточний прогрес (0..width) з координат вказівника.
        function dragProgressFromEvent(e) {
            if (!drawerDrag || !drawerDrag.side) return 0;
            var deltaX = e.clientX - drawerDrag.startX;
            if (drawerDrag.mode === 'close') {
                var closeDistance = drawerDrag.side === 'left' ? -deltaX : deltaX;
                return drawerDrag.width - clamp(closeDistance, 0, drawerDrag.width);
            }
            if (drawerDrag.side === 'left') return clamp(deltaX, 0, drawerDrag.width);
            return clamp(-deltaX, 0, drawerDrag.width);
        }

        function onPointerDown(e) {
            if (
                !window.PointerEvent ||
                !isMobileDrawerViewport() ||
                (e.pointerType === 'mouse') ||
                e.isPrimary === false
            ) {
                // Жести лише для тач-екранів на мобільному вьюпорті.
                return;
            }

            var openSide = currentDrawerSide();

            // ── Шухляда відкрита → готуємо жест закриття ──
            if (openSide) {
                // Тап по контролу всередині шухляди не повинен її тягнути.
                if (isControlGestureTarget(e.target)) return;
                drawerDrag = {
                    mode: 'close',
                    pointerId: e.pointerId,
                    startX: e.clientX,
                    startY: e.clientY,
                    side: openSide,
                    active: false,
                    width: drawerWidth(openSide),
                    lastX: e.clientX,
                    lastTime: window.performance ? performance.now() : Date.now(),
                    velocityX: 0,
                    captureTarget: null
                };
                return;
            }

            // Якщо відкрита модалка — не перехоплюємо.
            if (body.classList.contains('fin-any-drawer-open')) return;

            // ── Шухляда закрита → жест відкриття лише з крайової зони ──
            var width = viewportWidth();
            var x = e.clientX;
            var side = null;
            if (x <= EDGE_ZONE) side = 'left';
            else if (x >= width - EDGE_ZONE) side = 'right';
            if (!side) return;

            drawerDrag = {
                mode: 'open',
                pointerId: e.pointerId,
                startX: e.clientX,
                startY: e.clientY,
                side: side,
                active: false,
                width: drawerWidth(side),
                lastX: e.clientX,
                lastTime: window.performance ? performance.now() : Date.now(),
                velocityX: 0,
                captureTarget: null
            };
        }

        function onPointerMove(e) {
            if (!drawerDrag || e.pointerId !== drawerDrag.pointerId) return;

            var deltaX = e.clientX - drawerDrag.startX;
            var deltaY = e.clientY - drawerDrag.startY;
            var absX = Math.abs(deltaX);
            var absY = Math.abs(deltaY);
            var now = window.performance ? performance.now() : Date.now();
            var elapsed = Math.max(now - drawerDrag.lastTime, 1);
            // Згладжена миттєва швидкість (для розпізнавання «кидка»).
            drawerDrag.velocityX = (e.clientX - drawerDrag.lastX) / elapsed;
            drawerDrag.lastX = e.clientX;
            drawerDrag.lastTime = now;

            if (!drawerDrag.active) {
                // Вертикальний намір раніше за горизонтальний → це скрол, відпускаємо.
                if (absY >= VERTICAL_CANCEL && absY > absX * 1.3) {
                    resetDrawerDrag();
                    return;
                }
                // Рух у «неправильний» бік скасовує жест (з невеликим допуском).
                var wrongWay = drawerDrag.mode === 'open'
                    ? (drawerDrag.side === 'left' ? deltaX < -8 : deltaX > 8)
                    : (drawerDrag.side === 'left' ? deltaX > 8 : deltaX < -8);
                if (wrongWay) {
                    resetDrawerDrag();
                    return;
                }
                // Достатній горизонтальний зсув із домінуванням по X → активуємо.
                if (absX >= ACTIVATE_PX && absX >= absY * AXIS_RATIO) {
                    activateDrag(e);
                }
            }

            if (!drawerDrag || !drawerDrag.active) return;
            if (e.cancelable) e.preventDefault();
            setDrawerProgress(drawerDrag.side, dragProgressFromEvent(e), drawerDrag.width);
        }

        function onPointerEnd(e) {
            if (!drawerDrag || e.pointerId !== drawerDrag.pointerId) return;
            var side = drawerDrag.side;
            var mode = drawerDrag.mode || 'open';
            var active = drawerDrag.active;
            var progress = active ? dragProgressFromEvent(e) : (mode === 'close' ? drawerDrag.width : 0);
            var width = drawerDrag.width || drawerWidth(side);
            var velocity = drawerDrag.velocityX || 0;
            // Швидкість у бік відкриття/закриття (з урахуванням боку).
            var openVel = side === 'left' ? velocity : -velocity;   // >0 = тягне відкривати
            var flungOpen = openVel >= FLING_VELOCITY;
            var flungClosed = openVel <= -FLING_VELOCITY;
            var hidden = width - progress;

            var shouldOpen = mode === 'open' && active && (
                progress >= width * OPEN_COMMIT_RATIO ||
                (progress >= MIN_OPEN_PX && flungOpen)
            );
            var shouldClose = mode === 'close' && active && (
                hidden >= width * CLOSE_COMMIT_RATIO ||
                (hidden >= MIN_CLOSE_PX && flungClosed)
            );

            releasePointerCapture();
            drawerDrag = null;

            if (!active) {
                // Жест не активувався (звичайний тап) — нічого не робимо.
                clearDrawerProgress(side);
                body.classList.remove('fin-drawer-dragging');
                if (side === 'right') hideSettingsPreview();
                return;
            }

            if (shouldOpen && side === 'left') {
                hapticTick();
                settleDrawerRelease(side, width, width, function () { setSidebarOpen(true); });
            } else if (shouldOpen && side === 'right') {
                hapticTick();
                settleDrawerRelease(side, width, width, openSettingsDrawer);
            } else if (shouldClose && side === 'left') {
                hapticTick();
                settleDrawerRelease(side, 0, width, closeSidebarDrawer);
            } else if (shouldClose && side === 'right') {
                hapticTick();
                settleDrawerRelease(side, 0, width, closeSettingsDrawer);
            } else if (mode === 'close') {
                // Не дотягнули до закриття → лишаємо відкритою.
                settleDrawerRelease(side, width, width);
            } else {
                // Не дотягнули до відкриття → ховаємо назад.
                settleDrawerRelease(side, 0, width);
            }
        }

        // ── Постійне desktop-згортання бічної панелі ───────────────────────
        // На мобільних залишається drawer-бургер вище; цей контрол активний
        // тільки на широкому екрані й не перекриває вміст сторінки.
        var sidebarToggle = document.getElementById('fin-sidebar-toggle');
        var SIDEBAR_COLLAPSED_KEY = 'twocomms_finance_sidebar_collapsed';
        var desktopSidebarQuery = window.matchMedia ? window.matchMedia('(min-width: 901px)') : null;

        function isDesktopSidebarViewport() {
            return desktopSidebarQuery ? desktopSidebarQuery.matches : window.innerWidth > 900;
        }

        function readSidebarPreference() {
            try { return window.localStorage.getItem(SIDEBAR_COLLAPSED_KEY) === '1'; } catch (err) { return false; }
        }

        function writeSidebarPreference(collapsed) {
            try { window.localStorage.setItem(SIDEBAR_COLLAPSED_KEY, collapsed ? '1' : '0'); } catch (err) {}
        }

        function setDesktopSidebarCollapsed(collapsed, options) {
            options = options || {};
            if (!sidebarToggle || !sidebar) return;
            if (!isDesktopSidebarViewport()) {
                body.classList.remove('fin-sidebar-collapsed');
                sidebarToggle.setAttribute('aria-expanded', 'true');
                sidebarToggle.setAttribute('aria-label', 'Згорнути бічну панель');
                sidebarToggle.setAttribute('title', 'Згорнути бічну панель');
                sidebar.removeAttribute('aria-hidden');
                sidebar.removeAttribute('inert');
                return;
            }
            var next = !!collapsed;
            body.classList.toggle('fin-sidebar-collapsed', next);
            sidebarToggle.setAttribute('aria-expanded', next ? 'false' : 'true');
            sidebarToggle.setAttribute('aria-label', next ? 'Розгорнути бічну панель' : 'Згорнути бічну панель');
            sidebarToggle.setAttribute('title', next ? 'Розгорнути бічну панель' : 'Згорнути бічну панель');
            if (next) {
                sidebar.setAttribute('aria-hidden', 'true');
                sidebar.setAttribute('inert', '');
            } else {
                sidebar.removeAttribute('aria-hidden');
                sidebar.removeAttribute('inert');
            }
            if (!options.skipPersist) writeSidebarPreference(next);
        }

        if (sidebarToggle && sidebar) {
            setDesktopSidebarCollapsed(readSidebarPreference(), { skipPersist: true });
            sidebarToggle.addEventListener('click', function () {
                var willCollapse = !body.classList.contains('fin-sidebar-collapsed');
                setDesktopSidebarCollapsed(willCollapse);
                if (willCollapse) hapticTick();
                window.setTimeout(function () {
                    if (!willCollapse && body.classList.contains('fin-sidebar-collapsed') === false) sidebarToggle.focus();
                }, 390);
            });
            if (desktopSidebarQuery) {
                var onDesktopSidebarViewportChange = function () {
                    setDesktopSidebarCollapsed(readSidebarPreference(), { skipPersist: true });
                };
                if (typeof desktopSidebarQuery.addEventListener === 'function') {
                    desktopSidebarQuery.addEventListener('change', onDesktopSidebarViewportChange);
                } else if (typeof desktopSidebarQuery.addListener === 'function') {
                    desktopSidebarQuery.addListener(onDesktopSidebarViewportChange);
                }
            }
            window.addEventListener('resize', function () {
                if (!isDesktopSidebarViewport()) body.classList.remove('fin-sidebar-collapsed');
            }, { passive: true });
        }

        window.FinanceSidebar = {
            open: function () { setSidebarOpen(true); },
            close: function () { setSidebarOpen(false); },
            isOpen: function () { return body.classList.contains('fin-sidebar-open'); }
        };

        // Бургер відкриває фінансові показники на мобільних.
        if (burger) {
            burger.addEventListener('click', function () {
                setSidebarOpen(!body.classList.contains('fin-sidebar-open'));
            });
        }
        if (backdrop) {
            backdrop.addEventListener('click', function () {
                setSidebarOpen(false);
            });
        }

        document.addEventListener('keydown', function (e) {
            if (e.key === 'Escape') {
                setSidebarOpen(false);
            }
        });

        document.addEventListener('pointerdown', onPointerDown, { passive: true });
        document.addEventListener('pointermove', onPointerMove, { passive: false });
        document.addEventListener('pointerup', onPointerEnd, { passive: true });
        document.addEventListener('pointercancel', resetDrawerDrag, { passive: true });

        // Швидкі дії: справжні модалки додаються у Блоці 3.
        // Поки лишаємо хук, щоб кнопки не виглядали «мертвими».
        document.querySelectorAll('[data-fin-modal]').forEach(function (btn) {
            btn.addEventListener('click', function () {
                var kind = btn.getAttribute('data-fin-modal');
                if (window.FinanceModals && typeof window.FinanceModals.open === 'function') {
                    window.FinanceModals.open(kind);
                } else {
                    // Каркас: повідомлення-заглушка до підключення модалок.
                    console.info('[finance] modal requested:', kind);
                }
            });
        });

        // ---- Фільтр журналу за рахунками (зелена полоска) ----
        // Клік по рахунку у лівій панелі вмикає/вимикає його у фільтрі та веде
        // у журнал платежів із параметром ?accounts=. Нічого не обрано → усі.
        var accBtns = Array.prototype.slice.call(document.querySelectorAll('.fin-account[data-account-id]'));
        var resetBtn = document.getElementById('fin-accounts-reset');
        var hint = document.getElementById('fin-accounts-hint');

        function currentAccountSet() {
            // Джерело правди — URL журналу (?accounts=); інакше localStorage.
            var fromUrl = new URLSearchParams(window.location.search).get('accounts');
            if (fromUrl !== null) {
                return new Set(fromUrl.split(',').filter(Boolean));
            }
            try {
                return new Set(JSON.parse(localStorage.getItem('fin_selected_accounts') || '[]').map(String));
            } catch (e) { return new Set(); }
        }

        function persist(set) {
            try { localStorage.setItem('fin_selected_accounts', JSON.stringify(Array.from(set))); } catch (e) {}
        }

        function paintSelection(set) {
            accBtns.forEach(function (b) {
                b.classList.toggle('is-selected', set.has(String(b.dataset.accountId)));
            });
            var has = set.size > 0;
            if (resetBtn) resetBtn.hidden = !has;
            if (hint) hint.hidden = !has;
        }

        if (accBtns.length) {
            var sel = currentAccountSet();
            // Синхронізуємо localStorage з URL, якщо ми у журналі з фільтром.
            if (new URLSearchParams(window.location.search).has('accounts')) persist(sel);
            paintSelection(sel);

            accBtns.forEach(function (b) {
                b.addEventListener('click', function () {
                    var id = String(b.dataset.accountId);
                    var s = currentAccountSet();
                    if (s.has(id)) s.delete(id); else s.add(id);
                    persist(s);
                    paintSelection(s);
                    var qs = s.size ? ('?accounts=' + Array.from(s).join(',')) : '';
                    window.location.href = '/' + qs;
                });
            });

            if (resetBtn) resetBtn.addEventListener('click', function () {
                persist(new Set());
                window.location.href = '/';
            });
        }
    });
})();
