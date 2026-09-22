(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.TwcBotConversationScroll = api;
})(typeof window === "object" ? window : globalThis, function () {
  const SETTLE_TIMEOUT_MS = 30000;

  function pinToBottom(messages, options) {
    options = options || {};
    if (!messages || !messages.isConnected) return function () {};
    const isDocumentScrolled = options.isDocumentScrolled || function () { return false; };
    const documentNearBottom = options.documentNearBottom || function () { return false; };
    const raf = options.requestAnimationFrame || (typeof requestAnimationFrame === "function" ? requestAnimationFrame : function (fn) { return setTimeout(fn, 0); });
    const cancelRaf = options.cancelAnimationFrame || (typeof cancelAnimationFrame === "function" ? cancelAnimationFrame : clearTimeout);
    const scheduleTimeout = options.setTimeout || setTimeout;
    const cancelTimeout = options.clearTimeout || clearTimeout;
    const ResizeObserverCtor = options.ResizeObserver || (typeof ResizeObserver === "function" ? ResizeObserver : null);
    const nearBottom = function () {
      return isDocumentScrolled(messages)
        ? documentNearBottom()
        : messages.scrollHeight - messages.scrollTop - messages.clientHeight < 60;
    };
    let keepPinned = Boolean(options.force) || nearBottom();
    let ignoreScroll = false;
    let frame = 0;
    let timer = 0;
    let observer = null;
    let done = false;
    const onScroll = function () {
      if (!ignoreScroll && !nearBottom()) {
        keepPinned = false;
        cleanup();
      }
    };
    const cleanup = function () {
      if (done) return;
      done = true;
      if (frame) cancelRaf(frame);
      if (timer) cancelTimeout(timer);
      if (observer) observer.disconnect();
      messages.removeEventListener("scroll", onScroll);
    };
    const scrollNow = function () {
      if (!messages.isConnected || !keepPinned) {
        cleanup();
        return;
      }
      ignoreScroll = true;
      if (isDocumentScrolled(messages)) {
        const last = messages.lastElementChild;
        if (last && last.scrollIntoView) last.scrollIntoView({ block: "end", behavior: "auto" });
      } else {
        messages.scrollTop = messages.scrollHeight;
      }
      raf(function () { ignoreScroll = false; });
    };
    const schedule = function () {
      if (done || frame) return;
      frame = raf(function () {
        frame = 0;
        scrollNow();
      });
    };
    messages.addEventListener("scroll", onScroll, { passive: true });
    if (ResizeObserverCtor) {
      observer = new ResizeObserverCtor(schedule);
      observer.observe(messages);
      Array.from(messages.querySelectorAll("img")).forEach(function (image) { observer.observe(image); });
    }
    const mediaReady = Array.from(messages.querySelectorAll("img")).map(function (image) {
      if (image.complete) return Promise.resolve();
      if (typeof image.decode === "function") return image.decode().catch(function () {});
      return new Promise(function (resolve) {
        image.addEventListener("load", resolve, { once: true });
        image.addEventListener("error", resolve, { once: true });
      });
    });
    schedule();
    Promise.all(mediaReady).then(schedule);
    timer = scheduleTimeout(cleanup, SETTLE_TIMEOUT_MS);
    return cleanup;
  }

  return { pinToBottom, SETTLE_TIMEOUT_MS };
});
