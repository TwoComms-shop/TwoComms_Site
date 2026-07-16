/**
 * telegram-verify.js
 *
 * Універсальний клієнтський модуль для верифікації Telegram-контакту через бота
 * TwoComms. Використовується для:
 *   - привʼязки Telegram до профілю (purpose=profile_link),
 *   - входу через Telegram (purpose=login),
 *   - привʼязки management-бота (purpose=management_bind),
 *   - привʼязки дропшипера (purpose=dropshipper_link).
 *
 * Експортує `window.TelegramVerify` з методом `start({purpose, next, onSuccess, onError, onCancel})`.
 *
 * Залежить від:
 *   - модалки `partials/telegram_verify_modal.html`, що включена один раз у base.html;
 *   - cookie csrftoken;
 *   - endpoints `/accounts/telegram-verify/{start,status,cancel}/` та
 *     `/accounts/telegram-login/complete/`.
 */
(function () {
  "use strict";

  const ENDPOINTS = {
    start: "/accounts/telegram-verify/start/",
    status: "/accounts/telegram-verify/status/",
    cancel: "/accounts/telegram-verify/cancel/",
    loginComplete: "/accounts/telegram-login/complete/",
  };

  const POLL_INTERVAL_MS = 2200;
  const TTL_FALLBACK_SECONDS = 5 * 60;

  // ── helpers ──────────────────────────────────────────────────────
  function getCsrfToken() {
    const m = document.cookie.match(/(?:^|;\s*)csrftoken=([^;]+)/);
    return m ? decodeURIComponent(m[1]) : "";
  }

  async function postJSON(url, payload) {
    const response = await fetch(url, {
      method: "POST",
      credentials: "same-origin",
      headers: {
        "Content-Type": "application/json",
        "X-CSRFToken": getCsrfToken(),
        "X-Requested-With": "XMLHttpRequest",
      },
      body: JSON.stringify(payload || {}),
    });
    let data = null;
    try {
      data = await response.json();
    } catch (e) {
      data = null;
    }
    return { ok: response.ok, status: response.status, data };
  }

  async function getJSON(url) {
    const response = await fetch(url, {
      method: "GET",
      credentials: "same-origin",
      headers: { "X-Requested-With": "XMLHttpRequest" },
    });
    let data = null;
    try {
      data = await response.json();
    } catch (e) {
      data = null;
    }
    return { ok: response.ok, status: response.status, data };
  }

  function safeNotify(msg, type) {
    if (typeof window.showNotification === "function") {
      try {
        window.showNotification(msg, type || "info");
      } catch (e) {}
    }
  }

  // ── controller ───────────────────────────────────────────────────
  function buildController() {
    let modal = document.querySelector("[data-tg-verify-modal-universal]");
    if (!modal) {
      // Fallback: створюємо мінімальну модалку якщо partial не підключений.
      modal = document.createElement("div");
      modal.setAttribute("data-tg-verify-modal-universal", "");
      modal.className = "tgv-modal-overlay";
      modal.hidden = true;
      modal.innerHTML = MODAL_FALLBACK_HTML;
      document.body.appendChild(modal);
    } else if (modal.parentElement !== document.body) {
      // переносимо модалку в body для коректної fixed-позиції
      document.body.appendChild(modal);
    }

    const screens = {
      intro: modal.querySelector('[data-tg-verify-screen="intro"]'),
      waiting: modal.querySelector('[data-tg-verify-screen="waiting"]'),
      success: modal.querySelector('[data-tg-verify-screen="success"]'),
      error: modal.querySelector('[data-tg-verify-screen="error"]'),
    };
    const titleEl = modal.querySelector("[data-tg-verify-title]");
    const subtitleEl = modal.querySelector("[data-tg-verify-subtitle]");
    const successMeta = modal.querySelector("[data-tg-verify-success-meta]");
    const successContinueBtn = modal.querySelector("[data-tg-verify-continue]");
    const errorTitle = modal.querySelector("[data-tg-verify-error-title]");
    const errorMessage = modal.querySelector("[data-tg-verify-error-message]");
    const timerEl = modal.querySelector("[data-tg-verify-timer]");

    let currentRequest = null; // { purpose, callbacks, session, ... }
    let pollHandle = null;
    let timerHandle = null;
    let initialPurposeData = null;

    function showScreen(name) {
      Object.entries(screens).forEach(([k, el]) => {
        if (!el) return;
        el.hidden = k !== name;
      });
    }

    function setTexts(purpose) {
      const labels = LABELS[purpose] || LABELS.profile_link;
      if (titleEl) titleEl.textContent = labels.title;
      if (subtitleEl) subtitleEl.textContent = labels.subtitle;
    }

    function openModal() {
      modal.hidden = false;
      // force reflow
      // eslint-disable-next-line no-unused-expressions
      modal.offsetWidth;
      modal.classList.add("is-visible");
      document.body.style.overflow = "hidden";
    }

    function closeModal() {
      modal.classList.remove("is-visible");
      document.body.style.overflow = "";
      setTimeout(() => {
        if (!modal.classList.contains("is-visible")) modal.hidden = true;
      }, 220);
    }

    function startTimer(ttl) {
      stopTimer();
      let remaining = Math.max(1, ttl || TTL_FALLBACK_SECONDS);
      function paint() {
        if (!timerEl) return;
        const m = Math.floor(remaining / 60).toString().padStart(2, "0");
        const s = (remaining % 60).toString().padStart(2, "0");
        timerEl.textContent = `${m}:${s}`;
      }
      paint();
      timerHandle = setInterval(() => {
        remaining -= 1;
        if (remaining <= 0) {
          clearInterval(timerHandle);
          timerHandle = null;
          paint();
          handleExpired();
          return;
        }
        paint();
      }, 1000);
    }

    function stopTimer() {
      if (timerHandle) clearInterval(timerHandle);
      timerHandle = null;
    }

    function startPolling() {
      stopPolling();
      pollHandle = setInterval(pollStatus, POLL_INTERVAL_MS);
    }

    function stopPolling() {
      if (pollHandle) clearInterval(pollHandle);
      pollHandle = null;
    }

    async function pollStatus() {
      if (!currentRequest || !currentRequest.session) return;
      try {
        const url = `${ENDPOINTS.status}?token=${encodeURIComponent(currentRequest.session.token)}`;
        const { ok, data } = await getJSON(url);
        if (!ok || !data) return;
        if (data.status === "verified" && data.verified) {
          await handleVerified(data);
          return;
        }
        if (data.status === "expired") {
          handleExpired();
          return;
        }
        if (data.status === "cancelled") {
          handleCancelled();
          return;
        }
      } catch (e) {
        /* ignore */
      }
    }

    async function startSession() {
      try {
        const startBtn = modal.querySelector("[data-tg-verify-open-bot]");
        if (startBtn) startBtn.disabled = true;

        const reqPayload = {
          purpose: currentRequest.purpose,
        };
        if (currentRequest.next) reqPayload.next = currentRequest.next;
        if (currentRequest.bindCode) reqPayload.bind_code = currentRequest.bindCode;
        if (currentRequest.restockId) reqPayload.restock_id = currentRequest.restockId;

        const { ok, data } = await postJSON(ENDPOINTS.start, reqPayload);
        if (!ok || !data || !data.ok) {
          throw new Error((data && data.error) || "Не вдалося створити сесію.");
        }
        currentRequest.session = data;

        try {
          window.open(data.deep_link_url, "_blank", "noopener,noreferrer");
        } catch (e) {
          location.href = data.deep_link_url;
        }
        showScreen("waiting");
        startTimer(data.ttl_seconds);
        startPolling();
      } catch (err) {
        showScreen("error");
        if (errorTitle) errorTitle.textContent = "Не вдалося створити сесію";
        if (errorMessage) errorMessage.textContent = err.message || "Спробуйте ще раз пізніше.";
        const cb = currentRequest && currentRequest.callbacks && currentRequest.callbacks.onError;
        if (typeof cb === "function") {
          try { cb(err); } catch (e) {}
        }
      } finally {
        const startBtn = modal.querySelector("[data-tg-verify-open-bot]");
        if (startBtn) startBtn.disabled = false;
      }
    }

    async function handleVerified(data) {
      stopPolling();
      stopTimer();
      const verifiedData = {
        username: data.telegram_username || "",
        phone: data.phone || "",
        firstName: data.telegram_first_name || "",
        lastName: data.telegram_last_name || "",
        userId: data.telegram_user_id || null,
        purpose: currentRequest.purpose,
        token: currentRequest.session ? currentRequest.session.token : "",
      };

      // UI
      const meta = [];
      if (verifiedData.username) meta.push(`@${verifiedData.username.replace(/^@+/, "")}`);
      if (verifiedData.phone) meta.push(verifiedData.phone);
      const metaText = meta.join(" · ");
      if (successMeta) successMeta.textContent = metaText || "Контакт отримано.";
      showScreen("success");

      // Login flow — потрібно дозвонитись до complete-endpoint
      if (currentRequest.purpose === "login") {
        try {
          const { ok, data: completeData } = await postJSON(ENDPOINTS.loginComplete, {
            token: verifiedData.token,
          });
          if (!ok || !completeData || !completeData.ok) {
            throw new Error(
              (completeData && completeData.error) || "Не вдалося завершити вхід."
            );
          }
          // Аналітика: нова реєстрація через Telegram → CompleteRegistration,
          // вхід наявного користувача → лише GTM-подія login (не Meta-Lead,
          // щоб не засмічувати конверсії). created повертає back-end.
          try {
            if (completeData.created) {
              if (typeof window.trackEvent === "function") {
                var regEventId = (typeof window.safeGenerateAnalyticsEventId === "function")
                  ? window.safeGenerateAnalyticsEventId()
                  : String(Date.now());
                var regMeta = (typeof window.buildMetaWithUserData === "function")
                  ? window.buildMetaWithUserData(regEventId)
                  : { event_id: regEventId };
                window.trackEvent("CompleteRegistration", {
                  content_name: "Telegram registration",
                  status: true,
                  registration_method: "telegram",
                  event_id: regEventId,
                  __meta: regMeta,
                });
              }
            } else if (window.dataLayer && Array.isArray(window.dataLayer)) {
              window.dataLayer.push({ event: "login", method: "telegram" });
            }
          } catch (e) {}
          if (successContinueBtn) {
            successContinueBtn.textContent = "Перейти у профіль";
            successContinueBtn.onclick = () => {
              location.href = completeData.redirect || "/profile/setup/";
            };
          }
          // Через 1.4 секунди робимо redirect автоматично
          setTimeout(() => {
            location.href = completeData.redirect || "/profile/setup/";
          }, 1400);
          safeNotify("Ви увійшли через Telegram.", "success");
          if (currentRequest.callbacks && typeof currentRequest.callbacks.onSuccess === "function") {
            try { currentRequest.callbacks.onSuccess({ ...verifiedData, redirect: completeData.redirect }); } catch (e) {}
          }
        } catch (err) {
          showScreen("error");
          if (errorTitle) errorTitle.textContent = "Помилка входу";
          if (errorMessage) errorMessage.textContent = err.message || "Спробуйте ще раз.";
          if (currentRequest.callbacks && typeof currentRequest.callbacks.onError === "function") {
            try { currentRequest.callbacks.onError(err); } catch (e) {}
          }
        }
      } else {
        // profile_link / dropshipper_link / management_bind — back-end вже все
        // зробив у post_verify_purpose_action; нам залишилось показати success
        // і дернути callback.
        if (successContinueBtn) {
          successContinueBtn.textContent = "Готово";
          successContinueBtn.onclick = () => closeModal();
        }
        if (currentRequest.callbacks && typeof currentRequest.callbacks.onSuccess === "function") {
          try { currentRequest.callbacks.onSuccess(verifiedData); } catch (e) {}
        }
        safeNotify("Telegram підтверджено.", "success");
      }
    }

    function handleExpired() {
      stopPolling();
      stopTimer();
      showScreen("error");
      if (errorTitle) errorTitle.textContent = "Час сесії вийшов";
      if (errorMessage) errorMessage.textContent = "Спробуйте знову — нова сесія діятиме 5 хвилин.";
    }

    function handleCancelled() {
      stopPolling();
      stopTimer();
      showScreen("error");
      if (errorTitle) errorTitle.textContent = "Сесію скасовано";
      if (errorMessage) errorMessage.textContent = "Ви можете запустити підтвердження ще раз.";
    }

    async function cancelSession() {
      if (!currentRequest || !currentRequest.session || !currentRequest.session.token) return;
      try {
        await postJSON(ENDPOINTS.cancel, { token: currentRequest.session.token });
      } catch (e) {}
    }

    function reset() {
      stopPolling();
      stopTimer();
      currentRequest = null;
    }

    // ── public API ──
    function start(opts) {
      const purpose = (opts && opts.purpose) || "profile_link";
      currentRequest = {
        purpose,
        next: opts && opts.next ? String(opts.next) : "",
        bindCode: opts && opts.bindCode ? String(opts.bindCode) : "",
        restockId: opts && opts.restockId ? Number(opts.restockId) : null,
        callbacks: {
          onSuccess: opts && typeof opts.onSuccess === "function" ? opts.onSuccess : null,
          onError: opts && typeof opts.onError === "function" ? opts.onError : null,
          onCancel: opts && typeof opts.onCancel === "function" ? opts.onCancel : null,
        },
        session: null,
      };
      setTexts(purpose);
      showScreen("intro");
      openModal();
    }

    // ── modal events (one-time bind) ──
    modal.addEventListener("click", (event) => {
      if (event.target === modal) closeModal();
    });
    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape" && !modal.hidden) closeModal();
    });
    modal.querySelectorAll("[data-tg-verify-close]").forEach((btn) => {
      btn.addEventListener("click", () => {
        if (currentRequest && currentRequest.callbacks && currentRequest.callbacks.onCancel) {
          try { currentRequest.callbacks.onCancel(); } catch (e) {}
        }
        cancelSession();
        reset();
        closeModal();
      });
    });
    modal.querySelectorAll("[data-tg-verify-open-bot]").forEach((btn) => {
      btn.addEventListener("click", startSession);
    });
    modal.querySelectorAll("[data-tg-verify-reopen-bot]").forEach((btn) => {
      btn.addEventListener("click", () => {
        if (currentRequest && currentRequest.session && currentRequest.session.deep_link_url) {
          try {
            window.open(currentRequest.session.deep_link_url, "_blank", "noopener,noreferrer");
          } catch (e) {
            location.href = currentRequest.session.deep_link_url;
          }
        }
      });
    });
    modal.querySelectorAll("[data-tg-verify-copy-link]").forEach((btn) => {
      btn.addEventListener("click", async () => {
        if (!currentRequest || !currentRequest.session || !currentRequest.session.deep_link_url) return;
        try {
          await navigator.clipboard.writeText(currentRequest.session.deep_link_url);
          safeNotify("Посилання скопійовано.", "success");
        } catch (e) {
          safeNotify("Не вдалося скопіювати — використайте кнопку «Відкрити бота».", "error");
        }
      });
    });
    modal.querySelectorAll("[data-tg-verify-cancel]").forEach((btn) => {
      btn.addEventListener("click", async () => {
        await cancelSession();
        reset();
        closeModal();
      });
    });
    modal.querySelectorAll("[data-tg-verify-retry]").forEach((btn) => {
      btn.addEventListener("click", startSession);
    });

    return { start };
  }

  // ── labels per purpose ──
  const LABELS = {
    profile_link: {
      title: "Підтвердження Telegram",
      subtitle:
        "Ми отримаємо ваш номер телефону і Telegram ID, щоб надсилати статуси замовлень напряму в чат.",
    },
    dropshipper_link: {
      title: "Прив’язка Telegram дропшипера",
      subtitle:
        "Ми надсилатимемо нові замовлення та статуси відправок у Telegram цього акаунту.",
    },
    login: {
      title: "Вхід через Telegram",
      subtitle:
        "Натисніть «Відкрити бота» — Telegram передасть номер, і ми створимо або знайдемо ваш акаунт автоматично.",
    },
    management_bind: {
      title: "Прив’язка менеджмент-бота",
      subtitle:
        "Підтвердьте номер у боті TwoComms — далі завершимо привʼязку у боті менеджера.",
    },
    restock: {
      title: "Telegram для сповіщення",
      subtitle:
        "Підтвердьте номер у боті TwoComms. Ми напишемо лише тоді, коли вибраний розмір зʼявиться в наявності.",
    },
  };

  // ── fallback HTML (на випадок, якщо partial не підключений) ──
  const MODAL_FALLBACK_HTML = `
    <div class="tgv-modal-card">
      <button type="button" class="tgv-modal-close" data-tg-verify-close aria-label="Закрити">&times;</button>
      <div class="tgv-modal-icon" aria-hidden="true">
        <svg viewBox="0 0 64 64" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round">
          <line x1="60" y1="4" x2="28" y2="36"/>
          <polygon points="60 4 38 60 28 36 4 26 60 4"/>
        </svg>
      </div>
      <h2 class="tgv-modal-title" data-tg-verify-title>Підтвердження Telegram</h2>
      <p class="tgv-modal-subtitle" data-tg-verify-subtitle>Натисніть «Відкрити бота TwoComms» і поділіться номером.</p>
      <div class="tgv-modal-state" data-tg-verify-screen="intro">
        <ol class="tgv-modal-steps">
          <li><span>1</span> Натисніть «Відкрити бота TwoComms» — ми перенесемо вас у Telegram.</li>
          <li><span>2</span> У боті натисніть «📱 Поділитися номером».</li>
          <li><span>3</span> Поверніться сюди — ми завершимо автоматично.</li>
        </ol>
        <div class="tgv-modal-actions">
          <button type="button" class="tgv-modal-btn tgv-modal-btn--primary" data-tg-verify-open-bot>
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
              <line x1="22" y1="2" x2="11" y2="13"/>
              <polygon points="22 2 15 22 11 13 2 9 22 2"/>
            </svg>
            Відкрити бота TwoComms
          </button>
          <button type="button" class="tgv-modal-btn tgv-modal-btn--ghost" data-tg-verify-close>Скасувати</button>
        </div>
      </div>
      <div class="tgv-modal-state" data-tg-verify-screen="waiting" hidden>
        <div class="tgv-modal-spinner" aria-hidden="true"><span></span><span></span><span></span></div>
        <h3 class="tgv-modal-headline">Очікуємо ваш контакт у боті…</h3>
        <p class="tgv-modal-subline">У відкритому Telegram-боті натисніть «📱 Поділитися номером».</p>
        <div class="tgv-modal-timer" data-tg-verify-timer>05:00</div>
        <div class="tgv-modal-actions">
          <button type="button" class="tgv-modal-btn tgv-modal-btn--secondary" data-tg-verify-reopen-bot>Відкрити бота ще раз</button>
          <button type="button" class="tgv-modal-btn tgv-modal-btn--ghost" data-tg-verify-cancel>Скасувати</button>
        </div>
        <div class="tgv-modal-note">
          💡 Бот не відкрився? <button type="button" class="tgv-modal-link" data-tg-verify-copy-link>Скопіювати посилання</button>
        </div>
      </div>
      <div class="tgv-modal-state" data-tg-verify-screen="success" hidden>
        <div class="tgv-modal-success-icon" aria-hidden="true">✅</div>
        <h3 class="tgv-modal-headline">Telegram підтверджено!</h3>
        <p class="tgv-modal-subline" data-tg-verify-success-meta></p>
        <div class="tgv-modal-actions">
          <button type="button" class="tgv-modal-btn tgv-modal-btn--primary" data-tg-verify-continue data-tg-verify-close>Готово</button>
        </div>
      </div>
      <div class="tgv-modal-state" data-tg-verify-screen="error" hidden>
        <div class="tgv-modal-error-icon" aria-hidden="true">⏱</div>
        <h3 class="tgv-modal-headline" data-tg-verify-error-title>Сесія завершилась</h3>
        <p class="tgv-modal-subline" data-tg-verify-error-message>Спробуйте знову.</p>
        <div class="tgv-modal-actions">
          <button type="button" class="tgv-modal-btn tgv-modal-btn--primary" data-tg-verify-retry>Спробувати ще раз</button>
          <button type="button" class="tgv-modal-btn tgv-modal-btn--ghost" data-tg-verify-close>Закрити</button>
        </div>
      </div>
    </div>
  `;

  let _ctrl = null;
  function ensureController() {
    if (!_ctrl) _ctrl = buildController();
    return _ctrl;
  }

  window.TelegramVerify = {
    start(opts) {
      ensureController().start(opts);
    },
  };

  // ── Auto-bind for [data-tg-mini-login] (mini-profile в шапці) ──
  // Це дозволяє додавати кнопку «Увійти через Telegram» у будь-яке
  // місце сайту без дублювання обробників.
  function bindMiniLoginTriggers(root) {
    var scope = root || document;
    var nodes = scope.querySelectorAll
      ? scope.querySelectorAll("[data-tg-mini-login]")
      : [];
    Array.prototype.forEach.call(nodes, function (btn) {
      if (btn.dataset.tgMiniBound === "1") return;
      btn.dataset.tgMiniBound = "1";
      btn.addEventListener("click", function (event) {
        event.preventDefault();
        var nextUrl = btn.getAttribute("data-next") || "";
        if (!nextUrl) {
          // Якщо ми вже на /login або /register — редіректимо на профіль.
          var path = (location && location.pathname) || "/";
          if (path.startsWith("/login") || path.startsWith("/register")) {
            nextUrl = "/profile/setup/";
          } else {
            nextUrl = path + (location.search || "");
          }
        }
        ensureController().start({
          purpose: "login",
          next: nextUrl,
        });
      });
    });
  }

  function init() {
    ensureController();
    bindMiniLoginTriggers(document);
    try {
      var observer = new MutationObserver(function () {
        bindMiniLoginTriggers(document);
      });
      observer.observe(document.body, { childList: true, subtree: true });
    } catch (e) {
      /* ignore */
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
