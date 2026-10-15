/* Exact-list business consent is independent of a purchase and channel permission. */
(function (global) {
  'use strict';
  const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
  const digest = /^[0-9a-f]{64}$/i;
  const agreementLabels = { invited:'Очікуємо згоди клієнта', accepted:'Разову згоду підтверджено', revoked:'Згоду відкликано', expired:'Строк погодження минув', unverified:'Згоду не підтверджено' };
  const availabilityLabels = { available:'Наявність підтверджено', wait:'Очікується', unavailable:'Немає в наявності', clarify:'Потрібне уточнення' };
  function kyivDate(value) {
    if (!value) return '';
    const date = new Date(value);
    return Number.isNaN(date.valueOf()) ? '' : date.toLocaleString('uk-UA', { timeZone:'Europe/Kyiv', day:'2-digit', month:'2-digit', year:'numeric', hour:'2-digit', minute:'2-digit' }) + ' (Київ)';
  }
  function reasonLabel(reason) {
    return ({ waiting_window:'Очікуємо дозволеного контакту в Instagram.', quiet_hours:'Надсилання очікує дозволеного часу.', pending_inbound:'Спочатку потрібно опрацювати нове повідомлення клієнта.', manager_review_pending:'Потрібна фізична перевірка менеджера.', manager_decision_unverified:'Потрібна підтверджена фізична перевірка менеджера.', manager_review_expired:'Перевірка менеджера втратила актуальність.', manager_decision_expired:'Перевірка менеджера втратила актуальність.', manager_review_stale:'Умови змінилися — потрібна нова перевірка.', manager_review_unavailable:'Ще не всі позиції підтверджено як доступні.', inventory_unavailable:'Потрібна перевірка наявності всього списку.', inventory_unknown:'Дані обліку потребують фізичної перевірки.', service_case_open:'Спочатку потрібно опрацювати сервісне звернення.', customer_revoked:'Клієнт відкликав згоду.', expired:'Строк погодження минув.', subscription_source_changed:'Підставу погодження потрібно перевірити.', fulfilled_by_manager_update:'Наявність уже повідомлено у підтвердженій відповіді менеджера.' })[reason] || '';
  }
  class Controller {
    constructor(root, options) {
      this.root=root;this.options=options||{};this.document=root.ownerDocument||global.document;
      this.clientId=null;this.sequence=0;this.abort=null;this.busy=false;this.destroyed=false;this.data=null;this.pendingLoad=null;this.refreshButton=null;
      root.classList.add('ig-restock-waiting');root.setAttribute('aria-label','Лист очікування');
    }
    el(tag, cls, text) { const node=this.document.createElement(tag);if(cls)node.className=cls;if(text!==undefined)node.textContent=String(text);return node; }
    note(parent, text, cls) { if(text)parent.appendChild(this.el('p',cls||'ig-rwl-muted',text)); }
    date(parent, label, value) { const text=kyivDate(value);if(text)this.note(parent,label+': '+text); }
    heading() {
      const head=this.el('header','ig-rwl-head'),copy=this.el('div');copy.append(this.el('span','ig-rwl-eyebrow','Разове сповіщення'),this.el('h3','','Лист очікування'));
      const refresh=this.el('button','ig-rwl-button','Оновити');refresh.type='button';refresh.disabled=this.busy;refresh.addEventListener('click',()=>this.load());this.refreshButton=refresh;head.append(copy,refresh);return head;
    }
    message(text,error) { this.root.replaceChildren(this.heading());const note=this.el('p',error?'ig-rwl-error':'ig-rwl-muted',text);note.setAttribute('role',error?'alert':'status');this.root.appendChild(note); }
    item(row, review, recipientLabels) {
      const box=this.el('li','ig-rwl-item'),head=this.el('div','ig-rwl-item-head');head.append(this.el('h5','',row.title||'Назву позиції не підтверджено'),this.el('span','ig-rwl-kind',row.item_kind==='custom_apparel'?'Індивідуальний товар':'Каталог'));box.appendChild(head);
      const facts=[];if(row.color)facts.push('Колір: '+row.color);if(row.size)facts.push('Розмір: '+row.size);if(row.fit)facts.push('Крій: '+row.fit);
      if(Number.isInteger(row.quantity)&&row.quantity>0)facts.push('Кількість: '+row.quantity+' шт.');
      if(row.recipient_id)facts.push(recipientLabels.get(row.recipient_id));this.note(box,facts.filter(Boolean).join(' · '),'ig-rwl-facts');
      if(row.options&&typeof row.options==='object'&&!Array.isArray(row.options)){
        const values=Object.entries(row.options).filter(([key,value])=>key!=='fit'&&['string','number','boolean'].includes(typeof value));
        if(values.length)this.note(box,values.map(([key,value])=>key+': '+String(value)).join(' · '));
      }
      const matches=(Array.isArray(review.lines)?review.lines:[]).filter(line=>line.line_id===row.line_id),check=matches.length===1?matches[0]:null;
      if(check){
        this.note(box,availabilityLabels[check.availability]||'Результат перевірки не підтверджено','ig-rwl-item-state');
        if(check.availability==='available'&&Number.isInteger(check.checked_quantity))this.note(box,'Перевірено фізично: '+check.checked_quantity+' шт.');
        if(check.supply_basis==='accessible_unrecorded')this.note(box,'Доступно поза обліком. Складські залишки цим рішенням не змінювались.');
        this.date(box,'Перевірено',check.checked_at);
      } else this.note(box,'Фізичну наявність ще не підтверджено.');
      return box;
    }
    list(value,index) {
      const agreement=value.agreement||{},review=value.manager_review||{},notification=value.notification||{};
      const state=agreement.state==='accepted'&&agreement.invitation_receipt_confirmed!==true?'unverified':agreement.state;
      const card=this.el('article','ig-rwl-list'),head=this.el('div','ig-rwl-list-head');head.append(this.el('h4','','Точний список'+(index?' · '+(index+1):'')),this.el('span','ig-rwl-badge '+(state==='accepted'?'is-confirmed':''),agreementLabels[state]||agreementLabels.unverified));card.appendChild(head);
      this.note(card,'Одне сповіщення, коли менеджер підтвердить наявність усіх позицій у потрібній кількості.','ig-rwl-condition');
      const items=Array.isArray(value.items)?value.items:[],recipients=new Map();let ordinal=0;
      items.forEach(row=>{if(row.recipient_id&&!recipients.has(row.recipient_id))recipients.set(row.recipient_id,row.recipient_id==='self'?'Клієнт':'Одержувач '+(++ordinal));});
      const list=this.el('ul','ig-rwl-items');items.forEach(row=>list.appendChild(this.item(row,review,recipients)));card.appendChild(list);
      if(!items.length)this.note(card,'Точний склад списку недоступний.');
      const stages=this.el('div','ig-rwl-stages');
      const consent=this.el('section','ig-rwl-stage');consent.appendChild(this.el('h5','','Згода клієнта'));this.note(consent,agreementLabels[state]||agreementLabels.unverified,'ig-rwl-stage-value');
      if(state!=='unverified'){
        if(agreement.invitation_receipt_confirmed===true)this.note(consent,'Надсилання запрошення підтверджено Instagram.');
        this.date(consent,'Погоджено',agreement.accepted_at);this.date(consent,'Діє до',agreement.expires_at);
      }stages.appendChild(consent);
      const physical=this.el('section','ig-rwl-stage');physical.appendChild(this.el('h5','','Фізична перевірка'));
      const physicalLabel=review.state==='reviewed'?(review.all_ready===true?'Усі позиції підтверджено':'Ще не всі позиції доступні'):review.state==='expired'?'Потрібна свіжа перевірка':review.state==='pending'?'Очікуємо рішення менеджера':'Перевірку не підтверджено';
      this.note(physical,physicalLabel,'ig-rwl-stage-value');this.date(physical,'Перевірено',review.checked_at);
      if(this.options.canOperate===true&&review.ready===true&&uuid.test(review.request_id||'')&&digest.test(review.request_digest||'')&&typeof this.options.reviewUrl==='function'){
        const candidate=this.options.reviewUrl(this.clientId,review.request_id);let url;
        try{url=new URL(candidate,global.location.href);if(url.origin!==global.location.origin)url=null;}catch(_){url=null;}
        if(url){const link=this.el('a','ig-rwl-button','Відкрити перевірку');link.href=url.href;link.target='_blank';link.rel='noopener';link.setAttribute('aria-label','Відкрити точний запит на перевірку в новій вкладці');physical.appendChild(link);}
      }stages.appendChild(physical);
      const delivery=this.el('section','ig-rwl-stage');delivery.appendChild(this.el('h5','','Сповіщення'));
      const sent=notification.state==='sent'&&notification.native_receipt_confirmed===true;
      const uncertain=notification.state==='unknown'||notification.state==='sent'&&!sent;
      this.note(delivery,sent?'Надсилання підтверджено Instagram':uncertain?'Результат надсилання невідомий':notification.state==='pending'?'Очікує виконання':'Надсилання не підтверджено','ig-rwl-stage-value');
      if(sent)this.date(delivery,'Надіслано',notification.sent_at);
      if(uncertain)this.note(delivery,'Потрібна звірка попередньої спроби. Не надсилайте повторно.');else this.note(delivery,reasonLabel(notification.reason));
      stages.appendChild(delivery);card.appendChild(stages);
      this.note(card,'Згода не розширює дозволене вікно Instagram. Це сповіщення не створює резерв, оплату чи замовлення.','ig-rwl-policy');
      const refs=Array.isArray(value.source_refs)?value.source_refs:[];
      if(refs.length){const details=this.el('details','ig-rwl-proofs');details.appendChild(this.el('summary','','Підстави та дати'));const sources=this.el('ul');refs.forEach(ref=>{if(ref.kind==='message'&&Number.isInteger(ref.id)&&ref.id>0)sources.appendChild(this.el('li','','Повідомлення №'+ref.id+(kyivDate(ref.event_at)?' · '+kyivDate(ref.event_at):'')));});details.appendChild(sources);card.appendChild(details);}
      return card;
    }
    draw(value) {
      this.root.replaceChildren(this.heading());
      if(!value||value.version!==2||!Array.isArray(value.lists)){this.message('Дані листа очікування не вдалося підтвердити.',true);return;}
      if(!value.lists.length){this.note(this.root,value.coverage_complete===true?'Листа очікування поки немає. Очікування товару саме по собі не означає згоду на сповіщення.':'Повний перелік погоджень недоступний. Оновіть картку перед перевіркою.');return;}
      value.lists.forEach((list,index)=>this.root.appendChild(this.list(list,index)));
      if(value.coverage_complete!==true)this.note(this.root,'Показано обмежену частину історії. Це не повний перелік погоджень.','ig-rwl-coverage');
    }
    render(clientId) {
      const id=Number(clientId);
      if(!Number.isSafeInteger(id)||id<=0){this.destroy();this.clientId=null;return Promise.resolve();}
      if(id!==this.clientId){
        this.sequence++;if(this.abort)this.abort.abort();this.busy=false;this.pendingLoad=null;this.data=null;
        this.root.replaceChildren();this.clientId=id;
      }
      this.destroyed=false;return this.load();
    }
    load() {
      if(!this.clientId||this.destroyed)return Promise.resolve();
      if(this.busy&&this.pendingLoad)return this.pendingLoad;
      const sequence=++this.sequence,id=this.clientId;
      const abort=new AbortController();this.abort=abort;this.busy=true;this.root.setAttribute('aria-busy','true');
      // Parent polling must not erase verified content or restart a slow read.
      if(this.data){if(this.refreshButton)this.refreshButton.disabled=true;}
      else this.message('Завантаження листа очікування…');
      this.pendingLoad=this.read(id,sequence,abort);return this.pendingLoad;
    }
    async read(id,sequence,abort) {
      try{
        const response=await (this.options.fetch||global.fetch)(this.options.url(id),{method:'GET',credentials:'same-origin',cache:'no-store',signal:abort.signal});
        const payload=await response.json();if(sequence!==this.sequence||this.destroyed||id!==this.clientId)return;
        if(!response.ok||!payload||payload.success!==true)throw new Error(String(response.status));
        const value=payload.waiting_list;
        if(!value||value.version!==2||!Array.isArray(value.lists))throw new Error('invalid_projection');
        const unchanged=this.data&&JSON.stringify(this.data)===JSON.stringify(value);
        this.data=value;this.busy=false;if(!unchanged)this.draw(value);
      }catch(error){if(sequence!==this.sequence||this.destroyed)return;this.data=null;this.busy=false;this.message(error.message==='403'?'Немає доступу до листа очікування.':error.message==='410'?'Дані клієнта видаляються.':error.message==='invalid_projection'?'Дані листа очікування не вдалося підтвердити.':'Не вдалося завантажити лист очікування. Оновіть картку.',true);}
      finally{if(sequence===this.sequence){this.busy=false;this.pendingLoad=null;if(this.refreshButton)this.refreshButton.disabled=false;this.root.setAttribute('aria-busy','false');}}
    }
    canLeave(){return true;}
    destroy(){this.destroyed=true;this.sequence++;if(this.abort)this.abort.abort();this.pendingLoad=null;this.data=null;this.busy=false;this.root.setAttribute('aria-busy','false');this.root.replaceChildren();return true;}
  }
  const api={Controller,create:(root,options)=>new Controller(root,options)};
  if(typeof module==='object'&&module.exports)module.exports=api;
  global.IgRestockWaitingList=api;
})(typeof window!=='undefined'?window:globalThis);
