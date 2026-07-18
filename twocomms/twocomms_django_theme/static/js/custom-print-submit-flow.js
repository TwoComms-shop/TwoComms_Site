(function (global) {
  const focusableSelector = "a[href], button:not([disabled]), input:not([disabled]), textarea:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex='-1'])";
  let openDialogCount = 0;
  let lockedScrollY = 0;
  let documentWasStudioLocked = false;

  function lockDocumentScroll() {
    if (openDialogCount > 0) {
      openDialogCount += 1;
      return;
    }
    openDialogCount = 1;
    lockedScrollY = global.scrollY || document.documentElement.scrollTop || 0;
    documentWasStudioLocked = document.body.classList.contains("cp-studio-active");
    document.documentElement.classList.add("cp-dialog-open");
    document.body.classList.add("cp-dialog-open");
    if (!documentWasStudioLocked) {
      document.body.style.top = `${-lockedScrollY}px`;
    }
  }

  function unlockDocumentScroll() {
    if (!openDialogCount) return;
    openDialogCount -= 1;
    if (openDialogCount > 0) return;
    document.documentElement.classList.remove("cp-dialog-open");
    document.body.classList.remove("cp-dialog-open");
    document.body.style.removeProperty("top");
    if (!documentWasStudioLocked) global.scrollTo(0, lockedScrollY);
    documentWasStudioLocked = false;
  }

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
    dialog.addEventListener("close", () => {
      unlockDocumentScroll();
      returnFocus?.focus?.({ preventScroll: true });
    });
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
        lockDocumentScroll();
        try {
          dialog.showModal();
        } catch (error) {
          unlockDocumentScroll();
          throw error;
        }
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

    function openManagerDialog({ trigger, isB2b = false, summary = "" } = {}) {
      const title = managerDialog?.querySelector("[data-manager-title]");
      if (title) title.textContent = isB2b ? title.dataset.titleB2b : title.dataset.titleDefault;
      const summaryNode = managerDialog?.querySelector("[data-manager-summary]");
      if (summaryNode) summaryNode.textContent = summary;
      const telegram = managerDialog?.querySelector("[data-manager-telegram]");
      if (telegram) {
        const baseHref = telegram.dataset.baseHref || telegram.href;
        telegram.dataset.baseHref = baseHref;
        telegram.href = summary
          ? `${baseHref}${baseHref.includes("?") ? "&" : "?"}text=${encodeURIComponent(summary)}`
          : baseHref;
      }
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
