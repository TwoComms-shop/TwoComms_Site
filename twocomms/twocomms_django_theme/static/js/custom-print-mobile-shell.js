(function (global) {
  function create({ root, mobileBar, onExit, onBack, onManager, onPreview }) {
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

    function setActive(active) {
      root.classList.toggle("is-studio-active", active);
      document.body.classList.toggle("cp-studio-active", active);
      if (appbar) appbar.hidden = !active;
      if (mobileBar) mobileBar.hidden = !active;
    }

    function update(index, total = 8) {
      if (stepLabel) {
        const pattern = stepLabel.dataset.stepPattern || "Крок {current} з {total}";
        stepLabel.textContent = pattern.replace("{current}", String(index + 1)).replace("{total}", String(total));
      }
      if (progress) progress.style.width = `${((index + 1) / total) * 100}%`;
    }

    return { setActive, update };
  }

  global.CustomPrintMobileShell = { create };
})(globalThis);
