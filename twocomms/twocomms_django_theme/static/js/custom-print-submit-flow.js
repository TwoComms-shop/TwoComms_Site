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
    const postSubmitDialog = portal(root.querySelector("[data-post-submit-dialog]"));
    const preview = wireDialog(previewDialog);
    const manager = wireDialog(managerDialog);
    const cart = wireDialog(cartDialog);
    const postSubmit = wireDialog(postSubmitDialog);

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
        const baseHref = (telegram.dataset.baseHref || telegram.getAttribute("href") || "https://t.me/twocomms").split("?")[0];
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

    function openSuccessDialog({ trigger, kind = "lead", leadNumber = "", cartUrl = "" } = {}) {
      if (!postSubmitDialog || !postSubmit) return;
      const title = postSubmitDialog.querySelector("[data-post-submit-title]");
      const copy = postSubmitDialog.querySelector("[data-post-submit-copy]");
      const number = postSubmitDialog.querySelector("[data-post-submit-number]");
      const cartLink = postSubmitDialog.querySelector("[data-post-submit-cart]");
      const homeLink = postSubmitDialog.querySelector("[data-post-submit-home]");
      const instagramLink = postSubmitDialog.querySelector("[data-post-submit-instagram]");
      const telegramLink = postSubmitDialog.querySelector("[data-post-submit-telegram]");
      const isCart = kind === "cart";
      if (title) title.textContent = isCart ? "Заявку додано до кошика" : "Заявка вже у менеджера";
      if (copy) copy.textContent = isCart
        ? "Менеджер перевірить конфігурацію та уточнить файл, основу і фінальну ціну."
        : "Ми отримали конфігурацію. Менеджер звʼяжеться з вами у вибраному каналі.";
      if (number) number.textContent = leadNumber ? `Номер заявки · ${leadNumber}` : "Конфігурацію збережено";
      if (cartLink) {
        cartLink.hidden = !isCart;
        cartLink.href = cartUrl || "/cart/";
      }
      if (homeLink) homeLink.href = "/";
      if (instagramLink) instagramLink.href = "https://www.instagram.com/twocomms/";
      if (telegramLink) telegramLink.href = "https://t.me/twocomms";
      postSubmit?.open(trigger);
    }

    return { openPreviewDialog, openManagerDialog, openCartReviewDialog, openSuccessDialog };
  }

  global.CustomPrintSubmitFlow = { create };
})(globalThis);
