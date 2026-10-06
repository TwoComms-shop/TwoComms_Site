(function (global) {
  // Purpose is independent of B2C/B2B pricing and optional gift packaging.
  function fromChoice(choice) {
    return choice === "brand" ? "organization" : choice === "gift" ? "gift" : "personal";
  }

  function normalize(purpose, mode) {
    if (mode === "brand") return "organization";
    return purpose === "gift" ? "gift" : "personal";
  }

  function createReveal(element) {
    let pending = null;
    const duration = 2400;

    function finish(completed) {
      if (!pending) return;
      const { resolve, timer } = pending;
      pending = null;
      global.clearTimeout(timer);
      element.classList.remove("is-gift-opening");
      resolve(completed);
    }

    function play() {
      finish(false);
      if (!element || global.matchMedia?.("(prefers-reduced-motion: reduce)")?.matches) {
        return Promise.resolve(true);
      }
      // Restart the CSS timeline only on an explicit gift selection.
      void element.offsetWidth;
      element.classList.add("is-gift-opening");
      return new Promise((resolve) => {
        pending = { resolve, timer: global.setTimeout(() => finish(true), duration) };
      });
    }

    const onPreferenceChange = (event) => { if (event.matches) finish(true); };
    global.matchMedia?.("(prefers-reduced-motion: reduce)")?.addEventListener?.("change", onPreferenceChange);
    global.addEventListener?.("pagehide", () => finish(false));
    return { play, cancel: () => finish(false), duration };
  }

  global.CustomPrintPurpose = { fromChoice, normalize, createReveal };
})(globalThis);
