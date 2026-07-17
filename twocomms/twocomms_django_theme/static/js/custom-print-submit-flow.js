(function (global) {
  const focusableSelector = "a[href], button:not([disabled]), input:not([disabled]), textarea:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex='-1'])";

  function wireDialog(dialog) {
    if (!dialog) return null;
    let returnFocus = null;
    dialog.querySelectorAll("[data-dialog-close]").forEach((button) => {
      button.addEventListener("click", () => dialog.close());
    });
    dialog.addEventListener("cancel", (event) => {
      event.preventDefault();
      dialog.close();
    });
    dialog.addEventListener("close", () => returnFocus?.focus?.());
    dialog.addEventListener("keydown", (event) => {
      if (event.key !== "Tab") return;
      const items = Array.from(dialog.querySelectorAll(focusableSelector));
      if (!items.length) return;
      const first = items[0];
      const last = items[items.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    });
    return {
      open(trigger) {
        if (dialog.open) return;
        returnFocus = trigger || document.activeElement;
        dialog.showModal();
        dialog.querySelector(focusableSelector)?.focus();
      },
    };
  }

  function create(root) {
    function portal(dialog) {
      if (dialog && dialog.parentNode !== document.body) document.body.append(dialog);
      return dialog;
    }

    const previewDialog = portal(root.querySelector("[data-preview-dialog]"));
    const managerDialog = portal(root.querySelector("[data-manager-dialog]"));
    const cartDialog = portal(root.querySelector("[data-cart-review-dialog]"));
    const preview = wireDialog(previewDialog);
    const manager = wireDialog(managerDialog);
    const cart = wireDialog(cartDialog);

    function openPreviewDialog(trigger) {
      preview?.open(trigger);
    }

    function openManagerDialog({ trigger, isB2b = false } = {}) {
      const title = managerDialog?.querySelector("[data-manager-title]");
      if (title) title.textContent = isB2b ? title.dataset.titleB2b : title.dataset.titleDefault;
      manager?.open(trigger);
    }

    function openCartReviewDialog({ trigger, leadNumber, cartUrl } = {}) {
      const number = cartDialog?.querySelector("[data-cart-review-number]");
      const go = cartDialog?.querySelector("[data-cart-review-go]");
      if (number) number.textContent = leadNumber ? `Заявка №${leadNumber}` : "";
      if (go) go.href = cartUrl || "/cart/";
      cart?.open(trigger);
    }

    return { openPreviewDialog, openManagerDialog, openCartReviewDialog };
  }

  global.CustomPrintSubmitFlow = { create };
})(globalThis);
