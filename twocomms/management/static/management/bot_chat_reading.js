(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.TwcBotChatReading = api;
})(typeof window === 'object' ? window : globalThis, function () {
  function capture(messages, { documentScrolled = false, scrollWindow = typeof window === 'object' ? window : null } = {}) {
    if (!messages) return null;
    const top = documentScrolled ? 0 : messages.getBoundingClientRect().top;
    const anchor = Array.from(messages.querySelectorAll('[data-mid]')).find(
      item => item.getBoundingClientRect().bottom > top,
    );
    return {
      atBottom: documentScrolled && scrollWindow
        ? scrollWindow.innerHeight + scrollWindow.scrollY >= scrollWindow.document.documentElement.scrollHeight - 60
        : messages.scrollHeight - messages.scrollTop - messages.clientHeight < 60,
      scrollTop: messages.scrollTop,
      documentScrolled, scrollWindow, documentScrollTop: scrollWindow?.scrollY || 0,
      anchor,
      offset: anchor ? anchor.getBoundingClientRect().top - top : 0,
    };
  }

  function restore(messages, state) {
    if (!messages || !state || !messages.isConnected) return;
    if (state.documentScrolled && state.scrollWindow) {
      const view = state.scrollWindow;
      view.scrollTo({ top: state.documentScrollTop, behavior: 'auto' });
      if (state.anchor && messages.contains(state.anchor)) {
        view.scrollBy({ top: state.anchor.getBoundingClientRect().top - state.offset, behavior: 'auto' });
      }
      return;
    }
    messages.scrollTop = state.scrollTop;
    if (state.anchor && messages.contains(state.anchor)) {
      messages.scrollTop += state.anchor.getBoundingClientRect().top
        - messages.getBoundingClientRect().top - state.offset;
    }
  }

  // Keep loaded history and disclosure nodes when a card refresh overlaps a poll.
  function merge(messages, items, append, refresh) {
    const rows = new Map(Array.from(messages.querySelectorAll('[data-mid]')).map(
      item => [Number(item.dataset.mid), item],
    ));
    (items || []).forEach(item => {
      const id = Number(item.id), existing = rows.get(id);
      if (existing) {
        if (refresh && existing.dataset.messageSignature !== JSON.stringify(item)) refresh(existing, item);
        existing.dataset.messageTime = item.time || '';
        return;
      }
      append(messages, item);
      messages.lastElementChild.dataset.messageTime = item.time || '';
      rows.set(id, messages.lastElementChild);
    });
    order(messages);
  }

  // ID remains the pagination cursor. Only the loaded set is shown by event
  // time, so a delayed provider import does not masquerade as today's reply.
  function order(messages) {
    const rows = Array.from(messages.querySelectorAll('[data-mid]'));
    const focused = messages.ownerDocument?.activeElement;
    const sorted = rows.slice().sort((a, b) => {
      const left = Date.parse(a.dataset.messageTime), right = Date.parse(b.dataset.messageTime);
      return (Number.isFinite(left) ? left : 0) - (Number.isFinite(right) ? right : 0)
        || Number(a.dataset.mid) - Number(b.dataset.mid);
    });
    const current = rows.slice();
    sorted.forEach((row, index) => {
      if (row === current[index]) return;
      const before = current[index] || null;
      if (typeof messages.moveBefore === 'function') messages.moveBefore(row, before);
      else messages.insertBefore(row, before);
      current.splice(current.indexOf(row), 1); current.splice(index, 0, row);
    });
    if (focused && messages.contains(focused) && messages.ownerDocument.activeElement !== focused) focused.focus({ preventScroll: true });
  }

  function sectionKey(section) {
    return section.id || section.getAttribute('aria-label')
      || section.querySelector('.bot-context-title > span, summary')?.textContent || '';
  }

  // Retain the actual editor nodes and their listeners, not just field values.
  // A background snapshot must never recreate a draft with new server tokens.
  function captureContext(root, activeElement) {
    const sections = Array.from(root.children).filter(section =>
      section.dataset.operatorDirty === 'true' || section.contains(activeElement),
    ).map(section => ({ key: sectionKey(section), section }));
    return { sections, activeElement, scrollTop: root.scrollTop };
  }

  function restoreContext(root, state) {
    if (!state) return;
    state.sections.forEach(({ key, section }) => {
      if (section.parentNode === root) return;
      const replacement = key && Array.from(root.children).find(item => sectionKey(item) === key);
      if (replacement) replacement.replaceWith(section);
      else root.appendChild(section);
    });
    root.scrollTop = state.scrollTop;
    if (state.activeElement && root.contains(state.activeElement)) {
      state.activeElement.focus({ preventScroll: true });
    }
  }

  return { capture, restore, merge, order, captureContext, restoreContext };
});
