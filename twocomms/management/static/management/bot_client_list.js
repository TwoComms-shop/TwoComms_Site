(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.TwcBotClientList = api;
})(typeof window === 'object' ? window : globalThis, function () {
  'use strict';

  const DURATION_MS = 340;
  const EASING = 'cubic-bezier(.2,.75,.25,1)';

  // moveBefore preserves focus, selection and other browser-managed state. The
  // fallback is for browsers without that primitive; reconcile restores focus.
  function place(parent, child, before) {
    if (child === before) return;
    if (child.parentNode === parent && typeof parent.moveBefore === 'function') parent.moveBefore(child, before);
    else parent.insertBefore(child, before);
  }

  function sameKind(a, b) {
    return a.nodeType === b.nodeType && a.nodeName === b.nodeName &&
      (a.nodeType !== 1 || (a.classList[0] || '') === (b.classList[0] || ''));
  }

  // Render into a detached candidate, then update only changed attributes/text.
  // Retaining descendants also retains image loading and text selections.
  function patchNode(current, next) {
    if (current.nodeType === 3) {
      if (current.nodeValue !== next.nodeValue) current.nodeValue = next.nodeValue;
      return;
    }
    if (current.nodeType !== 1) return;
    Array.from(current.attributes).forEach(function (attr) {
      if (!next.hasAttribute(attr.name)) current.removeAttribute(attr.name);
    });
    Array.from(next.attributes).forEach(function (attr) {
      if (current.getAttribute(attr.name) !== attr.value) current.setAttribute(attr.name, attr.value);
    });
    let cursor = current.firstChild;
    Array.from(next.childNodes).forEach(function (candidate) {
      let existing = cursor;
      if (!existing || !sameKind(existing, candidate)) {
        existing = Array.from(current.childNodes).find(function (child) {
          return sameKind(child, candidate) && Array.from(current.childNodes).indexOf(child) >=
            (cursor ? Array.from(current.childNodes).indexOf(cursor) : current.childNodes.length);
        });
      }
      if (existing) {
        place(current, existing, cursor);
        patchNode(existing, candidate);
        cursor = existing.nextSibling;
      } else current.insertBefore(candidate, cursor);
    });
    while (cursor) {
      const nextSibling = cursor.nextSibling;
      current.removeChild(cursor);
      cursor = nextSibling;
    }
  }

  function captureInteraction(list, document) {
    const active = document.activeElement;
    const focused = active && list.contains(active) ? active : null;
    const selection = document.getSelection ? document.getSelection() : null;
    const selected = selection && selection.rangeCount && list.contains(selection.anchorNode) && list.contains(selection.focusNode)
      ? { anchor: selection.anchorNode, anchorOffset: selection.anchorOffset, focus: selection.focusNode, focusOffset: selection.focusOffset }
      : null;
    let inputSelection = null;
    if (focused && typeof focused.selectionStart === 'number') {
      inputSelection = [focused.selectionStart, focused.selectionEnd, focused.selectionDirection];
    }
    return { focused: focused, selected: selected, inputSelection: inputSelection };
  }

  function restoreInteraction(state, list, document, onFocusRemoved) {
    if (state.focused) {
      if (list.contains(state.focused)) {
        if (document.activeElement !== state.focused) state.focused.focus({ preventScroll: true });
        if (state.inputSelection) state.focused.setSelectionRange.apply(state.focused, state.inputSelection);
      } else if (onFocusRemoved) onFocusRemoved();
    }
    const selected = state.selected;
    if (!selected || !list.contains(selected.anchor) || !list.contains(selected.focus)) return;
    const selection = document.getSelection();
    const limit = function (node, offset) { return Math.min(offset, node.nodeType === 3 ? node.nodeValue.length : node.childNodes.length); };
    if (selection && selection.setBaseAndExtent) {
      selection.setBaseAndExtent(selected.anchor, limit(selected.anchor, selected.anchorOffset), selected.focus, limit(selected.focus, selected.focusOffset));
    }
  }

  function create(list, options) {
    options = options || {};
    if (!list || typeof options.createRow !== 'function') throw new TypeError('A client list and createRow are required');
    const document = list.ownerDocument;
    const view = document.defaultView || globalThis;
    const motion = options.motionQuery || (view.matchMedia ? view.matchMedia('(prefers-reduced-motion: reduce)') : { matches: false });
    const getKey = options.getKey || function (item) { return String(item.id); };
    const getActivity = options.getActivity || function (item) { return item.last_message_at || ''; };
    const getSignature = options.getSignature || function (item) { return JSON.stringify(item); };
    const records = new Map();
    const animations = new Map();
    let destroyed = false;

    function finish() {
      animations.forEach(function (animation, row) {
        animation.onfinish = null;
        animation.oncancel = null;
        animation.cancel();
        row.classList.remove('is-live-moving', 'is-client-list-promoted');
      });
      animations.clear();
    }

    function onVisibility() { if (document.hidden) finish(); }
    function onMotion() { if (motion.matches) finish(); }
    document.addEventListener('visibilitychange', onVisibility);
    if (motion.addEventListener) motion.addEventListener('change', onMotion);

    function reconcile(items, settings) {
      if (destroyed) return { activityCount: 0 };
      settings = settings || {};
      const animate = Boolean(settings.animate && !motion.matches && !document.hidden);
      const input = [];
      const keys = new Set();
      (items || []).forEach(function (item) {
        const key = String(getKey(item));
        if (!keys.has(key)) { keys.add(key); input.push({ key: key, item: item }); }
      });
      const interaction = captureInteraction(list, document);
      const before = new Map();
      const listRect = list.getBoundingClientRect();
      const internalScroll = list.scrollHeight > list.clientHeight + 1 &&
        (!view.getComputedStyle || /auto|scroll/.test(view.getComputedStyle(list).overflowY));
      const scrollTop = list.scrollTop;
      const documentScroll = !internalScroll && listRect.top < 0 && Number(view.scrollY) > 24;
      const documentScrollTop = Number(view.scrollY) || 0;
      let anchor = null;
      records.forEach(function (record, key) {
        const rect = record.row.getBoundingClientRect();
        before.set(key, rect);
        if (!anchor && internalScroll && scrollTop > 24 && keys.has(key) && rect.bottom > listRect.top + 1) {
          anchor = { row: record.row, offset: rect.top - listRect.top };
        } else if (!anchor && documentScroll && keys.has(key) && rect.bottom > 1) {
          anchor = { row: record.row, offset: rect.top };
        }
      });
      // Rectangles above include the in-flight transform. Cancelling only after
      // measuring lets a newer snapshot rebase smoothly from the visible frame.
      finish();
      let activityCount = 0;
      const nextRecords = new Map();
      let cursor = list.firstChild;
      input.forEach(function (entry) {
        const old = records.get(entry.key);
        const signature = String(getSignature(entry.item));
        const activity = String(getActivity(entry.item) || '');
        let row;
        if (old) {
          row = old.row;
          if (old.signature !== signature) {
            let candidate;
            if (options.updateRow) {
              candidate = row.cloneNode(false);
              options.updateRow(candidate, entry.item);
            } else candidate = options.createRow(entry.item);
            patchNode(row, candidate);
          }
        } else row = options.createRow(entry.item);
        if (options.isSelected) row.classList.toggle('active', Boolean(options.isSelected(entry.item)));
        place(list, row, cursor);
        cursor = row.nextSibling;
        if (settings.animate && (!old || old.activity !== activity)) {
          activityCount += 1;
          if (options.onActivity) options.onActivity(row, entry.item);
        }
        nextRecords.set(entry.key, { row: row, signature: signature, activity: activity });
      });
      while (cursor) {
        const nextSibling = cursor.nextSibling;
        list.removeChild(cursor);
        cursor = nextSibling;
      }
      records.clear();
      nextRecords.forEach(function (record, key) { records.set(key, record); });
      if (!input.length && options.empty) list.appendChild(options.empty());
      restoreInteraction(interaction, list, document, options.onFocusRemoved);
      // Browser scroll anchoring is disabled in CSS: preserve a visible reading
      // anchor while away from the top, and keep the top pinned otherwise.
      if (internalScroll) {
        list.scrollTop = scrollTop;
        if (anchor && list.contains(anchor.row)) list.scrollTop += anchor.row.getBoundingClientRect().top - listRect.top - anchor.offset;
      } else if (documentScroll && anchor && list.contains(anchor.row) && view.scrollTo) {
        const delta = anchor.row.getBoundingClientRect().top - anchor.offset;
        if (Math.abs(delta) > 0.5) view.scrollTo({ left: view.scrollX || 0, top: documentScrollTop + delta, behavior: 'instant' });
      }
      if (animate) {
        const moves = [];
        const viewportTop = internalScroll ? Math.max(0, listRect.top) : 0;
        const viewportBottom = Math.min(Number(view.innerHeight) || Infinity, internalScroll ? listRect.bottom : Infinity);
        records.forEach(function (record, key) {
          const first = before.get(key);
          if (!first || typeof record.row.animate !== 'function') return;
          const last = record.row.getBoundingClientRect();
          const delta = first.top - last.top;
          if (Math.abs(delta) < 1 || Math.max(first.bottom, last.bottom) < viewportTop || Math.min(first.top, last.top) > viewportBottom) return;
          moves.push({ row: record.row, delta: delta });
        });
        // Read all final rectangles before writing animations: one layout pass.
        moves.forEach(function (move) {
          const row = move.row;
          row.classList.add('is-live-moving');
          row.classList.toggle('is-client-list-promoted', move.delta > 0);
          const animation = row.animate([
            { transform: 'translate3d(0, ' + move.delta + 'px, 0)' },
            { transform: 'translate3d(0, 0, 0)' }
          ], { duration: DURATION_MS, easing: EASING, fill: 'both' });
          animations.set(row, animation);
          const cleanup = function () {
            if (animations.get(row) !== animation) return;
            animations.delete(row);
            row.classList.remove('is-live-moving', 'is-client-list-promoted');
            animation.onfinish = null;
            animation.oncancel = null;
            animation.cancel();
          };
          animation.onfinish = cleanup;
          animation.oncancel = cleanup;
        });
      }
      return { activityCount: activityCount };
    }

    function destroy() {
      finish();
      destroyed = true;
      document.removeEventListener('visibilitychange', onVisibility);
      if (motion.removeEventListener) motion.removeEventListener('change', onMotion);
      records.clear();
    }

    return { reconcile: reconcile, finish: finish, destroy: destroy };
  }

  return { create: create, DURATION_MS: DURATION_MS };
});
