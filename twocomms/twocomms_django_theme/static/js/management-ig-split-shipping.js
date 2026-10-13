(function (global, factory) {
  'use strict';
  const api = factory();
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  if (global) global.IgSplitShipping = api;
})(typeof window !== 'undefined' ? window : null, function () {
  'use strict';
  const labels = {available:'Наявність підтверджено',wait:'Очікується готовність',unavailable:'Немає в наявності',clarify:'Потрібне уточнення'};
  const payerLabels = {customer:'Клієнт сплачує перевізнику',merchant:'Доставку сплачує TwoComms'};
  const optionLabels = {together:'Разом',separate:'Окремими відправленнями'};
  const copy = {
    uk:{together:'Разом',separate:'Окремо'},
    ru:{together:'Вместе',separate:'Отдельно'},
    en:{together:'Together',separate:'Separately'}
  };
  function money(value, maximum) {
    if (typeof value !== 'string' || !/^(?:0|[1-9]\d{0,8})(?:[.,]\d{1,2})?$/.test(value.trim())) return null;
    const parts=value.trim().replace(',','.').split('.');
    const cents=Number(parts[0])*100+Number((parts[1]||'').padEnd(2,'0'));
    if (!Number.isSafeInteger(cents) || cents<0 || cents>(maximum==null?100000:maximum)*100) return null;
    return Math.floor(cents/100)+'.'+String(cents%100).padStart(2,'0');
  }
  function date(value) {
    if (typeof value!=='string'||!value) return '';
    const parsed=new Date(value);
    return Number.isNaN(parsed.valueOf())?'':parsed.toLocaleString('uk-UA',{timeZone:'Europe/Kyiv',day:'2-digit',month:'2-digit',year:'numeric',hour:'2-digit',minute:'2-digit'})+' (Київ)';
  }
  function timing(value) {
    if (!value || !Number.isInteger(value.min_days) || !Number.isInteger(value.max_days) || value.min_days<0 || value.max_days<value.min_days || !['calendar','business'].includes(value.unit)) return 'Строк не підтверджено';
    const anchor=date(value.anchor_at);
    if (!anchor) return 'Дату перевірки не підтверджено';
    if (value.min_days===0&&value.max_days===0) return 'У день перевірки · '+anchor;
    return (value.min_days===value.max_days?String(value.min_days):value.min_days+'–'+value.max_days)+' '+(value.unit==='business'?'робочих':'календарних')+' днів · від '+anchor;
  }
  function positiveId(value) { return /^[1-9]\d{0,14}$/.test(String(value||'')); }
  function hasDisplayLabel(line) {return line&&typeof line.display_label==='string'&&Boolean(line.display_label.trim());}
  function lineLabel(line) {return hasDisplayLabel(line)?line.display_label:line&&typeof line.label==='string'&&line.label||line&&typeof line.title==='string'&&line.title||'Позиція кошика';}
  function lineSummary(line) {return lineLabel(line)+(hasDisplayLabel(line)&&Number.isInteger(line.quantity)?' · '+line.quantity+' шт.':'');}
  function reason(value) {
    const groups = [
      [['decision_changed','decision_digest_changed','split_decision_changed','configuration_changed'], 'Товари або перевірка менеджера змінилися. Оновіть кошик і звірте позиції перед новим погодженням.'],
      [['customer_delivery_cap_exceeded','delivery_cap_invalid','split_delivery_cap_invalid'], 'Перевірте погоджений максимум вартості доставки для клієнта. Перевищення потребує нового погодження.'],
      [['split_dispatch_terms_changed','split_quote_source_changed','split_choice_terms_invalid'], 'Умови доставки чи замовлення змінилися. Оновіть план і звірте максимум для клієнта та післяплату перед фіксацією ТТН.'],
      [['split_order_amount_changed','split_money_allocation_invalid','split_cod_authority_missing','split_amount_invalid'], 'Потрібна звірка сум товарів, оплати та післяплати з погодженим замовленням. Зміну суми клієнт має погодити окремо.'],
      [['split_full_basket_not_ready','basket_readiness_unverified','split_feasibility_unverified','order_not_ready','checkout_not_ready'], 'Повний кошик ще не готовий до оформлення. Перевірте всі позиції, очікувані товари та складські залишки.'],
      [['split_inventory_receipt_missing','checkout_allocation_failed'], 'Підтвердження повного складського резерву недоступне. Звірте залишки та резерв у звичайному оформленні замовлення.'],
      [['split_verified_payment_missing','split_order_payment_requires_manual','split_paid_generation_changed','split_paid_order_binding_changed'], 'Оплата або її зв’язок із замовленням потребує перевірки. Відкрийте оплачену пропозицію та звірте платіжні докази.'],
      [['split_order_source_unknown','split_order_owner_changed','split_order_owner_missing','split_original_purchase_changed','split_order_item_map_changed','split_original_item_changed','split_full_order_coverage_missing','split_parcel_allocation_changed','split_manifest_coverage_changed','checkout_provenance_invalid'], 'Склад або джерело замовлення змінилися. Звірте клієнта, усі позиції та погоджену пропозицію перед прив’язкою плану.'],
      [['split_tracking_invalid'], 'Вкажіть дійсну фактичну ТТН Нової пошти: 14 цифр.'],
      [['split_tracking_already_used'], 'Цю ТТН уже прив’язано до відправлення. Перевірте наявну посилку перед новим збереженням.'],
      [['split_order_already_planned','split_choice_already_bound'], 'План уже прив’язано до замовлення. Оновіть картку та перегляньте збережений результат.'],
      [['split_order_already_shipped'], 'У замовлення вже є відправлення. Звірте наявні ТТН перед прив’язкою нового плану.'],
      [['split_dispatch_conflict'], 'Результат фіксації посилки змінився. Оновіть картку та звірте вже збережену ТТН перед повтором.'],
      [['split_plan_unverified','split_plan_binding_changed','split_manifest_required','split_initial_binding_clock_invalid','split_parcel_missing'], 'Підтвердження плану відправлень недоступне. Звірте погоджені умови, замовлення та повний склад посилок.'],
      [['split_order_unavailable'], 'Замовлення недоступне або скасоване. Перевірте його поточний стан перед оформленням відправлень.'],
      [['split_offer_superseded','offer_expired'], 'Умови пропозиції замінено або строк їх дії минув. Потрібне нове погодження клієнта.'],
      [['actor_forbidden','split_permission_changed','shipping_permission_denied'], 'Збереження недоступне. Перевірте авторизацію та право керування ботом.']
    ];
    const group=groups.find(([codes])=>codes.includes(value));
    return group?group[1]:({choice_missing:'Клієнт ще не підтвердив варіант відправлення.',split_choice_missing:'Клієнт ще не підтвердив варіант відправлення.',operation_conflict:'Ця операція має інший збережений вміст. Потрібна звірка результату.',manager_review_pending:'Спочатку потрібна перевірка менеджера.',manager_review_expired:'Перевірка менеджера втратила актуальність.',offer_body_too_large:'Текст повного кошика перевищує ліміт повідомлення. Потрібен коротший формат пропозиції від підтримки.',parcel_partition_invalid:'Розподіліть усі позиції по відправленнях без повторів і порожніх груп.'})[value]||'Дію не виконано. Оновіть картку та перевірте актуальні умови.';
  }
  class Controller {
    constructor(root, options) {
      this.root=root;this.options=options||{};this.document=root.ownerDocument;
      this.apiUrl=this.options.apiUrl||root.dataset.apiUrl||'';
      this.fetch=this.options.fetch||(typeof window!=='undefined'&&window.fetch.bind(window));
      this.canOperate=this.options.canOperate===true||this.options.canOperate==null&&root.dataset.canOperate==='true';
      this.csrf=this.options.csrfToken||root.dataset.csrfToken||'';
      this.uuid=this.options.operationId||(()=>{const crypto=this.options.crypto||(typeof window!=='undefined'&&window.crypto);if(!crypto||typeof crypto.randomUUID!=='function')throw new Error('Оновіть сторінку для безпечного збереження операції.');return crypto.randomUUID();});
      this.projection=null;this.busy=false;this.pending=null;this.unknown=false;this.destroyed=false;this.generation=0;this.editorOpen=false;this.draft=null;this.executionDraft={};
      root.classList.add('ig-split-shipping');this.draw();
    }
    node(tag, cls, text) {const node=this.document.createElement(tag);if(cls)node.className=cls;if(text!=null)node.textContent=String(text);return node;}
    button(text, action, primary) {const node=this.node('button','ig-ss-button'+(primary?' primary':''),text);node.type='button';node.addEventListener('click',action);return node;}
    note(text, cls) {return this.node('p',cls||'ig-ss-muted',text);}
    field(label, input) {const node=this.node('label','ig-ss-field',label);node.appendChild(input);return node;}
    say(text, error) {if(!this.feedback)return;this.feedback.textContent=text||'';this.feedback.classList.toggle('is-error',Boolean(error));}
    lock(value) {this.busy=value;this.root.setAttribute('aria-busy',String(value));this.root.querySelectorAll('button,input,select,textarea').forEach(node=>{node.disabled=value||this.unknown||node.dataset.ssDisabled==='true';});if(this.recoverButton)this.recoverButton.disabled=value;if(this.refreshButton)this.refreshButton.disabled=value;}
    disabled(node, value) {node.dataset.ssDisabled=String(Boolean(value));node.disabled=Boolean(value)||this.busy||this.unknown;}
    select(values, label, value) {const node=this.node('select');node.setAttribute('aria-label',label);values.forEach(([key,text])=>{const option=this.node('option','',text);option.value=key;node.appendChild(option);});node.value=value||'';return node;}
    get dirty() {return Boolean(this.editorOpen&&this.draft&&this.draft.touched)||Object.values(this.executionDraft).some(row=>Object.values(row).some(Boolean));}
    canLeave() {return !this.pending&&!this.dirty;}
    async load() {
      if(this.busy||this.destroyed)return false;
      const version=++this.generation;this.lock(true);this.say('Оновлюємо умови відправлення…');
      try {
        const response=await this.fetch(this.apiUrl,{method:'GET',credentials:'same-origin',cache:'no-store',headers:{'X-Requested-With':'XMLHttpRequest'}});
        const data=await response.json();if(this.destroyed||version!==this.generation)return false;
        if(!response.ok||data.success===false)throw new Error('Не вдалося прочитати умови відправлення.');
        this.projection=data.shipping||data.projection||data;this.draw();this.say(this.unknown?'Результат попередньої операції ще не підтверджено. Повторіть ту саму операцію для звірки.':'');return true;
      } catch(error) {if(!this.destroyed&&version===this.generation)this.say(error.message||'Не вдалося оновити умови.',true);return false;}
      finally {if(!this.destroyed&&version===this.generation)this.lock(false);}
    }
    newDraft() {
      this.draft={decisionDigest:(this.projection||{}).decision_digest||'',groups:[],assignments:{},terms:{together:{payer:'',cap:''}},touched:false};this.editorOpen=true;this.draw();
    }
    draftValid() {
      const current=this.projection||{},draft=this.draft,lines=current.lines||[];
      if(!draft||current.ready!==true||draft.decisionDigest!==current.decision_digest||!current.decision_digest||lines.length<2||lines.length>20||!lines.every(line=>['available','wait'].includes(line.availability)&&Number.isInteger(line.quantity)&&line.quantity>0))return false;
      if(draft.groups.length<2||draft.groups.length>Math.min(20,lines.length))return false;
      const ids=lines.map(line=>line.line_id);if(ids.some(id=>typeof id!=='string'||!id)||new Set(ids).size!==ids.length)return false;
      if(!ids.every(id=>draft.groups.includes(draft.assignments[id]))||!draft.groups.every(key=>ids.some(id=>draft.assignments[id]===key)))return false;
      return ['together',...draft.groups].every(key=>{const term=draft.terms[key]||{},cap=money(term.cap);return Boolean(payerLabels[term.payer])&&cap!==null&&(term.payer!=='merchant'||cap==='0.00');});
    }
    draftOptions() {
      if(!this.draftValid())throw new Error('Розподіліть весь кошик без порожніх груп і вкажіть погоджений максимум доставки для кожного варіанта.');
      const lines=this.projection.lines,draft=this.draft;
      const parcel=(draftKey,wireKey,ids)=>({key:wireKey,line_ids:ids,payer:draft.terms[draftKey].payer,customer_delivery_cap:money(draft.terms[draftKey].cap),currency:'UAH'});
      return {together:{parcels:[parcel('together','p1',lines.map(line=>line.line_id))]},separate:{parcels:draft.groups.map((key,index)=>parcel(key,'p'+(index+1),lines.filter(line=>draft.assignments[line.line_id]===key).map(line=>line.line_id)))}};
    }
    updateIssue() {if(this.issueButton)this.disabled(this.issueButton,!this.draftValid());}
    termsEditor(key,title) {
      const box=this.node('fieldset','ig-ss-terms');box.appendChild(this.node('legend','',title));
      const term=this.draft.terms[key]||(this.draft.terms[key]={payer:'',cap:''});
      const payer=this.select([['','Оберіть платника'],['customer','Клієнт'],['merchant','TwoComms']],title+' — платник',term.payer);
      const cap=this.node('input');cap.type='text';cap.inputMode='decimal';cap.maxLength=9;cap.value=term.cap;cap.placeholder='Наприклад, 150,00';cap.setAttribute('aria-label',title+' — максимальна вартість для клієнта');
      const row=this.node('div','ig-ss-fields');row.append(this.field('Хто сплачує перевізнику',payer),this.field('Максимальна вартість для клієнта, UAH',cap));
      payer.addEventListener('change',()=>{term.payer=payer.value;if(payer.value==='merchant'){term.cap='0.00';cap.value=term.cap;}this.disabled(cap,payer.value==='merchant');this.draft.touched=true;this.updateIssue();});
      cap.addEventListener('input',()=>{term.cap=cap.value;this.draft.touched=true;this.updateIssue();});this.disabled(cap,term.payer==='merchant');
      box.append(row,this.note('Це погоджений максимум оплати клієнтом перевізнику. Фактична сума може бути нижчою; перевищення потребує нового погодження.'));return box;
    }
    editor(current) {
      if(!this.draft)this.newDraft();
      const box=this.node('section','ig-ss-editor');box.append(this.node('h4','','Підготувати варіанти'),this.note('Пропозиція охоплює весь кошик. Готовність не означає, що посилку вже відправлено.'));
      if(this.draft.decisionDigest!==current.decision_digest){box.append(this.note('Перевірка менеджера змінилася. Чернетку збережено для звірки; створіть нову чернетку перед надсиланням.','ig-ss-warning'),this.button('Відкинути чернетку й почати заново',()=>this.newDraft()));return box;}
      if(!this.draft.groups.length)box.appendChild(this.button('Створити окремі відправлення',()=>{this.draft.groups=['parcel-1','parcel-2'];this.draft.touched=true;this.draw();}));
      const basket=this.node('div','ig-ss-assignment');
      (current.lines||[]).forEach(line=>{const row=this.node('div','ig-ss-assignment-row');row.appendChild(this.node('b','',lineSummary(line)));
        const picker=this.select([['','Оберіть відправлення'],...this.draft.groups.map((key,index)=>[key,'Відправлення '+(index+1)])],(line.label||'Позиція')+' — відправлення',this.draft.assignments[line.line_id]);
        picker.addEventListener('change',()=>{this.draft.assignments[line.line_id]=picker.value;this.draft.touched=true;this.updateIssue();});this.disabled(picker,!this.draft.groups.length);
        row.append(this.field('Група позиції',picker));basket.appendChild(row);});box.appendChild(basket);
      if(this.draft.groups.length&&this.draft.groups.length<Math.min(20,(current.lines||[]).length))box.appendChild(this.button('Додати відправлення',()=>{let number=1;while(this.draft.groups.includes('parcel-'+number))number++;this.draft.groups.push('parcel-'+number);this.draft.touched=true;this.draw();}));
      box.appendChild(this.termsEditor('together','Разом · весь кошик'));
      this.draft.groups.forEach((key,index)=>{const terms=this.termsEditor(key,'Відправлення '+(index+1));
        if(this.draft.groups.length>2)terms.appendChild(this.button('Прибрати це відправлення',()=>{this.draft.groups=this.draft.groups.filter(value=>value!==key);Object.keys(this.draft.assignments).forEach(id=>{if(this.draft.assignments[id]===key)delete this.draft.assignments[id];});delete this.draft.terms[key];this.draft.touched=true;this.draw();}));box.appendChild(terms);});
      box.append(this.note('Клієнт обере «разом» або «окремо» у повідомленні. Менеджер не підтверджує вибір замість клієнта.'));
      this.issueButton=this.button('Надіслати варіанти клієнту',()=>{try{this.perform({action:'issue_offer',expected_decision_digest:this.draft.decisionDigest,options:this.draftOptions()});}catch(error){this.say(error.message,true);}},true);
      const actions=this.node('div','ig-ss-actions');actions.append(this.issueButton,this.button('Відкинути чернетку',()=>{this.draft=null;this.editorOpen=false;this.draw();}));box.appendChild(actions);this.updateIssue();return box;
    }
    basket(current) {
      if(!Array.isArray(current.lines)||!current.lines.length)return null;
      const section=this.node('section','ig-ss-basket');section.appendChild(this.node('h4','','Увесь кошик'));
      (current.lines||[]).forEach(line=>{const row=this.node('article','ig-ss-line');const head=this.node('div','ig-ss-line-head');head.append(this.node('b','',lineLabel(line)),this.node('span','ig-ss-state '+(line.availability||'unknown'),labels[line.availability]||'Ще не перевірено'));
        row.appendChild(head);if(hasDisplayLabel(line))row.appendChild(this.note('Кількість: '+(Number.isInteger(line.quantity)?line.quantity:'не підтверджено')+' шт.'));
        row.append(this.note('Готовність: '+timing(line.preparation)),this.note('Відправка: '+timing(line.dispatch)));section.appendChild(row);});
      const checked=date(current.decided_at||current.checked_at||current.decision&&current.decision.decided_at);if(checked)section.appendChild(this.note('Перевірка менеджера: '+checked));return section;
    }
    offer(current) {
      const offer=current.offer,section=this.node('section','ig-ss-offer');section.append(this.node('h4','','Погодження варіантів · версія '+offer.version),this.note('Діє до: '+(date(offer.expires_at)||'строк не підтверджено')));
      const offeredLines=Array.isArray(offer.lines)?offer.lines:current.lines||[];
      const comparisons=this.node('div','ig-ss-comparison');
      ['together','separate'].forEach(option=>{const card=this.node('section','ig-ss-option');card.appendChild(this.node('h5','',optionLabels[option]));
        const parcels=offer.options&&offer.options[option]&&offer.options[option].parcels||[];
        let maximum=0,known=parcels.length>0;
        parcels.forEach((parcel,index)=>{const box=this.node('div','ig-ss-parcel');box.appendChild(this.node('b','','Відправлення '+(index+1)));const list=this.node('ul');
          (parcel.line_ids||[]).forEach(id=>{const line=offeredLines.find(item=>item.line_id===id);list.appendChild(this.node('li','',line?lineSummary(line):'Позиція попереднього кошика'));});
          const cap=parcel.currency==='UAH'?money(parcel.customer_delivery_cap):null;
          if(cap===null||!payerLabels[parcel.payer]||parcel.payer==='merchant'&&cap!=='0.00')known=false;else maximum+=Math.round(Number(cap)*100);
          box.append(list,this.note(payerLabels[parcel.payer]||'Платник не підтверджений'),this.note('Максимальна вартість для клієнта: '+(cap!==null?cap.replace('.',',')+' UAH':'не підтверджена')));card.appendChild(box);});
        card.appendChild(this.note('Максимум за всі відправлення: '+(known?(Math.floor(maximum/100)+','+String(maximum%100).padStart(2,'0')+' UAH'):'не підтверджено'),'ig-ss-option-total'));comparisons.appendChild(card);});section.appendChild(comparisons);
      const locale=String(offer.locale||current.locale||'uk').toLowerCase().split('-')[0],buttons=copy[locale]||copy.uk;
      const preview=this.node('div','ig-ss-customer-options');preview.append(this.node('span','',buttons.together),this.node('span','',buttons.separate));section.append(this.note('Варіанти вибору в повідомленні клієнту'),preview);
      if(typeof offer.body==='string'&&offer.body.trim()){
        const message=this.node('details','ig-ss-message-preview');message.append(this.node('summary','','Повідомлення клієнту'),this.node('p','',offer.body));section.appendChild(message);
      }
      const delivery=current.delivery||{},states={pending:'Повідомлення очікує надсилання',queued:'Повідомлення в черзі',sent:'Нативне надсилання підтверджено',unknown:'Результат надсилання невідомий. Потрібна звірка; повторне повідомлення не створюйте.',cancelled:'Надсилання скасовано',absent:'Підтвердження надсилання відсутнє'};
      section.appendChild(this.note(delivery.status==='sent'&&!delivery.mid?'Підтвердження надсилання відсутнє':states[delivery.status]||states.absent,'ig-ss-receipt'));
      const choice=current.choice;
      if(choice&&optionLabels[choice.option]&&choice.confirmed_at)section.appendChild(this.note('Клієнт обрав: '+optionLabels[choice.option]+' · '+(date(choice.confirmed_at)||'час не підтверджено'),'ig-ss-confirmed'));
      else section.appendChild(this.note('Вибір клієнта ще не підтверджено. Відправлення автоматично не розділяються.'));
      if(this.canOperate&&!this.editorOpen&&current.ready===true&&(!choice||current.can_issue_offer===true))section.appendChild(this.button('Підготувати нові умови',()=>this.newDraft()));return section;
    }
    binding(current) {
      const choice=current.choice;if(!choice||!choice.id||!optionLabels[choice.option]||!choice.confirmed_at)return null;
      if((current.plans||[]).some(plan=>plan.ready===true&&plan.choice_id===choice.id))return null;
      const section=this.node('section','ig-ss-binding');section.append(this.node('h4','','Прив’язати повне замовлення'),this.note('Використайте вже оформлене замовлення цього клієнта. Погодження відправлень не замінює перевірку оплати й повного залишку.'));
      if(!this.canOperate)return section;
      if(current.can_bind_order!==true){section.appendChild(this.note('Прив’язка ще недоступна. Потрібні звичайні підтвердження оформлення всього кошика.'));return section;}
      const candidates=Array.isArray(current.bindable_orders)?current.bindable_orders.filter(row=>row.choice_id===choice.id&&positiveId(row.id)):[];
      if(Array.isArray(current.bindable_orders)&&!candidates.length){section.appendChild(this.note('Для цього погодження немає готового повного замовлення.'));return section;}
      const order=candidates.length?this.select([['','Оберіть замовлення'],...candidates.map(row=>[String(row.id),row.number||('Замовлення №'+row.id)])],'Повне замовлення'):this.node('input');
      if(!candidates.length){order.type='text';order.inputMode='numeric';order.maxLength=15;order.setAttribute('aria-label','ID наявного повного замовлення');}
      const saved=this.executionDraft['bind:'+choice.id]||(this.executionDraft['bind:'+choice.id]={order:''});order.value=saved.order;
      const submit=this.button('Прив’язати погоджений план',()=>{if(positiveId(order.value))this.perform({action:'bind_order',choice_id:choice.id,order_id:Number(order.value)});},true);this.disabled(submit,true);
      const validate=()=>{saved.order=order.value;this.disabled(submit,!positiveId(order.value));};order.addEventListener('input',validate);order.addEventListener('change',validate);validate();
      section.append(this.field(candidates.length?'Замовлення':'ID наявного повного замовлення',order),submit);return section;
    }
    trackingStatus(parcel, trackingNumber) {
      const tracked=parcel.tracking||{},box=this.node('section','ig-ss-tracking');box.setAttribute('aria-label','Стан цієї посилки у перевізника');
      const code=String(tracked.code==null?'':tracked.code),event=date(tracked.provider_event_at),terminal=date(tracked.terminal_at),checked=date(tracked.checked_at);
      const returned=typeof tracked.status==='string'&&/поверн|возврат|вернул|returned|returning|return to|back to sender/i.test(tracked.status);
      const received=Boolean(trackingNumber&&!returned&&['9','10','11'].includes(code)&&event&&terminal&&checked&&new Date(tracked.provider_event_at).valueOf()===new Date(tracked.terminal_at).valueOf()&&new Date(tracked.checked_at).valueOf()>=new Date(tracked.provider_event_at).valueOf());
      const title=!trackingNumber?'ТТН ще не зафіксовано':received?'Отримання цієї посилки підтверджено перевізником':!parcel.tracking?'Очікує даних перевізника':'Отримання клієнтом ще не підтверджено';
      box.classList.toggle('is-received',received);box.appendChild(this.node('b','ig-ss-tracking-title',title));
      if(typeof tracked.status==='string'&&tracked.status.trim())box.appendChild(this.note('Статус перевізника: '+tracked.status));
      const facts=this.node('dl','ig-ss-tracking-facts');
      [['Код статусу',/^\d{1,3}$/.test(code)?code:''],['Подія перевізника',event],['Час завершальної події',terminal],['Перевірено',checked]].forEach(([label,value])=>{if(value){const row=this.node('div');row.append(this.node('dt','',label),this.node('dd','',value));facts.appendChild(row);}});
      if(facts.children.length){const details=this.node('details','ig-ss-tracking-details');details.append(this.node('summary','','Події та час перевірки'),facts);box.appendChild(details);}return box;
    }
    plans(current) {
      const section=this.node('section','ig-ss-plans');
      (current.plans||[]).forEach(plan=>{const box=this.node('section','ig-ss-plan');box.appendChild(this.node('h4','','План · замовлення '+(plan.order_number||('№'+plan.order_id))));
        if(plan.reason)box.appendChild(this.note(reason(plan.reason),'ig-ss-warning'));
        (plan.parcels||[]).forEach((parcel,index)=>{const row=this.node('section','ig-ss-plan-parcel');row.appendChild(this.node('h5','','Відправлення '+(index+1)));
          const lines=this.node('ul');(parcel.lines||[]).forEach(line=>lines.appendChild(this.node('li','',typeof line==='string'?line:lineSummary(line))));row.appendChild(lines);
          const cap=parcel.currency&&parcel.currency!=='UAH'?null:money(parcel.customer_delivery_cap);
          row.append(this.note(payerLabels[parcel.payer]||'Платник не підтверджений'),this.note('Максимальна вартість для клієнта: '+(cap!==null?cap.replace('.',',')+' UAH':'не підтверджена')));
          const dispatch=parcel.dispatch||{},tracking=dispatch.tracking_number||parcel.tracking&&parcel.tracking.tracking_number;
          row.appendChild(this.trackingStatus(parcel,tracking));
          if(tracking){const actualCod=typeof dispatch.cod_amount==='string'?money(dispatch.cod_amount,100000000):null;row.append(this.note('ТТН зафіксовано: '+tracking,'ig-ss-confirmed'),this.note('Фактична вартість для клієнта: '+(money(dispatch.customer_delivery_amount)!==null?money(dispatch.customer_delivery_amount).replace('.',',')+' UAH':'не підтверджена')),this.note('Післяплата за відправленням: '+(actualCod!==null?actualCod.replace('.',',')+' UAH':'не підтверджена')));}
          else if(this.canOperate){
            const cod=typeof parcel.cod_amount==='string'&&money(parcel.cod_amount,100000000)!==null?parcel.cod_amount:null;
            if(cap===null||cod===null||parcel.can_dispatch===false){row.appendChild(this.note('Фіксація ТТН недоступна: потрібні підтверджені умови доставки та сума післяплати.'));}
            else {const number=this.node('input');number.type='text';number.inputMode='numeric';number.maxLength=14;number.setAttribute('aria-label','ТТН відправлення '+(index+1));
              const amount=this.node('input');amount.type='text';amount.inputMode='decimal';amount.maxLength=9;amount.setAttribute('aria-label','Фактична вартість доставки для клієнта');
              const saved=this.executionDraft['dispatch:'+parcel.id]||(this.executionDraft['dispatch:'+parcel.id]={tracking:'',amount:''});number.value=saved.tracking;amount.value=saved.amount;
              const submit=this.button('Зафіксувати фактичну ТТН',()=>{const value=money(amount.value);if(/^[0-9]{14}$/.test(number.value)&&value!==null&&Number(value)<=Number(cap))this.perform({action:'dispatch',parcel_id:parcel.id,tracking_number:number.value,customer_delivery_amount:value,cod_amount:cod});},true);
              const validate=()=>{saved.tracking=number.value;saved.amount=amount.value;this.disabled(submit,!/^[0-9]{14}$/.test(number.value)||money(amount.value)===null||Number(money(amount.value))>Number(cap));};this.disabled(submit,true);number.addEventListener('input',validate);amount.addEventListener('input',validate);validate();
              row.append(this.note('Заповнюйте після фактичної передачі цих товарів у відправлення. Підтвердження спише саме їх зі складу один раз.'),this.field('Номер фактичної ТТН · 14 цифр',number),this.field('Фактична вартість для клієнта, UAH',amount),this.note('Післяплата за даними замовлення: '+cod.replace('.',',')+' UAH'),submit);}
          }box.appendChild(row);});section.appendChild(box);});return section;
    }
    draw() {
      this.root.replaceChildren();this.issueButton=null;this.recoverButton=null;
      const head=this.node('div','ig-ss-head');this.refreshButton=this.button('Оновити',()=>{if(this.dirty&&!this.unknown){this.say('Є незбережена чернетка. Відкиньте її перед оновленням.',true);return;}this.load();});head.append(this.node('h3','','Варіанти відправлення'),this.refreshButton);this.root.appendChild(head);
      this.feedback=this.node('div','ig-ss-feedback');this.feedback.setAttribute('role','status');this.feedback.setAttribute('aria-live','polite');
      const current=this.projection;
      if(current){const basket=this.basket(current);if(basket)this.root.appendChild(basket);else if(current.offer)this.root.appendChild(this.note('Нижче — збережені умови погодженої пропозиції. Актуальний кошик для нової пропозиції не показано.'));this.root.appendChild(this.note('Готовність і відправка — окремі строки. Дату прибуття визначає перевізник; вона не гарантується.'));
        if(current.offer)this.root.appendChild(this.offer(current));
        if(this.canOperate&&this.editorOpen&&this.draft)this.root.appendChild(this.editor(current));
        else if(this.canOperate&&!current.offer&&current.ready===true&&(current.lines||[]).length>=2&&(current.lines||[]).length<=20)this.root.appendChild(this.button('Підготувати варіанти',()=>this.newDraft(),true));
        else if(!current.offer&&current.ready===true)this.root.appendChild(this.note('Окремі відправлення потребують від 2 до 20 точних позицій у кошику.'));
        else if(!current.offer&&current.ready!==true)this.root.appendChild(this.note(current.reason?reason(current.reason):'Варіанти відправлення стануть доступні після перевірки менеджером усього кошика.'));
        const binding=this.binding(current);if(binding)this.root.appendChild(binding);
        if(Array.isArray(current.plans)&&current.plans.length)this.root.appendChild(this.plans(current));
        if(this.canOperate&&!this.pending&&Object.keys(this.executionDraft).length)this.root.appendChild(this.button('Відкинути введені дані оформлення',()=>{this.executionDraft={};this.draw();}));
        if(!this.canOperate)this.root.appendChild(this.note('Доступний перегляд. Умови змінює менеджер із правом керування ботом.'));
      }else this.root.appendChild(this.note('Умови відправлення ще не завантажені.'));
      if(this.unknown&&this.pending){const recovery=this.node('section','ig-ss-recovery');this.recoverButton=this.button('Перевірити ту саму операцію',()=>this.perform(null),true);recovery.append(this.note('Результат збереження невідомий. Умови операції зафіксовано; нова операція не створюється.'),this.recoverButton);this.root.appendChild(recovery);}
      this.root.appendChild(this.feedback);this.lock(this.busy);
    }
    async perform(payload) {
      if(!this.canOperate||this.busy||this.destroyed)return false;
      if(this.pending&&payload)return false;
      if(!this.pending){try{this.pending=Object.assign({},payload,{operation_id:this.uuid()});}catch(error){this.say(error.message,true);return false;}}
      const body=JSON.stringify(this.pending),version=this.generation;this.lock(true);this.say('Зберігаємо перевірену операцію…');
      try {
        const response=await this.fetch(this.apiUrl,{method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/json','X-CSRFToken':this.csrf,'X-Requested-With':'XMLHttpRequest'},body});
        const data=await response.json();if(this.destroyed||version!==this.generation)return false;
        if(response.status===409){if((data.reason||data.code)==='operation_conflict'){this.unknown=true;this.draw();this.say(reason('operation_conflict'),true);return false;}this.pending=null;this.unknown=false;this.lock(false);await this.load();this.say(reason(data.reason||data.code)+' Чернетку залишено для звірки.',true);return false;}
        if([400,401,403,404,410,422].includes(response.status)){this.pending=null;this.unknown=false;this.say(data.message||reason(data.reason||data.code),true);return false;}
        if(!response.ok||data.success===false||data.ready===false)throw new Error('Результат операції не підтверджено.');
        const saved=JSON.parse(body);this.pending=null;this.unknown=false;if(saved.action==='issue_offer'){this.editorOpen=false;this.draft=null;}else if(saved.action==='bind_order')delete this.executionDraft['bind:'+saved.choice_id];else if(saved.action==='dispatch')delete this.executionDraft['dispatch:'+saved.parcel_id];
        if(data.shipping||data.projection){this.projection=data.shipping||data.projection;this.draw();}else {this.lock(false);await this.load();}
        this.say('Операцію збережено. Надсилання, вибір клієнта й стан відправлення перевіряються окремо.');return true;
      } catch(error) {if(!this.destroyed&&version===this.generation){this.unknown=Boolean(this.pending);this.draw();this.say('З’єднання або відповідь не підтвердили результат. Повториться та сама операція з тими самими умовами.',true);}return false;}
      finally {if(!this.destroyed&&version===this.generation)this.lock(false);}
    }
    destroy() {if(!this.canLeave())return false;this.destroyed=true;this.generation++;this.root.replaceChildren();return true;}
  }
  function mount(root, options) {const controller=new Controller(root,options);controller.load();return controller;}
  return {Controller,mount,money,timing};
});
