(function (global) {
  function create({ root, mobileBar, onExit, onBack, onManager, onPreview, showMobileBar = () => true }) {
    const appbar = root.querySelector("[data-studio-appbar]");
    const exitButton = root.querySelector("[data-studio-exit]");
    const backButton = root.querySelector("[data-studio-back]");
    const managerButton = root.querySelector("[data-manager-open]");
    const previewButtons = root.querySelectorAll("[data-preview-open]");
    const stepLabel = root.querySelector("[data-appbar-step]");
    const progress = root.querySelector("[data-appbar-progress]");
    const viewport = root.querySelector("[data-step-viewport]");
    let tallestViewport = global.innerHeight || 0;
    let focusFrame = null;
    let previewSeen = false;
    let previewEligible = false;

    if (appbar && appbar.parentNode !== document.body) document.body.append(appbar);
    if (mobileBar && mobileBar.parentNode !== document.body) document.body.append(mobileBar);
    exitButton?.addEventListener("click", onExit);
    backButton?.addEventListener("click", onBack);
    managerButton?.addEventListener("click", onManager);
    previewButtons.forEach((button) => button.addEventListener("click", (event) => {
      previewSeen = true;
      updatePreviewCue();
      onPreview?.(event);
    }));

    function updatePreviewCue() {
      previewButtons.forEach((button) => button.classList.toggle("is-preview-cued", !previewSeen && previewEligible && root.classList.contains("is-studio-active") && !!appbar?.contains?.(button)));
    }

    function ensureFocusedField() {
      focusFrame = null;
      const field = document.activeElement;
      if (!root.classList.contains("is-studio-active") || !viewport?.contains?.(field) || !field?.matches?.("input, textarea, select")) return;
      const bounds = viewport.getBoundingClientRect();
      const rect = field.getBoundingClientRect();
      const delta = rect.top < bounds.top + 10 ? rect.top - bounds.top - 10
        : rect.bottom > bounds.bottom - 12 ? rect.bottom - bounds.bottom + 12 : 0;
      if (delta) viewport.scrollTo({ top: viewport.scrollTop + delta, behavior: "auto" });
    }

    // Measure the panels themselves: font size and the phone safe area can
    // change their height. A hidden bottom action reserves no empty strip.
    function measurePanels() {
      const style = document.body.style;
      if (!style?.setProperty) return;
      if (!root.classList.contains("is-studio-active") || global.matchMedia?.("(min-width: 1101px)")?.matches) {
        style.removeProperty("--cp-mobile-appbar-clearance");
        style.removeProperty("--cp-mobile-bar-clearance");
        style.removeProperty("--cp-visible-top");
        style.removeProperty("--cp-visible-bottom-inset");
        document.body.classList.remove("cp-keyboard-open");
        return;
      }
      const visible = global.visualViewport;
      const layoutHeight = global.innerHeight || 0;
      const top = Math.max(0, visible?.offsetTop || 0);
      const height = visible?.height || layoutHeight;
      const textFocused = document.activeElement?.matches?.("textarea, input:not([type=checkbox]):not([type=radio]):not([type=file]):not([type=button]):not([type=submit])");
      if (!textFocused) tallestViewport = height;
      const keyboard = !!textFocused && Math.max(layoutHeight - height, tallestViewport - height) > 120;
      document.body.classList.toggle("cp-keyboard-open", keyboard);
      style.setProperty("--cp-visible-top", `${top}px`);
      style.setProperty("--cp-visible-bottom-inset", `${Math.max(0, layoutHeight - top - height)}px`);
      const rect = appbar?.getBoundingClientRect?.();
      if (rect?.height) style.setProperty("--cp-mobile-appbar-clearance", `${Math.ceil(rect.bottom) + 8}px`);
      const barRect = mobileBar?.getBoundingClientRect?.();
      style.setProperty("--cp-mobile-bar-clearance", `${mobileBar?.hidden ? 0 : Math.ceil(barRect?.height || 0)}px`);
      if (textFocused && global.requestAnimationFrame && focusFrame === null) focusFrame = global.requestAnimationFrame(ensureFocusedField);
    }
    if (global.ResizeObserver) {
      const observer = new global.ResizeObserver(measurePanels);
      if (appbar) observer.observe(appbar);
      if (mobileBar) observer.observe(mobileBar);
    }
    global.addEventListener?.("resize", measurePanels);
    global.visualViewport?.addEventListener?.("resize", measurePanels);
    global.visualViewport?.addEventListener?.("scroll", measurePanels);
    document.addEventListener?.("focusin", measurePanels);
    document.addEventListener?.("focusout", () => global.requestAnimationFrame?.(measurePanels));

    function setActive(active) {
      root.classList.toggle("is-studio-active", active);
      document.body.classList.toggle("cp-studio-active", active);
      if (appbar) appbar.hidden = !active;
      if (mobileBar) mobileBar.hidden = !active || !showMobileBar();
      updatePreviewCue();
      measurePanels();
    }

    function update(index, total = 8) {
      previewEligible = [2, 3, 4].includes(index);
      updatePreviewCue();
      if (stepLabel) {
        const pattern = stepLabel.dataset.stepPattern || "Крок {current} з {total}";
        stepLabel.textContent = pattern.replace("{current}", String(index + 1)).replace("{total}", String(total));
      }
      if (progress) progress.style.width = `${((index + 1) / total) * 100}%`;
      measurePanels();
    }

    return { setActive, update };
  }

  global.CustomPrintMobileShell = { create };
})(globalThis);
