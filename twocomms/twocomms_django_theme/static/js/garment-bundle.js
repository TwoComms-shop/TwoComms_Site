/* A brief, one-time cue when the shipping condition enters the viewport. */
(() => {
  const notices = document.querySelectorAll('[data-shipping-attention]');
  if (!notices.length || !('IntersectionObserver' in window) ||
      window.matchMedia('(prefers-reduced-motion: reduce)').matches) return;
  const observer = new IntersectionObserver(entries => {
    entries.forEach(entry => {
      if (!entry.isIntersecting || entry.intersectionRatio < .6) return;
      entry.target.classList.add('is-highlighted');
      observer.unobserve(entry.target);
    });
  }, {threshold: .6});
  notices.forEach(notice => observer.observe(notice));
})();

/* A separate chooser keeps the tee's size and fit independent of the hoodie. */
(() => {
  'use strict';
  const dialog = document.getElementById('garment-bundle-dialog');
  const words = document.getElementById('garment-bundle-copy');
  if (!dialog || !words) return;
  const copy = JSON.parse(words.textContent);
  const body = dialog.querySelector('[data-bundle-content]');
  const status = dialog.querySelector('[data-bundle-status]');
  let opener, mode, hoodie, hoodieKey, hoodieId, products = [], selected, variant, fit, size = '', busy = false, requestId;
  let generation = 0, hoodieDiscount = 0, offerKind = 'ordinary';
  const esc = value => String(value ?? '').replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
  const money = value => `${new Intl.NumberFormat(document.documentElement.lang || 'uk', {maximumFractionDigits:2}).format(Number(value || 0))} ${copy.currency}`;
  const price = () => Number(fit?.offer_unit_price || 0);
  const normalPrice = () => Number(fit?.standalone_unit_price || 0);
  const newRequest = () => window.crypto?.randomUUID?.() || `${Date.now()}-${Math.random().toString(36).slice(2)}`;
  const getHoodie = () => {
    const root = document.getElementById('product-detail-container');
    const options = {};
    document.querySelectorAll('[data-product-option-axis]:checked').forEach(input => { options[input.dataset.productOptionAxis] = input.value; });
    const color = document.querySelector('#color-picker .color-swatch.active');
    return {product_id: Number(root?.dataset.productId || hoodieId), variant_id: color ? Number(color.dataset.variant) : null,
      size: document.querySelector('input[name="size"]:checked')?.value || '',
      fit_option: options.fit || document.querySelector('input[name="fit_option"]:checked')?.value || '', option_values: options};
  };
  const hoodiePrice = () => Number(document.getElementById('product-analytics-payload')?.dataset.price || 0);
  const totalPrice = () => mode === 'pair' ? Math.max(0, hoodiePrice() - hoodieDiscount) + price() : Math.max(0, price() - hoodieDiscount);
  const setStatus = text => { status.textContent = text; };
  const button = (kind, value, label, pressed, extra='') => `<button type="button" class="garment-option" data-bundle-${kind}="${esc(value)}" aria-pressed="${pressed ? 'true':'false'}" ${extra}>${label}</button>`;
  function chooseProduct(id) {
    selected = products.find(p => String(p.id) === String(id)) || products[0];
    variant = selected?.variants?.[0]; fit = variant?.fits?.[0]; size = ''; requestId = newRequest();
    render();
  }
  function render() {
    if (!selected || !variant || !fit) { body.innerHTML = `<p>${esc(copy.empty)}</p>`; return; }
    const fits = variant.fits || [];
    const teeSaving = Math.max(0, normalPrice() - price());
    const saving = teeSaving + hoodieDiscount;
    body.innerHTML = `<div class="garment-choice">
      ${selected.image || variant.image ? `<img src="${esc(variant.image || selected.image)}" alt="" width="78" height="96">`:''}
      <div><span class="garment-choice__kind">${esc(offerKind==='225' ? copy.brigade_kind : (selected.is_same_design ? copy.same : copy.other))}</span><h3>${esc(selected.title)}</h3>
      <div class="garment-choice__price" data-bundle-price>${esc(money(price()))}${teeSaving>0?`<del>${esc(money(normalPrice()))}</del>`:''}</div>
      ${products.length > 1 ? `<button type="button" class="garment-choice__change" data-bundle-other>${esc(copy.other_choice)}</button>`:''}</div></div>
      ${fits.length>1?`<fieldset class="garment-field"><legend>${esc(copy.fit)}</legend><div class="garment-options garment-options--fit">${fits.map(f=>button('fit',f.code,`${esc(f.label || (f.code==='oversize'?copy.oversize:copy.classic))}<small>${esc(money(f.offer_unit_price))}</small>`,f.code===fit.code)).join('')}</div></fieldset>`:`<p class="garment-dialog__note">${esc(copy.fit)}: ${esc(fit.label || fit.code)}</p>`}
      ${selected.variants.length>1?`<fieldset class="garment-field"><legend>${esc(copy.color)}</legend><div class="garment-options">${selected.variants.map(v=>button('variant',v.id,esc(v.color_name || v.name || v.title || v.id),v.id===variant.id)).join('')}</div></fieldset>`:''}
      <fieldset class="garment-field garment-field--size"><legend>${esc(copy.size)} <a class="garment-size-guide" href="${esc(dialog.dataset.sizeGuideUrl)}" target="_blank" rel="noopener">${esc(copy.size_guide)} ↗</a></legend><div class="garment-options">${(fit.sizes || []).map(s=>button('size',s,esc(s),String(s)===size)).join('')}</div></fieldset>
      ${mode === 'pair'?`<p class="garment-dialog__hoodie">${esc(copy.hoodie)}: ${esc(hoodie.size)} · <del>${esc(money(hoodiePrice()))}</del><strong>${esc(money(Math.max(0,hoodiePrice()-hoodieDiscount)))}</strong></p>`:`<p class="garment-dialog__discount"><span>${esc(copy.hoodie_discount)}</span><strong>−${esc(money(hoodieDiscount))}</strong></p>`}
      <p class="garment-dialog__note">${esc(mode === 'pair'?copy.pair_note:copy.tee_note)}</p><details class="garment-offer__terms"><summary>${esc(copy.rules_title)}</summary><p>${esc(offerKind==='225'?copy.brigade_terms:copy.terms_more)} ${esc(copy.shipping_separate)}</p></details>
      <div class="garment-dialog__footer"><div class="garment-dialog__totals"><span>${esc(mode==='pair'?copy.total:copy.cart_extra)}<strong data-bundle-total>${esc(money(totalPrice()))}</strong></span><span>${esc(copy.saving)}<b>${esc(money(saving))}</b></span></div>
      <button type="button" class="garment-dialog__submit" data-bundle-submit ${!size?'disabled':''}>${esc(size ? (mode==='pair'?copy.add_pair:copy.add_tee) : copy.select_size)}</button></div>`;
  }
  function renderPicker() {
    body.innerHTML = `<button type="button" class="garment-picker__back" data-bundle-back>← ${esc(copy.back)}</button><div class="garment-picker">${products.map(p=>{
      const values = p.variants.flatMap(v=>v.fits.map(f=>Number(f.offer_unit_price)));
      const minimum = Math.min(...values);
      return `<button type="button" data-bundle-product="${esc(p.id)}">${p.image?`<img src="${esc(p.image)}" alt="" width="140" height="160" loading="lazy">`:''}<strong>${esc(p.title)}</strong><small>${esc(p.is_same_design?copy.same:copy.other)} · ${esc(copy.from_)} ${esc(money(minimum))}</small></button>`;
    }).join('')}</div>`;
  }
  function setDialogKind() {
    dialog.dataset.bundleKind = offerKind;
    dialog.querySelector('#garment-bundle-title').textContent = offerKind==='225' ? copy.brigade_title : copy.title;
    dialog.querySelector('.garment-eyebrow').textContent = offerKind==='225' ? copy.brigade_eyebrow : copy.eyebrow;
  }
  async function loadOptions() {
    const token = ++generation;
    body.innerHTML = `<p>${esc(copy.loading)}</p>`;
    setStatus('');
    try {
      const url = new URL(dialog.dataset.optionsUrl, location.origin); url.searchParams.set('hoodie_id', hoodieId);
      if (mode === 'tee_only') {url.searchParams.set('mode', mode);url.searchParams.set('hoodie_key', hoodieKey);}
      const response = await fetch(url, {credentials:'same-origin', headers:{'Accept':'application/json'}});
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || copy.error);
      if (token !== generation || !dialog.open) return;
      hoodieDiscount = Math.max(0, Number(data.hoodie_unit_discount || 0));
      offerKind = data.kind === '225' ? '225' : 'ordinary';
      setDialogKind();
      products = (data.products || data.catalog?.products || []).filter(p=>p.variants?.some(v=>v.fits?.length));
      const preferredTee = new URLSearchParams(location.search).get('bundle_tee');
      chooseProduct(products.find(p => String(p.id) === preferredTee)?.id || products[0]?.id);
      if (opener?.dataset.bundleStart === 'picker' && offerKind !== '225' && products.length) renderPicker();
    } catch (error) {
      if (token !== generation) return;
      body.innerHTML = `<button type="button" class="garment-dialog__submit" data-bundle-retry>${esc(copy.retry)}</button>`;
      setStatus(copy.error);
    }
  }
  function trackAdded(data) {
    const items = data.added_items || [];
    if (!items.length || !window.trackEvent || items.some(i=>!i.offer_id)) return;
    const contents = items.map(i=>({id:i.offer_id,quantity:Number(i.quantity || i.qty || 1),item_price:Number(i.item_price ?? i.unit_price ?? 0)}));
    const eventId = window.safeGenerateAnalyticsEventId?.() || newRequest();
    window.trackEvent('AddToCart', {content_ids:contents.map(i=>i.id),content_type:'product',contents,
      num_items:contents.reduce((n,i)=>n+i.quantity,0),value:contents.reduce((n,i)=>n+i.quantity*i.item_price,0),currency:'UAH',event_id:eventId,
      __meta:window.buildMetaWithUserData?.(eventId) || {event_id:eventId}});
  }
  async function submit() {
    if (busy || !size) return;
    busy = true;
    const submitButton = body.querySelector('[data-bundle-submit]');
    submitButton.disabled = true; submitButton.textContent = copy.submitting; setStatus('');
    try {
      const csrf = document.cookie.split(';').map(x=>x.trim()).find(x=>x.startsWith('csrftoken='))?.slice(10) || document.querySelector('[name=csrfmiddlewaretoken]')?.value || '';
      const tee = {product_id:selected.id,variant_id:variant.id,size,fit_option:fit.code,option_values:fit.option_values || {fit:fit.code}};
      const payload = {mode,tee,request_id:requestId};
      if (mode === 'pair') payload.hoodie = hoodie; else payload.hoodie_key = hoodieKey;
      const response = await fetch(dialog.dataset.addUrl,{method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/json','X-CSRFToken':decodeURIComponent(csrf)},body:JSON.stringify(payload)});
      const data = await response.json();
      if (!response.ok || !data.ok) throw new Error(data.error || copy.error);
      try { if (!data.replayed) trackAdded(data); } catch (_) { /* Analytics never blocks a successful add. */ }
      document.dispatchEvent(new CustomEvent('cartUpdated', {detail:{action:'add',productIds:(data.added_items || []).map(item=>item.product_id)}}));
      try { if (window.updateCartBadge) window.updateCartBadge(data.count);
        if (window.refreshCartSummary) Promise.resolve(window.refreshCartSummary()).catch(()=>{});
      } catch (_) { /* Cart mutation is already confirmed. */ }
      if (window.refreshMiniCart) { try { await window.refreshMiniCart(); } catch (_) { /* Summary can refresh on open. */ } }
      dialog.close();
      if (document.querySelector('[data-cart-page]') || location.pathname.replace(/\/$/,'').endsWith('/cart')) location.reload();
      else if (window.openMiniCart) window.openMiniCart({skipRefresh:true});
    } catch (error) {
      setStatus(error.message || copy.error);
      submitButton.disabled = false; submitButton.textContent = mode==='pair'?copy.add_pair:copy.add_tee;
    } finally {busy = false;}
  }
  document.addEventListener('click', event => {
    const trigger = event.target.closest('[data-bundle-open]');
    if (!trigger) return;
    event.preventDefault();
    opener = trigger; mode = trigger.dataset.bundleMode || 'pair'; hoodieId = trigger.dataset.hoodieId; hoodieKey = trigger.dataset.hoodieKey || '';
    hoodie = mode === 'pair' ? getHoodie() : null; requestId = newRequest(); busy = false;
    if (mode === 'pair' && !hoodie.size) {
      const selector = document.querySelector('input[name="size"]:not(:disabled)');
      const group = selector?.closest('[role="radiogroup"]') || selector?.parentElement;
      if (group) {
        let hint = document.getElementById('bundle-hoodie-size-hint');
        if (!hint) {hint=document.createElement('p');hint.id='bundle-hoodie-size-hint';hint.className='garment-line-note';hint.setAttribute('role','alert');group.after(hint);}
        hint.textContent=copy.select_hoodie; group.scrollIntoView({block:'center',behavior:'auto'});selector.focus({preventScroll:true});
      }
      return;
    }
    document.getElementById('bundle-hoodie-size-hint')?.remove();
    offerKind = trigger.dataset.bundleKind || 'ordinary'; setDialogKind();
    if (!dialog.open) dialog.showModal();
    loadOptions();
  });
  dialog.addEventListener('click', event => {
    if (event.target.closest('[data-bundle-close]')) { if(!busy)dialog.close(); return; }
    if (busy) return;
    const target=event.target.closest('button'); if(!target)return;
    if(target.hasAttribute('data-bundle-retry'))return void loadOptions();
    if(target.hasAttribute('data-bundle-other')){renderPicker();body.querySelector('[data-bundle-back]')?.focus();return;}
    if(target.hasAttribute('data-bundle-back')){render();body.querySelector('[data-bundle-other]')?.focus();return;}
    if(target.hasAttribute('data-bundle-product')){chooseProduct(target.dataset.bundleProduct);body.querySelector('[data-bundle-fit], [data-bundle-size]')?.focus();return;}
    if(target.hasAttribute('data-bundle-variant')){variant=selected.variants.find(v=>String(v.id)===target.dataset.bundleVariant);fit=variant.fits.find(f=>f.code===fit.code)||variant.fits[0];size='';requestId=newRequest();render();body.querySelector(`[data-bundle-variant="${CSS.escape(String(variant.id))}"]`)?.focus();return;}
    if(target.hasAttribute('data-bundle-fit')){fit=variant.fits.find(f=>f.code===target.dataset.bundleFit);size='';requestId=newRequest();render();body.querySelector(`[data-bundle-fit="${CSS.escape(fit.code)}"]`)?.focus();return;}
    if(target.hasAttribute('data-bundle-size')){size=target.dataset.bundleSize;requestId=newRequest();render();body.querySelector(`[data-bundle-size="${CSS.escape(size)}"]`)?.focus({preventScroll:true});return;}
    if(target.hasAttribute('data-bundle-submit'))submit();
  });
  dialog.addEventListener('cancel', event=>{if(busy)event.preventDefault();});
  dialog.addEventListener('close',()=>{generation++;
    const target = opener?.isConnected && opener.getClientRects().length && !opener.closest('[inert]') ? opener : document.getElementById(innerWidth < 992 ? 'cart-toggle-mobile' : 'cart-toggle');
    target?.focus({preventScroll:true});
  });
})();
