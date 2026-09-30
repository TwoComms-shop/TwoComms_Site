/* Private state is fetched separately from cacheable product HTML. */
(() => {
  'use strict';
  const root = document.querySelector('[data-review-root]');
  if (!root || root.dataset.initialized) return;
  root.dataset.initialized = '1';
  const form = root.querySelector('[data-review-form]');
  const compose = root.querySelector('#review-compose');
  const feedback = root.querySelector('[data-review-feedback]');
  const submit = form.querySelector('[type=submit]');
  const privateBox = root.querySelector('[data-private-reviews]');
  const key = `twocomms.review.v1.${root.dataset.user}.${root.dataset.product}`;
  const fields = ['body', 'rating', 'author_name'];
  let owned = !compose.hidden ? false : true;
  let csrf = '';
  let needsRating = false;
  let previewURLs = [];
  const notify = message => { feedback.textContent = message; };
  function refreshForm() {
    const hasRating = Boolean(form.elements.rating.value);
    form.querySelectorAll('[name=rating]').forEach(input => { input.required = needsRating; });
    const hint = root.querySelector('[data-rating-hint]');
    hint.textContent = needsRating ? hint.dataset.followup : hint.dataset.default;
    root.querySelector('[data-rating-optional]').hidden = needsRating;
    root.querySelector('[data-clear-rating]').hidden = !hasRating || needsRating;
    const optin = form.elements.campaign_opt_in;
    if (optin) { optin.disabled = !hasRating; if (!hasRating) optin.checked = false; form.elements.email.required = optin.checked; }
    root.querySelector('[data-draft-status]').hidden = !form.elements.body.value;
    root.querySelector('[data-body-count]').textContent = `${form.elements.body.value.length} / 4000`;
  }
  try {
    const saved = JSON.parse(sessionStorage.getItem(key) || 'null');
    if (saved && Date.now() - saved.time < 24 * 60 * 60 * 1000) {
      fields.forEach(name => { if (typeof saved[name] === 'string') form.elements[name].value = saved[name]; });
      if (saved.body) compose.open = true;
    }
  } catch (_) { root.querySelector('[data-draft-status]').hidden = true; }
  refreshForm();
  form.addEventListener('input', () => {
    refreshForm();
    const draft = {time: Date.now()};
    fields.forEach(name => { draft[name] = form.elements[name].value; });
    try { sessionStorage.setItem(key, JSON.stringify(draft)); } catch (_) { root.querySelector('[data-draft-status]').hidden = true; }
  });
  root.querySelector('[data-clear-rating]').addEventListener('click', () => {
    form.querySelectorAll('[name=rating]').forEach(input => { input.checked = false; });
    form.dispatchEvent(new Event('input', {bubbles:true}));
  });
  root.querySelector('[data-clear-draft]').addEventListener('click', () => {
    fields.forEach(name => { if (name !== 'rating') form.elements[name].value = ''; });
    form.querySelectorAll('[name=rating]').forEach(input => { input.checked = false; });
    form.elements.email.value = '';
    form.elements.images.value = '';
    if (form.elements.campaign_opt_in) form.elements.campaign_opt_in.checked = false;
    previewURLs.forEach(URL.revokeObjectURL); previewURLs = [];
    root.querySelector('[data-photo-previews]').replaceChildren();
    try { sessionStorage.removeItem(key); } catch (_) {}
    refreshForm();
  });
  root.querySelector('[data-compose-open]').addEventListener('click', event => {
    event.preventDefault();
    if (owned) { privateBox.scrollIntoView({block: 'center'}); privateBox.querySelector('article')?.focus({preventScroll: true}); return; }
    compose.open = true;
    compose.scrollIntoView({block: 'start'});
    form.elements.body.focus({preventScroll: true});
  });
  form.elements.images.addEventListener('change', () => {
    previewURLs.forEach(URL.revokeObjectURL); previewURLs = [];
    const preview = root.querySelector('[data-photo-previews]'); preview.replaceChildren();
    const files = Array.from(form.elements.images.files);
    if (files.length > 5 || files.some(f => f.size > 5 * 1024 * 1024 || !['image/jpeg','image/png','image/webp'].includes(f.type))) {
      notify(root.querySelector('[data-photo-error]').textContent); form.elements.images.value = ''; return;
    }
    notify('');
    files.forEach(file => { const img = document.createElement('img'); const url = URL.createObjectURL(file); previewURLs.push(url); img.src = url; img.alt = file.name; preview.append(img); });
  });
  async function state() {
    const response = await fetch(root.dataset.stateUrl, {credentials: 'same-origin', cache: 'no-store'});
    if (!response.ok) throw new Error('state');
    const data = await response.json();
    csrf = data.csrf;
    form.elements.csrfmiddlewaretoken.value = csrf;
    privateBox.innerHTML = data.html; // server-rendered, escaped Django template
    root.dataset.stateReady = "1";
    owned = data.form_complete;
    compose.hidden = owned;
    needsRating = data.submitted_kinds.includes('comment');
    refreshForm();
    return data;
  }
  const initialState = state().catch(() => null);
  window.addEventListener('pageshow', event => { if (event.persisted) state().catch(() => {}); });
  form.addEventListener('submit', async event => {
    event.preventDefault();
    if (submit.disabled) return;
    refreshForm();
    if (!form.reportValidity()) return;
    submit.disabled = true; submit.textContent = submit.dataset.busy; notify('');
    form.querySelectorAll('[data-error]').forEach(e => { e.textContent = ''; });
    form.querySelectorAll('[aria-invalid]').forEach(e => e.removeAttribute('aria-invalid'));
    try {
      await initialState;
      if (!csrf) await state();
      const response = await fetch(form.action, {method: 'POST', body: new FormData(form), credentials: 'same-origin', headers: {'X-Requested-With':'XMLHttpRequest', 'X-CSRFToken':csrf}});
      const data = await response.json();
      if (!data.ok) {
        if (data.duplicate) { await state(); privateBox.scrollIntoView({block:'center'}); return; }
        if (data.errors) {
          Object.entries(data.errors).forEach(([name, errors]) => {
            const target = form.querySelector(`[data-error="${name}"]`);
            if (target) target.textContent = errors.map(e => e.message).join(' ');
            const input = form.querySelector(`[name="${name}"]`);
            if (input) { input.setAttribute('aria-invalid', 'true'); const details = input.closest('.rv-extras'); if (details) details.open = true; }
          });
          form.querySelector('[aria-invalid]')?.focus();
        } else { notify(data.error || data.message || root.querySelector('[data-network-error]').textContent); feedback.focus(); }
        return;
      }
      try { sessionStorage.removeItem(key); } catch (_) {}
      form.reset(); refreshForm();
      // A failed follow-up GET must never turn a successful POST into a resend.
      owned = true; compose.hidden = true; compose.open = false;
      privateBox.textContent = root.querySelector('[data-success-message]').textContent;
      try { await state(); } catch (_) {}
      privateBox.querySelector('article')?.focus();
      privateBox.scrollIntoView({block:'center'});
    } catch (_) { notify(root.querySelector('[data-network-error]').textContent); feedback.focus(); }
    finally { submit.disabled = false; submit.textContent = submit.dataset.idle; }
  });
})();
