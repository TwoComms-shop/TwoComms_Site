/* TwoComms Custom Print Configurator V2 — Waterfall + Stage Receipt
 * - Жодного автовибору. Початковий стан = nulls / [].
 * - Vertical waterfall: завершені кроки згортаються у summary-рядки.
 * - Stage Receipt: прорахунок живе всередині картки виробу.
 * - Per-zone uploads: окрема dropzone для кожної обраної зони.
 * - Smart sizing: qty=1 → chip-row, qty>1 → matrix з валідацією суми.
 * - Gift як платний сервіс +100 грн з полем побажання та промокодом.
 * - B2B живий калькулятор: кожні 8 шт відкривають наступний рівень ціни.
 * - Dual final: «Додати в кошик» (сесійний кошик) і «Надіслати менеджеру» (Telegram).
 */
(function () {
  const root = document.querySelector("[data-custom-print-root]");
  const configNode = document.getElementById("customPrintConfiguratorConfig");
  if (!root || !configNode) return;

  let CONFIG;
  try {
    CONFIG = JSON.parse(configNode.textContent || "{}");
  } catch (error) {
    console.error("[custom-print v2] config parse failed", error);
    return;
  }

  const STORAGE_KEY = CONFIG.storage_key || "twocomms.custom_print.v2.draft";
  const TRACK_EVENT_URL = CONFIG.track_event_url || "/api/track-event/";
  const STEPS = ["mode", "product", "config", "zones", "artwork", "quantity", "gift", "contact"];
  const STUDIO_STEPS = CONFIG.progress_steps || [];
  const stateTools = globalThis.CustomPrintStateTools;
  const STAGE_VISIBLE_AFTER = new Set(STEPS.filter((step) => step !== "mode"));
  const FRONT_SIZE_DEFAULT = CONFIG.front_size_default || "A4";
  const BACK_SIZE_DEFAULT = CONFIG.back_size_default || "A4";
  const SLEEVE_MODE_DEFAULT = CONFIG.sleeve_mode_default || "a6";
  const FRONT_SIZE_PRESETS = (CONFIG.front_size_presets || []).reduce((acc, item) => {
    if (item && item.value) acc[item.value] = item;
    return acc;
  }, {});
  const BACK_SIZE_PRESETS = (CONFIG.back_size_presets || []).reduce((acc, item) => {
    if (item && item.value) acc[item.value] = item;
    return acc;
  }, {});
  const CUSTOM_ZONE_PRESETS = (CONFIG.custom_zone_size_presets || []).reduce((acc, item) => {
    if (item && item.value) acc[item.value] = item;
    return acc;
  }, {});
  const CUSTOM_ZONE_LOCATIONS = [
    { value: "shoulder", label: "Плече", hint: "Шеврон або невеликий знак" },
    { value: "hem", label: "Низ виробу", hint: "Біля нижнього краю" },
    { value: "other", label: "Інше місце", hint: "Опишіть вручну нижче" },
  ];
  const SLEEVE_MODE_OPTIONS = (CONFIG.sleeve_mode_options || []).reduce((acc, item) => {
    if (item && item.value) acc[item.value] = item;
    return acc;
  }, {});
  const STAGE_PROFILES = CONFIG.stage_profiles || {};
  const UI_STRINGS = CONFIG.ui_strings || {};
  const submissionPolicy = globalThis.CustomPrintSubmissionPolicy || null;
  const mobileProgressQuery = globalThis.matchMedia ? globalThis.matchMedia("(max-width: 720px)") : null;
  const studioShellQuery = globalThis.matchMedia ? globalThis.matchMedia("(max-width: 1100px)") : null;

  const STATE = createState();
  const filesByPlacement = new Map(); // placement_key -> File[]
  let garmentPhotoFile = null;
  const analyticsState = {
    flowStarted: false,
    enteredSteps: new Set(),
    completedSteps: new Set(),
  };
  let leadSubmitInFlight = false;
  let cartSubmitInFlight = false;
  let studioManuallyExited = false;

  function ui(key, fallback) {
    return UI_STRINGS[key] || fallback;
  }

  // ── DOM refs ────────────────────────────────────────────────
  const plateArtCache = new Map();

  const dom = {
    shell: root.querySelector("[data-shell]"),
    hero: root.querySelector("[data-hero]"),
    form: root.querySelector("#customPrintConfiguratorForm"),
    progressShell: root.querySelector("[data-progress-shell]"),
    progressStrip: root.querySelector("[data-progress-strip]"),
    workbench: root.querySelector("[data-workbench]"),
    waterfall: root.querySelector("[data-waterfall]"),
    stageCard: root.querySelector("[data-stage-card]"),
    stageEyebrow: root.querySelector("[data-stage-eyebrow]"),
    stageTitleSecondary: root.querySelector("[data-stage-title-secondary]"),
    stageNote: root.querySelector("[data-stage-note]"),
    stageZones: root.querySelector("[data-stage-zones]"),
    stageAddons: root.querySelector("[data-stage-addons]"),
    stageLabel: root.querySelector("[data-stage-label]"),
    stageViewSwitch: root.querySelectorAll("[data-stage-view]"),
    receiptTotal: root.querySelector("[data-receipt-total]"),
    receiptList: root.querySelector("[data-receipt-list]"),
    receiptMode: root.querySelector("[data-receipt-mode]"),
    receiptHint: root.querySelector("[data-receipt-hint]"),
    modeList: root.querySelector("[data-mode-list]"),
    productList: root.querySelector("[data-product-list]"),
    fitList: root.querySelector("[data-fit-list]"),
    fabricList: root.querySelector("[data-fabric-list]"),
    fitBlock: root.querySelector("[data-fit-block]"),
    fabricBlock: root.querySelector("[data-fabric-block]"),
    colorList: root.querySelector("[data-color-list]"),
    standardColorBlock: root.querySelector("[data-standard-color-block]"),
    zoneList: root.querySelector("[data-zone-list]"),
    placementNoteWrap: root.querySelector("[data-placement-note-wrap]"),
    placementNoteInput: root.querySelector("[data-placement-note-input]"),
    customZoneWrap: root.querySelector("[data-custom-zone-wrap]"),
    customLocationList: root.querySelector("[data-custom-location-list]"),
    customSizeList: root.querySelector("[data-custom-size-list]"),
    frontSizeWrap: root.querySelector("[data-front-size-wrap]"),
    frontSizeList: root.querySelector("[data-front-size-list]"),
    backSizeWrap: root.querySelector("[data-back-size-wrap]"),
    backSizeList: root.querySelector("[data-back-size-list]"),
    sleeveWrap: root.querySelector("[data-sleeve-wrap]"),
    sleeveSideList: root.querySelector("[data-sleeve-side-list]"),
    sleeveEditors: root.querySelectorAll("[data-sleeve-editor]"),
    sleeveModeLists: root.querySelectorAll("[data-sleeve-mode-list]"),
    sleeveTextWraps: root.querySelectorAll("[data-sleeve-text-wrap]"),
    sleeveTextInputs: root.querySelectorAll("[data-sleeve-text-input]"),
    addonsList: root.querySelector("[data-addons-list]"),
    addonsWrap: root.querySelector("[data-addons-wrap]"),
    addonsTitle: root.querySelector("[data-addons-title]"),
    addonsNote: root.querySelector("[data-addons-note]"),
    productDetailNote: root.querySelector("[data-product-detail-note]"),
    productDetailTitle: root.querySelector("[data-product-detail-title]"),
    productDetailCopy: root.querySelector("[data-product-detail-copy]"),
    ownGarmentOptions: root.querySelector("[data-own-garment-options]"),
    ownShippingList: root.querySelector("[data-own-shipping-list]"),
    ownColorInput: root.querySelector("[data-own-color-input]"),
    ownColorHue: root.querySelector("[data-own-color-hue]"),
    ownColorValue: root.querySelector("[data-own-color-value]"),
    ownPhotoWrap: root.querySelector("[data-own-photo-wrap]"),
    ownPhotoInput: root.querySelector("[data-own-photo-input]"),
    artworkList: root.querySelector("[data-artwork-service-list]"),
    dropzoneGrid: root.querySelector("[data-dropzone-grid]"),
    dropzoneEmpty: root.querySelector("[data-dropzone-empty]"),
    briefInput: root.querySelector("[data-brief-input]"),
    qtyInput: root.querySelector("[data-quantity-input]"),
    qtyBar: root.querySelector("[data-quantity-bar]"),
    qtyHint: root.querySelector("[data-quantity-hint]"),
    qtySteps: root.querySelectorAll("[data-qty-step]"),
    sizeBlock: root.querySelector("[data-size-block]"),
    sizeGrid: root.querySelector("[data-size-grid]"),
    sizeMatrix: root.querySelector("[data-size-matrix]"),
    sizeWarning: root.querySelector("[data-size-warning]"),
    sizeHint: root.querySelector("[data-size-hint]"),
    sizeManagerBtn: root.querySelector("[data-size-manager]"),
    sizesNoteWrap: root.querySelector("[data-sizes-note-wrap]"),
    sizesNoteInput: root.querySelector("[data-sizes-note-input]"),
    garmentNoteWrap: root.querySelector("[data-own-item-note-wrap]"),
    garmentNoteInput: root.querySelector("[data-own-item-note-input]"),
    b2bMeta: root.querySelector("[data-b2b-meta]"),
    b2bDiscount: root.querySelector("[data-b2b-discount]"),
    giftToggle: root.querySelector("[data-gift-toggle]"),
    giftToggleState: root.querySelector("[data-gift-toggle-state]"),
    giftTextWrap: root.querySelector("[data-gift-text-wrap]"),
    giftTextInput: root.querySelector("[data-gift-text-input]"),
    giftContinue: root.querySelector("[data-gift-continue]"),
    contactChannelList: root.querySelector("[data-contact-channel-list]"),
    nameInput: root.querySelector("[data-name-input]"),
    contactValueInput: root.querySelector("[data-contact-value-input]"),
    brandFields: root.querySelector("[data-brand-fields]"),
    brandNameInput: root.querySelector("[data-brand-name-input]"),
    brandBrief: root.querySelector("[data-brand-brief]"),
    brandIntroName: root.querySelector("[data-brand-intro-name]"),
    brandContactPerson: root.querySelector("[data-brand-contact-person]"),
    brandContactChannelList: root.querySelector("[data-brand-contact-channel-list]"),
    brandContactValue: root.querySelector("[data-brand-contact-value]"),
    brandBusinessType: root.querySelector("[data-brand-business-type]"),
    brandProductList: root.querySelector("[data-brand-product-list]"),
    brandResource: root.querySelector("[data-brand-resource]"),
    brandPhone: root.querySelector("[data-brand-phone]"),
    brandDeadline: root.querySelector("[data-brand-deadline]"),
    brandQuantity: root.querySelector("[data-brand-quantity]"),
    brandWish: root.querySelector("[data-brand-wish]"),
    brandTierRail: root.querySelector("[data-brand-tier-rail]"),
    brandTierNote: root.querySelector("[data-brand-tier-note]"),
    brandContinue: root.querySelector("[data-brand-continue]"),
    statusBox: root.querySelector("[data-status-box]"),
    finalChecklist: root.querySelector("[data-final-checklist]"),
    addToCartBtn: root.querySelector("[data-action-add-to-cart]"),
    submitLeadBtn: root.querySelector("[data-action-submit-lead]"),
    safeExitButtons: root.querySelectorAll("[data-safe-exit-trigger]"),
    startFlow: root.querySelector("[data-start-flow]"),
    cartActionHint: root.querySelector("[data-cart-action-hint]"),
    leadActionHint: root.querySelector("[data-lead-action-hint]"),
    stepEditButtons: root.querySelectorAll("[data-step-edit]"),
    stepBackButtons: root.querySelectorAll("[data-step-back]"),
    stepNextButtons: root.querySelectorAll("[data-step-next]"),
    stepSkipButtons: root.querySelectorAll("[data-step-skip]"),
    mobileBar: root.querySelector("[data-mobile-bottom-bar]") || document.querySelector("[data-mobile-bottom-bar]"),
    mobileBarTotal: root.querySelector("[data-mobile-bar-total]") || document.querySelector("[data-mobile-bar-total]"),
    mobileBarLabel: root.querySelector("[data-mobile-bar-label]") || document.querySelector("[data-mobile-bar-label]"),
    mobileBarMeta: root.querySelector("[data-mobile-bar-meta]") || document.querySelector("[data-mobile-bar-meta]"),
    mobileBarAction: root.querySelector("[data-mobile-bar-action]") || document.querySelector("[data-mobile-bar-action]"),
    mobileBarActionLabel: root.querySelector("[data-mobile-bar-action-label]") || document.querySelector("[data-mobile-bar-action-label]"),
    draftResumeCard: root.querySelector("[data-draft-resume-card]"),
    draftResumeTitle: root.querySelector("[data-draft-resume-title]"),
    draftResumeButton: root.querySelector("[data-draft-resume]"),
    draftRestartButton: root.querySelector("[data-draft-restart]"),
    managerButton: root.querySelector("[data-manager-open]"),
    managerQuickButtons: root.querySelectorAll("[data-manager-quick-contact]"),
    personalManagerCta: root.querySelector("[data-personal-manager-cta]"),
  };
  const progressHome = dom.progressShell ? document.createComment("cp-progress-home") : null;
  if (progressHome && dom.progressShell?.parentNode) {
    dom.progressShell.parentNode.insertBefore(progressHome, dom.progressShell);
  }
  const previewController = globalThis.CustomPrintPreview?.create({ root, config: CONFIG, getState: () => STATE }) || null;
  const dialogFlow = globalThis.CustomPrintSubmitFlow?.create(root) || null;
  const mobileShell = globalThis.CustomPrintMobileShell?.create({
    root,
    mobileBar: dom.mobileBar,
    onExit: exitStudio,
    onBack: navigateBack,
    onManager: openManagerDialog,
    onPreview: openPreviewDialog,
  }) || null;

  init();
  mobileProgressQuery?.addEventListener?.("change", () => {
    syncProgressShellPlacement();
  });

  // ────────────────────────────────────────────────────────────
  function createState() {
    return {
      mode: null, // "personal" | "brand"
      product: {
        type: null, // "hoodie" | "tshirt" | "longsleeve" | "customer_garment"
        fit: null,
        fabric: null,
        color: null,
      },
      print: {
        zones: [],
        add_ons: [],
        placement_note: "",
        zone_options: {},
      },
      artwork: {
        service_kind: null,
        triage_status: null,
      },
      order: {
        quantity: 0,
        size_mode: "single",
        sizes_note: "",
        size_breakdown: {},
        delivery_method: "",
        gift_enabled: false,
        gift_text: "",
      },
      notes: {
        brand_name: "",
        brand_contact_person: "",
        brand_contact_channel: "",
        brand_contact_value: "",
        brand_business_type: "brand",
        brand_product_types: [],
        brief: "",
        garment_note: "",
        garment_color_hex: "#151515",
        brand_resource: "",
        brand_phone: "",
        brand_deadline: "",
        brand_wish: "",
        garment_photo_name: "",
      },
      contact: {
        channel: null,
        name: "",
        value: "",
      },
      ui: {
        current_step: "mode",
        done_steps: new Set(),
        stage_view: "front",
      },
    };
  }

  function buildAnalyticsMetadata(extra = {}) {
    const pricing = computePricing();
    return {
      current_step: STATE.ui.current_step || "mode",
      mode: STATE.mode || "",
      product_type: STATE.product.type || "",
      service_kind: STATE.artwork.service_kind || "",
      quantity: STATE.order.quantity || 0,
      zones: [...(STATE.print.zones || [])],
      file_count: collectOrderedFiles().length,
      estimate_required: !!pricing.estimate_required,
      final_total: pricing.final_total ?? null,
      ...extra,
    };
  }

  function sendAnalyticsEvent(eventType, metadata = {}) {
    if (!TRACK_EVENT_URL || !eventType) return;
    const body = JSON.stringify({
      event_type: eventType,
      metadata,
    });
    try {
      if (navigator.sendBeacon) {
        const blob = new Blob([body], { type: "application/json" });
        navigator.sendBeacon(TRACK_EVENT_URL, blob);
        return;
      }
    } catch (_) {
      // noop
    }
    fetch(TRACK_EVENT_URL, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-Requested-With": "XMLHttpRequest",
      },
      credentials: "same-origin",
      keepalive: true,
      body,
    }).catch(() => {});
  }

  function ensureFlowStarted(trigger) {
    if (analyticsState.flowStarted) return;
    analyticsState.flowStarted = true;
    sendAnalyticsEvent("custom_print_start", buildAnalyticsMetadata({ trigger }));
  }

  function bindFirstInteraction() {
    root.addEventListener("click", (event) => {
      if (analyticsState.flowStarted || studioManuallyExited) return;
      const target = event.target?.closest?.("button, input, textarea, select, label");
      if (!target || target.closest("[data-studio-exit]")) return;
      if (target.closest("[data-mode-list], [data-product-list], [data-fit-list], [data-fabric-list], [data-color-list], [data-zone-list], [data-artwork-service-list], [data-own-garment-options], [data-size-block], [data-step-next], [data-gift-toggle]")) {
        enterStudio("first_choice");
      }
    }, { capture: true });
  }

  function trackStepEnter(stepKey, extra = {}) {
    if (!stepKey || analyticsState.enteredSteps.has(stepKey)) return;
    analyticsState.enteredSteps.add(stepKey);
    sendAnalyticsEvent("custom_print_step_enter", buildAnalyticsMetadata({ step_key: stepKey, ...extra }));
  }

  function trackStepComplete(stepKey, extra = {}) {
    if (!stepKey || analyticsState.completedSteps.has(stepKey)) return;
    analyticsState.completedSteps.add(stepKey);
    sendAnalyticsEvent("custom_print_step_complete", buildAnalyticsMetadata({ step_key: stepKey, ...extra }));
  }

  function init() {
    if (dom.statusBox && !dom.statusBox.dataset.defaultStatus) {
      dom.statusBox.dataset.defaultStatus = dom.statusBox.textContent.trim();
    }
    renderModeChips();
    renderProductCards();
    renderArtworkCardModifiers();
    renderContactChannelChips();
    renderZoneChipsForCurrent();
    renderFrontSizeOptions();
    renderBackSizeOptions();
    renderSleeveControls();
    renderAddons();
    renderColorChips();
    renderFitChips();
    renderFabricChips();
    bindWaterfallNav();
    bindStageView();
    bindQuantity();
    bindGiftToggle();
    bindFinalActions();
    bindGenericInputs();
    bindOwnGarmentControls();
    bindBrandBriefNavigation();
    bindFirstInteraction();
    bindHeroMotion();
    bindMobileBottomBar();
    bindManagerQuickContact();
    bindStudioBoundary();
    setupDraftResume();
    setActiveStep(STATE.ui.current_step || "mode", { silent: true });
    refreshAll();
  }

  function enterStudio(trigger = "start_button") {
    studioManuallyExited = false;
    ensureFlowStarted(trigger);
    mobileShell?.setActive(true);
    setActiveStep(STATE.ui.current_step || "mode", { silent: true });
    scrollToStudioTarget(document.getElementById(`cp-step-${STATE.ui.current_step || "mode"}`));
  }

  function exitStudio() {
    studioManuallyExited = true;
    persistDraft();
    mobileShell?.setActive(false);
    // Removing app mode restores the global nav and changes the page height.
    // Wait for that reflow before returning to the page entry point; otherwise
    // iOS-sized viewports can stop halfway through the hero.
    globalThis.requestAnimationFrame(() => {
      const reducedMotion = window.matchMedia?.("(prefers-reduced-motion: reduce)")?.matches;
      window.scrollTo({ top: 0, behavior: reducedMotion ? "auto" : "smooth" });
    });
  }

  function openPreviewDialog(event) {
    previewController?.render();
    sendAnalyticsEvent("preview_open", buildAnalyticsMetadata({ studio_step: stateTools?.fromInternal(STATE.ui.current_step) }));
    dialogFlow?.openPreviewDialog(event?.currentTarget || event?.target);
  }

  function openManagerDialog(event) {
    persistDraft();
    sendAnalyticsEvent("manager_open", buildAnalyticsMetadata({ studio_step: stateTools?.fromInternal(STATE.ui.current_step) }));
    dialogFlow?.openManagerDialog({
      trigger: event?.currentTarget || event?.target,
      isB2b: STATE.mode === "brand",
      summary: buildManagerSummary(),
    });
  }

  function navigateBack() {
    const index = getStepIndex(STATE.ui.current_step);
    if (index <= 0) {
      exitStudio();
      return;
    }
    const previous = STEPS[index - 1];
    STATE.ui.done_steps.delete(STATE.ui.current_step);
    setActiveStep(previous, { fromStep: STATE.ui.current_step });
  }

  function bindManagerQuickContact() {
    // Keep the contextual CTA inside the same accessible contact sheet as the
    // app-bar action. The sheet confirms that the draft is saved and lets the
    // user review the compact configuration summary before opening Telegram.
    dom.managerQuickButtons?.forEach((button) => button.addEventListener("click", openManagerDialog));
  }

  function buildManagerSummary() {
    const cfg = getProductConfig() || {};
    const colors = getAllowedColorOptions(cfg);
    const fit = (cfg.fits || []).find((item) => item.value === STATE.product.fit)?.label || STATE.product.fit || "—";
    const color = colors.find((item) => item.value === STATE.product.color)?.label || STATE.product.color || "—";
    const fabric = getSelectedFabricConfig()?.label || STATE.product.fabric || "—";
    const zones = getExpandedPlacements().map((placement) => {
      const size = placement.size_preset ? ` · ${placement.size_preset}` : "";
      return `${placement.label}${size}`;
    }).join(", ") || "—";
    const quantity = STATE.order.quantity ? `${STATE.order.quantity} шт` : "—";
    const stepLabels = { mode: "Формат", product: "Виріб", config: "Налаштування", zones: "Зони друку", artwork: "Макет", quantity: "Кількість", gift: "Подарунок", contact: "Контакт" };
    const step = stepLabels[STATE.ui.current_step] || "—";
    const pricing = computePricing();
    const contact = STATE.contact.channel && STATE.contact.value
      ? `${STATE.contact.channel}: ${STATE.contact.value}`
      : "ще не вказано";
    const files = collectOrderedFiles().map(({ file, label }) => `${label}: ${file.name}`).join(", ") || "ще не додано";
    return [
      ui("manager_greeting", "Привіт! Хочу обговорити кастомний принт TwoComms."),
      "",
      "КОНФІГУРАЦІЯ",
      `• Формат: ${STATE.mode === "brand" ? "команда / бренд" : "для себе"}`,
      `• Виріб: ${cfg.label || "—"}`,
      `• Посадка: ${fit} · тканина: ${fabric} · колір: ${color}`,
      `• Зони: ${zones}`,
      `• Кількість: ${quantity}`,
      STATE.product.type === "customer_garment" ? `• Передача: ${STATE.order.delivery_method || "уточнити"} (доставку туди й назад оплачує покупець)` : "",
      STATE.mode === "brand" && STATE.notes.brand_resource ? `• Ресурс: ${STATE.notes.brand_resource}` : "",
      STATE.mode === "brand" && STATE.notes.brand_phone ? `• Додатковий контакт: ${STATE.notes.brand_phone}` : "",
      STATE.mode === "brand" && STATE.notes.brand_wish ? `• Побажання: ${STATE.notes.brand_wish}` : "",
      `• Подарунок: ${STATE.order.gift_enabled ? "так" : "ні"}`,
      `• Орієнтир: ${pricing.final_total ? formatPrice(pricing.final_total) : "уточнити"}`,
      "",
      `КОНТАКТ: ${contact}`,
      `ФАЙЛИ: ${files}`,
      `Поточний крок: ${step}`,
    ].join("\n");
  }

  function bindHeroMotion() {
    if (!dom.hero) return;

    const motionQuery = globalThis.matchMedia?.("(prefers-reduced-motion: reduce)") || null;
    let rafId = 0;

    const updateHeroMotion = () => {
      rafId = 0;
      if (motionQuery?.matches) {
        dom.hero.style.setProperty("--cp-hero-scroll", "0");
        return;
      }

      const rect = dom.hero.getBoundingClientRect();
      const travel = Math.max(rect.height || 0, 1);
      const progress = Math.min(Math.max((-rect.top) / travel, 0), 1);
      dom.hero.style.setProperty("--cp-hero-scroll", progress.toFixed(3));
    };

    const requestHeroMotionUpdate = () => {
      if (rafId) return;
      rafId = globalThis.requestAnimationFrame(updateHeroMotion);
    };

    const handleMotionPreferenceChange = (event) => {
      if (event.matches) {
        dom.hero.style.setProperty("--cp-hero-scroll", "0");
        return;
      }
      requestHeroMotionUpdate();
    };

    requestHeroMotionUpdate();
    globalThis.addEventListener("scroll", requestHeroMotionUpdate, { passive: true });
    globalThis.addEventListener("resize", requestHeroMotionUpdate);
    if (typeof motionQuery?.addEventListener === "function") {
      motionQuery.addEventListener("change", handleMotionPreferenceChange);
    } else if (typeof motionQuery?.addListener === "function") {
      motionQuery.addListener(handleMotionPreferenceChange);
    }
  }

  function bindStudioBoundary() {
    const boundary = root.querySelector("[data-studio-boundary]");
    if (!boundary) return;
    const checkBoundary = () => {
      if (studioManuallyExited || !analyticsState.flowStarted) return;
      // The app shell must remain stable during validation scrolls. The SEO
      // stack is already hidden in app mode, and leaving the studio is an
      // explicit X action, so a sentinel must never tear down the shell.
      if (!document.body.classList.contains("cp-studio-active")) {
        mobileShell?.setActive(true);
      }
      renderMobileBottomBar();
    };
    window.addEventListener("scroll", checkBoundary, { passive: true });
    checkBoundary();
  }

  function scrollToStudioTarget(target, { focus = false } = {}) {
    if (!target || !target.getBoundingClientRect) return;
    const stepViewport = root.querySelector(".cp-step-viewport");
    const usesInternalScroller = !!(
      studioShellQuery?.matches
      && document.body.classList.contains("cp-studio-active")
      && stepViewport
      && stepViewport.contains(target)
    );
    const reducedMotion = window.matchMedia?.("(prefers-reduced-motion: reduce)")?.matches;
    if (usesInternalScroller) {
      const viewportRect = stepViewport.getBoundingClientRect();
      const targetRect = target.getBoundingClientRect();
      const maxScroll = Math.max(0, stepViewport.scrollHeight - stepViewport.clientHeight);
      const nextTop = Math.max(0, Math.min(maxScroll, stepViewport.scrollTop + targetRect.top - viewportRect.top - 12));
      stepViewport.scrollTo({ top: nextTop, behavior: reducedMotion ? "auto" : "smooth" });
      if (focus) {
        window.setTimeout(() => target.focus?.({ preventScroll: true }), reducedMotion ? 0 : 180);
      }
      return;
    }
    const appbar = root.querySelector("[data-studio-appbar]");
    const appbarHeight = appbar ? Math.max(66, appbar.getBoundingClientRect().height) : 66;
    const topOffset = appbarHeight + 22;
    const bottomBar = document.querySelector("[data-mobile-action-bar]");
    const bottomClearance = bottomBar && !bottomBar.hidden
      ? Math.max(96, bottomBar.getBoundingClientRect().height + 18)
      : 24;
    const rect = target.getBoundingClientRect();
    const maxScroll = Math.max(0, document.documentElement.scrollHeight - window.innerHeight);
    const targetBottom = rect.bottom + window.scrollY;
    const viewportBottom = window.scrollY + window.innerHeight - bottomClearance;
    const preferredTop = rect.top + window.scrollY - topOffset;
    const nextTop = Math.max(0, Math.min(maxScroll,
      targetBottom > viewportBottom && rect.height < window.innerHeight - topOffset - bottomClearance
        ? targetBottom - window.innerHeight + bottomClearance
        : preferredTop
    ));
    window.scrollTo({ top: nextTop, behavior: reducedMotion ? "auto" : "smooth" });
    if (focus) {
      window.setTimeout(() => target.focus?.({ preventScroll: true }), reducedMotion ? 0 : 180);
    }
  }

  function isLobbyPhase() {
    return !STATE.mode && STATE.ui.current_step === "mode";
  }

  function getStepIndex(stepKey) {
    return STEPS.indexOf(stepKey);
  }

  function getStageType() {
    return STATE.product.type || "hoodie";
  }

  function getFrontSizePreset() {
    return (
      STATE.print.zone_options?.front?.size_preset ||
      FRONT_SIZE_DEFAULT
    );
  }

  function getBackSizePreset() {
    return (
      STATE.print.zone_options?.back?.size_preset ||
      BACK_SIZE_DEFAULT
    );
  }

  function getStageProfileProduct() {
    return STAGE_PROFILES[STATE.product.type || ""] || null;
  }

  function getStageFitKey() {
    const productProfiles = getStageProfileProduct();
    if (!productProfiles) return null;
    if (STATE.product.fit && productProfiles[STATE.product.fit]) return STATE.product.fit;
    return productProfiles.default_fit || Object.keys(productProfiles).find((key) => key !== "default_fit") || null;
  }

  function getStageProfile() {
    const productProfiles = getStageProfileProduct();
    const fitKey = getStageFitKey();
    if (!productProfiles || !fitKey) return null;
    const fitProfiles = productProfiles[fitKey] || {};
    return fitProfiles[STATE.ui.stage_view] || fitProfiles.front || null;
  }

  function getPlacementAnchor(placement) {
    const stageProfile = getStageProfile();
    if (!stageProfile) return null;
    const anchor = stageProfile.anchors?.[placement.placement_key];
    if (!anchor) return null;
    if (placement.placement_key === "front") {
      return {
        button: anchor.button,
        plate: anchor.presets?.[placement.size_preset || getFrontSizePreset()] || null,
      };
    }
    if (placement.placement_key === "back") {
      return {
        button: anchor.button,
        plate: anchor.presets?.[placement.size_preset || getBackSizePreset()] || null,
      };
    }
    if (placement.placement_key.startsWith("sleeve_")) {
      return {
        button: anchor.button,
        plate: anchor.modes?.[placement.mode || SLEEVE_MODE_DEFAULT] || null,
      };
    }
    return {
      button: anchor.button,
      plate: anchor.default || null,
    };
  }

  function ensureFrontZoneOptions() {
    if (!STATE.print.zone_options || typeof STATE.print.zone_options !== "object") {
      STATE.print.zone_options = {};
    }
    if (!STATE.print.zone_options.front || typeof STATE.print.zone_options.front !== "object") {
      STATE.print.zone_options.front = {};
    }
    if (!STATE.print.zone_options.front.size_preset) {
      STATE.print.zone_options.front.size_preset = FRONT_SIZE_DEFAULT;
    }
  }

  function ensureBackZoneOptions() {
    if (!STATE.print.zone_options || typeof STATE.print.zone_options !== "object") {
      STATE.print.zone_options = {};
    }
    if (!STATE.print.zone_options.back || typeof STATE.print.zone_options.back !== "object") {
      STATE.print.zone_options.back = {};
    }
    if (!STATE.print.zone_options.back.size_preset) {
      STATE.print.zone_options.back.size_preset = BACK_SIZE_DEFAULT;
    }
  }

  function ensureCustomZoneOptions() {
    if (!STATE.print.zone_options || typeof STATE.print.zone_options !== "object") STATE.print.zone_options = {};
    if (!STATE.print.zone_options.custom || typeof STATE.print.zone_options.custom !== "object") {
      STATE.print.zone_options.custom = {};
    }
    const custom = STATE.print.zone_options.custom;
    if (!CUSTOM_ZONE_PRESETS[custom.size_preset]) custom.size_preset = "A6";
    if (!CUSTOM_ZONE_LOCATIONS.some((item) => item.value === custom.location)) custom.location = "shoulder";
  }

  function ensureSleeveZoneOptions() {
    if (!STATE.print.zone_options || typeof STATE.print.zone_options !== "object") {
      STATE.print.zone_options = {};
    }
    if (!STATE.print.zone_options.sleeve || typeof STATE.print.zone_options.sleeve !== "object") {
      STATE.print.zone_options.sleeve = {};
    }
    const sleeve = STATE.print.zone_options.sleeve;
    if (typeof sleeve.left_enabled !== "boolean" && typeof sleeve.right_enabled !== "boolean") {
      sleeve.left_enabled = true;
      sleeve.right_enabled = false;
    }
    if (!sleeve.left_mode) sleeve.left_mode = SLEEVE_MODE_DEFAULT;
    if (!sleeve.right_mode) sleeve.right_mode = SLEEVE_MODE_DEFAULT;
    if (!sleeve.left_text) sleeve.left_text = "";
    if (!sleeve.right_text) sleeve.right_text = "";
  }

  function isSleeveSideEnabled(side) {
    ensureSleeveZoneOptions();
    return !!STATE.print.zone_options.sleeve?.[`${side}_enabled`];
  }

  function getSleeveMode(side) {
    ensureSleeveZoneOptions();
    return STATE.print.zone_options.sleeve?.[`${side}_mode`] || SLEEVE_MODE_DEFAULT;
  }

  function getSleeveText(side) {
    ensureSleeveZoneOptions();
    return STATE.print.zone_options.sleeve?.[`${side}_text`] || "";
  }

  function getExpandedPlacements() {
    const placements = [];
    (STATE.print.zones || []).forEach((zone) => {
      if (zone === "front") {
        placements.push({
          zone,
          placement_key: "front",
          label: (CONFIG.zone_labels && CONFIG.zone_labels.front) || "front",
          size_preset: getFrontSizePreset(),
          requires_artwork_file: true,
          scene_preview: getStagePreviewForZone("front"),
        });
        return;
      }
      if (zone === "back") {
        placements.push({
          zone,
          placement_key: "back",
          label: (CONFIG.zone_labels && CONFIG.zone_labels.back) || "back",
          size_preset: getBackSizePreset(),
          requires_artwork_file: true,
          scene_preview: getStagePreviewForZone("back"),
        });
        return;
      }
      if (zone === "custom") {
        ensureCustomZoneOptions();
        placements.push({
          zone,
          placement_key: "custom",
          label: (CONFIG.zone_labels && CONFIG.zone_labels.custom) || "Інша зона",
          size_preset: STATE.print.zone_options.custom.size_preset,
          location: STATE.print.zone_options.custom.location,
          placement_note: STATE.print.placement_note || "",
          requires_artwork_file: true,
          scene_preview: getStagePreviewForZone("custom"),
        });
        return;
      }
      if (zone === "sleeve") {
        ensureSleeveZoneOptions();
        ["left", "right"].forEach((side) => {
          if (!isSleeveSideEnabled(side)) return;
          placements.push({
            zone,
            placement_key: `sleeve_${side}`,
            label: (CONFIG.zone_labels && CONFIG.zone_labels[`sleeve_${side}`]) || `sleeve_${side}`,
            side,
            mode: getSleeveMode(side),
            text: getSleeveText(side),
            requires_artwork_file: getSleeveMode(side) !== "full_text",
            scene_preview: getStagePreviewForZone(`sleeve_${side}`),
          });
        });
        return;
      }
      placements.push({
        zone,
        placement_key: zone,
        label: (CONFIG.zone_labels && CONFIG.zone_labels[zone]) || zone,
        requires_artwork_file: true,
        scene_preview: getStagePreviewForZone(zone),
      });
    });
    return placements;
  }

  function normalizeClientState() {
    if (!STATE.print.zone_options || typeof STATE.print.zone_options !== "object") {
      STATE.print.zone_options = {};
    }

    if (STATE.product.type && !CONFIG.products?.[STATE.product.type]) {
      STATE.product.type = null;
    }

    const cfg = getProductConfig();
    const availableZones = cfg?.zones || [];
    const availableAddons = new Set((cfg?.add_ons || []).map((item) => item.value));
    const legacyAddons = new Map([["grommets", "lacing"], ["inside_label", "lacing"], ["hem_tag", "lacing"]]);

    STATE.print.zones = (STATE.print.zones || []).filter((zone) => availableZones.includes(zone));
    STATE.print.add_ons = (STATE.print.add_ons || [])
      .map((value) => legacyAddons.get(value) || value)
      .filter((value, index, list) => availableAddons.has(value) && list.indexOf(value) === index);

    Object.keys(STATE.print.zone_options).forEach((zone) => {
      if (!STATE.print.zones.includes(zone)) {
        delete STATE.print.zone_options[zone];
      }
    });

    if (STATE.print.zones.includes("front")) ensureFrontZoneOptions();
    else if (STATE.print.zone_options.front) delete STATE.print.zone_options.front;

    if (STATE.print.zones.includes("back")) ensureBackZoneOptions();
    else if (STATE.print.zone_options.back) delete STATE.print.zone_options.back;

    if (STATE.print.zones.includes("custom")) ensureCustomZoneOptions();
    else if (STATE.print.zone_options.custom) delete STATE.print.zone_options.custom;

    if (STATE.print.zones.includes("sleeve")) {
      ensureSleeveZoneOptions();
      if (!STATE.print.zone_options.sleeve.left_enabled && !STATE.print.zone_options.sleeve.right_enabled) {
        STATE.print.zone_options.sleeve.left_enabled = true;
      }
    } else if (STATE.print.zone_options.sleeve) {
      delete STATE.print.zone_options.sleeve;
    }

    const fitKey = STATE.product.fit || cfg?.default_fit || "";
    const fabricOptions = fitKey ? (cfg?.fabrics?.[fitKey] || []) : [];
    const availableFabricOptions = fabricOptions.filter((item) => item.available !== false);
    const includedFabric = availableFabricOptions.find((item) => item.included_in_base) || availableFabricOptions[0] || null;
    if (availableFabricOptions.length === 1 && includedFabric) {
      STATE.product.fabric = includedFabric.value;
    } else if (availableFabricOptions.length > 1 && !availableFabricOptions.some((item) => item.value === STATE.product.fabric)) {
      STATE.product.fabric = includedFabric?.value || null;
    } else if (!availableFabricOptions.length) {
      STATE.product.fabric = "";
    }

    if (STATE.print.zone_options.sleeve) {
      if (getSleeveMode("left") === "full_text") deletePlacementFiles("sleeve_left");
      if (getSleeveMode("right") === "full_text") deletePlacementFiles("sleeve_right");
      if (!isSleeveSideEnabled("left")) deletePlacementFiles("sleeve_left");
      if (!isSleeveSideEnabled("right")) deletePlacementFiles("sleeve_right");
    }

    const colorValues = new Set(getAllowedColorOptions(cfg).map((item) => item.value));
    if (cfg && (!STATE.product.color || !colorValues.has(STATE.product.color))) {
      STATE.product.color = cfg.default_color || getAllowedColorOptions(cfg)[0]?.value || null;
    }

    const currentIndex = Math.max(0, getStepIndex(STATE.ui.current_step));
    if (!(STATE.ui.done_steps instanceof Set)) {
      STATE.ui.done_steps = new Set(Array.isArray(STATE.ui.done_steps) ? STATE.ui.done_steps : []);
    }
    if (!STATE.mode) {
      STATE.ui.done_steps.clear();
      STATE.ui.current_step = "mode";
    } else if (!STATE.ui.done_steps.size && currentIndex > 0) {
      STEPS.slice(0, currentIndex).forEach((step) => STATE.ui.done_steps.add(step));
    }
  }

  function getAvailableZones() {
    return getProductConfig()?.zones || [];
  }

  function getStagePreviewForZone(zone) {
    const customSize = zone === "custom" ? (STATE.print.zone_options?.custom?.size_preset || "A6") : "";
    return {
      product_type: STATE.product.type || "",
      fit: getStageFitKey() || "",
      view: STATE.ui.stage_view || "front",
      color: STATE.product.color || "",
      placement_key: zone,
      size_preset: zone === "front" ? getFrontSizePreset() : zone === "back" ? getBackSizePreset() : customSize,
      mode: zone.startsWith("sleeve_") ? getSleeveMode(zone.endsWith("left") ? "left" : "right") : "",
    };
  }

  function buildZoneOptionsSnapshot() {
    const zoneOptions = {};
    (STATE.print.zones || []).forEach((zone) => {
      const current = { ...(STATE.print.zone_options?.[zone] || {}) };
      if (zone === "front") {
        current.size_preset = getFrontSizePreset();
        current.scene_preview = getStagePreviewForZone("front");
      }
      if (zone === "back") {
        current.size_preset = getBackSizePreset();
        current.scene_preview = getStagePreviewForZone("back");
      }
      if (zone === "custom") {
        ensureCustomZoneOptions();
        current.size_preset = STATE.print.zone_options.custom.size_preset;
        current.location = STATE.print.zone_options.custom.location;
        current.scene_preview = getStagePreviewForZone("custom");
      }
      if (zone === "sleeve") {
        ensureSleeveZoneOptions();
        current.left_enabled = isSleeveSideEnabled("left");
        current.right_enabled = isSleeveSideEnabled("right");
        current.left_mode = getSleeveMode("left");
        current.right_mode = getSleeveMode("right");
        current.left_text = getSleeveText("left");
        current.right_text = getSleeveText("right");
        if (current.left_enabled) current.left_scene_preview = getStagePreviewForZone("sleeve_left");
        if (current.right_enabled) current.right_scene_preview = getStagePreviewForZone("sleeve_right");
      }
      zoneOptions[zone] = current;
    });
    return zoneOptions;
  }

  function collectOrderedFiles() {
    const ordered = [];
    getExpandedPlacements()
      .filter((placement) => placement.requires_artwork_file)
      .forEach((placement) => {
        (filesByPlacement.get(placement.placement_key) || []).forEach((file) => {
          ordered.push({
            zone: placement.zone,
            placement_key: placement.placement_key,
            label: placement.label,
            file,
          });
        });
      });
    return ordered;
  }

  function deletePlacementFiles(placementKey) {
    filesByPlacement.delete(placementKey);
  }

  // Превʼю макета прямо на виробі (перший растровий файл placement-а)
  function getPlacementArtUrl(placementKey) {
    const files = filesByPlacement.get(placementKey) || [];
    const file = files.find((item) => /^image\/(png|jpe?g|webp|gif|svg\+xml)$/i.test(item.type || ""));
    const cached = plateArtCache.get(placementKey);
    if (!file) {
      if (cached) {
        URL.revokeObjectURL(cached.url);
        plateArtCache.delete(placementKey);
      }
      return "";
    }
    if (cached && cached.file === file) return cached.url;
    if (cached) URL.revokeObjectURL(cached.url);
    const url = URL.createObjectURL(file);
    plateArtCache.set(placementKey, { file, url });
    return url;
  }

  function deleteZoneFiles(zone) {
    if (zone === "sleeve") {
      deletePlacementFiles("sleeve_left");
      deletePlacementFiles("sleeve_right");
      return;
    }
    if (zone) {
      deletePlacementFiles(zone);
    }
  }

  function getRequiredArtworkPlacements() {
    return getExpandedPlacements().filter((placement) => placement.requires_artwork_file);
  }

  function getArtworkValidationIssues() {
    const issues = [];
    const serviceKind = STATE.artwork.service_kind;
    if (!serviceKind) {
      issues.push(ui("artwork_service_required", "Оберіть сценарій роботи з макетом."));
    }
    if (serviceKind === "design" && !STATE.notes.brief.trim()) {
      issues.push(ui("artwork_brief_design_required", "Опишіть бриф / завдання для дизайну."));
    }
    if (serviceKind === "adjust") {
      if (!STATE.notes.brief.trim()) {
        issues.push(ui("artwork_brief_adjust_required", "Опишіть, що саме потрібно змінити у файлі."));
      }
      const missingPlacements = getRequiredArtworkPlacements()
        .filter((placement) => !(filesByPlacement.get(placement.placement_key) || []).length)
        .map((placement) => placement.label);
      if (missingPlacements.length) {
        issues.push(`${ui("artwork_file_required", "Додайте макет для кожної вибраної зони.")} ${missingPlacements.join(", ")}.`);
      }
    }
    if (serviceKind === "ready") {
      const missingPlacements = getRequiredArtworkPlacements()
        .filter((placement) => !(filesByPlacement.get(placement.placement_key) || []).length)
        .map((placement) => placement.label);
      if (missingPlacements.length) {
        issues.push(`${ui("artwork_file_required", "Додайте макет для кожної вибраної зони.")} ${missingPlacements.join(", ")}.`);
      }
    }
    return issues;
  }

  // ── Renderers ───────────────────────────────────────────────
  function resetConfigurationForModeChange() {
    STATE.product = { type: null, fit: null, fabric: null, color: null };
    STATE.print = { zones: [], add_ons: [], placement_note: "", zone_options: {} };
    STATE.artwork = { service_kind: null, triage_status: null };
    STATE.order = {
      quantity: 0,
      size_mode: "single",
      sizes_note: "",
      size_breakdown: {},
      delivery_method: "",
      gift_enabled: false,
      gift_text: "",
    };
    STATE.notes = {
      brand_name: "",
      brand_contact_person: "",
      brand_contact_channel: "",
      brand_contact_value: "",
      brand_business_type: "brand",
      brand_product_types: [],
      brief: "",
      garment_note: "",
      garment_color_hex: "#151515",
      brand_resource: "",
      brand_phone: "",
      brand_deadline: "",
      brand_wish: "",
      garment_photo_name: "",
    };
    STATE.ui.done_steps = new Set();
    STATE.ui.current_step = "mode";
    filesByPlacement.clear();
    garmentPhotoFile = null;
    [dom.brandIntroName, dom.brandContactPerson, dom.brandContactValue, dom.brandResource, dom.brandPhone, dom.brandDeadline, dom.brandWish, dom.qtyInput, dom.ownColorInput, dom.garmentNoteInput].forEach((input) => {
      if (input) input.value = input.type === "color" ? "#151515" : "";
    });
    if (dom.ownColorValue) dom.ownColorValue.textContent = "#151515";
    if (dom.ownColorHue) dom.ownColorHue.value = "0";
  }

  function renderModeChips() {
    const container = dom.modeList;
    if (!container) return;
    container.querySelectorAll("[data-choice-value]").forEach((btn) => {
      btn.addEventListener("click", () => {
        const previousMode = STATE.mode;
        if (previousMode && previousMode !== btn.dataset.choiceValue) {
          resetConfigurationForModeChange();
        }
        STATE.mode = btn.dataset.choiceValue;
        normalizeClientState();
        updateBrandFieldsVisibility();
        if (STATE.mode === "brand") {
          setActiveStep("mode", { silent: true });
          refreshAll();
          persistDraft();
          return;
        }
        afterChoice("mode");
      });
    });
  }

  function bindBrandBriefNavigation() {
    dom.brandContinue?.addEventListener("click", () => {
      const name = (STATE.notes.brand_name || dom.brandIntroName?.value || "").trim();
      const person = (STATE.notes.brand_contact_person || dom.brandContactPerson?.value || "").trim();
      const contact = (STATE.notes.brand_contact_value || dom.brandContactValue?.value || "").trim();
      const channel = STATE.notes.brand_contact_channel || "";
      if (!name || !person || !channel || !contact) {
        showStatus("Заповніть назву, контактну особу, канал і контакт.", "warning");
        const target = !name ? dom.brandIntroName : !person ? dom.brandContactPerson : !channel ? dom.brandContactChannelList : dom.brandContactValue;
        target?.focus?.({ preventScroll: true });
        scrollToStudioTarget(target || dom.brandBrief, { focus: false });
        return;
      }
      STATE.contact.name = person;
      STATE.contact.channel = channel;
      STATE.contact.value = contact;
      STATE.notes.brief = buildBrandBriefText();
      persistDraft();
      handleSafeExit();
      showStatus("Бриф збережено. Менеджер звʼяжеться з вами незалежно від Telegram.", "success");
    });
  }

  function buildBrandBriefText() {
    const productLabels = { tshirt: "Футболки", hoodie: "Худі", longsleeve: "Лонгсліви", customer_garment: "Свій одяг", all: "Ще не визначились" };
    const selectedProducts = (STATE.notes.brand_product_types || []).map((value) => productLabels[value] || value).join(", ") || "Не вказано";
    const lines = [
      "B2B-бриф",
      `Бренд / компанія: ${STATE.notes.brand_name || "—"}`,
      `Контактна особа: ${STATE.notes.brand_contact_person || "—"}`,
      `Канал: ${STATE.notes.brand_contact_channel || "—"} · ${STATE.notes.brand_contact_value || "—"}`,
      `Формат: ${STATE.notes.brand_business_type || "—"}`,
      `Вироби: ${selectedProducts}`,
      `Орієнтовний тираж: ${STATE.order.quantity || "—"}`,
      `Ресурс: ${STATE.notes.brand_resource || "—"}`,
      `Дедлайн: ${STATE.notes.brand_deadline || "—"}`,
      `Побажання: ${STATE.notes.brand_wish || "—"}`,
    ];
    return lines.join("\n");
  }

  function renderModeChipsActive() {
    dom.modeList?.querySelectorAll("[data-choice-value]").forEach((btn) => {
      btn.classList.toggle("is-active", btn.dataset.choiceValue === STATE.mode);
    });
  }

  function renderProductCards() {
    if (!dom.productList) return;
    dom.productList.querySelectorAll("[data-choice-value]").forEach((btn) => {
      btn.addEventListener("click", () => {
        const value = btn.dataset.choiceValue;
        const previousType = STATE.product.type;
        STATE.product.type = value;
        STATE.product.fit = null;
        STATE.product.fabric = null;
        STATE.product.color = null;
        STATE.print.zones = [];
        STATE.print.add_ons = [];
        STATE.print.zone_options = {};
        STATE.print.placement_note = "";
        STATE.artwork.service_kind = null;
        STATE.artwork.triage_status = null;
        invalidateAfter("product");
        filesByPlacement.clear();
        if (previousType !== value) {
          STATE.order.quantity = 0;
          STATE.order.size_mode = "single";
          STATE.order.size_breakdown = {};
          STATE.order.sizes_note = "";
          STATE.order.delivery_method = "";
          STATE.notes.garment_note = "";
          STATE.notes.garment_color_hex = "#151515";
          STATE.notes.garment_photo_name = "";
          garmentPhotoFile = null;
        }
        normalizeClientState();
        renderFitChips();
        renderFabricChips();
        renderColorChips();
        renderAddons();
        renderZoneChipsForCurrent();
        renderFrontSizeOptions();
        renderBackSizeOptions();
        renderSleeveControls();
        afterChoice("product");
      });
    });
  }

  function renderProductCardsActive() {
    dom.productList?.querySelectorAll("[data-choice-value]").forEach((btn) => {
      btn.classList.toggle("is-active", btn.dataset.choiceValue === STATE.product.type);
    });
  }

  function renderArtworkCardModifiers() {
    if (!dom.artworkList) return;
    dom.artworkList.querySelectorAll("[data-choice-value]").forEach((btn) => {
      btn.addEventListener("click", () => {
        STATE.artwork.service_kind = btn.dataset.choiceValue;
        if (STATE.artwork.service_kind === "ready") STATE.artwork.triage_status = "print-ready";
        else if (STATE.artwork.service_kind === "adjust") STATE.artwork.triage_status = "needs-work";
        else STATE.artwork.triage_status = "reference-only";
        renderArtworkActiveState();
        renderDropzones();
        refreshAll();
        persistDraft();
      });
    });
  }

  function renderContactChannelChips() {
    if (!dom.contactChannelList) return;
    dom.contactChannelList.innerHTML = "";
    (CONFIG.contact_channels || []).forEach((ch) => {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "cp-contact-channel";
      btn.dataset.choiceValue = ch.value;
      btn.innerHTML = `${contactChannelIcon(ch.value)}<span>${escapeHtml(ch.label)}</span>`;
      btn.setAttribute("aria-label", ch.label);
      btn.addEventListener("click", () => {
        STATE.contact.channel = ch.value;
        if (dom.contactValueInput) dom.contactValueInput.placeholder = ch.placeholder || "@username або +380...";
        renderContactChannelChipsActive();
        refreshAll();
        persistDraft();
      });
      dom.contactChannelList.appendChild(btn);
    });
  }

  function contactChannelIcon(value) {
    if (value === "telegram") return '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="m21 4-3.2 15.1c-.2 1.1-.8 1.4-1.6.9l-4.4-3.3-2.1 2c-.2.2-.4.4-.8.4l.3-4.5 8.2-7.4c.4-.4-.1-.6-.6-.2L6.3 13 2 11.6c-.9-.3-.9-.9.2-1.3L19.4 3.8c.8-.3 1.8-.1 1.6.2Z" fill="currentColor"/></svg>';
    if (value === "whatsapp") return '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M20 11.6a8 8 0 0 1-11.7 7.1L4 20l1.4-4.1A8 8 0 1 1 20 11.6Z" fill="none" stroke="currentColor" stroke-width="1.7"/><path d="M9 8.2c.2-.4.4-.4.7-.4h.5c.2 0 .4.1.5.4l.7 1.6c.1.2 0 .4-.1.6l-.5.6c.5 1 1.3 1.8 2.4 2.3l.7-.5c.2-.1.4-.1.6 0l1.5.7c.2.1.3.3.2.6-.2.8-.8 1.3-1.6 1.4-2.1.1-5.1-2.4-5.9-4.3-.3-.8-.2-2 .3-3Z" fill="currentColor"/></svg>';
    return '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M7.5 4.5h2l1 4-1.5 1.5a12 12 0 0 0 5 5l1.5-1.5 4 1v2c0 1.1-.9 2-2 2C11.1 18.5 5.5 12.9 5.5 6.5c0-1.1.9-2 2-2Z" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linejoin="round"/></svg>';
  }

  function renderContactChannelChipsActive() {
    if (!dom.contactChannelList) return;
    dom.contactChannelList.querySelectorAll("[data-choice-value]").forEach((btn) => {
      btn.classList.toggle("is-active", btn.dataset.choiceValue === STATE.contact.channel);
    });
  }

  function getProductConfig() {
    if (!STATE.product.type) return null;
    return (CONFIG.products || {})[STATE.product.type] || null;
  }

  function renderProductDetailNote() {
    const cfg = getProductConfig();
    if (dom.productDetailTitle) dom.productDetailTitle.textContent = cfg?.detail_title || "Деталі виробу";
    if (dom.productDetailCopy) dom.productDetailCopy.textContent = cfg?.detail_note || "Опис оновиться після вибору виробу.";
    if (dom.ownGarmentOptions) dom.ownGarmentOptions.hidden = STATE.product.type !== "customer_garment";
    if (dom.standardColorBlock) dom.standardColorBlock.hidden = STATE.product.type === "customer_garment";
    renderOwnGarmentControls();
    if (STATE.product.type !== "customer_garment") return;
    if (!dom.ownShippingList) return;
    dom.ownShippingList.innerHTML = "";
    (cfg.shipping_methods || []).forEach((method) => {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "cp-shipping-choice";
      button.classList.toggle("is-active", STATE.order.delivery_method === method.value);
      button.dataset.choiceValue = method.value;
      button.setAttribute("aria-pressed", String(STATE.order.delivery_method === method.value));
      button.innerHTML = `<strong>${escapeHtml(method.label)}</strong><small>${escapeHtml(method.hint || "")}</small>`;
      button.addEventListener("click", () => {
        STATE.order.delivery_method = method.value;
        renderProductDetailNote();
        refreshAll();
        persistDraft();
      });
      dom.ownShippingList.appendChild(button);
    });
  }

  function renderOwnGarmentControls() {
    const visible = STATE.product.type === "customer_garment";
    applyOwnGarmentStageColor();
    if (!visible || !dom.ownColorInput) return;
    const hex = /^#[0-9a-f]{6}$/i.test(STATE.notes.garment_color_hex || "") ? STATE.notes.garment_color_hex : "#151515";
    dom.ownColorInput.value = hex;
    if (dom.ownColorValue) dom.ownColorValue.textContent = hex.toUpperCase();
    if (dom.ownColorHue) dom.ownColorHue.value = String(hexToHue(hex));
  }

  function bindOwnGarmentControls() {
    dom.ownColorInput?.addEventListener("input", () => {
      STATE.notes.garment_color_hex = dom.ownColorInput.value;
      if (dom.ownColorValue) dom.ownColorValue.textContent = dom.ownColorInput.value.toUpperCase();
      if (dom.ownColorHue) dom.ownColorHue.value = String(hexToHue(dom.ownColorInput.value));
      refreshAll();
      persistDraft();
    });
    dom.ownColorHue?.addEventListener("input", () => {
      const hex = hslToHex(Number(dom.ownColorHue.value) || 0, 52, 34);
      STATE.notes.garment_color_hex = hex;
      if (dom.ownColorInput) dom.ownColorInput.value = hex;
      if (dom.ownColorValue) dom.ownColorValue.textContent = hex.toUpperCase();
      refreshAll();
      persistDraft();
    });
  }

  function hexToHue(hex) {
    const value = String(hex || "").replace("#", "");
    if (value.length !== 6) return 0;
    const r = parseInt(value.slice(0, 2), 16) / 255;
    const g = parseInt(value.slice(2, 4), 16) / 255;
    const b = parseInt(value.slice(4, 6), 16) / 255;
    const max = Math.max(r, g, b), min = Math.min(r, g, b), delta = max - min;
    if (!delta) return 0;
    let hue = 0;
    if (max === r) hue = ((g - b) / delta) % 6;
    else if (max === g) hue = (b - r) / delta + 2;
    else hue = (r - g) / delta + 4;
    return Math.round((hue * 60 + 360) % 360);
  }

  function hslToHex(h, s, l) {
    const saturation = s / 100;
    const lightness = l / 100;
    const chroma = (1 - Math.abs(2 * lightness - 1)) * saturation;
    const x = chroma * (1 - Math.abs((h / 60) % 2 - 1));
    const m = lightness - chroma / 2;
    let r = 0, g = 0, b = 0;
    if (h < 60) [r, g, b] = [chroma, x, 0];
    else if (h < 120) [r, g, b] = [x, chroma, 0];
    else if (h < 180) [r, g, b] = [0, chroma, x];
    else if (h < 240) [r, g, b] = [0, x, chroma];
    else if (h < 300) [r, g, b] = [x, 0, chroma];
    else [r, g, b] = [chroma, 0, x];
    return `#${[r, g, b].map((value) => Math.round((value + m) * 255).toString(16).padStart(2, "0")).join("")}`;
  }

  function getSelectedFabricConfig() {
    const cfg = getProductConfig();
    if (!cfg || !cfg.fabrics || !STATE.product.fit) return null;
    return ((cfg.fabrics[STATE.product.fit] || []).find((item) => item.value === STATE.product.fabric)) || null;
  }

  function getAllowedColorOptions(cfg = getProductConfig()) {
    if (!cfg) return [];
    let colors = cfg.fit_colors?.[STATE.product.fit] || cfg.colors || [];
    const fabric = (cfg.fabrics?.[STATE.product.fit] || []).find((item) => item.value === STATE.product.fabric);
    if (fabric?.colors?.length) colors = fabric.colors;
    return colors;
  }

  function renderFitChips() {
    if (!dom.fitList) return;
    dom.fitList.innerHTML = "";
    const cfg = getProductConfig();
    const fits = cfg && cfg.fits ? cfg.fits : [];
    const fabricsMap = cfg && cfg.fabrics ? cfg.fabrics : {};
    dom.fitList.dataset.count = String(fits.length);
    if (!fits.length) {
      if (dom.fitBlock) dom.fitBlock.hidden = true;
      return;
    }
    if (dom.fitBlock) dom.fitBlock.hidden = false;
    fits.forEach((f) => {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "cp-fit-card";
      if (STATE.product.fit === f.value) btn.classList.add("is-active");
      btn.dataset.choiceValue = f.value;
      const fabricOptions = fabricsMap[f.value] || [];
      const helperNote = f.value === "oversize" && cfg?.label === "Худі"
        ? "Преміум-тканина фіксується автоматично"
        : fabricOptions.length > 1
          ? "Нижче зʼявляться доступні тканини і ціни"
          : "Тканина фіксується автоматично";
      const selectorAssets = {
        "tshirt:regular": "ui/tshirt-regular.png",
        "tshirt:oversize": "ui/tshirt-oversize.png",
        "hoodie:regular": "ui/hoodie-regular.png",
        "hoodie:oversize": "ui/hoodie-oversize.png",
      };
      const fitAsset = selectorAssets[`${STATE.product.type}:${f.value}`]
        || "studio/longsleeve-front.png";
      btn.innerHTML = `
        <div class="cp-fit-card-figure" style="display: ${'block'}">
          <img src="/static/img/configurator/${fitAsset}" alt="${f.label}" onerror="this.parentElement.style.display='none'">
        </div>
        <div class="cp-fit-card-info">
          <small>Посадка</small>
          <strong>${f.label}</strong>
          <span>${f.description || ""}</span>
          <em>${helperNote}</em>
        </div>
      `;
      btn.addEventListener("click", () => {
        const previousFit = STATE.product.fit;
        STATE.product.fit = f.value;
        STATE.product.fabric = STATE.product.type === "hoodie" && f.value === "oversize" ? "premium" : null;
        if (previousFit !== f.value) {
          STATE.order.quantity = 0;
          STATE.order.size_mode = "single";
          STATE.print.zones = [];
          STATE.print.zone_options = {};
          STATE.artwork.service_kind = null;
          STATE.artwork.triage_status = null;
          STATE.notes.brief = "";
          filesByPlacement.clear();
          STATE.order.size_breakdown = {};
          invalidateAfter("config");
        }
        renderFabricChips();
        renderColorChips();
        renderFitChips();
        refreshAll();
        persistDraft();
      });
      dom.fitList.appendChild(btn);
    });
  }

  function renderFabricChips() {
    if (!dom.fabricList) return;
    dom.fabricList.innerHTML = "";
    const cfg = getProductConfig();
    const fabricsMap = cfg && cfg.fabrics ? cfg.fabrics : {};
    const fabrics = STATE.product.fit ? fabricsMap[STATE.product.fit] || [] : [];
    dom.fabricList.dataset.count = String(fabrics.length);
    if (!fabrics.length) {
      if (dom.fabricBlock) dom.fabricBlock.hidden = true;
      return;
    }
    if (dom.fabricBlock) dom.fabricBlock.hidden = false;
    const availableFabrics = fabrics.filter((item) => item.available !== false);
    const baseFabric = availableFabrics.find((item) => item.included_in_base) || availableFabrics[0];
    if (!STATE.product.fabric || !availableFabrics.some((item) => item.value === STATE.product.fabric)) {
      STATE.product.fabric = baseFabric?.value || null;
    }
    const isLocked = availableFabrics.length === 1;
    fabrics.forEach((fab) => {
      const option = document.createElement("div");
      option.className = "cp-fabric-option";
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "cp-mini-chip cp-mini-chip--fabric";
      if (STATE.product.fabric === fab.value) btn.classList.add("is-active");
      if (isLocked) btn.classList.add("is-locked", "is-single");
      if (fab.value === "premium") btn.classList.add("is-premium");
      if (fab.included_in_base) btn.classList.add("is-included");
      const isUnavailable = fab.available === false;
      if (isLocked || isUnavailable) {
        btn.disabled = true;
        btn.setAttribute("aria-disabled", "true");
      }
      btn.dataset.choiceValue = fab.value;
      const priceDelta = Number(fab.price_delta || 0);
      const priceLabel = isUnavailable
        ? "тимчасово недоступна"
        : fab.included_in_base
        ? "входить у базу"
        : priceDelta > 0
          ? `+${priceDelta} грн`
          : "без доплати";

      let btnContent = `
        <span class="cp-fabric-chip-title">
          <strong>${escapeHtml(fab.label)}</strong>
          ${fab.value === "thermo" ? `<span class="cp-thermo-badge" aria-label="Термо: змінює колір від тепла"><svg viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="M13.5 3.5c.2 3.4-2.8 4.4-2.8 7.1 0 1.2.8 2.2 1.8 2.7-.1-1.5.7-2.5 2-3.3 1.3 1.4 2.1 2.7 2.1 4.4a4.8 4.8 0 1 1-9.6 0c0-2.4 1.2-4.1 2.8-5.8-.1 2.1.5 3.3 1.5 4.2-.2-3.3 1.2-6.3 2.2-9.3Z" fill="currentColor"/></svg><span>WOW</span></span>` : ""}
        </span>

        <span class="cp-fabric-chip-meta">${escapeHtml(priceLabel)}</span>
        ${fab.short_desc ? `<span class="cp-fabric-chip-hint">${escapeHtml(fab.short_desc)}</span>` : ""}
      `;
      
      btn.innerHTML = `<span class="cp-fabric-chip-content">${btnContent}</span>`;
      
      btn.addEventListener("click", () => {
        if (isLocked || isUnavailable) return;
        const previousFabric = STATE.product.fabric;
        STATE.product.fabric = fab.value;
        if (previousFabric !== fab.value) {
          STATE.order.quantity = 0;
          STATE.order.size_mode = "single";
          STATE.order.size_breakdown = {};
          STATE.artwork.service_kind = null;
          STATE.artwork.triage_status = null;
          STATE.notes.brief = "";
          filesByPlacement.clear();
          invalidateAfter("config");
        }
        renderFabricChips();
        renderColorChips();
        refreshAll();
        persistDraft();
      });
      option.appendChild(btn);

      if (fab.info_title) {
        const trigger = document.createElement("button");
        trigger.type = "button";
        trigger.className = "cp-fabric-info-trigger";
        trigger.setAttribute("aria-label", `Відкрити опис матеріалу: ${fab.label || "матеріал"}`);
        trigger.innerHTML = `<svg viewBox="0 0 24 24" fill="none" aria-hidden="true"><circle cx="12" cy="12" r="9" stroke="currentColor" stroke-width="1.8"/><path d="M12 10.5v5" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/><circle cx="12" cy="7.5" r="1" fill="currentColor"/></svg>`;
        trigger.addEventListener('click', (e) => {
          e.preventDefault();
          e.stopPropagation();
          showFabricInfoModal(fab);
        });
        option.appendChild(trigger);
      }
      dom.fabricList.appendChild(option);
    });
  }

  function showFabricInfoModal(fab) {
    let modal = document.getElementById("cp-fabric-info-modal");
    const previousFocus = document.activeElement;
    if (!modal) {
      modal = document.createElement("div");
      modal.id = "cp-fabric-info-modal";
      modal.className = "cp-fabric-modal-overlay";
      modal.setAttribute("role", "dialog");
      modal.setAttribute("aria-modal", "true");
      modal.innerHTML = `
        <div class="cp-fabric-modal-box">
          <button type="button" class="cp-fabric-modal-close" aria-label="Закрити опис матеріалу">&times;</button>
          <div class="cp-fabric-modal-media">
             <!-- Placeholder for reference image -->
             <img src="/static/img/configurator/ui/thermo-preview.png" alt="Thermo Effect" onerror="this.style.display='none'">
          </div>
          <h3 class="cp-fabric-modal-title" id="cp-fabric-info-title"></h3>
          <p class="cp-fabric-modal-desc"></p>
        </div>
      `;
      modal.setAttribute("aria-labelledby", "cp-fabric-info-title");
      document.body.appendChild(modal);
      modal._previousFocus = previousFocus;
      modal.querySelector(".cp-fabric-modal-close").addEventListener("click", () => {
        modal.classList.remove("is-visible");
        modal._previousFocus?.focus?.({ preventScroll: true });
      });
      modal.addEventListener("click", (e) => {
        if (e.target === modal) {
          modal.classList.remove("is-visible");
          modal._previousFocus?.focus?.({ preventScroll: true });
        }
      });
      document.addEventListener("keydown", (event) => {
        if (event.key === "Tab" && modal.classList.contains("is-visible")) {
          const focusable = Array.from(modal.querySelectorAll("button, a, input, textarea, select, [tabindex]:not([tabindex='-1'])")).filter((el) => !el.disabled && el.offsetParent !== null);
          if (focusable.length) {
            const first = focusable[0];
            const last = focusable[focusable.length - 1];
            if (event.shiftKey && document.activeElement === first) {
              event.preventDefault();
              last.focus();
            } else if (!event.shiftKey && document.activeElement === last) {
              event.preventDefault();
              first.focus();
            }
          }
        }
        if (event.key === "Escape" && modal.classList.contains("is-visible")) {
          modal.classList.remove("is-visible");
          modal._previousFocus?.focus?.({ preventScroll: true });
        }
      });
    }
    modal._previousFocus = previousFocus;
    
    const media = modal.querySelector(".cp-fabric-modal-media");
    const mediaImage = media?.querySelector("img");
    const previewImage = fab.preview_image || "";
    if (media && mediaImage) {
      media.hidden = !previewImage;
      if (previewImage) {
        mediaImage.src = previewImage;
        mediaImage.alt = fab.info_title || fab.label || "Fabric preview";
      } else {
        mediaImage.removeAttribute("src");
        mediaImage.alt = "";
      }
    }

    modal.querySelector(".cp-fabric-modal-title").textContent = fab.info_title;
    modal.querySelector(".cp-fabric-modal-desc").innerHTML = String(fab.info_desc || "").replace(/\r?\n|\\n/g, "<br>");
    
    modal.className = "cp-fabric-modal-overlay is-visible";
    if (fab.info_theme) {
      modal.classList.add(`is-${fab.info_theme}-theme`);
    } else if (fab.value === "thermo") {
      modal.classList.add("is-thermo-theme");
    }
    modal.querySelector(".cp-fabric-modal-close")?.focus({ preventScroll: true });
  }

  function renderColorChips() {
    if (!dom.colorList) return;
    dom.colorList.innerHTML = "";
    const cfg = getProductConfig();
    const colors = getAllowedColorOptions(cfg);

    // Force color update if current isn't in scope
    if (colors.length > 0 && !colors.some(c => c.value === STATE.product.color)) {
      STATE.product.color = colors[0].value;
    }

    colors.forEach((c) => {
      const isThermo = STATE.product.fabric === "thermo";
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "cp-swatch";
      if (isThermo) btn.classList.add("cp-swatch--thermo");
      btn.dataset.choiceValue = c.value;
      btn.style.setProperty("--swatch", c.hex);
      btn.setAttribute("aria-label", isThermo ? `${ui("thermo_fabric", "Термохромна тканина")}: ${c.label}` : c.label);
      if (STATE.product.color === c.value) btn.classList.add("is-active");
      btn.innerHTML = `${isThermo ? `<svg class="cp-swatch-thermo-icon" viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="M13.5 3.5c.2 3.4-2.8 4.4-2.8 7.1 0 1.2.8 2.2 1.8 2.7-.1-1.5.7-2.5 2-3.3 1.3 1.4 2.1 2.7 2.1 4.4a4.8 4.8 0 1 1-9.6 0c0-2.4 1.2-4.1 2.8-5.8-.1 2.1.5 3.3 1.5 4.2-.2-3.3 1.2-6.3 2.2-9.3Z" fill="currentColor"/></svg>` : ""}<span>${c.label}</span>`;
      btn.addEventListener("click", () => {
        STATE.product.color = c.value;
        renderColorChips();
        refreshAll();
        persistDraft();
      });
      dom.colorList.appendChild(btn);
    });
  }

  function renderZoneChipsForCurrent() {
    if (!dom.zoneList) return;
    dom.zoneList.innerHTML = "";
    const cfg = getProductConfig();
    const zones = cfg && cfg.zones ? cfg.zones : [];
    if (!zones.length) {
      dom.zoneList.innerHTML = `<small class="cp-empty-hint">Спочатку оберіть виріб.</small>`;
      return;
    }
    zones.forEach((z) => {
      const isActive = STATE.print.zones.includes(z);
      const meta = getZoneCardMeta(z);
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "cp-zone-card";
      if (isActive) btn.classList.add("is-active");
      btn.dataset.choiceValue = z;
      btn.setAttribute("aria-pressed", isActive ? "true" : "false");
      btn.innerHTML = `
        <span class="cp-zone-card-check" aria-hidden="true">
          <svg viewBox="0 0 16 16" width="14" height="14">
            <path d="M3 8.5l3 3 7-8" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"/>
          </svg>
        </span>
        <span class="cp-zone-card-icon" aria-hidden="true">${meta.icon}</span>
        <strong class="cp-zone-card-title">${escapeHtml(meta.title)}</strong>
        <small class="cp-zone-card-hint">${escapeHtml(meta.hint)}</small>
        ${meta.badge ? `<span class="cp-zone-card-badge">${escapeHtml(meta.badge)}</span>` : ""}
      `;
      btn.addEventListener("click", () => {
        toggleZone(z);
      });
      dom.zoneList.appendChild(btn);
    });
    renderCustomZoneOptions();
    renderFrontSizeOptions();
    renderBackSizeOptions();
    renderSleeveControls();
  }

  function renderCustomZoneOptions() {
    const active = STATE.print.zones.includes("custom");
    if (dom.customZoneWrap) dom.customZoneWrap.hidden = !active;
    if (!active) return;
    ensureCustomZoneOptions();
    if (dom.placementNoteInput) dom.placementNoteInput.value = STATE.print.placement_note || "";
    if (dom.customLocationList) {
      dom.customLocationList.innerHTML = CUSTOM_ZONE_LOCATIONS.map((item) => `
        <button type="button" class="cp-custom-location ${STATE.print.zone_options.custom.location === item.value ? "is-active" : ""}" data-custom-location="${item.value}">
          <strong>${escapeHtml(item.label)}</strong><small>${escapeHtml(item.hint)}</small>
        </button>`).join("");
      dom.customLocationList.querySelectorAll("[data-custom-location]").forEach((button) => {
        button.addEventListener("click", () => {
          ensureCustomZoneOptions();
          STATE.print.zone_options.custom.location = button.dataset.customLocation;
          renderCustomZoneOptions();
          refreshAll();
          persistDraft();
        });
      });
    }
    if (dom.customSizeList) {
      dom.customSizeList.innerHTML = Object.values(CUSTOM_ZONE_PRESETS).map((preset) => `
        <button type="button" class="cp-size-preset cp-custom-size-preset ${STATE.print.zone_options.custom.size_preset === preset.value ? "is-active" : ""}" data-custom-size="${preset.value}">
          <span class="cp-size-icon"><img src="/static/img/configurator/ui/size-${String(preset.value).toLowerCase()}.svg" alt="${preset.label}" onerror="this.src='/static/img/configurator/ui/size-a4.svg'"></span>
          <span class="cp-size-details"><strong>${preset.label}</strong><span>${escapeHtml(preset.range_label || "")}</span><em>+${Number(preset.price_delta || 0)} грн</em></span>
        </button>`).join("");
      dom.customSizeList.querySelectorAll("[data-custom-size]").forEach((button) => {
        button.addEventListener("click", () => {
          ensureCustomZoneOptions();
          STATE.print.zone_options.custom.size_preset = button.dataset.customSize;
          renderCustomZoneOptions();
          refreshAll();
          persistDraft();
        });
      });
    }
  }

  function getZoneCardMeta(zone) {
    const labels = (CONFIG.zone_labels || {});
    const fallbackLabel = labels[zone] || zone;
    const meta = {
      front: {
        title: labels.front || "Спереду",
        hint: "Класичне розміщення головного принта",
        icon: '<svg viewBox="0 0 32 32" fill="none" xmlns="http://www.w3.org/2000/svg" width="22" height="22" aria-hidden="true"><path d="M9 7l3-2h8l3 2 3 2v6l-3-1v14H9V14l-3 1V9l3-2z" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round"/><rect x="13" y="13" width="6" height="8" rx="1" stroke="currentColor" stroke-width="1.4" fill="none"/></svg>',
      },
      back: {
        title: labels.back || "Спина",
        hint: "Більший формат для сильного візуалу",
        icon: '<svg viewBox="0 0 32 32" fill="none" xmlns="http://www.w3.org/2000/svg" width="22" height="22" aria-hidden="true"><path d="M9 7l3-2h8l3 2 3 2v6l-3-1v14H9V14l-3 1V9l3-2z" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round"/><rect x="11.5" y="11" width="9" height="13" rx="1.6" stroke="currentColor" stroke-width="1.4" fill="none"/></svg>',
      },
      kangaroo: {
        title: labels.kangaroo || "Кенгуряча кишеня",
        hint: "Центральний карман худі — симетричне розміщення принта",
        icon: '<svg viewBox="0 0 32 32" fill="none" xmlns="http://www.w3.org/2000/svg" width="22" height="22" aria-hidden="true"><path d="M9 7l3-2h8l3 2 3 2v6l-3-1v14H9V14l-3 1V9l3-2z" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round"/><path d="M12 20c2-2 6-2 8 0v5h-8z" stroke="currentColor" stroke-width="1.4" stroke-linejoin="round"/></svg>',
      },
      sleeve: {
        title: labels.sleeve || "Рукави",
        hint: "Текст або символи на рукаві (можна обидва рукави)",
        icon: '<svg viewBox="0 0 32 32" fill="none" xmlns="http://www.w3.org/2000/svg" width="22" height="22" aria-hidden="true"><path d="M5 11l4-4h6v18H8l-3-2V11z" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round"/><path d="M27 11l-4-4h-6v18h7l3-2V11z" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round"/></svg>',
      },
      custom: {
        title: labels.custom || "Інша зона",
        hint: "Опишіть нестандартне розміщення в полі нижче",
        icon: '<svg viewBox="0 0 32 32" fill="none" xmlns="http://www.w3.org/2000/svg" width="22" height="22" aria-hidden="true"><path d="M16 4l3 7 7 1-5 5 1.5 7L16 21l-6.5 3L11 17l-5-5 7-1z" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round"/></svg>',
        badge: "Менеджер",
      },
    };
    return meta[zone] || { title: fallbackLabel, hint: "Окрема зона нанесення", icon: '<svg viewBox="0 0 32 32" width="22" height="22" aria-hidden="true"><circle cx="16" cy="16" r="9" fill="none" stroke="currentColor" stroke-width="1.6"/></svg>' };
  }

  function toggleZone(zone) {
    const current = new Set(STATE.print.zones || []);
    if (current.has(zone)) {
      current.delete(zone);
      deleteZoneFiles(zone);
      if (zone === "sleeve" && STATE.print.zone_options.sleeve) {
        delete STATE.print.zone_options.sleeve;
      }
    } else {
      current.add(zone);
      if (zone === "front") ensureFrontZoneOptions();
      if (zone === "back") ensureBackZoneOptions();
      if (zone === "sleeve") ensureSleeveZoneOptions();
      if (zone === "custom") ensureCustomZoneOptions();
      if (zone === "front") applyStageView("front");
      else if (zone === "back") applyStageView("back");
    }
    STATE.print.zones = getAvailableZones().filter((item) => current.has(item));
    invalidateAfter("zones");
    renderZoneChipsForCurrent();
    renderDropzones();
    refreshAll();
    persistDraft();
  }

  function renderFrontSizeOptions() {
    if (!dom.frontSizeList || !dom.frontSizeWrap) return;
    dom.frontSizeList.innerHTML = "";
    const enabled = STATE.print.zones.includes("front");
    dom.frontSizeWrap.hidden = !enabled;
    if (!enabled) return;
    ensureFrontZoneOptions();
    (CONFIG.front_size_presets || []).forEach((preset) => {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "cp-size-preset";
      btn.dataset.choiceValue = preset.value;
      if (getFrontSizePreset() === preset.value) btn.classList.add("is-active");
      const price = Number(preset.price_delta || 0);
      btn.innerHTML = `
        <div class="cp-size-icon">
          <img src="/static/img/configurator/ui/size-${String(preset.value || "").toLowerCase()}.svg" alt="${preset.label}" onerror="this.src='/static/img/configurator/ui/size-a4.svg'">
        </div>
        <div class="cp-size-details">
          <strong class="cp-size-format">${preset.label}</strong>
          <span class="cp-size-range">${escapeHtml(preset.range_label || "До формату ISO")}</span>
          <em class="cp-size-price">+${price} грн за зону</em>
        </div>
      `;
      btn.addEventListener("click", () => {
        ensureFrontZoneOptions();
        STATE.print.zone_options.front.size_preset = preset.value;
        applyStageView("front");
        renderFrontSizeOptions();
        refreshAll();
        persistDraft();
      });
      dom.frontSizeList.appendChild(btn);
    });
  }

  function renderBackSizeOptions() {
    if (!dom.backSizeList || !dom.backSizeWrap) return;
    dom.backSizeList.innerHTML = "";
    const enabled = STATE.print.zones.includes("back");
    dom.backSizeWrap.hidden = !enabled;
    if (!enabled) return;
    ensureBackZoneOptions();
    (CONFIG.back_size_presets || []).forEach((preset) => {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "cp-size-preset";
      btn.dataset.choiceValue = preset.value;
      if (getBackSizePreset() === preset.value) btn.classList.add("is-active");
      const price = Number(preset.price_delta || 0);
      btn.innerHTML = `
        <div class="cp-size-icon">
          <img src="/static/img/configurator/ui/size-${String(preset.value || "").toLowerCase()}.svg" alt="${preset.label}" onerror="this.src='/static/img/configurator/ui/size-a4.svg'">
        </div>
        <div class="cp-size-details">
          <strong class="cp-size-format">${preset.label}</strong>
          <span class="cp-size-range">${escapeHtml(preset.range_label || "До формату ISO")}</span>
          <em class="cp-size-price">+${price} грн за зону</em>
        </div>
      `;
      btn.addEventListener("click", () => {
        ensureBackZoneOptions();
        STATE.print.zone_options.back.size_preset = preset.value;
        applyStageView("back");
        renderBackSizeOptions();
        refreshAll();
        persistDraft();
      });
      dom.backSizeList.appendChild(btn);
    });
  }

  function renderSleeveControls() {
    if (!dom.sleeveWrap || !dom.sleeveSideList) return;
    const enabled = STATE.print.zones.includes("sleeve");
    dom.sleeveWrap.hidden = !enabled;
    dom.sleeveSideList.innerHTML = "";
    dom.sleeveEditors.forEach((node) => { node.hidden = true; });
    if (!enabled) return;
    ensureSleeveZoneOptions();

    [
      { side: "left", label: (CONFIG.zone_labels && CONFIG.zone_labels.sleeve_left) || "Лівий рукав" },
      { side: "right", label: (CONFIG.zone_labels && CONFIG.zone_labels.sleeve_right) || "Правий рукав" },
    ].forEach(({ side, label }) => {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "cp-mini-chip cp-mini-chip--sleeve-side";
      if (isSleeveSideEnabled(side)) btn.classList.add("is-active");
      btn.innerHTML = `<span>${escapeHtml(label)}</span><small>${side === "right" ? "окрема платна зона" : "базовий рукав"}</small>`;
      btn.addEventListener("click", () => {
        ensureSleeveZoneOptions();
        const next = !isSleeveSideEnabled(side);
        STATE.print.zone_options.sleeve[`${side}_enabled`] = next;
        if (!next) deletePlacementFiles(`sleeve_${side}`);
        if (!STATE.print.zone_options.sleeve.left_enabled && !STATE.print.zone_options.sleeve.right_enabled) {
          STATE.print.zones = STATE.print.zones.filter((zone) => zone !== "sleeve");
          delete STATE.print.zone_options.sleeve;
        }
        renderZoneChipsForCurrent();
        renderDropzones();
        refreshAll();
        persistDraft();
      });
      dom.sleeveSideList.appendChild(btn);
    });

    dom.sleeveEditors.forEach((node) => {
      const side = node.dataset.sleeveEditor;
      const active = isSleeveSideEnabled(side);
      node.hidden = !active;
      if (!active) return;
      const modeList = root.querySelector(`[data-sleeve-mode-list="${side}"]`);
      const textWrap = root.querySelector(`[data-sleeve-text-wrap="${side}"]`);
      const textInput = root.querySelector(`[data-sleeve-text-input="${side}"]`);
      if (modeList) {
        modeList.innerHTML = "";
        (CONFIG.sleeve_mode_options || []).forEach((option) => {
          const btn = document.createElement("button");
          btn.type = "button";
          btn.className = "cp-size-preset";
          if (getSleeveMode(side) === option.value) btn.classList.add("is-active");
          btn.innerHTML = `
            <div class="cp-size-icon">
              ${sleeveModeSvg(option.value)}
            </div>
            <div class="cp-size-details">
              <strong>${escapeHtml(option.label)}</strong>
              <span>${escapeHtml(option.badge || "")}</span>
            </div>
          `;
          btn.addEventListener("click", () => {
            ensureSleeveZoneOptions();
            STATE.print.zone_options.sleeve[`${side}_mode`] = option.value;
            if (option.value === "full_text") {
              deletePlacementFiles(`sleeve_${side}`);
            }
            renderSleeveControls();
            renderDropzones();
            refreshAll();
            persistDraft();
          });
          modeList.appendChild(btn);
        });
      }
      if (textWrap) {
        const useText = getSleeveMode(side) === "full_text";
        textWrap.hidden = !useText;
      }
      if (textInput) {
        textInput.value = getSleeveText(side);
        textInput.oninput = () => {
          ensureSleeveZoneOptions();
          STATE.print.zone_options.sleeve[`${side}_text`] = textInput.value.slice(0, 120);
          refreshAll();
          persistDraft();
        };
      }
    });
  }

  function sleeveModeSvg(mode) {
    if (mode === "full_text") {
      return `<svg viewBox="0 0 48 48" fill="none" aria-hidden="true">
        <path d="M17 8h14l7 7-4 5v20H14V20l-4-5 7-7Z" stroke="currentColor" stroke-width="2" stroke-linejoin="round"/>
        <path d="M22 14v19M27 14v19M32 14v19" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-dasharray="2 3"/>
      </svg>`;
    }
    return `<svg viewBox="0 0 48 48" fill="none" aria-hidden="true">
      <path d="M17 8h14l7 7-4 5v20H14V20l-4-5 7-7Z" stroke="currentColor" stroke-width="2" stroke-linejoin="round"/>
      <rect x="20" y="18" width="8" height="10" rx="1.5" stroke="currentColor" stroke-width="2"/>
    </svg>`;
  }

  function renderAddons() {
    if (!dom.addonsList) return;
    dom.addonsList.innerHTML = "";
    const cfg = getProductConfig();
    const addons = cfg && cfg.add_ons ? cfg.add_ons : [];
    if (dom.addonsTitle) dom.addonsTitle.textContent = cfg?.detail_title || "Додаткові деталі";
    if (dom.addonsNote) dom.addonsNote.textContent = cfg?.detail_note || "Опційні зміни моделі.";
    const fleeceOptions = addons.filter((addon) => addon.group === "fleece");
    if (fleeceOptions.length && !fleeceOptions.some((addon) => STATE.print.add_ons.includes(addon.value))) {
      const defaultFleece = fleeceOptions.find((addon) => addon.value === "no_fleece") || fleeceOptions[0];
      STATE.print.add_ons.push(defaultFleece.value);
    }
    
    // Filter addons: if auto-include condition exists, only show them if condition is met
    const visibleAddons = addons.filter(a => {
      if (a.auto_include_condition === "premium_or_oversize") {
        return (STATE.product.fabric === "premium" || STATE.product.fabric === "thermo" || STATE.product.fit === "oversize");
      }
      return true;
    });

    if (!visibleAddons.length) {
      if (dom.addonsWrap) dom.addonsWrap.hidden = true;
      return;
    }
    if (dom.addonsWrap) dom.addonsWrap.hidden = false;
    if (fleeceOptions.length) {
      const activeFleece = fleeceOptions.find((addon) => STATE.print.add_ons.includes(addon.value)) || fleeceOptions[0];
      const fleeceWrap = document.createElement("div");
      fleeceWrap.className = "cp-fleece-toggle";
      fleeceWrap.innerHTML = `
        <span class="cp-fleece-title">${escapeHtml(ui("fleece_title", "Утеплення"))}</span>
        <button type="button" class="cp-fleece-switch" role="switch" aria-checked="${activeFleece.value === "fleece"}">
          <span class="cp-fleece-switch-label">${escapeHtml(ui("fleece_off", "Без флісу"))}</span>
          <span class="cp-fleece-switch-track"><span class="cp-fleece-switch-thumb"></span></span>
          <span class="cp-fleece-switch-label">${escapeHtml(ui("fleece_on", "З флісом"))}</span>
        </button>`;
      fleeceWrap.querySelector(".cp-fleece-switch")?.addEventListener("click", () => {
        const next = activeFleece.value === "fleece" ? "no_fleece" : "fleece";
        STATE.print.add_ons = STATE.print.add_ons.filter((item) => !fleeceOptions.some((addon) => addon.value === item));
        STATE.print.add_ons.push(next);
        renderAddons();
        refreshAll();
        persistDraft();
      });
      dom.addonsList.appendChild(fleeceWrap);
    }

    visibleAddons.forEach((a) => {
      if (a.group === "fleece") return;

      const isAutoIncluded = a.auto_include_condition === "premium_or_oversize";
      const isActive = isAutoIncluded ? true : STATE.print.add_ons.includes(a.value);
      
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = `cp-addon-card cp-addon-card--${a.value}`;
      if (isActive) btn.classList.add("is-active");
      if (isAutoIncluded) btn.classList.add("is-auto-included"); // new class to lock
      btn.dataset.choiceValue = a.value;
      btn.dataset.priceModifier = String(a.price_delta || 0);
      btn.setAttribute("aria-pressed", String(isActive));
      if (isAutoIncluded) btn.setAttribute("aria-disabled", "true");
      const toggleLabel = isAutoIncluded ? "Входить" : isActive ? "Увімкнено" : "Вимкнено";
      btn.innerHTML = `
        <span class="cp-addon-card-icon" aria-hidden="true">${addonSvg(a.icon)}</span>
        <span class="cp-addon-card-body">
          <span class="cp-addon-card-topline">
            <strong>${escapeHtml(a.label)}</strong>
            <span class="cp-addon-card-control">
              <span class="cp-addon-card-control-text">${toggleLabel}</span>
              <span class="cp-addon-card-switch" aria-hidden="true">
                <span class="cp-addon-card-switch-thumb"></span>
              </span>
            </span>
          </span>
          ${a.badge ? `<span class="cp-addon-card-badge">${escapeHtml(a.badge)}</span>` : ""}
          <span class="cp-addon-card-hint">${escapeHtml(a.hint || "")}</span>
        </span>
      `;
      btn.addEventListener("click", () => {
        if (isAutoIncluded) return; // Prevent toggling included items
        const i = STATE.print.add_ons.indexOf(a.value);
        if (i >= 0) STATE.print.add_ons.splice(i, 1);
        else STATE.print.add_ons.push(a.value);
        renderAddons();
        refreshAll();
        persistDraft();
      });
      dom.addonsList.appendChild(btn);
    });
  }  function addonSvg(name) {
    if (name === "lacing") return lacingSvg();
    if (name === "ribbed_neck") return ribbedNeckSvg();
    if (name === "twill_tape") return twillTapeSvg();
    return defaultDotSvg();
  }

  function ribbedNeckSvg() {
    return `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M5 6C5 6 9 10 12 10C15 10 19 6 19 6C19 6 22 17 22 17H2C2 17 5 6 5 6Z"/><path d="M9 10V17M15 10V17M12 10V17"/></svg>`;
  }

  function twillTapeSvg() {
    return `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M4 12L20 12M4 8L20 8M4 16L20 16" stroke-dasharray="2 4"/></svg>`;
  }

  function lacingSvg() {
    return `
      <svg viewBox="0 0 56 56" fill="none" xmlns="http://www.w3.org/2000/svg" aria-hidden="true">
        <circle cx="18" cy="20" r="4" stroke="currentColor" stroke-width="2"/>
        <circle cx="38" cy="20" r="4" stroke="currentColor" stroke-width="2"/>
        <circle cx="18" cy="36" r="4" stroke="currentColor" stroke-width="2"/>
        <circle cx="38" cy="36" r="4" stroke="currentColor" stroke-width="2"/>
        <path d="M22 20 L34 36 M34 20 L22 36 M22 36 L34 20" stroke="currentColor" stroke-width="2" stroke-linecap="round"/>
        <path d="M14 24 C 8 28, 8 28, 14 32 M42 24 C 48 28, 48 28, 42 32" stroke="currentColor" stroke-width="2" stroke-linecap="round"/>
      </svg>
    `;
  }

  function defaultDotSvg() {
    return `<svg viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg"><circle cx="12" cy="12" r="6" stroke="currentColor" stroke-width="2"/></svg>`;
  }

  function renderArtworkActiveState() {
    if (!dom.artworkList) return;
    dom.artworkList.querySelectorAll("[data-choice-value]").forEach((btn) => {
      btn.classList.toggle("is-active", btn.dataset.choiceValue === STATE.artwork.service_kind);
    });
  }

  function renderDropzones() {
    if (!dom.dropzoneGrid) return;
    dom.dropzoneGrid.querySelectorAll(".cp-dropzone").forEach((el) => el.remove());
    dom.dropzoneGrid.querySelectorAll(".cp-dropzone-progress").forEach((el) => el.remove());
    dom.dropzoneGrid.parentNode?.querySelectorAll(":scope > .cp-dropzone-progress").forEach((el) => el.remove());
    const placements = getRequiredArtworkPlacements();
    const serviceKind = STATE.artwork.service_kind || "";
    const isUploadRequired = serviceKind === "ready" || serviceKind === "adjust";
    if (!STATE.print.zones.length) {
      if (dom.dropzoneEmpty) {
        dom.dropzoneEmpty.hidden = false;
        dom.dropzoneEmpty.textContent = "Спочатку оберіть зони друку — для кожного placement’а з макетом зʼявиться окреме поле завантаження.";
      }
      return;
    }
    if (!placements.length) {
      if (dom.dropzoneEmpty) {
        dom.dropzoneEmpty.hidden = false;
        dom.dropzoneEmpty.textContent = "Для активних placement’ів зараз не потрібен окремий файл. Наприклад, текстові рукави можна залишити без аплоаду.";
      }
      return;
    }
    if (dom.dropzoneEmpty) dom.dropzoneEmpty.hidden = true;

    // Прогрес-блок над dropzones для режимів з обовʼязковим аплоадом.
    if (isUploadRequired) {
      const filledCount = placements.filter((p) => (filesByPlacement.get(p.placement_key) || []).length > 0).length;
      const totalCount = placements.length;
      const progress = document.createElement("div");
      progress.className = "cp-dropzone-progress";
      if (filledCount === totalCount) progress.classList.add("is-complete");
      progress.innerHTML = `
        <div class="cp-dropzone-progress-text">
          <strong>${filledCount === totalCount ? "Усі макети додані" : "Додайте макет на кожну зону"}</strong>
          <small>${filledCount} з ${totalCount} ${totalCount === 1 ? "зона" : "зон"} ${filledCount === totalCount ? "✅" : "·  ⚠ обовʼязково"}</small>
        </div>
        <div class="cp-dropzone-progress-bar" aria-hidden="true">
          <span style="width: ${Math.round((filledCount / totalCount) * 100)}%"></span>
        </div>
      `;
      dom.dropzoneGrid.parentNode?.insertBefore(progress, dom.dropzoneGrid);
    }

    placements.forEach((placement) => {
      const wrap = document.createElement("label");
      wrap.className = "cp-dropzone";
      wrap.dataset.zone = placement.zone;
      wrap.dataset.placementKey = placement.placement_key;
      const label = getDropzoneTitle(placement);
      const filesInfo = filesByPlacement.get(placement.placement_key) || [];
      const metaLabel = getDropzoneMeta(placement, filesInfo.length);
      const status = filesInfo.length > 0 ? "ok" : (isUploadRequired ? "missing" : "optional");
      wrap.dataset.status = status;
      const statusBadgeText = status === "ok"
        ? "Готово"
        : status === "missing"
          ? "Потрібен файл"
          : "За бажанням";
      wrap.innerHTML = `
        <div class="cp-dropzone-status-badge" aria-hidden="true">
          <span class="cp-dropzone-status-dot"></span>
          <span class="cp-dropzone-status-text">${escapeHtml(statusBadgeText)}</span>
        </div>
        <div class="cp-dropzone-head">
          <small>Макет</small>
          <strong>${label}</strong>
        </div>
        <input type="file" multiple accept="image/*,application/pdf,.ai,.eps,.psd,.tiff,.svg" data-dropzone-input>
        <div class="cp-dropzone-body">
          <span class="cp-dropzone-cta">${filesInfo.length ? "+ Додати ще файл" : "+ Завантажити файл"}</span>
          <span class="cp-dropzone-meta" data-dropzone-meta>${metaLabel}</span>
        </div>
        ${filesInfo.length ? `<ul class="cp-dropzone-list">${filesInfo.map((f) => `<li>${escapeHtml(f.name)} <small>${formatBytes(f.size)}</small></li>`).join("")}</ul>` : ""}
      `;
      const input = wrap.querySelector("[data-dropzone-input]");
      input.addEventListener("change", (event) => {
        const files = Array.from(event.target.files || []);
        if (!files.length) {
          renderDropzones();
          refreshAll();
          return;
        }
        filterFilesByTransparency(files).then((acceptedFiles) => {
          if (acceptedFiles.length) {
            // append (а не replace) щоб не втрачалися попередні
            const existing = filesByPlacement.get(placement.placement_key) || [];
            filesByPlacement.set(placement.placement_key, [...existing, ...acceptedFiles]);
          }
          renderDropzones();
          refreshAll();
        });
      });
      dom.dropzoneGrid.appendChild(wrap);
    });
  }

  function getDropzoneTitle(placement) {
    if (placement.placement_key === "front") return "Макет для переду";
    if (placement.placement_key === "back") return "Макет для спини";
    if (placement.placement_key === "sleeve_left") return "Макет для лівого рукава";
    if (placement.placement_key === "sleeve_right") return "Макет для правого рукава";
    return `Макет для ${placement.label.toLowerCase()}`;
  }

  function getDropzoneMeta(placement, fileCount) {
    if (fileCount) {
      return `${fileCount} файл(ів) додано`;
    }
    if (placement.size_preset) {
      return `${placement.size_preset} · PDF, AI, EPS, PSD, PNG, JPG, TIFF, SVG`;
    }
    if (placement.zone === "sleeve") {
      return "A6 · PDF, AI, EPS, PSD, PNG, JPG, TIFF, SVG";
    }
    return "PDF, AI, EPS, PSD, PNG, JPG, TIFF, SVG";
  }

  // ── Transparency guard for "ready" artwork ─────────────────
  const NO_ALPHA_EXTENSIONS = ["jpg", "jpeg", "jfif", "bmp", "heic", "heif"];
  const ALPHA_CAPABLE_RASTER = ["png", "webp", "gif", "avif"];

  function getFileExtension(name) {
    const match = /\.([a-z0-9]+)$/i.exec(name || "");
    return match ? match[1].toLowerCase() : "";
  }

  async function detectTransparencyIssue(file) {
    const ext = getFileExtension(file.name);
    if (NO_ALPHA_EXTENSIONS.includes(ext)) {
      return `Формат ${ext.toUpperCase()} не підтримує прозорість — фон зображення буде надруковано на виробі разом із принтом.`;
    }
    if (!ALPHA_CAPABLE_RASTER.includes(ext)) return "";
    // PNG/WebP/GIF: перевіряємо, чи є реальні прозорі пікселі
    try {
      const bitmap = await createImageBitmap(file);
      const maxSide = 96;
      const scale = Math.min(1, maxSide / Math.max(bitmap.width, bitmap.height));
      const canvas = document.createElement("canvas");
      canvas.width = Math.max(1, Math.round(bitmap.width * scale));
      canvas.height = Math.max(1, Math.round(bitmap.height * scale));
      const ctx = canvas.getContext("2d", { willReadFrequently: true });
      ctx.drawImage(bitmap, 0, 0, canvas.width, canvas.height);
      if (typeof bitmap.close === "function") bitmap.close();
      const data = ctx.getImageData(0, 0, canvas.width, canvas.height).data;
      for (let i = 3; i < data.length; i += 4) {
        if (data[i] < 250) return "";
      }
      return "У цьому зображенні немає прозорих ділянок — фон буде надруковано суцільним прямокутником.";
    } catch (err) {
      return "";
    }
  }

  async function filterFilesByTransparency(files) {
    // Попереджаємо лише коли клієнт каже «маю готовий файл» —
    // для доопрацювання/дизайну референси можуть бути будь-якими.
    if ((STATE.artwork.service_kind || "") !== "ready") return files;
    const accepted = [];
    for (const file of files) {
      const issue = await detectTransparencyIssue(file);
      if (!issue) {
        accepted.push(file);
        continue;
      }
      const keep = await showTransparencyConfirm(file.name, issue);
      if (keep) accepted.push(file);
    }
    return accepted;
  }

  function showTransparencyConfirm(fileName, issueText) {
    return new Promise((resolve) => {
      const overlay = document.createElement("div");
      overlay.className = "cp-transparency-overlay";
      overlay.style.cssText = "position:fixed;inset:0;z-index:10050;display:flex;align-items:center;justify-content:center;padding:16px;background:rgba(8,8,12,.72);backdrop-filter:blur(6px);";
      const card = document.createElement("div");
      card.style.cssText = "max-width:440px;width:100%;background:#17171f;border:1px solid rgba(255,255,255,.12);border-radius:18px;padding:24px;color:#f4f4f7;font:inherit;box-shadow:0 24px 64px rgba(0,0,0,.5);";
      card.setAttribute("role", "alertdialog");
      card.setAttribute("aria-modal", "true");
      card.innerHTML = `
        <div style="font-size:34px;line-height:1;margin-bottom:12px;">⚠️</div>
        <h3 style="margin:0 0 8px;font-size:18px;font-weight:700;">Файл без прозорого фону</h3>
        <p style="margin:0 0 6px;font-size:14px;opacity:.92;word-break:break-word;"><strong>${escapeHtml(fileName)}</strong></p>
        <p style="margin:0 0 10px;font-size:14px;opacity:.8;">${escapeHtml(issueText)}</p>
        <p style="margin:0 0 18px;font-size:14px;opacity:.8;">Готовий макет для переносу зазвичай має прозорий фон (PNG). Ви впевнені, що це фінальний файл для друку?</p>
        <div style="display:flex;gap:10px;flex-wrap:wrap;">
          <button type="button" data-tr-confirm style="flex:1 1 auto;min-height:44px;padding:10px 16px;border:none;border-radius:12px;background:#7c5cff;color:#fff;font-weight:600;cursor:pointer;">Так, файл готовий</button>
          <button type="button" data-tr-cancel style="flex:1 1 auto;min-height:44px;padding:10px 16px;border:1px solid rgba(255,255,255,.2);border-radius:12px;background:transparent;color:#f4f4f7;font-weight:600;cursor:pointer;">Прибрати файл</button>
        </div>
      `;
      overlay.appendChild(card);
      const cleanup = (result) => {
        overlay.remove();
        document.removeEventListener("keydown", onKeydown);
        resolve(result);
      };
      const onKeydown = (event) => {
        if (event.key === "Escape") cleanup(false);
      };
      card.querySelector("[data-tr-confirm]").addEventListener("click", () => cleanup(true));
      card.querySelector("[data-tr-cancel]").addEventListener("click", () => cleanup(false));
      overlay.addEventListener("click", (event) => {
        if (event.target === overlay) cleanup(false);
      });
      document.addEventListener("keydown", onKeydown);
      document.body.appendChild(overlay);
      card.querySelector("[data-tr-cancel]").focus();
    });
  }

  // ── 3D-ротор сцени: плавне обертання перед/зад + drag ─────────
  function stageMotionReduced() {
    return window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  }

  function rotorNorm(angle) {
    return ((angle % 360) + 360) % 360;
  }

  function rotorSideFor(angle) {
    const a = rotorNorm(angle);
    return a > 90 && a < 270 ? "back" : "front";
  }

  function rotorPhi(angle) {
    const a = rotorNorm(angle);
    if (a > 90 && a < 270) return a - 180;
    return a >= 270 ? a - 360 : a;
  }

  function rotorApplyTransform() {
    if (!dom.stageRotor) return;
    dom.stageRotor.style.transform = `rotateY(${rotorPhi(STAGE_ROTOR.angle).toFixed(2)}deg)`;
  }

  function rotorSyncSide() {
    const side = rotorSideFor(STAGE_ROTOR.angle);
    if (side !== (STATE.ui.stage_view || "front")) {
      STATE.ui.stage_view = side;
      renderStageSideEffects();
    }
  }

  function rotorStop() {
    if (STAGE_ROTOR.raf) {
      cancelAnimationFrame(STAGE_ROTOR.raf);
      STAGE_ROTOR.raf = 0;
    }
  }

  function rotorAnimateTo(target, duration = 640, done) {
    rotorStop();
    if (!dom.stageRotor || stageMotionReduced()) {
      STAGE_ROTOR.angle = rotorNorm(target);
      rotorSyncSide();
      rotorApplyTransform();
      if (done) done();
      return;
    }
    const from = STAGE_ROTOR.angle;
    const startTs = performance.now();
    const ease = (t) => (t < 0.5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2);
    const frame = (now) => {
      const t = Math.min((now - startTs) / duration, 1);
      STAGE_ROTOR.angle = from + (target - from) * ease(t);
      rotorSyncSide();
      rotorApplyTransform();
      if (t < 1) {
        STAGE_ROTOR.raf = requestAnimationFrame(frame);
      } else {
        STAGE_ROTOR.raf = 0;
        STAGE_ROTOR.angle = rotorNorm(target);
        if (done) done();
      }
    };
    STAGE_ROTOR.raf = requestAnimationFrame(frame);
  }

  function rotorSwing() {
    if (!dom.stageRotor || stageMotionReduced() || STAGE_ROTOR.dragging) return;
    const base = rotorNorm(STAGE_ROTOR.angle);
    rotorAnimateTo(base + 14, 230, () => rotorAnimateTo(base, 430));
  }

  function rotorSpin() {
    if (!dom.stageRotor || stageMotionReduced() || STAGE_ROTOR.dragging) return;
    rotorAnimateTo(rotorNorm(STAGE_ROTOR.angle) + 360, 950);
  }

  function stageReactToState() {
    const sig = `${STATE.product.type || ""}|${STATE.product.fit || ""}|${STATE.product.color || ""}`;
    if (STAGE_ROTOR.lastSig === null) {
      STAGE_ROTOR.lastSig = sig;
      return;
    }
    if (sig === STAGE_ROTOR.lastSig) return;
    const prevType = STAGE_ROTOR.lastSig.split("|")[0];
    const nextType = sig.split("|")[0];
    STAGE_ROTOR.lastSig = sig;
    if (!STATE.product.type) return;
    if (prevType !== nextType) rotorSpin();
    else rotorSwing();
  }

  function renderStageSideEffects() {
    const view = STATE.ui.stage_view || "front";
    if (dom.garment) {
      dom.garment.classList.toggle("cp-garment--front", view === "front");
      dom.garment.classList.toggle("cp-garment--back", view === "back");
    }
    dom.stageViewSwitch?.forEach((btn) => {
      btn.classList.toggle("is-active", btn.dataset.stageView === view);
    });
    renderStageSvg();
    applyGarmentAddons();
    renderZoneOverlay();
  }

  function applyStageView(view) {
    STATE.ui.stage_view = view;
    dom.stageViewSwitch?.forEach((btn) => {
      btn.classList.toggle("is-active", btn.dataset.stageView === view);
    });
  }

  function bindStageRotorDrag() {
    const frame = dom.stageFrame;
    if (!frame || !dom.stageRotor) return;
    let startX = 0;
    let startAngle = 0;
    let active = false;
    let moved = false;
    let pid = null;
    frame.addEventListener("pointerdown", (event) => {
      if (event.target.closest("button")) return;
      if (!STATE.product.type) return;
      active = true;
      moved = false;
      startX = event.clientX;
      startAngle = STAGE_ROTOR.angle;
      pid = event.pointerId;
      rotorStop();
    });
    frame.addEventListener("pointermove", (event) => {
      if (!active) return;
      const dx = event.clientX - startX;
      if (!moved && Math.abs(dx) > 6) {
        moved = true;
        STAGE_ROTOR.dragging = true;
        try {
          frame.setPointerCapture(pid);
        } catch (err) {
          /* noop */
        }
        frame.classList.add("is-rotating");
      }
      if (!moved) return;
      STAGE_ROTOR.angle = startAngle + dx * 0.45;
      rotorSyncSide();
      rotorApplyTransform();
    });
    const finishDrag = () => {
      if (!active) return;
      active = false;
      if (!moved) return;
      STAGE_ROTOR.dragging = false;
      frame.classList.remove("is-rotating");
      const target = Math.round(STAGE_ROTOR.angle / 180) * 180;
      rotorAnimateTo(target, 470);
    };
    frame.addEventListener("pointerup", finishDrag);
    frame.addEventListener("pointercancel", finishDrag);
  }

  function getStageTargets() {
    const cfg = getProductConfig();
    if (!cfg) return [];
    const targets = [];
    if ((cfg.zones || []).includes("front")) {
      targets.push({
        key: "front",
        zone: "front",
        label: (CONFIG.zone_labels && CONFIG.zone_labels.front) || "front",
        isActive: STATE.print.zones.includes("front"),
      });
    }
    if ((cfg.zones || []).includes("back")) {
      targets.push({
        key: "back",
        zone: "back",
        label: (CONFIG.zone_labels && CONFIG.zone_labels.back) || "back",
        isActive: STATE.print.zones.includes("back"),
      });
    }
    if ((cfg.zones || []).includes("kangaroo")) {
      targets.push({
        key: "kangaroo",
        zone: "kangaroo",
        label: (CONFIG.zone_labels && CONFIG.zone_labels.kangaroo) || "Кенгуряча кишеня",
        isActive: STATE.print.zones.includes("kangaroo"),
      });
    }
    if ((cfg.zones || []).includes("sleeve")) {
      const sleeve = STATE.print.zone_options?.sleeve || {};
      targets.push(
        {
          key: "sleeve_left",
          zone: "sleeve",
          side: "left",
          label: (CONFIG.zone_labels && CONFIG.zone_labels.sleeve_left) || "Лівий рукав",
          isActive: STATE.print.zones.includes("sleeve") && !!sleeve.left_enabled,
        },
        {
          key: "sleeve_right",
          zone: "sleeve",
          side: "right",
          label: (CONFIG.zone_labels && CONFIG.zone_labels.sleeve_right) || "Правий рукав",
          isActive: STATE.print.zones.includes("sleeve") && !!sleeve.right_enabled,
        }
      );
    }
    if ((cfg.zones || []).includes("custom")) {
      targets.push({
        key: "custom",
        zone: "custom",
        label: (CONFIG.zone_labels && CONFIG.zone_labels.custom) || "custom",
        isActive: STATE.print.zones.includes("custom"),
      });
    }
    return targets;
  }

  function toggleStageTarget(targetKey) {
    if (targetKey === "front" || targetKey === "back" || targetKey === "kangaroo" || targetKey === "custom") {
      toggleZone(targetKey);
      return;
    }
    if (!targetKey.startsWith("sleeve_")) return;
    const side = targetKey.endsWith("left") ? "left" : "right";
    const hadSleeveZone = STATE.print.zones.includes("sleeve");
    const existingSleeve = STATE.print.zone_options?.sleeve || {};
    if (!hadSleeveZone) {
      STATE.print.zones = [...STATE.print.zones, "sleeve"];
      STATE.print.zone_options.sleeve = {
        left_enabled: side === "left",
        right_enabled: side === "right",
        left_mode: existingSleeve.left_mode || SLEEVE_MODE_DEFAULT,
        right_mode: existingSleeve.right_mode || SLEEVE_MODE_DEFAULT,
        left_text: existingSleeve.left_text || "",
        right_text: existingSleeve.right_text || "",
      };
    } else {
      ensureSleeveZoneOptions();
      STATE.print.zone_options.sleeve[`${side}_enabled`] = !isSleeveSideEnabled(side);
    }
    if (!STATE.print.zone_options.sleeve.left_enabled && !STATE.print.zone_options.sleeve.right_enabled) {
      STATE.print.zones = STATE.print.zones.filter((zone) => zone !== "sleeve");
      delete STATE.print.zone_options.sleeve;
      deleteZoneFiles("sleeve");
    }
    invalidateAfter("zones");
    renderZoneChipsForCurrent();
    renderDropzones();
    refreshAll();
    persistDraft();
  }

  function renderZoneOverlay() {
    if (!dom.zoneLayer || !dom.stageOverlay) return;
    dom.zoneLayer.innerHTML = "";
    dom.stageOverlay.innerHTML = "";
    if (dom.stagePlaceholder) dom.stagePlaceholder.hidden = !!STATE.product.type;
    
    // Hide hitboxes when not actively configuring zones to keep scene clean
    if (STATE.ui.current_step !== "zones" && STATE.ui.current_step !== "artwork") {
      dom.stageOverlay.style.display = "none";
    } else {
      dom.stageOverlay.style.display = "";
    }
    
    if (!STATE.product.type) return;
    const activePlacements = getExpandedPlacements();
    getStageTargets().forEach((target) => {
      const placement = activePlacements.find((item) => item.placement_key === target.key) || {
        zone: target.zone,
        placement_key: target.key,
        label: target.label,
        side: target.side,
        mode: target.side ? getSleeveMode(target.side) : "",
        text: target.side ? getSleeveText(target.side) : "",
        size_preset: target.key === "front" ? getFrontSizePreset() : target.key === "back" ? getBackSizePreset() : "",
      };
      const anchor = getPlacementAnchor(placement);
      if (!anchor?.button) return;
      // Removed cp-zone-pin, we will attach the label directly to the plate if we want.

      if (anchor.plate) {
        const plate = document.createElement("button");
        plate.type = "button";
        plate.className = `cp-stage-print cp-stage-print--${target.zone} cp-stage-print--${target.key}`;
        plate.classList.add(`cp-stage-print--shape-${anchor.plate.shape || "panel"}`);
        if (!target.isActive) plate.classList.add("is-inactive");
        else plate.classList.add("is-active");
        if (placement.zone === "sleeve" && placement.mode === "full_text") {
          plate.classList.add("is-text-placement");
        }
        plate.style.setProperty("--plate-top", asPercent(anchor.plate.y));
        plate.style.setProperty("--plate-left", asPercent(anchor.plate.x));
        plate.style.setProperty("--plate-width", asPercent(anchor.plate.width));
        plate.style.setProperty("--plate-height", asPercent(anchor.plate.height));
        plate.style.setProperty("--plate-rotate", `${Number(anchor.plate.rotate || 0)}deg`);
        plate.style.setProperty("--plate-radius", `${Number(anchor.plate.radius || 18)}px`);
        plate.dataset.zone = target.key;
        plate.setAttribute("aria-pressed", String(!!target.isActive));
        plate.setAttribute("aria-label", `${target.label}${target.isActive ? ", увімкнено" : ", вимкнено"}`);
        const badgeText = escapeHtml(getStageBadgeText(placement));
        const artUrl = target.isActive ? getPlacementArtUrl(target.key) : "";
        const dimsText = target.isActive && anchor.plate.dims ? escapeHtml(anchor.plate.dims) : "";
        plate.innerHTML = `
          ${artUrl ? `<span class="cp-stage-print-art" style="background-image:url('${artUrl}')"></span>` : ""}
          <span class="cp-stage-print-badge">${badgeText || "ON"}</span>
          ${dimsText ? `<span class="cp-stage-print-dims">${dimsText}</span>` : ""}
        `;
        if (artUrl) plate.classList.add("has-art");
        plate.title = target.label;
        plate.addEventListener("click", () => handleStageTargetInteraction(target.key));
        dom.stageOverlay.appendChild(plate);
      }
    });
  }

  function getStageTargetView(targetKey) {
    if (targetKey === "front" || targetKey === "kangaroo" || targetKey === "custom") return "front";
    if (targetKey === "sleeve_left" || targetKey === "sleeve_right") return STATE.ui.stage_view || "front";
    return "back";
  }

  function focusStageTarget(targetKey) {
    const targetView = getStageTargetView(targetKey);
    if (targetView !== (STATE.ui.stage_view || "front")) {
      applyStageView(targetView, { animate: true });
    }
  }

  function handleStageTargetInteraction(targetKey) {
    focusStageTarget(targetKey);
    if (STATE.ui.current_step === "zones") {
      toggleStageTarget(targetKey);
    }
  }

  function getStagePinCode(target) {
    if (target.key === "front") return "FR";
    if (target.key === "back") return "BK";
    if (target.key === "kangaroo") return "KP";
    if (target.key === "sleeve_left") return "SL";
    if (target.key === "sleeve_right") return "SR";
    return "C";
  }

  function getStageBadgeText(placement) {
    if (placement.zone === "sleeve" && placement.mode === "full_text") return "TEXT";
    if (placement.size_preset) return placement.size_preset;
    if (placement.zone === "sleeve") return "A6";
    return "ON";
  }

  function getStageGuideText() {
    if (!STATE.product.type) return "Оберіть виріб, щоб побачити сцену і доступні placement’и.";
    if (STATE.ui.current_step === "zones") {
      return "На кроці «Зони друку» торкніться мітки або картки зони, щоб увімкнути чи вимкнути placement.";
    }
    return "Торкніться мітки або картки нижче, щоб сфокусувати потрібну зону без перевантаження сцени.";
  }

  function getStageLegendMeta(target, placement) {
    if (!target.isActive) {
      return STATE.ui.current_step === "zones" ? "Натисніть, щоб увімкнути зону" : "Доступна зона";
    }
    if (!placement) return "Активна зона";
    if (placement.zone === "sleeve" && placement.mode === "full_text") {
      const text = (placement.text || "").trim();
      return text ? `Текст · ${text.slice(0, 22)}` : "Текст на рукаві";
    }
    if (placement.size_preset) return placement.size_preset;
    if (placement.zone === "sleeve") return "A6";
    return "Активна зона";
  }

  function renderStageLegend() {
    if (!dom.stageLegend) return;
    dom.stageLegend.innerHTML = "";
    if (dom.stageGuide) dom.stageGuide.textContent = getStageGuideText();
    if (!STATE.product.type) {
      dom.stageLegend.innerHTML = `<div class="cp-stage-legend-empty">Зони зʼявляться після вибору виробу та кроку з конфігурацією принта.</div>`;
      return;
    }
    const placements = getExpandedPlacements();
    getStageTargets().forEach((target) => {
      const placement = placements.find((item) => item.placement_key === target.key) || null;
      const chip = document.createElement("button");
      chip.type = "button";
      chip.className = "cp-stage-legend-chip";
      if (target.isActive) chip.classList.add("is-active");
      if (getStageTargetView(target.key) === (STATE.ui.stage_view || "front")) {
        chip.classList.add("is-focused");
      }
      chip.innerHTML = `
        <small>${target.isActive ? "Активна зона" : "Доступна зона"}</small>
        <strong>${escapeHtml(target.label)}</strong>
        <span>${escapeHtml(getStageLegendMeta(target, placement))}</span>
      `;
      chip.addEventListener("click", () => {
        if (STATE.ui.current_step === "zones") {
          handleStageTargetInteraction(target.key);
        } else {
          focusStageTarget(target.key);
          renderStageLegend();
        }
      });
      dom.stageLegend.appendChild(chip);
    });
  }

  function asPercent(value) {
    return `${Number(value || 0)}%`;
  }

  function buildGarmentGradient(hex) {
    const top = mixHex(hex, "#ffffff", 0.22);
    const middle = mixHex(hex, "#2d2520", 0.16);
    const bottom = mixHex(hex, "#000000", 0.34);
    return `linear-gradient(180deg, ${top} 0%, ${middle} 48%, ${bottom} 100%)`;
  }

  function applyOwnGarmentStageColor() {
    const target = dom.stageCard || dom.stageSvg || dom.garment;
    if (!target?.style) return;
    if (STATE.product.type !== "customer_garment") {
      ["--cp-stage-base-fill", "--cp-stage-top-fill", "--cp-stage-shade-fill", "--cp-garment-fill"].forEach((name) => target.style.removeProperty(name));
      return;
    }
    const hex = /^#[0-9a-f]{6}$/i.test(STATE.notes.garment_color_hex || "") ? STATE.notes.garment_color_hex : "#151515";
    target.style.setProperty("--cp-stage-base-fill", hex);
    target.style.setProperty("--cp-stage-top-fill", mixHex(hex, "#ffffff", 0.2));
    target.style.setProperty("--cp-stage-shade-fill", mixHex(hex, "#000000", 0.32));
    target.style.setProperty("--cp-garment-fill", buildGarmentGradient(hex));
  }

  function bindStageView() {
    dom.stageViewSwitch?.forEach((btn) => {
      btn.addEventListener("click", () => {
        applyStageView(btn.dataset.stageView);
        refreshAll();
        persistDraft();
      });
    });
  }

  // ── Quantity + Smart sizing ─────────────────────────────────
  function bindQuantity() {
    if (dom.qtyInput) {
      dom.qtyInput.addEventListener("input", () => {
        const v = parseInt(dom.qtyInput.value, 10);
        STATE.order.quantity = isFinite(v) && v > 0 ? v : 0;
        clampSizeBreakdownToQuantity();
        renderSizing();
        refreshAll();
        persistDraft();
      });
    }
    dom.qtySteps?.forEach((btn) => {
      btn.addEventListener("click", () => {
        const delta = parseInt(btn.dataset.qtyStep, 10) || 0;
        const next = Math.max(0, (STATE.order.quantity || 0) + delta);
        STATE.order.quantity = next;
        clampSizeBreakdownToQuantity();
        if (dom.qtyInput) dom.qtyInput.value = next || "";
        renderSizing();
        refreshAll();
        persistDraft();
      });
    });
    dom.sizeManagerBtn?.addEventListener("click", () => {
      STATE.order.size_mode = "manager";
      renderSizing();
      refreshAll();
      persistDraft();
    });
    dom.sizesNoteInput?.addEventListener("input", () => {
      STATE.order.sizes_note = dom.sizesNoteInput.value;
      persistDraft();
    });
    dom.garmentNoteInput?.addEventListener("input", () => {
      STATE.notes.garment_note = dom.garmentNoteInput.value;
      persistDraft();
    });
    const bindNote = (input, key, onInput = null) => {
      input?.addEventListener("input", () => {
        STATE.notes[key] = input.value;
        onInput?.(input.value);
        persistDraft();
      });
    };
    bindNote(dom.brandIntroName, "brand_name", (value) => {
      if (dom.brandNameInput) dom.brandNameInput.value = value;
    });
    bindNote(dom.brandResource, "brand_resource");
    bindNote(dom.brandPhone, "brand_phone");
    dom.brandQuantity?.addEventListener("input", () => {
      const value = Math.max(0, parseInt(dom.brandQuantity.value, 10) || 0);
      if (value > 0) {
        STATE.order.quantity = value;
        if (dom.qtyInput) dom.qtyInput.value = String(value);
      }
      updateBrandTierNote();
      refreshAll();
      persistDraft();
    });
    bindNote(dom.brandWish, "brand_wish", (value) => {
      if (value && !STATE.notes.brief) STATE.notes.brief = value;
    });
    dom.ownPhotoInput?.addEventListener("change", () => {
      garmentPhotoFile = dom.ownPhotoInput.files?.[0] || null;
      STATE.notes.garment_photo_name = garmentPhotoFile?.name || "";
      persistDraft();
    });
  }

  function clampSizeBreakdownToQuantity() {
    const limit = Math.max(0, STATE.order.quantity || 0);
    const grid = CONFIG.size_grid || ["S", "M", "L", "XL", "2XL"];
    const next = {};
    let remaining = limit;
    grid.forEach((size) => {
      const requested = Math.max(0, parseInt(STATE.order.size_breakdown?.[size], 10) || 0);
      const accepted = Math.min(requested, remaining);
      if (accepted) next[size] = accepted;
      remaining -= accepted;
    });
    STATE.order.size_breakdown = next;
  }

  function renderSizing() {
    if (!dom.sizeBlock) return;
    const qty = STATE.order.quantity || 0;
    if (STATE.product.type === "customer_garment") {
      if (dom.qtyBar) dom.qtyBar.hidden = true;
      dom.sizeBlock.hidden = true;
      if (dom.b2bMeta) dom.b2bMeta.hidden = true;
      return;
    }
    if (dom.qtyBar) dom.qtyBar.hidden = false;
    const grid = CONFIG.size_grid || ["S", "M", "L", "XL", "2XL"];
    if (qty <= 0) {
      dom.sizeBlock.hidden = true;
      if (dom.qtyHint) dom.qtyHint.textContent = "Введіть кількість — ми покажемо адаптивний вибір розмірів.";
      if (dom.b2bMeta) dom.b2bMeta.hidden = true;
      return;
    }
    dom.sizeBlock.hidden = false;
    if (qty === 1) {
      // Single-size chip row
      STATE.order.size_mode = "single";
      dom.sizeGrid.hidden = false;
      dom.sizeMatrix.hidden = true;
      if (dom.sizeManagerBtn) dom.sizeManagerBtn.hidden = false;
      if (dom.garmentNoteWrap) dom.garmentNoteWrap.hidden = true;
      if (dom.ownPhotoWrap) dom.ownPhotoWrap.hidden = true;
      if (dom.sizesNoteWrap) dom.sizesNoteWrap.hidden = true;
      if (dom.qtyHint) dom.qtyHint.textContent = "Один виріб — один розмір. Натисніть, щоб обрати.";
      // Reset size_breakdown to single
      const currentSingle = grid.find((s) => (STATE.order.size_breakdown || {})[s]) || null;
      dom.sizeGrid.innerHTML = "";
      grid.forEach((s) => {
        const chip = document.createElement("button");
        chip.type = "button";
        chip.className = "cp-mini-chip";
        if (currentSingle === s) chip.classList.add("is-active");
        chip.dataset.choiceValue = s;
        chip.textContent = s;
        chip.addEventListener("click", () => {
          STATE.order.size_breakdown = { [s]: 1 };
          renderSizing();
          refreshAll();
          persistDraft();
        });
        dom.sizeGrid.appendChild(chip);
      });
      if (dom.sizeWarning) dom.sizeWarning.hidden = true;
    } else {
      // Matrix mode
      if (STATE.order.size_mode === "manager") {
        dom.sizeGrid.hidden = true;
        dom.sizeMatrix.hidden = true;
        if (dom.sizeManagerBtn) dom.sizeManagerBtn.hidden = false;
        if (dom.sizesNoteWrap) dom.sizesNoteWrap.hidden = false;
        if (dom.garmentNoteWrap) dom.garmentNoteWrap.hidden = true;
        if (dom.ownPhotoWrap) dom.ownPhotoWrap.hidden = true;
        if (dom.qtyHint) dom.qtyHint.textContent = "Розміри уточнимо разом з менеджером — заповніть примітку.";
        if (dom.sizeWarning) dom.sizeWarning.hidden = true;
        return;
      }
      STATE.order.size_mode = "mixed";
      dom.sizeGrid.hidden = true;
      dom.sizeMatrix.hidden = false;
      if (dom.sizeManagerBtn) dom.sizeManagerBtn.hidden = false;
      if (dom.sizesNoteWrap) dom.sizesNoteWrap.hidden = false;
      if (dom.garmentNoteWrap) dom.garmentNoteWrap.hidden = true;
      if (dom.ownPhotoWrap) dom.ownPhotoWrap.hidden = true;
      if (dom.qtyHint) dom.qtyHint.textContent = `Розподіліть ${qty} шт. по розмірах. Сума має дорівнювати ${qty}.`;

      dom.sizeMatrix.innerHTML = "";
      grid.forEach((s) => {
        const wrap = document.createElement("label");
        wrap.className = "cp-size-matrix-cell";
        const count = Number(STATE.order.size_breakdown?.[s] || 0);
        wrap.innerHTML = `
          <span class="cp-size-matrix-label">${s}</span>
          <span class="cp-size-stepper" data-size-stepper="${s}">
            <button type="button" class="cp-size-step" data-size-step="-1" data-size-value="${s}" aria-label="Зменшити ${s}">−</button>
            <strong data-size-count="${s}">${count}</strong>
            <button type="button" class="cp-size-step" data-size-step="1" data-size-value="${s}" aria-label="Збільшити ${s}">+</button>
          </span>`;
        const updateSize = (delta) => {
          const current = Number(STATE.order.size_breakdown?.[s] || 0);
          const assignedElsewhere = Object.entries(STATE.order.size_breakdown || {})
            .filter(([size]) => size !== s)
            .reduce((total, [, value]) => total + (parseInt(value, 10) || 0), 0);
          const next = Math.min(Math.max(0, current + delta), Math.max(0, qty - assignedElsewhere));
          if (!STATE.order.size_breakdown) STATE.order.size_breakdown = {};
          STATE.order.size_breakdown[s] = next;
          validateSizeMatrix();
          renderSizing();
          refreshAll();
          persistDraft();
        };
        wrap.querySelectorAll("[data-size-step]").forEach((button) => {
          const delta = parseInt(button.dataset.sizeStep, 10) || 0;
          const assignedElsewhere = Object.entries(STATE.order.size_breakdown || {})
            .filter(([size]) => size !== s)
            .reduce((total, [, value]) => total + (parseInt(value, 10) || 0), 0);
          button.disabled = delta < 0 ? count <= 0 : assignedElsewhere + count >= qty;
          button.addEventListener("click", () => updateSize(parseInt(button.dataset.sizeStep, 10) || 0));
        });
        dom.sizeMatrix.appendChild(wrap);
      });
      validateSizeMatrix();
    }
    // B2B live calc
    updateB2bMeta();
  }

  function validateSizeMatrix() {
    if (!dom.sizeWarning) return;
    const qty = STATE.order.quantity || 0;
    const sum = Object.values(STATE.order.size_breakdown || {}).reduce((a, b) => a + (parseInt(b, 10) || 0), 0);
    if (qty > 1 && STATE.order.size_mode === "mixed" && sum !== qty) {
      dom.sizeWarning.hidden = false;
      dom.sizeWarning.textContent = sum < qty
        ? `Не вистачає ${qty - sum} шт. — додайте розміри.`
        : `Перевищено на ${sum - qty} шт. — зменшіть розміри.`;
      dom.sizeWarning.classList.toggle("is-error", sum !== qty);
    } else {
      dom.sizeWarning.hidden = true;
    }
  }

  function updateB2bMeta() {
    const qty = STATE.order.quantity || 0;
    const isB2b = STATE.mode === "brand";
    if (!isB2b || qty < 8) {
      if (dom.b2bMeta) dom.b2bMeta.hidden = true;
      return;
    }
    const tier = CONFIG.b2b_tier || { unit_step: 8, discount_per_unit: 10 };
    const steps = Math.floor(qty / tier.unit_step);
    const discount = steps * tier.discount_per_unit;
    if (dom.b2bMeta) dom.b2bMeta.hidden = false;
    if (dom.b2bDiscount) dom.b2bDiscount.textContent = `-${discount} грн / шт`;
  }

  // ── Gift toggle ─────────────────────────────────────────────
  function bindGiftToggle() {
    dom.giftToggle?.addEventListener("click", () => {
      STATE.order.gift_enabled = !STATE.order.gift_enabled;
      dom.giftToggle.classList.toggle("is-active", STATE.order.gift_enabled);
      dom.giftToggle.setAttribute("aria-pressed", String(STATE.order.gift_enabled));
      if (dom.giftToggleState) dom.giftToggleState.textContent = STATE.order.gift_enabled ? "Увімкнено" : "Вимкнено";
      if (dom.giftTextWrap) dom.giftTextWrap.hidden = !STATE.order.gift_enabled;
      updateGiftContinueLabel();
      refreshAll();
      persistDraft();
    });
    dom.giftTextInput?.addEventListener("input", () => {
      STATE.order.gift_text = dom.giftTextInput.value;
      persistDraft();
    });
    updateGiftContinueLabel();
  }

  function getGiftContinueLabel() {
    return ui(STATE.order.gift_enabled ? "gift_continue_on" : "gift_continue_off", "Далі");
  }

  function updateGiftContinueLabel() {
    if (dom.giftContinue) dom.giftContinue.textContent = getGiftContinueLabel();
  }

  // ── Generic inputs ──────────────────────────────────────────
  function bindGenericInputs() {
    const bindNote = (input, key, onInput = null) => {
      input?.addEventListener("input", () => {
        STATE.notes[key] = input.value;
        onInput?.(input.value);
        persistDraft();
      });
    };
    dom.placementNoteInput?.addEventListener("input", () => {
      STATE.print.placement_note = dom.placementNoteInput.value;
      persistDraft();
    });
    dom.briefInput?.addEventListener("input", () => {
      STATE.notes.brief = dom.briefInput.value;
      persistDraft();
    });
    dom.nameInput?.addEventListener("input", () => {
      STATE.contact.name = dom.nameInput.value;
      refreshAll();
      persistDraft();
    });
    dom.contactValueInput?.addEventListener("input", () => {
      STATE.contact.value = dom.contactValueInput.value;
      refreshAll();
      persistDraft();
    });
    dom.brandNameInput?.addEventListener("input", () => {
      STATE.notes.brand_name = dom.brandNameInput.value;
      persistDraft();
    });
    bindNote(dom.brandContactPerson, "brand_contact_person");
    bindNote(dom.brandContactValue, "brand_contact_value");
    dom.brandContactChannelList?.querySelectorAll("[data-brand-contact-channel]").forEach((button) => {
      button.addEventListener("click", () => {
        STATE.notes.brand_contact_channel = button.dataset.brandContactChannel || "";
        dom.brandContactChannelList.querySelectorAll("[data-brand-contact-channel]").forEach((item) => item.classList.toggle("is-active", item === button));
        persistDraft();
      });
    });
    dom.brandBusinessType?.addEventListener("change", () => {
      STATE.notes.brand_business_type = dom.brandBusinessType.value;
      persistDraft();
    });
    dom.brandProductList?.querySelectorAll("[data-brand-product]").forEach((button) => {
      button.addEventListener("click", () => {
        const value = button.dataset.brandProduct;
        const current = new Set(STATE.notes.brand_product_types || []);
        if (value === "all") current.clear();
        else current.delete("all");
        if (current.has(value)) current.delete(value);
        else current.add(value);
        STATE.notes.brand_product_types = [...current];
        dom.brandProductList.querySelectorAll("[data-brand-product]").forEach((item) => item.classList.toggle("is-active", current.has(item.dataset.brandProduct)));
        persistDraft();
      });
    });
    bindNote(dom.brandDeadline, "brand_deadline");
    dom.startFlow?.addEventListener("click", () => {
      enterStudio("start_button");
    });
  }

  // ── Waterfall step navigation ───────────────────────────────
  function bindWaterfallNav() {
    dom.stepEditButtons?.forEach((btn) => {
      btn.addEventListener("click", () => setActiveStep(btn.dataset.stepEdit));
    });
    dom.stepBackButtons?.forEach((btn) => {
      btn.addEventListener("click", () => setActiveStep(btn.dataset.stepBack));
    });
    dom.stepNextButtons?.forEach((btn) => {
      btn.addEventListener("click", () => {
        const target = btn.dataset.stepNext;
        if (canAdvance(STATE.ui.current_step)) {
          ensureFlowStarted(`step_next_${STATE.ui.current_step}`);
          trackStepComplete(STATE.ui.current_step, { transition_to: target });
          markStepDone(STATE.ui.current_step);
          setActiveStep(target, { fromStep: STATE.ui.current_step });
        } else {
          showStepProblem(STATE.ui.current_step);
        }
      });
    });
    dom.stepSkipButtons?.forEach((btn) => {
      btn.addEventListener("click", () => {
        // Skip current step (e.g. gift)
        STATE.order.gift_enabled = false;
        if (dom.giftToggle) dom.giftToggle.classList.remove("is-active");
        if (dom.giftToggle) dom.giftToggle.setAttribute("aria-pressed", "false");
        if (dom.giftTextWrap) dom.giftTextWrap.hidden = true;
        ensureFlowStarted(`step_skip_${STATE.ui.current_step}`);
        trackStepComplete(STATE.ui.current_step, { transition_to: btn.dataset.stepSkip, skipped: true });
        markStepDone(STATE.ui.current_step);
        setActiveStep(btn.dataset.stepSkip, { fromStep: STATE.ui.current_step });
        refreshAll();
        persistDraft();
      });
    });
  }

  function setActiveStep(key, opts = {}) {
    if (!STEPS.includes(key)) return;
    if (!opts.silent) {
      resetStatus();
      clearValidationTargets();
    }
    STATE.ui.current_step = key;
    const currentIndex = getStepIndex(key);
    document.querySelectorAll("[data-step]").forEach((section) => {
      const stepKey = section.dataset.step;
      const stepIndex = getStepIndex(stepKey);
      const isCurrent = stepKey === key;
      const isDone = STATE.ui.done_steps.has(stepKey);
      const isVisible = isCurrent || (isDone && stepIndex < currentIndex);
      section.hidden = !isVisible;
      section.classList.toggle("is-active", isCurrent);
      section.classList.toggle("is-done", !isCurrent && isDone);
      section.classList.toggle("is-pending", !isCurrent && !isDone && stepIndex > currentIndex);
      if (isCurrent) {
        section.classList.remove("has-validation-error");
        section.removeAttribute("aria-invalid");
      }
    });
    const studioKey = stateTools?.fromInternal(key) || "format";
    root.querySelectorAll("[data-studio-step]").forEach((group) => {
      group.classList.toggle("is-studio-active", group.dataset.studioStep === studioKey);
    });
    refreshAll();
    if (!opts.silent) {
      ensureFlowStarted(`step_enter_${key}`);
      trackStepEnter(key, { from_step: opts.fromStep || null });
      const target = document.getElementById(`cp-step-${key}`);
      scrollToStudioTarget(target);
    }
    if (analyticsState.flowStarted) persistDraft();
  }

  function markStepDone(key) {
    if (!STEPS.includes(key)) return;
    STATE.ui.done_steps.add(key);
  }

  function invalidateAfter(stepKey) {
    const stepIndex = getStepIndex(stepKey);
    STEPS.slice(stepIndex + 1).forEach((item) => STATE.ui.done_steps.delete(item));
    if (getStepIndex(STATE.ui.current_step) > stepIndex) {
      STATE.ui.current_step = stepKey;
    }
  }

  function afterChoice(stepKey) {
    ensureFlowStarted(`after_choice_${stepKey}`);
    const next = nextStepAfter(stepKey);
    trackStepComplete(stepKey, { transition_to: next });
    markStepDone(stepKey);
    refreshAll();
    // Determine next step
    if (next) setActiveStep(next, { fromStep: stepKey });
    persistDraft();
  }

  function nextStepAfter(stepKey) {
    let idx = STEPS.indexOf(stepKey);
    if (idx < 0) return null;
    for (let i = idx + 1; i < STEPS.length; i++) {
      if (STEPS[i] === "quantity" && STATE.product.type === "customer_garment") continue;
      return STEPS[i];
    }
    return null;
  }

  function canAdvance(stepKey) {
    switch (stepKey) {
      case "mode": return !!STATE.mode;
      case "product": return !!STATE.product.type;
      case "config":
        if (!STATE.product.type) return false;
        if (STATE.product.type === "customer_garment") return !!STATE.product.color && !!STATE.order.delivery_method;
        if (getProductConfig()?.fits?.length) return !!STATE.product.fit && !!STATE.product.fabric && !!STATE.product.color;
        return !!STATE.product.color;
      case "zones":
        if (STATE.print.zones.length === 0) return false;
        if (STATE.print.zones.includes("custom") && !STATE.print.placement_note.trim()) return false;
        if (STATE.print.zones.includes("sleeve")) {
          ensureSleeveZoneOptions();
          const leftNeedsText = isSleeveSideEnabled("left") && getSleeveMode("left") === "full_text" && !getSleeveText("left").trim();
          const rightNeedsText = isSleeveSideEnabled("right") && getSleeveMode("right") === "full_text" && !getSleeveText("right").trim();
          if (leftNeedsText || rightNeedsText) return false;
        }
        return true;
      case "artwork": return getArtworkValidationIssues().length === 0;
      case "quantity":
        if (STATE.order.quantity <= 0) return false;
        if (STATE.product.type === "customer_garment") return !!STATE.notes.garment_note.trim();
        if (STATE.order.size_mode === "manager") return true;
        if (STATE.order.quantity === 1) return Object.values(STATE.order.size_breakdown || {}).some((v) => v > 0);
        const sum = Object.values(STATE.order.size_breakdown || {}).reduce((a, b) => a + (parseInt(b, 10) || 0), 0);
        return sum === STATE.order.quantity;
      case "gift":
        return true; // gift is optional, the next button always works
      case "contact":
        return !!STATE.contact.channel && !!STATE.contact.name && !!STATE.contact.value;
      default:
        return true;
    }
  }

  function getStepProblem(stepKey) {
    const problems = {
      mode: ["Оберіть: для себе чи для команди / бренду.", "[data-mode-list] button"],
      product: ["Оберіть виріб для друку.", "[data-product-list] button"],
      config: ["Завершіть вибір посадки, тканини та кольору.", "[data-fit-list], [data-fabric-list], [data-color-list]"],
      zones: ["Оберіть хоча б одну зону та формат принта.", "[data-zone-list]"],
      artwork: ["Оберіть, чи макет готовий, чи потрібна допомога.", "[data-artwork-service-list]"],
      quantity: ["Вкажіть кількість і розподіліть усі речі за розмірами.", "[data-quantity-input]"],
      contact: ["Заповніть імʼя, канал звʼязку та контакт.", "[data-name-input]"],
    };
    if (stepKey === "config") {
      const productConfig = getProductConfig();
      if (STATE.product.type === "customer_garment" && !STATE.order.delivery_method) {
        return ["Оберіть спосіб передачі виробу.", "[data-own-shipping-list] button"];
      }
      if (productConfig?.fits?.length && !STATE.product.fit) {
        return [ui("config_fit_required", "Оберіть посадку."), "[data-fit-list] button"];
      }
      if (productConfig?.fits?.length && !STATE.product.fabric) {
        return [ui("config_fabric_required", "Оберіть тканину."), "[data-fabric-list] button:not([disabled])"];
      }
      if (!STATE.product.color) {
        return [ui("config_color_required", "Оберіть колір."), "[data-color-list] button"];
      }
    }
    if (stepKey === "zones" && STATE.print.zones.includes("sleeve")) {
      const missing = Array.from(dom.sleeveTextInputs || []).find((input) => !input.closest("[hidden]") && !input.value.trim());
      if (missing) return ["Додайте текст для вибраного рукава.", `[data-sleeve-text-input="${missing.dataset.sleeveTextInput}"]`];
    }
    if (stepKey === "zones" && STATE.print.zones.includes("custom") && !STATE.print.placement_note.trim()) {
      return ["Опишіть, де саме має бути нестандартний принт.", "[data-placement-note-input]"];
    }
    if (stepKey === "artwork") {
      const missingPlacement = getRequiredArtworkPlacements().find(
        (placement) => !(filesByPlacement.get(placement.placement_key) || []).length
      );
      if ((STATE.artwork.service_kind === "ready" || STATE.artwork.service_kind === "adjust") && missingPlacement) {
        return [
          `${ui("artwork_file_required", "Додайте макет для кожної вибраної зони.")} ${missingPlacement.label}.`,
          `[data-placement-key="${missingPlacement.placement_key}"] [data-dropzone-input]`,
        ];
      }
      if ((STATE.artwork.service_kind === "design" || STATE.artwork.service_kind === "adjust") && !STATE.notes.brief.trim()) {
        return [
          STATE.artwork.service_kind === "design"
            ? ui("artwork_brief_design_required", "Опишіть бриф / завдання для дизайну.")
            : ui("artwork_brief_adjust_required", "Опишіть, що саме потрібно змінити у файлі."),
          "[data-brief-input]",
        ];
      }
    }
    if (stepKey === "quantity") {
      if (STATE.order.quantity <= 0) return ["Вкажіть кількість виробів.", "[data-quantity-input]"];
      if (STATE.product.type === "customer_garment" && !STATE.notes.garment_note.trim()) {
        return ["Опишіть свій виріб для попереднього прорахунку.", "[data-own-item-note-wrap]"];
      }
      if (STATE.product.type !== "customer_garment" && STATE.order.size_mode !== "manager") {
        const sum = Object.values(STATE.order.size_breakdown || {}).reduce((total, value) => total + (parseInt(value, 10) || 0), 0);
        if (STATE.order.quantity === 1 && sum === 0) return ["Оберіть розмір виробу.", "[data-size-block]"];
        if (STATE.order.quantity > 1 && sum !== STATE.order.quantity) {
          return [
            sum < STATE.order.quantity ? `Розподіліть ще ${STATE.order.quantity - sum} шт. за розмірами.` : "Зменште кількість одного з розмірів.",
            "[data-size-block]",
          ];
        }
      }
    }
    return problems[stepKey] || ["Перевірте поточний крок.", `[data-step="${stepKey}"] button, [data-step="${stepKey}"] input`];
  }

  function clearValidationTargets() {
    root.querySelectorAll(".is-validation-target").forEach((node) => {
      node.classList.remove("is-validation-target");
      node.classList.remove("is-validation-group");
      node.removeAttribute("aria-invalid");
    });
    root.querySelectorAll("[data-inline-validation]").forEach((node) => node.remove());
  }

  function showStepProblem(stepKey) {
    clearValidationTargets();
    const [message, selector] = getStepProblem(stepKey);
    showStatus(message, "warning");
    const field = root.querySelector(selector);
    const step = root.querySelector(`[data-step="${stepKey}"]`);
    const group = field?.closest?.("[data-zone-list], [data-artwork-service-list], [data-fit-list], [data-fabric-list], [data-color-list]");
    const target = group || field?.closest(".cp-dropzone, .cp-size-matrix-cell") || field;
    const isValidationGroup = !!group;
    target?.classList.add("is-validation-target");
    if (isValidationGroup) target.classList.add("is-validation-group");
    target?.setAttribute("aria-invalid", "true");
    if (target && !target.querySelector("[data-inline-validation]")) {
      const note = document.createElement("small");
      note.className = "cp-inline-validation";
      if (isValidationGroup) note.classList.add("cp-inline-validation--group");
      note.dataset.inlineValidation = "true";
      note.textContent = message;
      target.appendChild(note);
    }
    scrollToStudioTarget(field || step);
    if (field) {
      window.setTimeout(() => field.focus?.({ preventScroll: true }), 180);
    }
  }

  // ── Refresh: stage card + receipt + summaries + side states ─
  function refreshAll() {
    normalizeClientState();
    if (dom.statusBox?.classList.contains("is-warning") && canAdvance(STATE.ui.current_step)) {
      resetStatus();
    }
    updateFlowPhase();
    syncProgressShellPlacement();
    updateStageVisibility();
    updateStageMeta();
    renderProductDetailNote();
    applyStageView(STATE.ui.stage_view || "front");
    renderModeChipsActive();
    renderProductCardsActive();
    updateBrandFieldsVisibility();
    updateSummaries();
    renderProgressStrip();
    updateB2bMeta();
    renderReceipt();
    updateFinalActionsAvailability();
    updateGiftContinueLabel();
    renderMobileBottomBar();
    previewController?.render();
    const studioIndex = stateTools?.progressIndex(STATE.ui.current_step) || 0;
    mobileShell?.update(studioIndex, STUDIO_STEPS.length || 8);
  }

  function updateFlowPhase() {
    const lobby = isLobbyPhase();
    const stageVisible = !lobby && STAGE_VISIBLE_AFTER.has(STATE.ui.current_step);
    root.dataset.flowPhase = lobby ? "lobby" : "studio";
    if (dom.shell) dom.shell.dataset.flowPhase = lobby ? "lobby" : "studio";
    if (dom.progressShell) dom.progressShell.hidden = lobby;
    if (dom.workbench) dom.workbench.classList.toggle("is-lobby-mode", !stageVisible);
  }

  function syncProgressShellPlacement() {
    if (!dom.progressShell || !progressHome?.parentNode) return;
    // Keep one stable progress owner. Re-parenting this node into each active
    // step caused mobile scroll jumps and made the eye action disappear.
    if (dom.progressShell.parentNode !== progressHome.parentNode) {
      progressHome.parentNode.insertBefore(dom.progressShell, progressHome.nextSibling);
    }
  }

  function renderProgressStrip() {
    if (!dom.progressStrip) return;
    const steps = STUDIO_STEPS;
    const currentStudioStep = stateTools?.fromInternal(STATE.ui.current_step) || "format";
    dom.progressStrip.innerHTML = "";
    steps.forEach((step, index) => {
      const btn = document.createElement("button");
      const stepKey = step.value;
      const stepIndex = index + 1;
      const isActive = currentStudioStep === stepKey;
      const isDone = stateTools?.isComplete(stepKey, STATE.ui.done_steps) || false;
      btn.type = "button";
      btn.className = "cp-build-chip";
      btn.dataset.stepIndex = String(stepIndex);
      if (isActive) btn.classList.add("is-active");
      else if (isDone) btn.classList.add("is-done");
      else btn.classList.add("is-pending");
      btn.setAttribute("aria-label", `${stepIndex}. ${step.label}. ${getProgressStepValue(stepKey)}`);
      btn.innerHTML = `
        <small>${stepIndex}. ${escapeHtml(step.label)}</small>
        <strong>${escapeHtml(getProgressStepValue(stepKey))}</strong>
      `;
      btn.disabled = !isActive && !isDone;
      btn.addEventListener("click", () => {
        if (!btn.disabled) setActiveStep(stateTools?.firstInternal(stepKey) || "mode");
      });
      dom.progressStrip.appendChild(btn);
    });
  }

  function getProgressStepValue(stepKey) {
    const summaryKey = {
      format: "mode",
      garment: "product",
      config: "config",
      placement: "zones",
      artwork: "artwork",
      quantity: "quantity",
      gift: "gift",
      contact: "contact",
    }[stepKey] || stepKey;
    const value = root.querySelector(`[data-step-summary-value="${summaryKey}"]`)?.textContent?.trim();
    return value && value !== "—" ? value : "Ще не вибрано";
  }

  function updateStageVisibility() {
    if (!dom.stageCard) return;
    const visible = !isLobbyPhase() && STAGE_VISIBLE_AFTER.has(STATE.ui.current_step);
    dom.stageCard.classList.toggle("is-hidden", !visible);
  }

  function updateStageMeta() {
    const cfg = getProductConfig();
    const placeholder = CONFIG.stage_meta || {};
    if (dom.stageEyebrow) dom.stageEyebrow.textContent = cfg ? (cfg.eyebrow || "Виріб") : "Сцена";
    if (dom.stageLabel) dom.stageLabel.textContent = cfg ? `${cfg.label} на сцені` : (placeholder.placeholder_title || "Виріб на сцені");
    if (dom.stageTitleSecondary) dom.stageTitleSecondary.textContent = cfg ? cfg.label : (placeholder.placeholder_title || "—");
    if (dom.stageNote) dom.stageNote.textContent = cfg ? (cfg.hero_note || cfg.summary || "") : (placeholder.placeholder_note || "Оберіть виріб, щоб побачити деталі.");
    if (dom.stageZones) {
      const zones = getExpandedPlacements().map((item) => item.label);
      dom.stageZones.textContent = zones.length ? zones.join(", ") : (cfg ? "Зони ще не активовані" : "Зони зʼявляться після вибору виробу");
    }
    if (dom.stageAddons) {
      const cfgAddons = (cfg && cfg.add_ons) || [];
      const labels = STATE.print.add_ons
        .map((v) => (cfgAddons.find((a) => a.value === v) || {}).label)
        .filter(Boolean);
      getExpandedPlacements().forEach((placement) => {
        if (placement.zone === "front") labels.push(`Спереду · ${placement.size_preset}`);
        else if (placement.zone === "back") labels.push(`На спині · ${placement.size_preset}`);
        else if (placement.zone === "sleeve") {
          labels.push(
            placement.mode === "full_text"
              ? `${placement.label} · текст`
              : `${placement.label} · A6`
          );
        }
      });
      const giftLabel = STATE.order.gift_enabled ? `Подарункова упаковка` : null;
      const all = [...labels, giftLabel].filter(Boolean);
      dom.stageAddons.textContent = all.length ? all.join(" · ") : (cfg ? "Без додаткових деталей." : "Поки що без активних деталей.");
    }
  }

  function getSharedSubmissionIssues() {
    const issues = [];
    if (!STATE.mode) issues.push(ui("mode_required", "Оберіть формат замовлення."));
    if (!STATE.product.type) issues.push(ui("product_required", "Оберіть виріб."));
    if (!canAdvance("config")) issues.push(ui("product_config_required", "Завершіть налаштування виробу."));
    if (!canAdvance("zones")) issues.push(ui("placement_required", "Оберіть і налаштуйте зони друку."));
    if (!STATE.artwork.service_kind) {
      issues.push(ui("artwork_service_required", "Оберіть сценарій роботи з макетом."));
    } else if (STATE.artwork.service_kind === "design" && !(STATE.notes.brief || "").trim()) {
      issues.push(ui("artwork_brief_design_required", "Опишіть бриф / завдання для дизайну."));
    } else if (STATE.artwork.service_kind === "adjust" && !(STATE.notes.brief || "").trim()) {
      issues.push(ui("artwork_brief_adjust_required", "Опишіть, що саме потрібно змінити у файлі."));
    }
    if (!canAdvance("quantity")) issues.push(ui("quantity_required", "Заповніть кількість і розміри."));
    if (!canAdvance("contact")) issues.push(ui("contact_required", "Заповніть ім'я, канал зв'язку і контакт."));
    return issues;
  }

  function buildActionPolicy() {
    const pricing = computePricing();
    const baseIssues = getSharedSubmissionIssues();
    const artworkIssues = getArtworkValidationIssues();
    const serviceKind = STATE.artwork.service_kind || "";
    const artworkRequiredForLead = serviceKind === "ready" || serviceKind === "adjust";
    if (submissionPolicy?.buildFinalActionPolicy) {
      return submissionPolicy.buildFinalActionPolicy({
        baseIssues,
        artworkIssues,
        estimateRequired: pricing.estimate_required,
        isCustomerGarment: STATE.product.type === "customer_garment",
        serviceKind,
      });
    }
    const leadReady = baseIssues.length === 0
      && (!artworkRequiredForLead || artworkIssues.length === 0);
    return {
      leadReady,
      cartReady: baseIssues.length === 0 && artworkIssues.length === 0 && !pricing.estimate_required && STATE.product.type !== "customer_garment",
      leadHint: baseIssues[0] || (artworkRequiredForLead && artworkIssues[0]) || "Бот відправить заявку в Telegram",
      cartHint: artworkIssues[0] || baseIssues[0] || "Передзамовлення зі снимком конфігурації",
    };
  }

  function updateBrandFieldsVisibility() {
    if (dom.brandFields) dom.brandFields.hidden = STATE.mode !== "brand";
    if (dom.brandBrief) dom.brandBrief.hidden = STATE.mode !== "brand";
    if (dom.personalManagerCta) dom.personalManagerCta.hidden = STATE.mode === "brand";
    if (STATE.mode === "brand" && dom.brandIntroName && !dom.brandIntroName.value && STATE.notes.brand_name) {
      dom.brandIntroName.value = STATE.notes.brand_name;
    }
    if (STATE.mode === "brand") {
      if (dom.brandContactPerson && !dom.brandContactPerson.value) dom.brandContactPerson.value = STATE.notes.brand_contact_person || "";
      if (dom.brandContactValue && !dom.brandContactValue.value) dom.brandContactValue.value = STATE.notes.brand_contact_value || "";
      if (dom.brandResource && !dom.brandResource.value) dom.brandResource.value = STATE.notes.brand_resource || "";
      if (dom.brandDeadline && !dom.brandDeadline.value) dom.brandDeadline.value = STATE.notes.brand_deadline || "";
      if (dom.brandBusinessType) dom.brandBusinessType.value = STATE.notes.brand_business_type || "brand";
      dom.brandContactChannelList?.querySelectorAll("[data-brand-contact-channel]").forEach((item) => item.classList.toggle("is-active", item.dataset.brandContactChannel === STATE.notes.brand_contact_channel));
      dom.brandProductList?.querySelectorAll("[data-brand-product]").forEach((item) => item.classList.toggle("is-active", (STATE.notes.brand_product_types || []).includes(item.dataset.brandProduct)));
    }
    updateBrandTierNote();
  }

  function updateBrandTierNote() {
    const tier = CONFIG.b2b_tier || { unit_step: 8, tiers: [] };
    const qty = Number(STATE.order.quantity || 0);
    renderBrandTierRail(tier, qty);
    if (!dom.brandTierNote) return;
    if (STATE.mode !== "brand") {
      dom.brandTierNote.textContent = "";
      return;
    }
    const next = (tier.tiers || []).find((item) => qty < Number(item.minimum || 0));
    dom.brandTierNote.textContent = next
      ? `Наступний рівень: ${next.label} — ${next.note}, від ${next.minimum} шт.`
      : (qty >= (tier.unit_step || 8) ? "Ви на найвигіднішому доступному рівні — фінальну ціну узгодить менеджер." : `Партійна ціна стартує від ${tier.unit_step || 8} шт.`);
  }

  function renderBrandTierRail(tier, qty) {
    if (!dom.brandTierRail) return;
    const tiers = tier.tiers || [];
    dom.brandTierRail.innerHTML = tiers.map((item, index) => {
      const minimum = Number(item.minimum || 0);
      const active = STATE.mode === "brand" && qty >= minimum;
      const current = active && (!tiers[index + 1] || qty < Number(tiers[index + 1].minimum || 0));
      const saving = Math.floor(minimum / Number(tier.unit_step || 8)) * Number(tier.discount_per_unit || 0);
      return `<span class="cp-brand-tier ${active ? "is-active" : ""} ${current ? "is-current" : ""}">
        <span class="cp-brand-tier-marker">${minimum}+</span>
        <strong>${escapeHtml(item.label || "")}</strong>
        <small>${saving ? `−${saving} грн/шт · ` : ""}${escapeHtml(item.note || "")}</small>
      </span>`;
    }).join("");
    dom.brandTierRail.classList.toggle("is-visible", STATE.mode === "brand");
  }

  function renderFinalChecklist(actionPolicy) {
    if (!dom.finalChecklist) return;
    const items = [
      { step: "mode", label: "Формат", ready: !!STATE.mode, detail: "Для себе або для команди" },
      { step: "product", label: "Виріб", ready: !!STATE.product.type, detail: "Основа і посадка" },
      { step: "config", label: "Налаштування", ready: canAdvance("config"), detail: "Посадка, тканина і колір" },
      { step: "zones", label: "Зони", ready: canAdvance("zones"), detail: "Розташування та формат" },
      { step: "artwork", label: "Макет", ready: getArtworkValidationIssues().length === 0 && !!STATE.artwork.service_kind, detail: "Файл, бриф або дизайн" },
      { step: "quantity", label: "Кількість", ready: canAdvance("quantity"), detail: STATE.product.type === "customer_garment" ? "Кількість без розмірів" : "Кількість і розміри" },
      { step: "gift", label: "Подарунок", ready: true, detail: STATE.order.gift_enabled ? "Упаковка додана" : "Без упаковки" },
      { step: "contact", label: "Контакт", ready: canAdvance("contact"), detail: "Імʼя і канал звʼязку" },
    ];
    dom.finalChecklist.innerHTML = items.map((item) => {
      const stateLabel = item.ready ? "Готово" : "Потрібно заповнити";
      return `<li class="cp-checklist-item ${item.ready ? "is-ready" : "is-missing"}">
        <span class="cp-checklist-mark" aria-hidden="true">${item.ready ? "✓" : "!"}</span>
        <span class="cp-checklist-copy"><strong>${item.label}</strong><small>${item.detail}</small></span>
        <span class="cp-checklist-state">${item.ready ? stateLabel : `<button type="button" data-checklist-step="${item.step}">${stateLabel}</button>`}</span>
      </li>`;
    }).join("");
    dom.finalChecklist.querySelectorAll("[data-checklist-step]").forEach((button) => {
      button.addEventListener("click", () => setActiveStep(button.dataset.checklistStep, { fromStep: "contact" }));
    });
    dom.finalChecklist.dataset.ready = actionPolicy.leadReady ? "true" : "false";
  }

  function updateSummaries() {
    setStepSummary("mode", STATE.mode === "brand" ? "Для команди / бренду" : STATE.mode === "personal" ? "Для себе" : "—");

    const cfg = getProductConfig();
    setStepSummary("product", cfg ? cfg.label : "—");

    if (STATE.product.type === "hoodie") {
      const parts = [];
      const colorPalette = cfg?.fit_colors?.[STATE.product.fit] || cfg?.colors || [];
      if (STATE.product.fit) parts.push(STATE.product.fit === "oversize" ? "Оверсайз" : "Класичний");
      if (STATE.product.fabric) parts.push(STATE.product.fabric === "premium" ? "Преміум" : "База");
      if (STATE.product.color) {
        const c = colorPalette.find((x) => x.value === STATE.product.color);
        if (c) parts.push(c.label);
      }
      if (STATE.print.add_ons.includes("lacing")) parts.push("Люверси");
      setStepSummary("config", parts.length ? parts.join(" · ") : "—");
    } else {
      const colorPalette = cfg?.fit_colors?.[STATE.product.fit] || cfg?.colors || [];
      const c = colorPalette.find((x) => x.value === STATE.product.color) || null;
      const parts = [];
      const selectedFabric = getSelectedFabricConfig();
      if (STATE.product.fit) parts.push(STATE.product.fit === "oversize" ? "Оверсайз" : "Класична");
      if (selectedFabric) parts.push(selectedFabric.label);
      else if (STATE.product.fabric) parts.push(STATE.product.fabric);
      if (c) parts.push(c.label);
      setStepSummary("config", parts.length ? parts.join(" · ") : "—");
    }

    setStepSummary("zones", getExpandedPlacements().length
      ? getExpandedPlacements().map((placement) => {
        if (placement.zone === "front") return `${placement.label} · ${placement.size_preset}`;
        if (placement.zone === "back") return `${placement.label} · ${placement.size_preset}`;
        if (placement.zone === "sleeve") {
          return placement.mode === "full_text"
            ? `${placement.label} · текст`
            : `${placement.label} · A6`;
        }
        return placement.label;
      }).join(", ")
      : "—");

    if (STATE.artwork.service_kind) {
      const services = CONFIG.artwork_services || [];
      const item = services.find((s) => s.value === STATE.artwork.service_kind);
      const totalFiles = Array.from(filesByPlacement.values()).reduce((acc, list) => acc + list.length, 0);
      setStepSummary("artwork", `${item ? item.label : "—"}${totalFiles ? ` · ${totalFiles} файл(ів)` : ""}`);
    } else {
      setStepSummary("artwork", "—");
    }

    if (STATE.order.quantity > 0) {
      let breakdown = "";
      if (STATE.product.type !== "customer_garment" && STATE.order.size_mode !== "manager") {
        const entries = Object.entries(STATE.order.size_breakdown || {})
          .filter(([, v]) => v > 0)
          .map(([k, v]) => `${k}×${v}`);
        if (entries.length) breakdown = ` · ${entries.join(", ")}`;
      } else if (STATE.order.size_mode === "manager") {
        breakdown = " · через менеджера";
      }
      setStepSummary("quantity", `${STATE.order.quantity} шт${breakdown}`);
    } else {
      setStepSummary("quantity", "—");
    }

    setStepSummary("gift", STATE.order.gift_enabled ? `Так (+${(CONFIG.gift_service || {}).price || 100} грн)` : "Без подарунку");

    if (STATE.contact.channel || STATE.contact.value) {
      const channelMap = (CONFIG.contact_channels || []).reduce((acc, ch) => { acc[ch.value] = ch.label; return acc; }, {});
      const ch = channelMap[STATE.contact.channel] || "—";
      setStepSummary("contact", `${ch}${STATE.contact.value ? `: ${STATE.contact.value}` : ""}`);
    } else {
      setStepSummary("contact", "—");
    }
  }

  function setStepSummary(key, value) {
    const el = root.querySelector(`[data-step-summary-value="${key}"]`);
    if (el) el.textContent = value;
  }

  // ── Pricing ─────────────────────────────────────────────────
  function computePricing() {
    const cfg = getProductConfig();
    if (!cfg || !cfg.pricing) {
      return {
        base_price: null,
        design_price: 0,
        addons_price: 0,
        gift_price: 0,
        zones_price: 0,
        unit_total: null,
        b2b_discount_per_unit: 0,
        final_total: null,
        estimate_required: !!cfg && (cfg.pricing?.base === null || STATE.product.type === "customer_garment"),
        estimate_reason: STATE.product.type === "customer_garment" ? "Свій одяг — потрібен ручний прорахунок." : "",
        breakdown: [],
      };
    }
    const pricing = cfg.pricing;
    const breakdown = [];

    let base = pricing.base ?? 0;
    const fabricConfig = getSelectedFabricConfig();
    const fabricOptions = STATE.product.fit ? (cfg.fabrics?.[STATE.product.fit] || []) : [];
    const hasFabricChoice = fabricOptions.length > 1;
    if (fabricConfig && hasFabricChoice) {
      base += Number(fabricConfig.price_delta || 0);
    } else if (!fabricConfig && hasFabricChoice) {
      if (STATE.product.fabric === "premium") base += pricing.premium_delta || 0;
      else if (STATE.product.fabric === "thermo") base += pricing.thermo_delta || 0;
    }
    if (STATE.product.fit === "oversize") base += pricing.oversize_delta || 0;
    const baseLabelParts = ["База", cfg.label];
    if (STATE.product.fit === "oversize") baseLabelParts.push("Оверсайз");
    else if (STATE.product.fit === "regular") baseLabelParts.push(cfg.label === "Футболка" ? "Класична" : "Класичний");
    if (fabricConfig?.label) baseLabelParts.push(fabricConfig.label);
    breakdown.push({ label: baseLabelParts.join(" · "), value: base });

    const placements = getExpandedPlacements();
    const printPrice = placements.reduce((total, placement) => {
      const preset = placement.zone === "front"
        ? FRONT_SIZE_PRESETS[placement.size_preset]
        : placement.zone === "back"
          ? BACK_SIZE_PRESETS[placement.size_preset]
          : placement.zone === "custom"
            ? CUSTOM_ZONE_PRESETS[placement.size_preset]
            : null;
      const sleevePreset = placement.zone === "sleeve"
        ? SLEEVE_MODE_OPTIONS[placement.mode || SLEEVE_MODE_DEFAULT]
        : null;
      return total + Number(preset?.price_delta || sleevePreset?.price_delta || 0);
    }, 0);
    if (printPrice > 0) breakdown.push({ label: `Друк · ${placements.length} ${placements.length === 1 ? "зона" : "зони"}`, value: printPrice });

    const extraZones = Math.max(0, placements.length - 1);
    const zonesPrice = extraZones * (pricing.extra_zone_delta || 0);
    if (extraZones > 0) breakdown.push({ label: `Додаткові зони · ${extraZones}`, value: zonesPrice });

    let designPrice = 0;
    const services = CONFIG.artwork_services || [];
    const svc = services.find((s) => s.value === STATE.artwork.service_kind);
    if (svc && svc.price_delta) {
      designPrice = svc.price_delta;
      breakdown.push({ label: svc.label, value: designPrice });
    }

    let addonsPrice = 0;
    const cfgAddons = cfg.add_ons || [];
    STATE.print.add_ons.forEach((value) => {
      const a = cfgAddons.find((x) => x.value === value);
      if (a && a.price_delta) {
        addonsPrice += a.price_delta;
        breakdown.push({ label: a.label, value: a.price_delta });
      }
    });

    let giftPrice = 0;
    if (STATE.order.gift_enabled) {
      giftPrice = (CONFIG.gift_service || {}).price || 0;
      if (giftPrice) breakdown.push({ label: "Подарункова упаковка", value: giftPrice });
    }

    let unitTotal = base + printPrice + zonesPrice + designPrice + addonsPrice;
    let b2bDiscountPerUnit = 0;
    const qty = STATE.order.quantity || 0;
    if (STATE.mode === "brand" && qty >= 8) {
      const tier = CONFIG.b2b_tier || { unit_step: 8, discount_per_unit: 10 };
      const steps = Math.floor(qty / tier.unit_step);
      b2bDiscountPerUnit = steps * tier.discount_per_unit;
    }
    const unitAfterDiscount = Math.max(0, unitTotal - b2bDiscountPerUnit);
    const subTotal = unitAfterDiscount * Math.max(1, qty || 1);
    const finalTotal = subTotal + giftPrice;
    if (b2bDiscountPerUnit > 0) {
      breakdown.push({ label: `B2B знижка (-${b2bDiscountPerUnit} грн/шт × ${qty})`, value: -b2bDiscountPerUnit * qty });
    }

    return {
      base_price: base,
      design_price: designPrice,
      addons_price: addonsPrice,
      gift_price: giftPrice,
      zones_price: zonesPrice,
      print_price: printPrice,
      unit_total: unitAfterDiscount,
      b2b_discount_per_unit: b2bDiscountPerUnit,
      final_total: finalTotal,
      quantity: qty,
      estimate_required: false,
      estimate_reason: "",
      breakdown,
    };
  }

  function renderReceipt() {
    if (!dom.receiptList || !dom.receiptTotal) return;
    const pricing = computePricing();
    if (!STATE.product.type) {
      dom.receiptTotal.textContent = "Ціну побачите після першого вибору";
      dom.receiptList.innerHTML = `<li class="is-empty">Поки що нічого не додано — оберіть виріб, щоб побачити прорахунок.</li>`;
      if (dom.receiptMode) dom.receiptMode.textContent = "Базова конфігурація";
      if (dom.receiptHint) dom.receiptHint.textContent = "Ціна оновлюється в реальному часі.";
      return;
    }
    if (pricing.estimate_required || pricing.base_price === null) {
      dom.receiptTotal.textContent = "Прорахунок з менеджером";
      dom.receiptList.innerHTML = `<li class="is-empty">${escapeHtml(pricing.estimate_reason || "Цей виріб потребує індивідуального прорахунку.")}</li>`;
      if (dom.receiptMode) dom.receiptMode.textContent = STATE.mode === "brand" ? "B2B" : "B2C";
      if (dom.receiptHint) dom.receiptHint.textContent = "Натисніть «Надіслати менеджеру» — підготуємо точну ціну.";
      return;
    }
    dom.receiptList.innerHTML = pricing.breakdown.map((row) => `
      <li><span>${escapeHtml(row.label)}</span><strong>${formatPrice(row.value)}</strong></li>
    `).join("");
    const qty = STATE.order.quantity || 1;
    const totalText = qty > 1
      ? `${formatPrice(pricing.final_total)} <small>· ${formatPrice(pricing.unit_total)}/шт × ${qty}${pricing.gift_price ? " + подарунок" : ""}</small>`
      : `${formatPrice(pricing.final_total)}`;
    dom.receiptTotal.innerHTML = totalText;
    if (dom.receiptMode) dom.receiptMode.textContent = STATE.mode === "brand" ? "B2B · опт" : "B2C · роздріб";
    if (dom.receiptHint) {
      const hints = [];
      if (pricing.b2b_discount_per_unit > 0) hints.push(`B2B: -${pricing.b2b_discount_per_unit} грн/шт`);
      if (STATE.order.gift_enabled) hints.push("Подарунок включено");
      hints.push("Ціна оновлюється в реальному часі.");
      dom.receiptHint.textContent = hints.join(" · ");
    }
  }

  function updateFinalActionsAvailability() {
    const actionPolicy = buildActionPolicy();
    const pricing = computePricing();
    renderFinalChecklist(actionPolicy);
    if (dom.addToCartBtn) {
      dom.addToCartBtn.disabled = false;
      dom.addToCartBtn.setAttribute("aria-disabled", String(!actionPolicy.cartReady));
      dom.addToCartBtn.classList.toggle("is-disabled", !actionPolicy.cartReady);
    }
    if (dom.submitLeadBtn) {
      dom.submitLeadBtn.disabled = false;
      dom.submitLeadBtn.setAttribute("aria-disabled", String(!actionPolicy.leadReady));
      dom.submitLeadBtn.classList.toggle("is-disabled", !actionPolicy.leadReady);
    }
    if (dom.cartActionHint) {
      dom.cartActionHint.textContent = actionPolicy.cartReady
        ? `${formatPrice(pricing.final_total)} · додамо в кошик зі снимком конфігурації`
        : actionPolicy.cartHint;
    }
    if (dom.leadActionHint) {
      dom.leadActionHint.textContent = actionPolicy.leadHint;
    }
  }

  // ── Mobile bottom bar (sticky на мобільному) ───────────────
  function renderMobileBottomBar() {
    if (!dom.mobileBar) return;
    const stepKey = STATE.ui.current_step || "mode";
    const lobby = isLobbyPhase();
    const stageVisible = !lobby && STAGE_VISIBLE_AFTER.has(stepKey);
    // Bar показуємо тільки після того як обрано режим (вийшли з lobby).
    if (lobby && !STATE.product.type) {
      dom.mobileBar.hidden = true;
      return;
    }
    dom.mobileBar.hidden = false;

    const pricing = computePricing();
    if (dom.mobileBarTotal) {
      if (!STATE.product.type) {
        dom.mobileBarTotal.textContent = "Оберіть виріб";
      } else if (pricing.estimate_required || pricing.base_price === null) {
        dom.mobileBarTotal.textContent = "Прорахунок з менеджером";
      } else if (pricing.final_total !== null && pricing.final_total !== undefined) {
        dom.mobileBarTotal.textContent = formatPrice(pricing.final_total);
      } else {
        dom.mobileBarTotal.textContent = "—";
      }
    }
    if (dom.mobileBarLabel) {
      dom.mobileBarLabel.textContent = pricing.estimate_required ? "Орієнтир" : "Поточна ціна";
    }
    if (dom.mobileBarMeta) {
      const qty = STATE.order.quantity || 0;
      if (qty > 1 && pricing.unit_total) {
        dom.mobileBarMeta.textContent = `${formatPrice(pricing.unit_total)} × ${qty}`;
      } else {
        dom.mobileBarMeta.textContent = "";
      }
    }
    // Текст кнопки залежить від кроку.
    const stepLabels = {
      mode: { label: "Обрати формат", target: "mode" },
      product: { label: "До виробу", target: "product" },
      config: { label: "Налаштувати", target: "config" },
      zones: { label: "До зон", target: "zones" },
      artwork: { label: "До макета", target: "artwork" },
      quantity: { label: "До кількості", target: "quantity" },
      gift: { label: "Далі", target: "gift" },
      contact: { label: "Надіслати менеджеру", target: "submit" },
    };
    const policy = buildActionPolicy();
    const stepIndex = getStepIndex(stepKey);
    const isLastStep = stepKey === "contact";
    const nextStepKey = STEPS[stepIndex + 1] || stepKey;
    const labelInfo = stepLabels[stepKey] || { label: "Далі", target: stepKey };
    if (dom.mobileBarActionLabel) {
      if (isLastStep) {
        dom.mobileBarActionLabel.textContent = "Надіслати";
      } else if (stepKey === "gift") {
        dom.mobileBarActionLabel.textContent = getGiftContinueLabel();
      } else {
        const nextLabel = stepLabels[nextStepKey]?.label || "Далі";
        dom.mobileBarActionLabel.textContent = nextLabel;
      }
    }
    if (dom.mobileBarAction) {
      dom.mobileBarAction.classList.toggle("is-disabled", isLastStep && !policy.leadReady);
      dom.mobileBarAction.dataset.stepTarget = isLastStep ? "submit" : nextStepKey;
    }
  }

  function bindMobileBottomBar() {
    if (!dom.mobileBarAction) return;
    dom.mobileBarAction.addEventListener("click", () => {
      const target = dom.mobileBarAction.dataset.stepTarget;
      if (target === "submit") {
        handleSubmitLead();
        return;
      }
      if (target) {
        if (!canAdvance(STATE.ui.current_step)) {
          showStepProblem(STATE.ui.current_step);
          return;
        }
        trackStepComplete(STATE.ui.current_step, { transition_to: target, source: "mobile_action" });
        markStepDone(STATE.ui.current_step);
        setActiveStep(target, { fromStep: STATE.ui.current_step });
      }
    });
  }

  function focusFirstIncomplete() {
    const incomplete = STEPS.find((step) => step !== "gift" && !canAdvance(step));
    if (!incomplete) return false;
    setActiveStep(incomplete, { fromStep: STATE.ui.current_step });
    showStepProblem(incomplete);
    return true;
  }

  function navigateToStep(stepKey) {
    if (!stepKey || !STEPS.includes(stepKey)) return;
    STATE.ui.current_step = stepKey;
    // Помічаємо всі попередні кроки як завершені.
    const idx = STEPS.indexOf(stepKey);
    if (!(STATE.ui.done_steps instanceof Set)) {
      STATE.ui.done_steps = new Set();
    }
    STEPS.slice(0, idx).forEach((step) => STATE.ui.done_steps.add(step));
    refreshAll();
    persistDraft();
    // Скролимо до секції плавно.
    const sectionEl = root.querySelector(`[data-step="${stepKey}"]`);
    scrollToStudioTarget(sectionEl);
  }

  // ── Final actions ───────────────────────────────────────────
  function bindFinalActions() {
    dom.addToCartBtn?.addEventListener("click", handleAddToCart);
    dom.submitLeadBtn?.addEventListener("click", (event) => {
      event.preventDefault();
      handleSubmitLead();
    });
    dom.form?.addEventListener("submit", (event) => {
      event.preventDefault();
      handleSubmitLead();
    });
    dom.safeExitButtons?.forEach((button) => {
      button.addEventListener("click", handleSafeExit);
    });
  }

  function buildSnapshot(submissionType) {
    return {
      version: 2,
      submission_type: submissionType,
      quick_start_mode: "start_blank",
      mode: STATE.mode || "personal",
      starter_style: "",
      product: { ...STATE.product },
      print: {
        zones: [...STATE.print.zones],
        add_ons: [...STATE.print.add_ons],
        placement_note: STATE.print.placement_note || "",
        zone_options: buildZoneOptionsSnapshot(),
      },
      artwork: { ...STATE.artwork, files: serializeFiles() },
      order: {
        quantity: STATE.order.quantity || 0,
        size_mode: STATE.order.size_mode || "single",
        sizes_note: STATE.order.sizes_note || "",
        size_breakdown: { ...STATE.order.size_breakdown },
        delivery_method: STATE.order.delivery_method || "",
        gift: { enabled: !!STATE.order.gift_enabled, text: STATE.order.gift_text || "" },
      },
      contact: { ...STATE.contact },
      notes: { ...STATE.notes },
      pricing: computePricing(),
      ui: { current_step: STATE.ui.current_step },
    };
  }

  function serializeFiles() {
    const out = [];
    collectOrderedFiles().forEach(({ zone, placement_key, label, file }, index) => {
      out.push({
        name: file.name,
        zone,
        placement_key,
        label,
        status: STATE.artwork.triage_status || "needs-review",
        role: "design",
        file_index: index,
      });
    });
    if (garmentPhotoFile) {
      out.push({
        name: garmentPhotoFile.name,
        zone: "garment_reference",
        placement_key: "garment_reference",
        label: "Фото виробу",
        status: "reference-only",
        role: "garment_reference",
        file_index: out.length,
      });
    }
    return out;
  }

  function buildPlacementSpecs(snapshot) {
    const specs = [];
    let artworkFileIndex = 0;
    const zoneOptions = snapshot.print?.zone_options || {};
    (snapshot.print?.zones || []).forEach((zone, zoneIndex) => {
      const options = zoneOptions[zone] || {};
      if (zone === "sleeve") {
        [
          { side: "left", enabled: !!options.left_enabled, mode: options.left_mode || SLEEVE_MODE_DEFAULT, text: options.left_text || "", scene_preview: options.left_scene_preview },
          { side: "right", enabled: !!options.right_enabled, mode: options.right_mode || SLEEVE_MODE_DEFAULT, text: options.right_text || "", scene_preview: options.right_scene_preview },
        ].forEach((item) => {
          if (!item.enabled) return;
          const requiresArtworkFile = item.mode !== "full_text";
          specs.push({
            zone: "sleeve",
            placement_key: `sleeve_${item.side}`,
            label: (CONFIG.zone_labels && CONFIG.zone_labels[`sleeve_${item.side}`]) || `sleeve_${item.side}`,
            variant: specs.length === 0 ? "standard" : "estimate",
            is_free: specs.length === 0,
            format: item.mode === "full_text" ? "text_vertical" : "custom",
            size: item.mode === "full_text" ? "full_sleeve" : "A6",
            mode: item.mode,
            text: item.text,
            side: item.side,
            attachment_role: "design",
            requires_artwork_file: requiresArtworkFile,
            ...(requiresArtworkFile ? { file_index: artworkFileIndex++ } : {}),
            ...(item.scene_preview ? { scene_preview: item.scene_preview } : {}),
          });
        });
        return;
      }
      const spec = {
        zone,
        placement_key: zone,
        label: (CONFIG.zone_labels && CONFIG.zone_labels[zone]) || zone,
        variant: specs.length === 0 && (zone === "front" || zone === "back") ? "standard" : "estimate",
        is_free: specs.length === 0,
        format: zone === "front" || zone === "back" ? "standard" : "custom",
        size: zone === "front" || zone === "back" ? "standard" : "manager_review",
        file_index: artworkFileIndex++,
        attachment_role: "design",
        requires_artwork_file: true,
      };
      if ((zone === "front" || zone === "back") && options.size_preset) {
        spec.size_preset = options.size_preset;
        spec.size = options.size_preset;
      }
      if (zone === "custom") {
        spec.size_preset = options.size_preset || "A6";
        spec.size = spec.size_preset;
        spec.location = options.location || "shoulder";
        spec.placement_note = snapshot.print?.placement_note || "";
      }
      if (options.scene_preview) spec.scene_preview = options.scene_preview;
      specs.push(spec);
    });
    return specs;
  }

  function buildSizesNoteForSubmit() {
    // Менеджер у Telegram має бачити розміри завжди, тому збираємо
    // людинозрозумілий підсумок із size_breakdown + вільної примітки.
    const note = (STATE.order.sizes_note || "").trim();
    if (STATE.product.type === "customer_garment") return note;
    if (STATE.order.size_mode === "manager") {
      return note ? `Уточнити з менеджером. ${note}` : "Уточнити з менеджером";
    }
    const grid = CONFIG.size_grid || ["S", "M", "L", "XL", "2XL"];
    const summary = Object.entries(STATE.order.size_breakdown || {})
      .map(([size, count]) => [size, parseInt(count, 10) || 0])
      .filter(([, count]) => count > 0)
      .sort((a, b) => grid.indexOf(a[0]) - grid.indexOf(b[0]))
      .map(([size, count]) => `${size} — ${count} шт`)
      .join(", ");
    if (summary && note) return `${summary}. ${note}`;
    return summary || note;
  }

  function buildFormData(submissionType) {
    const fd = new FormData();
    const snap = buildSnapshot(submissionType);
    const pricing = snap.pricing || computePricing();
    fd.append("placement_specs_json", JSON.stringify(buildPlacementSpecs(snap)));
    fd.append("pricing_snapshot_json", JSON.stringify(pricing));
    fd.append("config_draft_json", JSON.stringify(snap));

    fd.append("service_kind", STATE.artwork.service_kind || "design");
    fd.append("product_type", STATE.product.type || "hoodie");
    (STATE.print.zones || []).forEach((z) => fd.append("placements", z));
    fd.append("placement_note", STATE.print.placement_note || "");
    fd.append("quantity", String(STATE.order.quantity || 1));
    fd.append("size_mode", STATE.order.size_mode || "single");
    fd.append("sizes_note", buildSizesNoteForSubmit());
    fd.append("client_kind", STATE.mode || "personal");
    fd.append("business_kind", STATE.mode === "brand" ? "branding" : "");
    fd.append("brand_name", STATE.notes.brand_name || "");
    fd.append("fit", STATE.product.fit || "");
    fd.append("fabric", STATE.product.fabric || "");
    fd.append("color_choice", STATE.product.color || "");
    fd.append("garment_note", STATE.notes.garment_note || "");
    fd.append("file_triage_status", STATE.artwork.triage_status || "needs-review");
    fd.append("exit_step", STATE.ui.current_step || "contact");
    fd.append("name", STATE.contact.name || "");
    fd.append("contact_channel", STATE.contact.channel || "");
    fd.append("contact_value", STATE.contact.value || "");
    fd.append("brief", STATE.notes.brief || "");

    collectOrderedFiles().forEach(({ file }) => {
      fd.append("files", file);
    });
    if (garmentPhotoFile) fd.append("files", garmentPhotoFile);
    return fd;
  }

  async function handleSubmitLead() {
    if (leadSubmitInFlight) return;
    const actionPolicy = buildActionPolicy();
    if (!actionPolicy.leadReady) {
      showStatus(actionPolicy.leadHint || "Перевірте форму перед відправкою.", "error");
      focusFirstIncomplete();
      return;
    }
    const url = CONFIG.submit_url;
    if (!url) return;
    leadSubmitInFlight = true;
    setBusy(true);
    showStatus("Заявку відправляємо менеджеру…", "warning");
    try {
      const response = await fetch(url, {
        method: "POST",
        headers: { "X-CSRFToken": getCsrfToken(), "X-Requested-With": "XMLHttpRequest" },
        body: buildFormData("lead"),
      });
      const data = await safeJson(response);
      if (!response.ok) {
        const msg = data?.errors ? formatErrors(data.errors) : "Не вдалося надіслати заявку. Спробуйте ще раз.";
        showStatus(msg, "error");
        return;
      }
      clearDraft();
      const number = data?.lead_number ? ` №${data.lead_number}` : "";
      showStatus(`Дякуємо! Заявка${number} вже у менеджера.`, "success");
      try {
        if (window.trackEvent) {
          const pricing = computePricing();
          const leadValue = (pricing && Number.isFinite(pricing.final_total) && pricing.final_total > 0)
            ? pricing.final_total
            : 0;
          const eventId = (typeof window.safeGenerateAnalyticsEventId === "function")
            ? window.safeGenerateAnalyticsEventId()
            : String(Date.now());
          const meta = (typeof window.buildMetaWithUserData === "function")
            ? window.buildMetaWithUserData(eventId)
            : { event_id: eventId };
          const leadPayload = {
            content_name: "Custom print lead",
            content_category: "custom-print",
            currency: "UAH",
            event_id: eventId,
            __meta: meta,
          };
          if (leadValue > 0) leadPayload.value = leadValue;
          if (data?.lead_number) leadPayload.lead_number = String(data.lead_number);
          window.trackEvent("Lead", leadPayload);
        }
      } catch (_) { }
    } catch (error) {
      console.error("[custom-print v2] submit lead failed", error);
      showStatus("Сервер тимчасово недоступний. Спробуйте через кілька хвилин.", "error");
    } finally {
      leadSubmitInFlight = false;
      setBusy(false);
    }
  }

  async function handleAddToCart() {
    if (cartSubmitInFlight) return;
    const actionPolicy = buildActionPolicy();
    if (!actionPolicy.cartReady) {
      showStatus(actionPolicy.cartHint || "Перевірте форму перед додаванням у кошик.", actionPolicy.leadReady ? "warning" : "error");
      focusFirstIncomplete();
      return;
    }
    const pricing = computePricing();
    const url = CONFIG.add_to_cart_url;
    if (!url) {
      showStatus("Кошик тимчасово недоступний. Скористайтесь Telegram-кнопкою.", "error");
      return;
    }
    cartSubmitInFlight = true;
    setBusy(true);
    showStatus("Додаємо конфігурацію в кошик і передаємо її менеджеру…", "warning");
    try {
      const response = await fetch(url, {
        method: "POST",
        headers: { "X-CSRFToken": getCsrfToken(), "X-Requested-With": "XMLHttpRequest" },
        body: buildFormData("cart"),
      });
      const data = await safeJson(response);
      if (!response.ok || !data?.ok) {
        const msg = data?.errors ? formatErrors(data.errors) : (data?.error || "Не вдалося додати в кошик. Спробуйте ще раз.");
        showStatus(msg, "error");
        return;
      }
      clearDraft();
      showStatus(`Додано в кошик · ${formatPrice(pricing.final_total)}. Перейти до оформлення?`, "success");
      try {
        if (window.trackEvent) {
          const cartValue = (pricing && Number.isFinite(pricing.final_total) && pricing.final_total > 0)
            ? pricing.final_total
            : 0;
          const qty = (pricing && pricing.quantity) || STATE.order.quantity || 1;
          const offerId = data?.offer_id || data?.cart_key || ("custom-" + (STATE.product.type || "garment"));
          const eventId = (typeof window.safeGenerateAnalyticsEventId === "function")
            ? window.safeGenerateAnalyticsEventId()
            : String(Date.now());
          const meta = (typeof window.buildMetaWithUserData === "function")
            ? window.buildMetaWithUserData(eventId)
            : { event_id: eventId };
          const unitPrice = qty > 0 ? (cartValue / qty) : cartValue;
          const cartPayload = {
            content_ids: [offerId],
            content_type: "product",
            content_name: "Custom print",
            content_category: "custom-print",
            currency: "UAH",
            num_items: qty,
            contents: [{ id: offerId, quantity: qty, item_price: unitPrice, brand: "TwoComms" }],
            event_id: eventId,
            __meta: meta,
          };
          if (cartValue > 0) cartPayload.value = cartValue;
          window.trackEvent("AddToCart", cartPayload);
        }
      } catch (_) { }
      dialogFlow?.openCartReviewDialog({
        trigger: dom.addToCartBtn,
        leadNumber: data.lead_number,
        cartUrl: data.cart_url,
      });
    } catch (error) {
      console.error("[custom-print v2] add to cart failed", error);
      showStatus("Не вдалося додати в кошик. Спробуйте ще раз.", "error");
    } finally {
      cartSubmitInFlight = false;
      setBusy(false);
    }
  }

  async function handleSafeExit() {
    const url = CONFIG.safe_exit_url;
    if (!url) {
      window.open(CONFIG.telegram_manager_url || "https://t.me/twocomms", "_blank");
      return;
    }
    try {
      const snap = buildSnapshot("safe_exit");
      const response = await fetch(url, {
        method: "POST",
        headers: { "X-CSRFToken": getCsrfToken(), "Content-Type": "application/json", "X-Requested-With": "XMLHttpRequest" },
        body: JSON.stringify(snap),
      });
      const data = await safeJson(response);
      const link = data?.manager_url || CONFIG.telegram_manager_url || "https://t.me/twocomms";
      window.open(link, "_blank");
    } catch (error) {
      console.error("[custom-print v2] safe exit failed", error);
      window.open(CONFIG.telegram_manager_url || "https://t.me/twocomms", "_blank");
    }
  }

  function setBusy(busy) {
    [dom.addToCartBtn, dom.submitLeadBtn].forEach((btn) => {
      if (!btn) return;
      btn.classList.toggle("is-busy", busy);
      if (busy) {
        btn.dataset.wasDisabled = btn.disabled ? "1" : "0";
        btn.disabled = true;
      } else {
        btn.disabled = btn.dataset.wasDisabled === "1";
        delete btn.dataset.wasDisabled;
      }
    });
    if (!busy) updateFinalActionsAvailability();
  }

  function showStatus(message, kind) {
    if (!dom.statusBox) return;
    dom.statusBox.textContent = message;
    dom.statusBox.classList.remove("is-success", "is-error", "is-warning");
    if (kind === "success") dom.statusBox.classList.add("is-success");
    if (kind === "error") dom.statusBox.classList.add("is-error");
    if (kind === "warning") dom.statusBox.classList.add("is-warning");
  }

  function resetStatus() {
    if (!dom.statusBox) return;
    dom.statusBox.textContent = dom.statusBox.dataset.defaultStatus || "";
    dom.statusBox.classList.remove("is-success", "is-error", "is-warning");
  }

  // ── Helpers ─────────────────────────────────────────────────
  function getCsrfToken() {
    const match = document.cookie.match(/(?:^|;\s*)csrftoken=([^;]+)/);
    return match ? decodeURIComponent(match[1]) : "";
  }

  async function safeJson(response) {
    try { return await response.json(); } catch { return null; }
  }

  function formatErrors(errors) {
    const lines = [];
    Object.entries(errors).forEach(([field, list]) => {
      (list || []).forEach((m) => lines.push(`${field}: ${m}`));
    });
    return lines.join("\n");
  }

  function formatPrice(value) {
    if (value === null || value === undefined) return "—";
    return `${Math.round(value).toLocaleString("uk-UA")} грн`;
  }

  function formatBytes(bytes) {
    if (!bytes && bytes !== 0) return "";
    if (bytes < 1024) return `${bytes} B`;
    if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
    return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
  }

  function escapeHtml(value) {
    if (!value) return "";
    return String(value)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  function mixHex(base, target, weight) {
    const clampWeight = Math.min(Math.max(weight, 0), 1);
    const parse = (hex) => {
      const normalized = String(hex || "").replace("#", "");
      const value = normalized.length === 3
        ? normalized.split("").map((char) => char + char).join("")
        : normalized.padEnd(6, "0").slice(0, 6);
      return {
        r: parseInt(value.slice(0, 2), 16),
        g: parseInt(value.slice(2, 4), 16),
        b: parseInt(value.slice(4, 6), 16),
      };
    };
    const a = parse(base);
    const b = parse(target);
    const mix = (left, right) => Math.round(left + (right - left) * clampWeight);
    return `rgb(${mix(a.r, b.r)}, ${mix(a.g, b.g)}, ${mix(a.b, b.b)})`;
  }

  // ── Draft ───────────────────────────────────────────────────
  function persistDraft() {
    if (!analyticsState.flowStarted && !STATE.mode && !STATE.product.type) return;
    try {
      const draft = {
        v: 2,
        ts: Date.now(),
        mode: STATE.mode,
        product: STATE.product,
        print: STATE.print,
        artwork: STATE.artwork,
        order: STATE.order,
        notes: STATE.notes,
        contact: STATE.contact,
        ui: {
          current_step: STATE.ui.current_step,
          done_steps: Array.from(STATE.ui.done_steps),
          stage_view: STATE.ui.stage_view,
        },
      };
      localStorage.setItem(STORAGE_KEY, JSON.stringify(draft));
    } catch (err) {
      // ignore quota
    }
  }

  function readDraft() {
    try {
      const raw = localStorage.getItem(STORAGE_KEY);
      if (!raw) return null;
      const draft = JSON.parse(raw);
      return draft && draft.v === 2 ? draft : null;
    } catch (_) {
      return null;
    }
  }

  function setupDraftResume() {
    const draft = readDraft();
    if (!draft || (!draft.mode && !draft.product?.type)) return;
    if (dom.draftResumeCard) dom.draftResumeCard.hidden = false;
    const internalStep = draft.ui?.current_step || "mode";
    const stepIndex = (stateTools?.progressIndex(internalStep) || 0) + 1;
    if (dom.draftResumeTitle) {
      const pattern = dom.draftResumeTitle.dataset.titlePattern || "Продовжити з кроку {step}";
      dom.draftResumeTitle.textContent = pattern.replace("{step}", String(stepIndex));
    }
    dom.draftResumeButton?.addEventListener("click", () => {
      loadDraft(draft);
      analyticsState.flowStarted = true;
      sendAnalyticsEvent("draft_resume", buildAnalyticsMetadata({ resumed_step: internalStep }));
      if (dom.draftResumeCard) dom.draftResumeCard.hidden = true;
      enterStudio("draft_resume");
    }, { once: true });
    dom.draftRestartButton?.addEventListener("click", () => {
      clearDraft();
      if (dom.draftResumeCard) dom.draftResumeCard.hidden = true;
      enterStudio("draft_restart");
    }, { once: true });
  }

  function loadDraft(draftOverride = null) {
    try {
      const draft = draftOverride || readDraft();
      if (!draft) return;
      if (draft.mode) STATE.mode = draft.mode;
      if (draft.product) Object.assign(STATE.product, draft.product);
      if (draft.print) Object.assign(STATE.print, draft.print);
      if (draft.artwork) Object.assign(STATE.artwork, draft.artwork);
      if (draft.order) Object.assign(STATE.order, draft.order);
      if (draft.notes) Object.assign(STATE.notes, draft.notes);
      if (draft.contact) Object.assign(STATE.contact, draft.contact);
      if (draft.ui) {
        STATE.ui.current_step = draft.ui.current_step || "mode";
        STATE.ui.done_steps = new Set(draft.ui.done_steps || []);
        STATE.ui.stage_view = draft.ui.stage_view || "front";
      }
      normalizeClientState();
      // Restore inputs
      if (dom.qtyInput && STATE.order.quantity) dom.qtyInput.value = STATE.order.quantity;
      if (dom.placementNoteInput) dom.placementNoteInput.value = STATE.print.placement_note || "";
      if (dom.briefInput) dom.briefInput.value = STATE.notes.brief || "";
      if (dom.nameInput) dom.nameInput.value = STATE.contact.name || "";
      if (dom.contactValueInput) dom.contactValueInput.value = STATE.contact.value || "";
      if (dom.brandNameInput) dom.brandNameInput.value = STATE.notes.brand_name || "";
      if (dom.brandIntroName) dom.brandIntroName.value = STATE.notes.brand_name || "";
      if (dom.brandContactPerson) dom.brandContactPerson.value = STATE.notes.brand_contact_person || "";
      if (dom.brandContactValue) dom.brandContactValue.value = STATE.notes.brand_contact_value || "";
      if (dom.brandResource) dom.brandResource.value = STATE.notes.brand_resource || "";
      if (dom.brandPhone) dom.brandPhone.value = STATE.notes.brand_phone || "";
      if (dom.brandQuantity) dom.brandQuantity.value = STATE.order.quantity || "";
      if (dom.brandDeadline) dom.brandDeadline.value = STATE.notes.brand_deadline || "";
      if (dom.brandBusinessType) dom.brandBusinessType.value = STATE.notes.brand_business_type || "brand";
      if (dom.brandWish) dom.brandWish.value = STATE.notes.brand_wish || "";
      dom.brandContactChannelList?.querySelectorAll("[data-brand-contact-channel]").forEach((item) => item.classList.toggle("is-active", item.dataset.brandContactChannel === STATE.notes.brand_contact_channel));
      dom.brandProductList?.querySelectorAll("[data-brand-product]").forEach((item) => item.classList.toggle("is-active", (STATE.notes.brand_product_types || []).includes(item.dataset.brandProduct)));
      if (dom.giftTextInput) dom.giftTextInput.value = STATE.order.gift_text || "";
      if (dom.giftToggle) dom.giftToggle.classList.toggle("is-active", !!STATE.order.gift_enabled);
      if (dom.giftToggle) dom.giftToggle.setAttribute("aria-pressed", String(!!STATE.order.gift_enabled));
      if (dom.giftToggleState) dom.giftToggleState.textContent = STATE.order.gift_enabled ? "Увімкнено" : "Вимкнено";
      if (dom.giftTextWrap) dom.giftTextWrap.hidden = !STATE.order.gift_enabled;
      if (dom.garmentNoteInput) dom.garmentNoteInput.value = STATE.notes.garment_note || "";
      if (dom.sizesNoteInput) dom.sizesNoteInput.value = STATE.order.sizes_note || "";
      // Re-render dependent UI
      renderFitChips();
      renderFabricChips();
      renderColorChips();
      renderZoneChipsForCurrent();
      renderFrontSizeOptions();
      renderBackSizeOptions();
      renderSleeveControls();
      renderAddons();
      renderArtworkActiveState();
      renderContactChannelChipsActive();
      renderSizing();
      renderDropzones();
      setActiveStep(STATE.ui.current_step || "mode", { silent: true });
      refreshAll();
    } catch (err) {
      console.warn("[custom-print v2] draft load failed", err);
    }
  }

  function clearDraft() {
    try { localStorage.removeItem(STORAGE_KEY); } catch (_) { /* ignore */ }
  }
})();
