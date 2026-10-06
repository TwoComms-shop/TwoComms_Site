(function (global) {
  function create({ root, mobileBar, onExit, onBack, onManager, onPreview, showMobileBar = () => true }) {
    const appbar = root.querySelector("[data-studio-appbar]");
    const exitButton = root.querySelector("[data-studio-exit]");
    const backButton = root.querySelector("[data-studio-back]");
    const managerButton = root.querySelector("[data-manager-open]");
    const previewButtons = root.querySelectorAll("[data-preview-open]");
    const stepLabel = root.querySelector("[data-appbar-step]");
    const progress = root.querySelector("[data-appbar-progress]");

    if (appbar && appbar.parentNode !== document.body) document.body.append(appbar);
    if (mobileBar && mobileBar.parentNode !== document.body) document.body.append(mobileBar);
    exitButton?.addEventListener("click", onExit);
    backButton?.addEventListener("click", onBack);
    managerButton?.addEventListener("click", onManager);
    previewButtons.forEach((button) => button.addEventListener("click", onPreview));

    // Measure the panels themselves: font size and the phone safe area can
    // change their height. A hidden bottom action reserves no empty strip.
    function measurePanels() {
      const style = document.body.style;
      if (!style?.setProperty) return;
      if (!root.classList.contains("is-studio-active") || global.matchMedia?.("(min-width: 1101px)")?.matches) {
        style.removeProperty("--cp-mobile-appbar-clearance");
        style.removeProperty("--cp-mobile-bar-clearance");
        return;
      }
      const rect = appbar?.getBoundingClientRect?.();
      if (rect?.height) style.setProperty("--cp-mobile-appbar-clearance", `${Math.ceil(rect.bottom) + 8}px`);
      const barRect = mobileBar?.getBoundingClientRect?.();
      style.setProperty("--cp-mobile-bar-clearance", `${mobileBar?.hidden ? 0 : Math.ceil(barRect?.height || 0)}px`);
    }
    if (global.ResizeObserver) {
      const observer = new global.ResizeObserver(measurePanels);
      if (appbar) observer.observe(appbar);
      if (mobileBar) observer.observe(mobileBar);
    }
    global.addEventListener?.("resize", measurePanels);
    global.visualViewport?.addEventListener?.("resize", measurePanels);

    function setActive(active) {
      root.classList.toggle("is-studio-active", active);
      document.body.classList.toggle("cp-studio-active", active);
      if (appbar) appbar.hidden = !active;
      if (mobileBar) mobileBar.hidden = !active || !showMobileBar();
      measurePanels();
    }

    function update(index, total = 8) {
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
