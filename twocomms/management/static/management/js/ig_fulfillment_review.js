(function (global) {
  'use strict';
  const availabilityLabels = {available:'Є в наявності',unavailable:'Немає в наявності',wait:'Потрібно почекати',clarify:'Потрібне уточнення',unknown:'Ще не перевірено'};
  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text != null) node.textContent = String(text);
    return node;
  }
  function button(text, action, primary) {
    const node = el('button', 'ig-fr-button' + (primary ? ' primary' : ''), text);
    node.type = 'button'; node.addEventListener('click', action); return node;
  }
  function select(options, label) {
    const node = el('select'); node.setAttribute('aria-label', label);
    options.forEach(([value, text]) => {const option = el('option', '', text); option.value = value; node.appendChild(option);});
    return node;
  }
  function field(label, control) {const node=el('label','ig-fr-field',label);node.appendChild(control);return node;}
  function numeric(label) {const node=el('input');node.type='number';node.min='0';node.step='1';node.setAttribute('aria-label',label);return node;}
  function operationId() {
    if (global.crypto && global.crypto.randomUUID) return global.crypto.randomUUID();
    if (!global.crypto || !global.crypto.getRandomValues) throw new Error('Неможливо безпечно зберегти операцію. Оновіть сторінку.');
    const bytes=global.crypto.getRandomValues(new Uint8Array(16));bytes[6]=(bytes[6]&15)|64;bytes[8]=(bytes[8]&63)|128;
    const hex=Array.from(bytes,n=>n.toString(16).padStart(2,'0')).join('');return [hex.slice(0,8),hex.slice(8,12),hex.slice(12,16),hex.slice(16,20),hex.slice(20)].join('-');
  }
  function safeText(value, fallback) {return typeof value==='string'||typeof value==='number'?String(value):fallback;}
  function requirement(value, labels) {
    if(value===true)return 'Так';if(value===false)return 'Ні';
    return labels && labels[value] || safeText(value,'Не уточнено');
  }
  function reduceRequirements(value) {
    if(!Array.isArray(value))return value&&typeof value==='object'?value:{};
    const result={};
    value.forEach(item=>{
      if(!item||typeof item!=='object')return;
      const reasons=Array.isArray(item.reasons)?item.reasons:[];
      const explicit=Array.isArray(item.explicit_fields)?item.explicit_fields:[];
      const sparse=!Object.prototype.hasOwnProperty.call(item,'raw_quote');
      ['urgency','gift'].forEach(key=>{
        if(Object.prototype.hasOwnProperty.call(item,key)&&(typeof item[key]==='boolean'||explicit.includes(key)||reasons.includes(key+'_ambiguous')||sparse))result[key]=item[key];
      });
      if(Object.prototype.hasOwnProperty.call(item,'delivery_preference')&&(item.delivery_preference==='together'||item.delivery_preference==='split'||explicit.includes('delivery_preference')||reasons.includes('delivery_preference_ambiguous')||sparse))result.delivery_preference=item.delivery_preference;
      const withdrawal=item.supersession_hint==='withdrawn';
      const dated=['resolved','past','needs_clarification'].includes(item.date_status);
      if(withdrawal||dated||item.supersession_hint==='changed'||sparse&&['deadline_raw','deadline_date','date_status'].some(key=>Object.prototype.hasOwnProperty.call(item,key))){
        result.deadline_raw=withdrawal?'':item.deadline_raw||'';
        result.deadline_date=withdrawal?null:item.deadline_date==null?null:item.deadline_date;
        result.date_status=withdrawal?'none':item.date_status==='none'&&item.supersession_hint==='changed'?'needs_clarification':item.date_status;
        result.deadline_withdrawn=withdrawal;
      }
    });
    return result;
  }
  function deadlineLabel(requirements) {
    if(requirements.deadline_withdrawn||requirements.supersession_hint==='withdrawn')return 'Клієнт відкликав строк';
    const suffix={resolved:'',past:' · цей строк уже минув',needs_clarification:' · дату потрібно уточнити',none:''};
    return (requirements.deadline_raw||requirements.deadline_date||'Не вказано')+(suffix[requirements.date_status]||'');
  }
  function textInput(label,maxLength) {
    const input=el('textarea');input.rows=2;input.maxLength=maxLength;input.setAttribute('aria-label',label);return input;
  }
  function timingLabel(value) {
    if(!value||!Number.isInteger(value.min_days)||!Number.isInteger(value.max_days))return 'Не визначено';
    const anchor=new Date(value.anchor_at);
    if(value.min_days===0&&value.max_days===0)return 'У день перевірки'+(value.anchor_at&&!Number.isNaN(anchor.valueOf())?' · '+anchor.toLocaleDateString('uk-UA',{timeZone:'Europe/Kyiv'}):' · дату не підтверджено');
    const range=value.min_days===value.max_days?String(value.min_days):value.min_days+'–'+value.max_days;
    let text=range+' '+(value.unit==='business'?'робочих днів':'календарних днів');
    if(value.anchor_at&&!Number.isNaN(anchor.valueOf()))text+=' · від '+anchor.toLocaleString('uk-UA',{timeZone:'Europe/Kyiv'});
    return text;
  }
  const supplyLabels={warehouse_checked:'Перевірено на складі',accessible_unrecorded:'Доступно поза обліком складу',incoming_confirmed:'Підтверджене надходження'};
  function lineTarget(line) {
    const target=line.target||{};
    return [target.size?('Розмір '+target.size):'',target.color_label||target.color||'',target.fit_label||target.fit||'',target.quantity!=null?('Кількість: '+target.quantity):''].filter(Boolean).join(' · ');
  }
  function normalizeReview(data) {
    const current=data&&data.current_request;
    if(!data)return null;
    return Object.assign({},data,{
      request_id:current&&current.id,
      requirement_sources:current&&current.customer_requirements||[],
      customer_requirements:reduceRequirements(current&&current.customer_requirements),
      lines:current&&current.lines||[],
      source_refs:current&&current.sources||[],
      unavailable:!current||data.status==='none'||data.status==='unavailable'
    });
  }
  function timingEditor(title, saved) {
    const root=el('div','ig-fr-timing');root.appendChild(el('h4','',title));
    const grid=el('div','ig-fr-timing-grid');
    const preset=select([['','Не визначено'],['0','У день перевірки'],['1-3','1–3 дні'],['3','3 дні'],['7','7 днів'],['custom','Свій проміжок']],title+' — термін');
    const unit=select([['calendar','Календарні дні'],['business','Робочі дні']],title+' — одиниця');
    const range=el('div','ig-fr-range');const min=numeric(title+' — від днів'),max=numeric(title+' — до днів');min.max='90';max.max='90';
    range.append(field('Від, днів',min),field('До, днів',max));range.hidden=true;
    grid.append(field('Термін',preset),field('Рахуємо',unit),range);root.appendChild(grid);
    root.appendChild(el('p','ig-fr-muted','Відлік — від моменту збереження перевірки.'));
    preset.addEventListener('change',()=>{range.hidden=preset.value!=='custom';});
    if(saved&&Number.isInteger(saved.min_days)&&Number.isInteger(saved.max_days)){
      preset.value=saved.min_days===1&&saved.max_days===3?'1-3':saved.min_days===saved.max_days&&[0,3,7].includes(saved.min_days)?String(saved.min_days):'custom';
      min.value=saved.min_days;max.value=saved.max_days;range.hidden=preset.value!=='custom';unit.value=saved.unit||'calendar';
      if(saved.anchor_at){const date=new Date(saved.anchor_at);if(!Number.isNaN(date.valueOf()))root.appendChild(el('p','ig-fr-muted','Попередній відлік: '+date.toLocaleString('uk-UA')));}
    }
    return {root, read(){
      if(!preset.value)return null;
      let lower,upper;if(preset.value==='custom'){lower=Number(min.value);upper=Number(max.value);if(!min.value||!max.value||!Number.isInteger(lower)||!Number.isInteger(upper)||lower<0||upper<lower||upper>90){min.focus();throw new Error(title+': вкажіть проміжок від 0 до 90 днів.');}}
      else {const values=preset.value.split('-').map(Number);lower=values[0];upper=values[1]||values[0];}
      return {min_days:lower,max_days:upper,unit:unit.value};
    }};
  }
  function create(root, options) {
    const canOperate=options.canOperate===true;
    let clientId=null,review=null,generation=0,abort=null,busy=false,dirty=false,pendingOperation=null;
    root.classList.add('ig-fulfillment');
    const feedback=el('div','ig-fr-feedback');feedback.setAttribute('role','status');feedback.setAttribute('aria-live','polite');
    function say(text,error){feedback.textContent=text||'';feedback.classList.toggle('is-error',Boolean(error));}
    function lock(value){busy=value;root.setAttribute('aria-busy',String(value));root.querySelectorAll('button,input,select,textarea').forEach(node=>{node.disabled=value;});}
    function heading() {
      const head=el('div','ig-fr-head');const text=el('div');text.append(el('span','ig-fr-eyebrow','Наявність та строки'),el('h3','','Перевірка виконання'),el('p','ig-fr-muted','Точні позиції, готовність і відправка — окремо.'));
      head.append(text,button('Оновити',()=>load(true)));root.appendChild(head);
    }
    function restockInterest(interest) {
      if(!interest||typeof interest!=='object'||Array.isArray(interest))return null;
      const labels={accepted:'Разову згоду підтверджено',revoked:'Згоду відкликано',expired:'Строк погодження минув',unverified:'Згоду не підтверджено'};
      const status=Object.prototype.hasOwnProperty.call(labels,interest.status)?interest.status:'unverified';
      const aside=el('aside','ig-fr-interest');aside.setAttribute('aria-label','Згода на сповіщення про поповнення');
      const head=el('div','ig-fr-interest-head');head.append(el('h4','','Сповіщення про поповнення'),el('span','ig-fr-interest-status '+status,labels[status]));aside.appendChild(head);
      aside.appendChild(el('p','ig-fr-interest-item',safeText(interest.label,'Точна позиція не підтверджена')));
      const target=lineTarget({target:interest.target});if(target)aside.appendChild(el('p','ig-fr-muted',target));
      aside.appendChild(el('p','ig-fr-muted',status==='accepted'?'Погодження стосується одного сповіщення про цю позицію.':'Запит стосується одного сповіщення про цю позицію.'));
      if(interest.accepted_at){
        const acceptedAt=new Date(interest.accepted_at);
        if(!Number.isNaN(acceptedAt.valueOf()))aside.appendChild(el('p','ig-fr-muted','Погоджено: '+acceptedAt.toLocaleString('uk-UA',{timeZone:'Europe/Kyiv',day:'2-digit',month:'2-digit',year:'numeric',hour:'2-digit',minute:'2-digit'})+' (Київ)'));
      }
      if(status==='accepted')aside.appendChild(el('p','ig-fr-interest-note','Згоду клієнта збережено. Можливість майбутнього повідомлення поза дозволеним вікном Instagram ще не підтверджена.'));
      const notification=typeof interest.notification_state==='string'?interest.notification_state.toLowerCase():'';
      const notificationLabels={sent:'Сповіщення: запис нативного надсилання збережено.',pending:'Сповіщення очікує виконання.',unknown:'Результат сповіщення невідомий. Потрібна звірка попередньої спроби.',cancelled:'Сповіщення скасовано.'};
      if(notificationLabels[notification])aside.appendChild(el('p','ig-fr-muted',notificationLabels[notification]));
      const receipt=interest.notification_receipt;
      if(notification==='sent'&&receipt&&receipt.native_receipt_confirmed===true&&receipt.purpose==='fulfillment_manager_update')aside.appendChild(el('p','ig-fr-muted','Наявність уже повідомлено у відповіді менеджера. Повторне сповіщення не потрібне.'));
      const refs=Array.isArray(interest.source_refs)?interest.source_refs:[];
      const sourceLabels=refs.map(ref=>{
        if(!ref||typeof ref!=='object')return '';
        const id=ref.proof_message_id||ref.source_message_id||ref.message_id||ref.id;
        return typeof id==='number'||typeof id==='string'&&/^[0-9]{1,20}$/.test(id)?'Повідомлення №'+id:'';
      }).filter(Boolean);
      if(sourceLabels.length){const details=el('details','ig-fr-interest-sources');details.append(el('summary','','Підстава погодження'),el('p','ig-fr-muted',Array.from(new Set(sourceLabels)).join(' · ')));aside.appendChild(details);}
      return aside;
    }
    function evidence(current) {
      const details=el('details','ig-fr-evidence');details.appendChild(el('summary','','Звідки ці вимоги'));
      const refs=Array.isArray(current.source_refs)?current.source_refs:[];
      if(refs.length){const list=el('ul');refs.forEach(ref=>{const id=ref.proof_message_id||ref.source_message_id||ref.message_id||ref.id;const text=ref.quote||ref.text||ref.excerpt;list.appendChild(el('li','',(id?'Повідомлення №'+id:'Підтверджене джерело')+(typeof text==='string'?' · '+text:'')));});details.appendChild(list);}
      else details.appendChild(el('p','','Джерела вимог не передані. Наявність і строки потребують перевірки.'));
      if(Array.isArray(current.requirement_sources)&&current.requirement_sources.length){
        const list=el('ul');current.requirement_sources.forEach(item=>{
          if(!item||typeof item!=='object')return;
          const facts=[];
          if(['deadline_raw','deadline_date','date_status'].some(key=>Object.prototype.hasOwnProperty.call(item,key)))facts.push('Строк: '+deadlineLabel(item));
          if(Object.prototype.hasOwnProperty.call(item,'urgency'))facts.push('Терміновість: '+(item.urgency===true?'терміново':item.urgency===false?'звичайний строк':'не уточнено'));
          if(Object.prototype.hasOwnProperty.call(item,'gift'))facts.push('На подарунок: '+requirement(item.gift));
          if(Object.prototype.hasOwnProperty.call(item,'delivery_preference'))facts.push('Відправка: '+requirement(item.delivery_preference,{together:'разом',split:'окремо'}));
          const entry=el('li','',(item.source_id?'Джерело №'+item.source_id+' · ':'')+(facts.join(' · ')||'Вимоги з цього джерела не уточнено'));
          if(typeof item.raw_quote==='string'&&item.raw_quote)entry.appendChild(el('p','ig-fr-muted',item.raw_quote));
          list.appendChild(entry);
        });details.append(el('p','','Побажання за джерелами. Пізніше явне уточнення замінює попереднє.'),list);
      }
      const tech=el('details');tech.appendChild(el('summary','','Технічні ідентифікатори'));tech.appendChild(el('pre','',JSON.stringify({request_id:current.request_id,request_digest:current.request_digest,decision_id:current.decision&&current.decision.id,decision_version:current.decision&&current.decision.version},null,2)));details.appendChild(tech);return details;
    }
    function draw(current) {
      root.replaceChildren();heading();
      const interest=restockInterest(current&&current.restock_interest);if(interest)root.appendChild(interest);
      const unavailable=!current||current.unavailable;
      if(unavailable){root.append(el('p','ig-fr-muted',current&&current.status==='none'?'Активної перевірки наявності чи строків поки немає.':'Поточна перевірка недоступна. Оновіть картку після уточнення вибору.'),feedback);return;}
      const validDecision=current.status==='reviewed'&&current.decision&&current.decision.request_digest===current.request_digest;
      const finalized=current.status==='reviewed',readOnly=finalized||!canOperate;
      root.appendChild(el('span','ig-fr-status',validDecision?'Перевірено менеджером':current.status==='stale'?'Попередня перевірка втратила актуальність':'Очікує перевірки'));
      if(current.current_request&&current.current_request.scope==='restock')root.appendChild(el('p','ig-fr-muted','Сповіщення про поповнення наявності. Нижче — точні позиції з цього запиту.'));
      const requirements=current.customer_requirements||{};
      const update=current.customer_update||{};
      const deliveryConfirmed=update.state==='sent'&&update.native_receipt_confirmed===true;
      const deliveryCopy=deliveryConfirmed?'Надсилання підтверджено Instagram':({pending:'Відповідь очікує надсилання',unknown:'Результат надсилання невідомий · потрібна звірка',cancelled:'Надсилання скасовано',absent:'Підтвердження надсилання відсутнє'})[update.state]||'Підтвердження надсилання відсутнє';
      const timeline=el('ol','ig-fr-timeline');
      [['Вимоги клієнта','З поточного запиту',true],['Перевірка менеджера',validDecision?'Рішення збережене':'Ще потрібна',Boolean(validDecision)],['Відповідь клієнту',deliveryCopy,deliveryConfirmed]].forEach(([title,copy,done])=>{const item=el('li',done?'is-done':'');item.append(el('b','',title),el('span','',copy));timeline.appendChild(item);});root.appendChild(timeline);
      if(update.state==='unknown')root.appendChild(el('p','ig-fr-muted','Не надсилайте повторно без звірки результату попередньої спроби.'));
      if(deliveryConfirmed&&update.sent_at){const sentAt=new Date(update.sent_at);if(!Number.isNaN(sentAt.valueOf()))root.appendChild(el('p','ig-fr-muted','Час підтвердження Send API: '+sentAt.toLocaleString('uk-UA')));}
      const facts=el('div','ig-fr-requirements');
      [['Бажаний строк',deadlineLabel(requirements)],['Терміновість',requirements.urgency===true?'Терміново':requirements.urgency===false?'Звичайний строк':'Не уточнено'],['На подарунок',requirement(requirements.gift)],['Як відправляти',requirement(requirements.delivery_preference,{together:'Разом',split:'Окремими відправками',unknown:'Не уточнено'})]].forEach(([title,value])=>{const fact=el('div','ig-fr-fact');fact.append(el('span','',title),el('b','',value));facts.appendChild(fact);});root.appendChild(facts);
      root.appendChild(el('p','ig-fr-muted','«Разом» або «окремо» — побажання клієнта. Строк відправки не гарантує дату прибуття.'));
      const form=el('form');const lines=el('div','ig-fr-lines');const editors=[];
      (current.lines||[]).forEach((line,index)=>{
        const saved=(readOnly?((current.decision||{}).lines||[]):[]).find(item=>String(item.line_id)===String(line.line_id))||{};
        const box=el('fieldset','ig-fr-line');box.appendChild(el('legend','',safeText(line.label,'Позиція '+(index+1))));
        const target=lineTarget(line);if(target)box.appendChild(el('p','ig-fr-muted',target));
        const observation=line.observation||{};const state=typeof observation==='string'?observation:observation.availability||observation.status;
        box.appendChild(el('div','ig-fr-source-state','Знімок складського обліку у запиті: '+(availabilityLabels[state]||'Ще не перевірено')));
        if(readOnly){
          if(!saved.availability){box.appendChild(el('p','ig-fr-muted','Рішення менеджера ще немає.'));lines.appendChild(box);return;}
          box.appendChild(el('p','',(current.status==='stale'?'Попереднє рішення: ':'Рішення менеджера: ')+(availabilityLabels[saved.availability]||'Рішення недоступне')));
          if(saved.checked_quantity!=null)box.appendChild(el('p','ig-fr-muted','Перевірена кількість: '+saved.checked_quantity+' шт.'));
          if(supplyLabels[saved.supply_basis])box.appendChild(el('p','ig-fr-muted',supplyLabels[saved.supply_basis]));
          if(saved.supply_basis==='accessible_unrecorded')box.appendChild(el('p','ig-fr-muted','Менеджер підтвердив доступність поза обліком. Залишки складу цим рішенням не змінювались.'));
          if(saved.source_location)box.appendChild(el('p','ig-fr-muted','Де доступно: '+safeText(saved.source_location,'')));
          box.append(el('p','ig-fr-muted','Готовність товару: '+timingLabel(saved.preparation)),el('p','ig-fr-muted','Відправка: '+timingLabel(saved.dispatch)));
          if(saved.clarification)box.appendChild(el('p','ig-fr-muted',safeText(saved.clarification,'')));
          if(saved.alternative)box.appendChild(el('p','ig-fr-muted','Альтернатива: '+safeText(saved.alternative,'')));
          lines.appendChild(box);return;
        }
        const availability=select([['','Оберіть результат'],['available','Є в наявності'],['unavailable','Немає в наявності'],['wait','Потрібно почекати'],['clarify','Потрібне уточнення']],'Наявність — '+safeText(line.label,index+1));
        availability.value=saved.availability||'';
        const quantity=numeric('Перевірена кількість — '+safeText(line.label,index+1));quantity.min=String(Math.max(1,Number((line.target||{}).quantity)||1));quantity.max='999';if(saved.checked_quantity!=null)quantity.value=saved.checked_quantity;
        const quantityField=field('Перевірена кількість, шт.',quantity);quantityField.hidden=availability.value!=='available';
        const fields=el('div','ig-fr-fields');fields.append(field('Результат перевірки',availability),quantityField);box.appendChild(fields);
        const supplyBasis=select([['','Оберіть підтвердження'],['warehouse_checked','Перевірено на складі'],['accessible_unrecorded','Поза обліком складу']],'Підстава наявності — '+safeText(line.label,index+1));
        const basisField=field('Як підтверджено доступність',supplyBasis);
        const basisHint=el('p','ig-fr-muted');basisHint.setAttribute('aria-live','polite');basisField.appendChild(basisHint);
        supplyBasis.addEventListener('change',()=>{basisHint.textContent=supplyBasis.value==='accessible_unrecorded'?'Доступність підтверджуєте ви. Залишки у складському обліку не зміняться.':supplyBasis.value==='incoming_confirmed'?'Підтверджене майбутнє надходження. Товар поки не позначається доступним.':'';});
        const sourceLocation=el('input');sourceLocation.type='text';sourceLocation.maxLength=350;sourceLocation.placeholder='Наприклад, уже у відділенні Нової пошти';
        const locationField=field('Де доступний товар · необов’язково',sourceLocation);
        const supplyFields=el('div','ig-fr-fields ig-fr-extra');supplyFields.append(basisField,locationField);supplyFields.hidden=true;box.appendChild(supplyFields);
        const clarification=textInput('Пояснення або питання — '+safeText(line.label,index+1),350);
        clarification.placeholder='Пояснення перевірки або конкретне питання клієнту';
        const clarificationField=field('Пояснення · необов’язково',clarification);clarificationField.classList.add('ig-fr-extra');box.appendChild(clarificationField);
        const alternative=el('input');alternative.type='text';alternative.maxLength=350;alternative.placeholder='Яку іншу позицію можна запропонувати';
        const alternativeField=field('Альтернатива · необов’язково',alternative);alternativeField.classList.add('ig-fr-extra');box.appendChild(alternativeField);
        const preparation=timingEditor('Готовність товару',saved.preparation);const dispatch=timingEditor('Відправка товару',saved.dispatch);
        const timings=el('div');timings.append(preparation.root,dispatch.root);timings.hidden=!['available','wait'].includes(availability.value);box.appendChild(timings);
        availability.addEventListener('change',()=>{
          quantityField.hidden=availability.value!=='available';timings.hidden=!['available','wait'].includes(availability.value);supplyFields.hidden=!['available','wait'].includes(availability.value);
          const incoming=supplyBasis.querySelector('option[value="incoming_confirmed"]');
          if(availability.value==='wait'&&!incoming){const option=el('option','','Очікуємо надходження');option.value='incoming_confirmed';supplyBasis.appendChild(option);}
          else if(availability.value!=='wait'&&incoming){incoming.remove();basisHint.textContent='';}
          clarificationField.firstChild.textContent=availability.value==='clarify'?'Конкретне питання клієнту · обов’язково':'Пояснення або уточнення · необов’язково';
          clarification.required=availability.value==='clarify';
        });
        editors.push({line,availability,quantity,supplyBasis,sourceLocation,clarification,alternative,preparation,dispatch});lines.appendChild(box);
      });form.appendChild(lines);
      const actions=el('div','ig-fr-actions');const submit=el('button','ig-fr-button primary','Зберегти перевірку');submit.type='submit';if(!readOnly)actions.appendChild(submit);form.appendChild(actions);
      form.addEventListener('input',()=>{dirty=true;pendingOperation=null;});form.addEventListener('change',()=>{dirty=true;pendingOperation=null;});
      form.addEventListener('submit',event=>{event.preventDefault();save(editors);});
      if(!editors.length||!current.request_id||!current.request_digest)submit.disabled=true;
      root.append(form,feedback);
      if(finalized)root.appendChild(el('p','ig-fr-muted','Фінальне рішення для цього запиту збережено. Нові факти потребують нового запиту на перевірку.'));
      if(validDecision&&(current.decision.lines||[]).some(line=>line.supply_basis==='accessible_unrecorded'))root.appendChild(el('p','ig-fr-interest-note','Перед оформленням звірте складський облік або підготуйте замовлення вручну.'));
      else if(!canOperate)root.appendChild(el('p','ig-fr-muted','Доступний перегляд стану. Зберегти перевірку може менеджер із правом керування ботом.'));
      const stock=el('p','ig-fr-stock','Перевірка стосується лише цих позицій і не оновлює складський облік. Залишки складу змінюються окремо.');
      if(options.warehouseUrl){const link=el('a','','Відкрити склад');link.href=options.warehouseUrl;stock.append(' ',link);}root.append(stock,evidence(current));
    }
    async function load(manual) {
      if(busy)return;
      if(dirty){say('Є незбережена перевірка. Збережіть її або відкиньте, щоб прочитати актуальний запит.');
        if(!root.querySelector('[data-fr-discard]')){const discard=button('Відкинути зміни й оновити',()=>{dirty=false;pendingOperation=null;load(true);});discard.dataset.frDiscard='';feedback.after(discard);}return;}
      const version=++generation;const id=clientId;if(abort)abort.abort();abort=new AbortController();lock(true);if(manual)say('Оновлюємо запит…');
      try{const response=await fetch(options.url(id),{cache:'no-store',credentials:'same-origin',signal:abort.signal,headers:{'X-Requested-With':'XMLHttpRequest'}});const data=await response.json().catch(()=>({}));
        if(version!==generation)return;
        if(!response.ok||data.success!==true)throw new Error(response.status===401||response.status===403?'Немає доступу до перевірки. Оновіть авторизацію.':'Не вдалося прочитати перевірку виконання.');
        review=normalizeReview(data.review);draw(review);say('');
      }catch(error){if(version===generation&&error.name!=='AbortError')say(error.message||'Не вдалося прочитати перевірку.',true);}
      finally{if(version===generation){abort=null;lock(false);const submit=root.querySelector('[type="submit"]');if(submit&&(!review||!review.request_id||!review.request_digest||!review.lines||!review.lines.length))submit.disabled=true;}}
    }
    async function save(editors) {
      if(!canOperate||busy||!review||review.status==='reviewed')return;let decisions;
      try{decisions=editors.map(({line,availability,quantity,supplyBasis,sourceLocation,clarification,alternative,preparation,dispatch})=>{
        if(!availability.value){availability.focus();throw new Error('Перевірте наявність кожної позиції окремо.');}
        const result={line_id:line.line_id,availability:availability.value};
        if(availability.value==='available'){
          const amount=Number(quantity.value),required=Math.max(1,Number((line.target||{}).quantity)||1);
          if(!quantity.value||!Number.isInteger(amount)||amount<required||amount>999){quantity.focus();throw new Error('Для цієї позиції перевірена кількість має бути від '+required+' до 999 шт.');}result.checked_quantity=amount;
          if(!['warehouse_checked','accessible_unrecorded'].includes(supplyBasis.value)){supplyBasis.focus();throw new Error('Вкажіть, як підтверджено доступність кожної позиції в наявності.');}
        }
        if(['available','wait'].includes(availability.value)&&supplyBasis.value)result.supply_basis=supplyBasis.value;
        if(['available','wait'].includes(availability.value)&&sourceLocation.value.trim())result.source_location=sourceLocation.value.trim();
        if(availability.value==='clarify'&&!clarification.value.trim()){clarification.focus();throw new Error('Вкажіть конкретне питання, яке потрібно уточнити у клієнта.');}
        if(clarification.value.trim())result.clarification=clarification.value.trim().replace(/\s+/g,' ');
        if(alternative.value.trim())result.alternative=alternative.value.trim().replace(/\s+/g,' ');
        if(['available','wait'].includes(availability.value)){result.preparation=preparation.read();result.dispatch=dispatch.read();}
        return result;
      });if(!pendingOperation)pendingOperation=operationId();}catch(error){say(error.message,true);return;}
      const version=generation;const id=clientId;const payload={request_id:review.request_id,expected_request_digest:review.request_digest,operation_id:pendingOperation,decisions_by_line:decisions};lock(true);say('Зберігаємо перевірку…');
      try{const response=await fetch(options.url(id),{method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/json','X-CSRFToken':options.csrfToken,'X-Requested-With':'XMLHttpRequest'},body:JSON.stringify(payload)});const data=await response.json().catch(()=>({}));if(version!==generation)return;
        if(response.status===409){dirty=false;pendingOperation=null;review=null;draw(null);say('Запит клієнта змінився або перевірку вже оновив інший менеджер. Натисніть «Оновити» й звірте всі позиції перед новим збереженням.',true);return;}
        if(!response.ok||data.success!==true)throw new Error(response.status===401||response.status===403?'Збереження недоступне. Перевірте дозволи та авторизацію.':safeText(data.message,'Не вдалося зберегти перевірку. Ваші поля залишено; можна повторити.'));
        dirty=false;pendingOperation=null;if(data.review){review=normalizeReview(data.review);draw(review);say('Перевірку збережено.');}else{lock(false);await load(true);say('Перевірку збережено.');}
      }catch(error){if(version===generation)say(error.message||'Не вдалося зберегти перевірку.',true);}
      finally{if(version===generation)lock(false);}
    }
    return {get clientId(){return clientId;},canLeave(id){
      if(Number(clientId)===Number(id)||(!dirty&&!(busy&&pendingOperation)))return true;
      if(dirty&&!busy)load(true);
      return false;
    },render(id){
      if(Number(clientId)===Number(id)){if(!dirty&&!busy)load(false);return;}
      generation++;if(abort)abort.abort();clientId=Number(id);review=null;busy=false;dirty=false;pendingOperation=null;draw(null);say('Завантажуємо запит…');load(false);
    },clear(){generation++;if(abort)abort.abort();clientId=null;review=null;busy=false;dirty=false;pendingOperation=null;root.replaceChildren();}};
  }
  global.IgFulfillmentReview={create};
})(window);
