(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.TwcBotChatReading = api;
})(typeof window === 'object' ? window : globalThis, function () {
  function capture(messages) {
    if (!messages) return null;
    const top = messages.getBoundingClientRect().top;
    const anchor = Array.from(messages.querySelectorAll('[data-mid]')).find(
      item => item.getBoundingClientRect().bottom > top,
    );
    return {
      atBottom: messages.scrollHeight - messages.scrollTop - messages.clientHeight < 60,
      scrollTop: messages.scrollTop,
      anchor,
      offset: anchor ? anchor.getBoundingClientRect().top - top : 0,
    };
  }

  function restore(messages, state) {
    if (!messages || !state || !messages.isConnected) return;
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
        return;
      }
      append(messages, item);
      rows.set(id, messages.lastElementChild);
    });
  }

  return { capture, restore, merge };
});
