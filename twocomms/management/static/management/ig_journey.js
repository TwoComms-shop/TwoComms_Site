/* Read-only client journey, mounted above the existing management chat. */
(function () {
  'use strict';
  const NS='http://www.w3.org/2000/svg';
  const STATES=new Set(['open','complete','partial','skipped','not_applicable','invalidated','superseded']);
  const LABELS={possible:'Можливий етап',open:'Ще немає підтвердження',complete:'Підтверджено',partial:'Є частина даних',skipped:'Пропущено з причиною',not_applicable:'Не потрібен',invalidated:'Потрібно уточнити',superseded:'Є новіше значення'};
  const ICONS={
    megaphone:'M3 9h4l12-5v16l-12-5H3z M7 15l2 6h4l-2-5 M22 8v8',
    message:'M5 5h14v10H9l-4 4V5z M8 9h8 M8 12h5',
    shirt:'M8 4l-5 4 3 4 2-1v9h8v-9l2 1 3-4-5-4c-1 3-7 3-8 0z',
    brief:'M7 3h8l3 3v15H7z M15 3v4h3 M10 11h5 M10 15h5',
    image:'M4 4h16v16H4z M4 16l5-5 4 4 3-3 4 4 M15 8h.01',
    tag:'M3 4h8l10 10-7 7L3 10z M7 8h.01',
    link:'M10 14l4-4 M8 16l-1 1a4 4 0 0 1-6-6l5-5a4 4 0 0 1 6 0 M16 8l1-1a4 4 0 0 1 6 6l-5 5a4 4 0 0 1-6 0',
    money:'M3 6h18v12H3z M7 6c0 2-2 4-4 4 M17 18c0-2 2-4 4-4 M14 12a2 2 0 1 1-4 0 2 2 0 0 1 4 0',
    package:'M3 7l9-4 9 4v10l-9 4-9-4z M3 7l9 4 9-4 M12 11v10 M8 5l9 4v4',
    return:'M9 6L4 11l5 5 M4 11h10a5 5 0 0 1 0 10 M14 3h6v7',
    handshake:'M3 8l4-3 4 1 3-1 7 4-4 9-3 2-7-4z M11 6l-4 6 3 1 3-3 6 5 M3 8v7l4 1',
    work:'M4 7h16v13H4z M9 7V4h6v3 M4 12h16 M10 12v3h4v-3',
    bell:'M6 16V10a6 6 0 0 1 12 0v6l2 2H4z M10 21h4',
    gift:'M3 9h18v4H3z M5 13v8h14v-8 M12 9v12 M12 9C4 9 5 1 9 4l3 5c8 0 7-8 3-5z',
    question:'M4 4h16v12h-9l-5 4v-4H4z M10 8a2 2 0 1 1 3 2l-1 1 M12 14h.01',
    clock:'M12 3a9 9 0 1 1 0 18 9 9 0 0 1 0-18 M12 7v5l3 2',
    check:'M5 12l4 4L19 6', cross:'M7 7l10 10 M17 7L7 17',
    info:'M12 3a9 9 0 1 1 0 18 9 9 0 0 1 0-18 M12 11v6 M12 7h.01',
    person:'M12 4a3 3 0 1 1 0 6 3 3 0 0 1 0-6 M5 21v-4c0-6 14-6 14 0v4',
    repeat:'M4 8h13l-3-3 M20 16H7l3 3 M20 8v4 M4 16v-4'
  };
  function el(tag,cls,text){const n=document.createElement(tag);if(cls)n.className=cls;if(text!==undefined)n.textContent=String(text);return n;}
  function svg(tag,attrs={}){const n=document.createElementNS(NS,tag);Object.entries(attrs).forEach(([k,v])=>n.setAttribute(k,String(v)));return n;}
  function icon(name){const n=svg('svg',{viewBox:'0 0 24 24','aria-hidden':'true',focusable:'false'});n.append(svg('path',{d:ICONS[name]||ICONS.info}));return n;}
  function iconFor(n){if(n.semantic_key==='journey_case')return n.topic==='price'?'tag':n.topic==='payment'?'money':'question';const mapped=window.TwcJourneyGeometry?.visualFor(n);if(mapped?.icon)return mapped.icon;const key=n.route_kind||n.semantic_key||n.id.split(':')[1];return ({inbound:'message',inquiry:'message',catalog:'shirt',selection:'shirt',brief:'brief',custom_print:'image',dtf:'image',quoted_offer:'tag',terms:'tag',offer:'image',settlement:'money',payment:'money',fulfillment:'package',support:'return',objection_case:'question',journey_case:'question',employment:'work',collaboration:'handshake',information:'info',community:'gift',consent:'bell',reward:'gift'})[key]||(n.id.startsWith('episode:')?'repeat':'info');}
  function date(value){const d=new Date(value);return value&&!Number.isNaN(d.getTime())?new Intl.DateTimeFormat('uk-UA',{day:'numeric',month:'short',hour:'2-digit',minute:'2-digit'}).format(d):'';}
  function valueText(value){if(value===null||value===undefined||value==='')return 'Не визначено';if(typeof value==='boolean')return value?'Так':'Ні';if(typeof value!=='object')return String(value);if(Array.isArray(value))return value.map(valueText).filter(Boolean).join(' · ');return [value.title,value.size,value.fit_option_label,value.qty?('×'+value.qty):'',value.order_id?('Замовлення №'+value.order_id):'',value.status_label].filter(Boolean).join(' · ')||'Дані збережено';}
  function interpreted(edge){return edge.relation==='transcript_interpretation'&&edge.authority==='none'&&edge.provenance==='transcript_reconstruction'&&(edge.evidence_refs||[]).length>0;}
  function contextual(edge){return ['client_scope_assignment','client_order_lifecycle','client_report_context','advertising_attribution','moderation_context','story_context','case_record_context'].includes(edge.relation);}
  function contextNode(node){return node.producer==='client_order_assignments'&&node.scope==='client'&&node.episode_id===null;}
  function witnessed(edge){return !['route','prerequisite','transcript_interpretation','client_scope_assignment','client_order_lifecycle','client_report_context','advertising_attribution','moderation_context','story_context','case_record_context'].includes(edge.relation)&&(edge.evidence_refs||[]).length>0;}
  function sourceFirst(ids,nodes){
    const ads=nodes.filter(n=>n.producer==='advertising_attribution'&&ids.includes(n.id)).map(n=>n.id);
    if(!ads.length)return ids;
    const rest=ids.filter(id=>!ads.includes(id)),entry=rest.findIndex(id=>id==='guide:inquiry'||nodes.find(n=>n.id===id)?.semantic_key==='inbound');
    rest.splice(Math.max(0,entry+1),0,...ads);return rest;
  }
  function edgePair(edge){return edge.from_node_id+'\0'+edge.to_node_id;}
  function edgePriority(edge){return witnessed(edge)?2:interpreted(edge)?1:0;}
  function edgeCount(edge){return Number.isInteger(edge.repeated_count)&&edge.repeated_count>0?edge.repeated_count:1;}
  function returnEdge(edge){return edge.relation==='return'||(interpreted(edge)&&edge.interpretation_kind==='return');}
  const RETURN_OUTCOMES=new Set(['configuration_correction','offer_correction','settlement_correction','new_selection','amended_offer','choose_alternative']);
  const NEGATIVE_OUTCOMES=new Set(['declined','rejected','blocked','cancelled','restock_consent_not_granted']);
  const RETRY_OUTCOMES=new Set(['new_attempt','wait_for_attempt']);
  function edgeTone(edge){
    if(!witnessed(edge)&&!interpreted(edge))return 'neutral';
    if(edge.presentation_schema_version==='journey-presentation.v1')return edge.marker_eligible===true?(edge.materiality==='technical_failure'?'danger':'warning'):'recorded';
    if(edge.tone==='danger'||returnEdge(edge)||RETURN_OUTCOMES.has(edge.outcome)||NEGATIVE_OUTCOMES.has(edge.outcome))return 'danger';
    if(edge.tone==='warning'||edge.relation==='retry'||RETRY_OUTCOMES.has(edge.outcome))return 'warning';
    return edge.tone==='success'?'success':'recorded';
  }
  function exceptional(edge){if(edge.presentation_schema_version==='journey-presentation.v1')return edgePriority(edge)>0&&edge.marker_eligible===true;return edgePriority(edge)>0&&(returnEdge(edge)||['danger','warning'].includes(edgeTone(edge))||['objection','retry','negative'].includes(edge.interpretation_kind)||edge.relation==='retry');}
  function hasActiveWait(node,now){
    return Boolean(node.waiting?.evidence_refs?.length)||(node.timers||[]).some(t=>
      ['running','scheduled','paused'].includes(t.status)&&(t.evidence_refs||[]).length>0&&
      (!t.due_at||(Number.isFinite(now)&&Date.parse(t.due_at)>now)));
  }

  function consentView(progress={}){
    const purpose=progress.purpose||'post_purchase_marketing',reminder=purpose==='payment_reminder',restock=purpose==='restock_notification';
    const scoped=progress.schema==='journey-consent.v1'&&progress.channel==='instagram'&&['post_purchase_marketing','payment_reminder','restock_notification'].includes(progress.purpose);
    const backed=key=>scoped&&(progress[key]?.evidence_refs||[]).length>0;
    const delivered=backed('delivery')&&progress.delivery.status==='received';
    const sent=backed('invitation')&&progress.invitation.status==='sent';
    const response=backed('response')?progress.response.status:'unknown';
    const permission=backed('permission')?progress.permission.status:'unconfirmed';
    const blocked=progress.permission?.status==='blocked',refused=response==='declined',accepted=response==='accepted',revoked=['revoked','expired'].includes(permission);
    const subjectReady=backed('subject')&&progress.subject.status==='confirmed';
    const granted=accepted&&permission==='granted'&&!blocked&&!revoked&&(!(reminder||restock)||subjectReady);
    const completed=backed('notification')&&progress.notification.status==='sent';
    const label=blocked?'Повідомлення заборонено':refused?'Клієнт відмовився':revoked?'Дозвіл не чинний':completed?'Сповіщення надіслано':granted?(reminder?'Нагадування дозволено':restock?'Чекаємо наявність':delivered?'Згоду підтверджено':'Згода є · чекаємо отримання'):accepted?'Згода · перевірка каналу':sent?'Очікуємо відповідь':reminder?'Дата + окрема згода':restock?'На конкретний варіант':'Запит після оплати';
    const invitation={label:'Запрошення',state:sent?'done':'todo',note:sent?'Надіслано':progress.invitation?.status==='unavailable'?'Нативне джерело не підключено':'Відправку не підтверджено',evidence_refs:progress.invitation?.evidence_refs};
    const answer={label:'Відповідь',state:refused?'cancelled':accepted?'done':sent?'next':'todo',note:refused?'Відмовився':accepted?'Прийняв':sent?'Очікуємо відповідь':'Відповіді немає',evidence_refs:progress.response?.evidence_refs};
    const parts=reminder||restock?[
      {label:reminder?'Час нагадування':'Точний варіант',state:subjectReady?'done':'todo',note:subjectReady?(progress.subject.label||'Узгоджено'):reminder?'Узгодити дату з клієнтом':'Товар, розмір і колір',evidence_refs:progress.subject?.evidence_refs},
      invitation,answer,
      {label:reminder?'Нагадування':'Наявність → сповіщення',state:blocked||refused?'cancelled':completed?'done':granted?'next':'todo',note:completed?'Надіслано':granted?(reminder?'На узгоджену дату · нове посилання':'Після появи саме цього варіанта'):'Потрібен окремий чинний дозвіл',evidence_refs:[...(progress.permission?.evidence_refs||[]),...(progress.notification?.evidence_refs||[])]}
    ]:[
      invitation,answer,
      {label:'Отримання замовлення',state:delivered?'done':'todo',note:delivered?'Підтверджено перевізником':'Маркетинг після отримання',evidence_refs:progress.delivery?.evidence_refs},
      {label:'Дозвіл',state:blocked||refused?'cancelled':granted&&delivered?'done':revoked?'need':'todo',note:blocked?'Загальна заборона повідомлень':granted&&delivered?'Чинний для теми та каналу':revoked?'Відкликано або строк минув':'Перевірка перед кожним повідомленням',evidence_refs:progress.permission?.evidence_refs}
    ];
    return {label,tone:blocked||refused?'danger':granted&&(reminder||restock||delivered)?'success':sent||accepted||revoked?'warning':'neutral',parts};
  }
  function invoiceCountdown(timer,now){
    if(!timer||timer.kind!=='invoice_expiry'||!(timer.evidence_refs||[]).length||!['running','expired'].includes(timer.status))return null;
    const start=Date.parse(timer.started_at||''),due=Date.parse(timer.due_at||'');
    if(!Number.isFinite(now)||!Number.isFinite(start)||!Number.isFinite(due)||due<=start)return null;
    const seconds=Math.max(0,Math.ceil((due-now)/1000)),expired=timer.status==='expired'||seconds===0;
    return {expired,remaining:expired?0:Math.max(0,Math.min(1,(due-now)/(due-start))),label:expired?'Час посилання минув':seconds<60?seconds+' с':seconds<3600?Math.ceil(seconds/60)+' хв':Math.floor(seconds/3600)+' год '+Math.floor(seconds%3600/60)+' хв'};
  }
  function selectionView(fields,requirements){
    const source=fields?.schema==='journey-selection.v1'?fields:requirements;
    const list=source?.items?.filter(i=>i.required===true);
    const known=Number.isInteger(source?.total)&&source.total>0;
    const items=list?.length?list:[{label:'Тип речі'},{label:'Товар / принт'},{label:'Посадка'},{label:'Колір'},{label:'Розмір'}];
    const parts=items.map(i=>({label:i.label,state:i.status==='complete'?'done':i.status==='invalidated'?'cancelled':i.status?'need':'todo'}));
    const completed=parts.filter(i=>i.state==='done').length;
    return {parts,known,completed,total:known?items.length:null,label:known?completed+' із '+items.length+' параметрів':list?.length?completed+' відомо · вимоги уточнюються':'Параметри залежать від товару'};
  }
  function cartView(cart,fields,requirements){
    if(cart?.schema!=='journey-cart.v1'||!cart.lines?.length)return selectionView(fields,requirements);
    if(cart.lines.length===1)return {...selectionView(cart.lines[0].fields,requirements),cartLabel:cart.item_count===null?'Кількість уточнюємо':cart.item_count+' шт.'};
    const parts=cart.lines.map((line,i)=>({label:'Позиція '+(i+1),state:line.fields?.total>0&&line.fields.completed===line.fields.total?'done':line.fields?.items?.some(f=>f.status==='invalidated')?'cancelled':'need'}));
    return {parts,known:true,completed:parts.filter(p=>p.state==='done').length,total:parts.length,
      label:cart.line_count+' поз. · '+(cart.item_count===null?'кількість ?':cart.item_count+' шт.'),cartLabel:''};
  }
  function paymentView(progress={},timers=[],now){
    const timer=timers.find(t=>invoiceCountdown(t,now)),countdown=invoiceCountdown(timer,now);
    const paid=progress.paid===true,expired=!paid&&Boolean(countdown?.expired),issued=Boolean(countdown);
    const discussion=progress.discussion===true;
    if(!paid&&progress.manager_review_pending===true){
      const receipt=progress.receipt_received===true;
      return {label:'Очікує перевірки менеджером',tone:'warning',expired,counts_known:receipt,
        items:[{label:'Квитанція',state:receipt?'done':'todo',note:receipt?'Є квитанція з підтвердженого джерела; зарахування ще не перевірено.':'Наявність квитанції не підтверджено поточним джерелом.',evidence_refs:progress.receipt_evidence_refs||[]},
          {label:'Перевірка менеджером',state:'next',note:'Менеджер має звірити доказ і прийняти рішення.',evidence_refs:progress.review_evidence_refs||[]},
          {label:'Оплачено',state:'todo',note:'Квитанція та очікування перевірки не підтверджують зарахування.'}]};
    }
    return {label:paid?'Оплачено':expired?'Строк минув · оплату не підтверджено':issued?'Очікуємо оплату':discussion?'Обговорюємо оплату':'Посилання → очікування → оплата',
      tone:paid?'success':expired?'danger':issued?'warning':'neutral',expired,
      items:[{label:'Посилання на оплату',state:issued?'done':discussion?'discussion':'todo',note:issued?'Рахунок створено; строк прив’язаний до його джерела.':'Потрібне підтверджене джерело платіжного посилання.'},
        {label:'Очікування оплати',state:paid?'done':expired?'cancelled':issued||progress.current?'next':discussion?'discussion':'todo',note:expired?'Час посилання минув. З’ясовуємо причину; це ще не підтверджена відмова.':paid?'Очікування завершено.':'Таймер показує строк конкретного рахунку.'},
        {label:expired?'Оплату не підтверджено':'Оплачено',state:paid?'done':expired?'cancelled':'todo',note:paid?'Є джерело підтвердження розрахунку.':expired?'Допомога з оплатою → повторна спроба або окрема згода нагадати.':'Лише підтвердження платежу завершує цей крок.'}]};
  }
  function hasChannelHandoff(node){return ['telegram','whatsapp'].includes(node.channel_handoff?.channel)&&(node.channel_handoff?.evidence_refs||[]).length>0;}
  function eventNode(node){return Boolean(node?.presentation_event);}
  function moderationView(progress){
    const p=progress?.schema==='journey-moderation.v1'?progress:null,marked=p?.marked?.status==='recorded',warning=p?.warning?.status==='sent',stopped=p?.processing?.status==='stopped';
    return {label:stopped?'Бот на паузі':marked?'Є позначка спаму':'Позначка → попередження → пауза',items:[
      {label:'Спам позначено',state:marked?'done':'todo',note:marked?(p.mark_source==='classifier'?'Зафіксовано класифікатором · страйків '+p.strikes:'Позначка картки; автор рішення не вказаний.'):'Лише явний спам або рішення менеджера.'},
      {label:'Попередження клієнту',state:warning?'done':'todo',note:warning?'Є вихідне повідомлення з підтвердженим ID провайдера.':'Надсилання попередження не підтверджене.'},
      {label:'Обробку обмежено',state:stopped?'done':'todo',note:stopped?'На картці активна пауза або блокування бота.':marked?'Бот ще не зупинений.':'Окреме рішення за spam policy або вручну.'}]};
  }
  function caseOutcome(node){
    if(node.semantic_key!=='journey_case')return null;
    if(node.topic==='manager')return {key:node.status==='handled'?'addressed':'manager',label:node.status==='handled'?'Є відповідь · результат уточнюється':'Потрібне уточнення менеджера',tone:'manager'};
    if(node.outcome==='refused'||node.status==='refused')return {key:'refused',label:'Відмова',tone:'danger'};
    if(node.outcome==='resolved'||node.status==='resolved')return {key:'resolved',label:'Вирішено',tone:'success'};
    if(node.status==='handled')return {key:'addressed',label:'Обговорено · результат невідомий',tone:'recorded'};
    if(node.status==='unresolved')return {key:'open',label:node.materiality==='technical_failure'?'Технічна проблема':'Не вирішено',tone:node.materiality==='technical_failure'?'danger':'warning'};
    return {key:'unknown',label:'Результат невідомий',tone:'neutral'};
  }
  function planned(node){return ['possible','interpretation','client_context'].includes(node.presentation_kind)&&node.implementation_status==='planned';}
  function nodeStatusLabel(node){if(node.post_purchase&&node.state!=='complete'&&!recorded(node))return node.post_purchase.label;if(node.moderation_progress?.schema==='journey-moderation.v1')return moderationView(node.moderation_progress).label;if(node.consent_progress?.invitation?.evidence_refs?.length)return consentView(node.consent_progress).label;return node.presentation_kind==='reported'?(node.channel_report?((node.facts||[]).some(f=>f.source==='manager_message')?'Пропозиція менеджера · перехід не перевірено':'Зі слів клієнта · перехід не перевірено'):'Зі слів клієнта · потрібно звірити замовлення'):node.presentation_kind==='interpretation'?'За перепискою'+(planned(node)?' · Заплановано':''):planned(node)?'Заплановано':node.presentation_kind==='possible'?LABELS.possible:node.state==='complete'&&node.tone==='danger'?'Завершено з негативним результатом':LABELS[node.state]||LABELS.open;}
  function recorded(node){const visits=node.recorded_visits;return visits&&Number.isInteger(visits.count)&&visits.count>0&&Array.isArray(visits.evidence_refs)&&visits.evidence_refs.length>0;}
  const CONDITIONS={advertising_referral:'Якщо надійшов рекламний referral',ad_product_matched:'Товар однозначно визначено за рекламою',ad_product_needs_clarification:'Відома лише тема або товар не визначено',ad_price_question:'Якщо запитує ціну цього товару',customer_requests_later:'Клієнт хоче оплатити пізніше',reminder_permission_and_date:'Узгоджено час та окрему згоду',new_invoice_after_reminder:'Нове посилання після нагадування',confirmed_coverage:'Коли оплату підтверджено',settlement_correction:'Якщо змінилися дані розрахунку',eligible_opt_in:'Запропонувати потрібну згоду',consent_recorded:'Коли згоду зафіксовано',send_capable:'Якщо контакт дозволено',catalog_match:'Якщо це товар каталогу',custom_reference:'Якщо потрібен власний принт',availability:'Перевірити доступність',eligible_follow_up:'Якщо потрібна допомога',wait_for_attempt:'Повторити оплату',offer_correction:'Змінити умови',configuration_correction:'Змінити склад',payment_objection:'Обговорити заперечення',new_attempt:'Нова спроба оплати',current_mockup_accepted:'Після погодження чинного макета',payment_required:'Якщо потрібна оплата',verified_entitlement_covers_total:'Якщо підтверджене право покриває суму',permission_check:'Перевірити дозвіл на контакт',new_selection:'Підібрати інший товар',current_configuration_confirmed:'Якщо чинний склад підтверджено',authorised_reward_grant:'Після дозволу на нагороду'};
  function toneFor(node){if(['success','warning','danger','manager'].includes(node.tone))return node.tone;return node.state==='complete'?'success':node.state==='invalidated'?'warning':'neutral';}
  function pathCoverageText(graph){
    const path=graph?.coverage?.semantic_path;
    if(path?.reason==='conversation_route_history_truncated')return 'Частину історії маршрутів не показано через ліміт; синя лінія відображає лише доступний фрагмент.';
    if(path?.reason==='history_events_without_semantic_transitions')return 'Є збережені події етапів, але переходи між ними не зафіксовані; синя лінія не будується.';
    if(path?.reason==='trace_current_node_omitted')return 'Переписку відновлено частково; поточний етап не визначено. Окремі підтверджені переходи не заповнюють цю прогалину.';
    if(path?.reason==='trace_partial')return 'Шлях за перепискою неповний: частину переходів пропущено. Окремі підтверджені події показані зі своїми джерелами.';
    if(path?.reason==='no_history_events')return 'Переходи між етапами не зафіксовані.';
    if(path?.state==='available')return 'Суцільні стрілки — збережені переходи; пунктир — можливі шляхи.';
    return 'Переходи між етапами не зафіксовані.';
  }
  Object.assign(CONDITIONS,{check_selected_availability:'Перевірити наявність обраного',stock_rechecked_available:'Коли потрібний варіант є в наявності',unavailable_wait:'Якщо варіанта немає — очікувати',choose_alternative:'Підібрати інший варіант',offer_restock_consent:'Запропонувати дозвіл сповістити',restock_consent_granted:'Дозвіл отримано',restock_consent_not_granted:'Без дозволу на повідомлення'});
  const GUIDE_STRUCTURE={'guide:inquiry':'inbound','guide:selection':'catalog_discovery','guide:terms':'quoted_offer','guide:offer':'awaiting_payment','guide:payment':'settlement','guide:fulfillment':'fulfillment'};
  const TOPIC_STRUCTURE={catalog:'catalog_discovery',custom_print:'custom_print',dtf:'dtf_only',employment:'employment',collaboration:'collaboration',information:'information_question',support:'post_sale_request'};
  // F3-FUN-06 / F3-FUN-09: ланцюг каталогу тепер доведено до кінця життєвого циклу:
  // оплата → виконання (квартет доставки) → згода на канал (opt-in, потрібна для контакту
  // поза 20-годинним вікном) → пропозиція після покупки → перевірка UGC → право/видача
  // нагороди → новий інтерес → наступна покупка. Так «забрали» більше не висить у порожнечі.
  const POST_SALE_TAIL=['post_sale_case','channel_consent','channel_grant_checked','post_purchase_contact_offer','ugc_assessment','reward_entitlement','reward_delivery','reward_use','repeat_interest','new_purchase_interest'];
  const INLINE_CHAINS={catalog:['inbound','catalog_discovery','configured_line','quoted_offer','awaiting_payment','settlement','fulfillment',...POST_SALE_TAIL],custom:['inbound','custom_print','custom_brief','mockup_current_acceptance','configured_line','quoted_offer','awaiting_payment','settlement','fulfillment',...POST_SALE_TAIL],dtf:['inbound','dtf_only','custom_brief','mockup_current_acceptance','configured_line','quoted_offer','awaiting_payment','settlement','fulfillment',...POST_SALE_TAIL],employment:['inbound','employment','employment_response'],collaboration:['inbound','collaboration','business_decision'],information:['inbound','information_question','information_resolved'],support:['inbound','post_sale_request','post_sale_case'],inbound:['inbound','catalog_discovery','configured_line']};
  class Journey {
    constructor(options={}){
      this.options=options;this.snapshot=null;this.selected=null;this.selectedEdge=null;this.buttons=new Map();this.cells=new Map();this.edgeButtons=new Map();this.sequence=0;this.destroyed=false;this.panelKey='';this.nodeIds=[];this.zoom=1;this.modal=null;
      this.root=el('section','twc-journey');this.root.setAttribute('aria-label','Шлях клієнта');
      const top=el('div','twc-journey-top');this.title=el('h3','twc-journey-heading','Звернення');this.mode=el('span','twc-journey-mode');const heading=el('div','twc-journey-context');heading.append(this.title,this.mode);
      this.select=el('select','twc-journey-picker');this.select.setAttribute('aria-label','Покупка для перегляду');this.select.hidden=true;
      this.expand=el('button','twc-journey-expand','Карта ↗');this.expand.type='button';this.expand.setAttribute('aria-haspopup','dialog');this.expand.addEventListener('click',()=>this.openMap('all'));top.append(heading,this.select,this.expand);
      this.error=el('p','twc-journey-error');this.error.hidden=true;this.error.setAttribute('role','status');
      this.map=el('div','twc-journey-map');this.svg=svg('svg',{'aria-hidden':'true',focusable:'false'});this.svg.classList.add('twc-journey-guides');this.grid=el('div','twc-journey-grid');this.edgeLayer=el('div','twc-journey-edge-layer');this.bands=el('div','twc-journey-bands');this.map.append(this.bands,this.svg,this.grid,this.edgeLayer);
      this.detailButtons=new Map();this.discussionDetails=el('div','twc-journey-discussion-details');this.discussionDetails.hidden=true;
      this.note=el('p','twc-journey-note');this.note.hidden=true;
      this.inlineKey=el('div','twc-journey-inline-key');this.inlineKey.append(el('span','twc-journey-key-actual','Фактичний перехід'),el('span','twc-journey-key-possible','Можливий шлях'));
      this.contextKey=el('span','twc-journey-key-context');this.contextKey.hidden=true;this.inlineKey.append(this.contextKey);
      this.traceKey=el('span','twc-journey-key-trace','За перепискою');this.traceKey.hidden=true;this.inlineKey.append(this.traceKey);
      this.returnReason=el('button','twc-journey-return-reason');this.returnReason.type='button';this.returnReason.hidden=true;this.returnReason.addEventListener('click',()=>this.selectEdge(this.returnReason.dataset.edgeId));this.inlineKey.append(this.returnReason);
      this.directions=el('button','twc-journey-directions');this.directions.type='button';this.directions.setAttribute('aria-haspopup','dialog');this.directions.addEventListener('click',()=>{this.openMap('all');});this.inlineKey.append(this.directions);
      this.objections=el('section','twc-journey-objections');this.objections.setAttribute('aria-label','Питання, перешкоди та менеджер');this.objectionButtons=new Map();
      // F3-FUN-08: явна дія «Показати хід». Точка не бігає сама — власник сам вирішує,
      // коли дивитись анімацію, щоб вона не відволікала під час звичайної роботи.
      this.play=el('button','twc-journey-play','▶ Показати хід');this.play.type='button';this.play.setAttribute('aria-pressed','false');this.play.addEventListener('click',()=>this.toggleWalk());this.inlineKey.append(this.play);
      this.walker=el('span','twc-journey-walker');this.map.append(this.walker);
      this.mobileContext=el('button','twc-journey-mobile-context');this.mobileContext.type='button';this.mobileContext.addEventListener('click',()=>this.openMap());
      this.orderOutcomes=el('div','twc-journey-order-outcomes');this.orderOutcomes.hidden=true;
      this.root.append(top,this.mobileContext,this.orderOutcomes,this.error,this.map,this.inlineKey,this.objections,this.discussionDetails,this.note);
      this.select.addEventListener('change',()=>this.selectEpisode(this.select.value));
      this.escape=event=>{if(event.key==='Escape'&&this.selected){event.preventDefault();event.stopPropagation();this.closePanel(true);}};
      this.root.addEventListener('keydown',this.escape);
      this.reposition=()=>this.positionPanel();window.addEventListener('scroll',this.reposition,true);
      this.outside=event=>{if(this.panel&&!this.panel.contains(event.target)&&!event.target.closest('.twc-journey-step,.twc-journey-edge-marker,.twc-journey-detail-link,.twc-journey-objection-card,.twc-journey-concern,.twc-journey-band-chip'))this.closePanel(false);};document.addEventListener('pointerdown',this.outside);
      this.resize=new ResizeObserver(()=>this.queueLayout());this.resize.observe(this.root);
      this.visibility=()=>{if(!document.hidden)this.updateTimers();};document.addEventListener('visibilitychange',this.visibility);
      this.timerInterval=setInterval(()=>{if(!document.hidden)this.updateTimers();},1000);
    }
    async selectEpisode(value){
      if(!this.options.onEpisodeChange||!this.snapshot)return;
      const ticket=++this.sequence,previous=this.snapshot.viewed_episode_id;this.select.disabled=true;this.error.hidden=true;
      try{const snapshot=await this.options.onEpisodeChange(value?Number(value):null);if(this.destroyed||ticket!==this.sequence)return;if(!snapshot||snapshot.client_id!==this.snapshot.client_id)throw new Error('Invalid snapshot');this.closePanel(false);this.update(snapshot,{fromSelection:true});}
      catch(_){if(this.destroyed||ticket!==this.sequence)return;this.select.value=previous?String(previous):'';this.error.textContent='Не вдалося оновити карту. Попередні дані збережено.';this.error.hidden=false;}
      finally{if(!this.destroyed&&ticket===this.sequence)this.select.disabled=false;}
    }
    update(snapshot,{fromSelection=false,force=false}={}){
      if(this.destroyed||!snapshot||![1,2].includes(snapshot.schema_version)||!Array.isArray(snapshot.nodes))return;
      if(this.snapshot&&snapshot.client_id!==this.snapshot.client_id)return;
      if(!fromSelection&&(this.select.disabled||(this.snapshot?.is_history&&snapshot.viewed_episode_id!==this.snapshot.viewed_episode_id)))return;
      const server=Date.parse(snapshot.server_now||snapshot.as_of||'');if(Number.isFinite(server)){this.serverTime=server;this.serverAnchor=performance.now();}
      if(!force&&this.snapshot&&snapshot.revision===this.snapshot.revision&&snapshot.viewed_episode_id===this.snapshot.viewed_episode_id){this.updateTimers();return;}
      const sameEpisode=this.snapshot&&snapshot.viewed_episode_id===this.snapshot.viewed_episode_id;
      const previousEdges=sameEpisode?new Set((this.edges||[]).filter(witnessed).map(e=>e.id)):null;
      this.stopWalk();this.snapshot=snapshot;
      const source=snapshot.graph?.schema_version===1?snapshot.graph:{nodes:snapshot.nodes,edges:[]};
      const incoming=this.presentEvents(this.presentGraph(source,snapshot));
      const seen=new Set();this.graph={...incoming,nodes:(incoming.nodes||[]).filter(n=>{if(!n||typeof n.id!=='string'||seen.has(n.id))return false;seen.add(n.id);return true;})};this.nodeIds=[...seen];
      this.edges=(incoming.edges||[]).filter(e=>e&&typeof e.id==='string'&&seen.has(e.from_node_id)&&seen.has(e.to_node_id));
      this.newEdges=previousEdges?new Set(this.edges.filter(e=>witnessed(e)&&!previousEdges.has(e.id)).map(e=>e.id)):new Set();
      for(const [id,button]of this.buttons)if(!seen.has(id)){button.remove();this.cells.get(id)?.remove();this.buttons.delete(id);this.cells.delete(id);}
      if(this.selected&&!seen.has(this.selected))this.closePanel(false);
      if(this.selectedEdge&&!this.edges.some(e=>e.id===this.selectedEdge))this.closePanel(false);
      this.root.dataset.clientId=String(snapshot.client_id);this.root.dataset.episodeId=String(snapshot.viewed_episode_id||'');
      const current=this.graph.display_focus?this.graph.nodes.find(n=>n.id===this.graph.display_focus.node_id):this.graph.nodes.find(n=>n.route_focus)||this.graph.nodes.find(n=>n.current);this.currentId=current?.id;
      const nonCommercial=current?.semantic_key?.startsWith('collaboration')||['employment','employment_response','business_decision','information_question','information_resolved','spam_confirmed'].includes(current?.semantic_key);
      const conversational=nonCommercial||current?.route_kind||!snapshot.viewed_episode_id||!current||current.id==='guide:inquiry';
      this.title.textContent=conversational?'Звернення':incoming.transcript_reconstruction?.scope==='client'?'Шлях клієнта':(snapshot.is_history?'Історія · ':'')+(snapshot.viewed_episode?.label||'Покупка '+(snapshot.viewed_episode?.sequence||''));
      this.mode.textContent=current&&current.label!==this.title.textContent?' · '+current.label+(current.interpreted_focus?' · за перепискою':''):'';
      this.mobileContext.textContent=['Коротка карта',current?.label||'Шлях клієнта',source.nodes?.find(n=>n.story_interactions)?.story_interactions?.count?('сторис · '+source.nodes.find(n=>n.story_interactions).story_interactions.count):'',source.trace_cases?.length?('питання · '+source.trace_cases.length):''].filter(Boolean).join('  ·  ');
      this.traceKey.hidden=!incoming.transcript_reconstruction;this.traceKey.title=incoming.transcript_reconstruction?.freshness==='new_messages'?'Є нові повідомлення; шлях потребує оновлення':'Відновлено за текстовою перепискою';this.inlineKey.dataset.hasTrace=String(!this.traceKey.hidden);
      const items=[...(snapshot.episodes?.items||[])];if(snapshot.viewed_episode&&!items.some(e=>e.id===snapshot.viewed_episode.id))items.unshift(snapshot.viewed_episode);
      const optionKey=JSON.stringify(items.map(e=>[e.id,e.label,e.current]));if(optionKey!==this.optionKey){this.optionKey=optionKey;this.select.replaceChildren();if(!snapshot.current_episode_id)this.select.append(new Option('Поточний діалог',''));items.forEach(e=>this.select.append(new Option((e.label||'Покупка '+e.sequence)+(e.current?' · поточна':''),String(e.id))));}
      this.select.value=snapshot.viewed_episode_id?String(snapshot.viewed_episode_id):'';this.select.hidden=this.select.options.length<2;
      this.graph.nodes.forEach(data=>{
        let button=this.buttons.get(data.id);
        if(!button){button=el('button','twc-journey-step');button.type='button';button.dataset.nodeId=data.id;const core=el('span','twc-journey-core');core.append(el('span','twc-journey-icon'),el('span','twc-journey-status'));button.append(core,el('span','twc-journey-step-label'),el('span','twc-journey-count'));button.addEventListener('click',()=>this.toggleNode(data.id));this.buttons.set(data.id,button);const cell=el('div','twc-journey-cell');cell.append(button);this.cells.set(data.id,cell);this.grid.append(cell);}
        const key=iconFor(data);if(button.dataset.icon!==key){button.dataset.icon=key;button.querySelector('.twc-journey-icon').replaceChildren(icon(key));}
        const state=data.presentation_kind==='possible'?'possible':STATES.has(data.state)?data.state:'open';button.dataset.state=state;button.dataset.tone=eventNode(data)?data.presentation_event.tone:toneFor(data);button.dataset.current=String(data.id===this.currentId);
        // F3-FUN-01: позначаємо заперечення, для якого немає жодного підтвердженого ребра,
        // щоб CSS показав його як «можливе» (пунктирний контур), а не як реальний бар'єр.
        if(['objection_case','journey_case'].includes(data.semantic_key)){button.dataset.objection=data.presentation_kind==='possible'?'possible':'confirmed';}else delete button.dataset.objection;
        // v6 · Сегментне кільце — одна мова для складених етапів.
        // Доставка: 4 сегменти (оформлено → відправлено → у дорозі → отримано), заповнюються по кроку.
        // Підбір: по сегменту на кожну обов'язкову умову (товар, посадка, колір, розмір, кількість).
        // Заповнено = зелений; треба заповнити = світлий янтар; не застосовується = тьмяний.
        this.renderSegments(button,data);
        // F3-FUN-11: вузол, на якому зараз чогось чекають (клієнт/менеджер/оплата),
        // отримує м'яку пульсацію — видно, де саме «стоїть» розмова.
        const waiting=hasActiveWait(data,this.serverTime);
        button.dataset.waiting=String(waiting);
        // F3-FUN-12: розмова, перенесена в Telegram/WhatsApp, отримує бейдж каналу.
        const channelled=hasChannelHandoff(data);
        button.dataset.channel=String(channelled);
        let badge=button.querySelector('.twc-journey-channel');
        if(channelled){if(!badge){badge=el('span','twc-journey-channel');button.querySelector('.twc-journey-core').append(badge);}badge.textContent=data.channel_handoff.channel==='whatsapp'?'WA':'TG';badge.title='Розмову продовжено в '+(data.channel_handoff.channel==='whatsapp'?'WhatsApp':'Telegram');}
        else badge?.remove();
        let selectionCaption=button.querySelector('.twc-journey-selection-caption');
        if(data.selection_progress){if(!selectionCaption){selectionCaption=el('span','twc-journey-selection-caption');button.append(selectionCaption);}selectionCaption.textContent=[data.selection_progress.label,data.selection_progress.cartLabel].filter(Boolean).join(" · ");}else selectionCaption?.remove();
        let adBadge=button.querySelector('.twc-journey-ad-badge');
        if(data.semantic_key==='advertising_entry'){if(!adBadge){adBadge=el('span','twc-journey-ad-badge','AD');button.querySelector('.twc-journey-core').append(adBadge);}adBadge.title=data.ad_entry?.product_id?'Реклама · товар визначено':'Реклама · товар потрібно уточнити';}else adBadge?.remove();
        button.dataset.postPurchase=data.post_purchase?.readiness||'';
        button.dataset.reported=String(['website_order_report','channel_contact_report'].includes(data.producer));
        button.dataset.adOrigin=String(data.semantic_key==='advertising_entry');
        let adCaption=button.querySelector('.twc-journey-ad-caption');
        if(data.semantic_key==='advertising_entry'){if(!adCaption){adCaption=el('span','twc-journey-ad-caption');button.append(adCaption);}adCaption.textContent=data.ad_entry?.product_id?'Товар визначено':data.ad_entry?.theme?'Відома тема':data.ad_entry?'Товар уточнюємо':data.presentation_kind==='interpretation'?'Зі слів клієнта':'Можливий рекламний вхід';}else adCaption?.remove();
        let afterCaption=button.querySelector('.twc-journey-aftercare-caption');
        if(data.post_purchase&&!data.consent_progress){if(!afterCaption){afterCaption=el('span','twc-journey-aftercare-caption');button.append(afterCaption);}afterCaption.textContent=data.state==='complete'?'Підтверджено':data.post_purchase.label;}else afterCaption?.remove();
        let storyCaption=button.querySelector('.twc-journey-story-caption');
        if(data.semantic_key==='story_interactions'){if(!storyCaption){storyCaption=el('span','twc-journey-selection-caption twc-journey-story-caption');button.append(storyCaption);}storyCaption.textContent=data.story_interactions?(data.story_interactions.truncated?'≥ ':'')+data.story_interactions.count+' · відповіді '+data.story_interactions.reply_count:'Повторні взаємодії';}else storyCaption?.remove();
        let moderationCaption=button.querySelector('.twc-journey-moderation-caption');
        if(data.moderation_view){if(!moderationCaption){moderationCaption=el('span','twc-journey-selection-caption twc-journey-moderation-caption');button.append(moderationCaption);}moderationCaption.textContent=data.moderation_view.label;}else moderationCaption?.remove();
        button.dataset.composite=String(Boolean(data.semantic_key==='story_interactions'||data.moderation_view||data.selection_progress||data.consent_progress||data.payment_progress||data.delivery_progress||(data.requirements&&Array.isArray(data.requirements.items)&&data.requirements.items.some(i=>i.required===true))));
        button.querySelector('.twc-journey-step-label').textContent=(this.modal&&this.mapMode==='all'?data.label:null)||data.short_label||window.TwcJourneyGeometry?.visualFor(data)?.short_label||data.label;
        let consentCaption=button.querySelector('.twc-journey-consent-caption');
        if(data.consent_progress){if(!consentCaption){consentCaption=el('span','twc-journey-consent-caption');button.append(consentCaption);}const view=consentView(data.consent_progress);consentCaption.textContent=view.label;consentCaption.dataset.tone=view.tone;}else consentCaption?.remove();
        button.dataset.event=String(eventNode(data));this.cells.get(data.id).dataset.event=String(eventNode(data));
        // v6 · Маяк поточного етапу: мітка «ЗАРАЗ» над вузлом + хвиля-кільце (CSS). Лише для
        // справжнього фокуса — не для можливого етапу, щоб маяк не брехав про місце клієнта.
        let now=button.querySelector('.twc-journey-now');
        if(data.id===this.currentId&&data.presentation_kind!=='possible'&&!eventNode(data)){if(!now){now=el('span','twc-journey-now','зараз');button.prepend(now);}}else now?.remove();
        button.dataset.recorded=String(Boolean(recorded(data)));button.dataset.interpreted=String(data.presentation_kind==='interpretation');
        const statusKey=eventNode(data)?null:planned(data)?'brief':data.tone==='danger'?'cross':state==='complete'?'check':state==='invalidated'?'return':data.tone==='manager'?'person':data.waiting?.evidence_refs?.length?'clock':null;const status=button.querySelector('.twc-journey-status');status.hidden=!statusKey;if(statusKey)status.replaceChildren(icon(statusKey));
        const progress=data.requirements;const valid=progress&&Number.isInteger(progress.completed)&&Number.isInteger(progress.total)&&progress.total>0&&progress.completed>=0&&progress.completed<=progress.total;
        const mentions=data.semantic_key==='objection_case'?(data.facts||[]).find(f=>f.id?.endsWith(':repeat_count')):null;const eventCount=data.presentation_event?.count;const repeats=Number.isInteger(eventCount)&&eventCount>1?eventCount:Number.isInteger(mentions?.value)&&mentions.value>1?mentions.value:0;const count=button.querySelector('.twc-journey-count');
        // F3-FUN-02: для заперечень показуємо розклад результатів (оброблено/не вирішено/відмова),
        // а не саму лише кількість. Числа беруться з ig_journey_trace_projection (reason_code),
        // тому відсутній код чесно не потрапляє в жодну з категорій.
        const obOut=data.objection_outcomes;
        let countText='',countTitle='';
        if(valid){countText=progress.completed+'/'+progress.total;countTitle='Виконано обов’язкових умов: '+countText;}
        else if(obOut&&(obOut.addressed||obOut.open||obOut.refused)){countText='×'+(repeats||1);countTitle='Обговорено: '+obOut.addressed+' · Не вирішено: '+obOut.open+(obOut.refused?' · Відмова: '+obOut.refused:'');}
        else if(repeats){countText='×'+repeats;countTitle='Повторних згадок: '+repeats;}
        const outcome=caseOutcome(data);if(outcome){countText=outcome.key==='addressed'?'↩':outcome.key==='resolved'?'✓':outcome.key==='refused'?'×':outcome.key==='unknown'?'?':'!';countTitle=outcome.label;}
        count.hidden=!countText;count.textContent=countText;count.title=countTitle;
        // F3-FUN-02: підсвічуємо бейдж за найгіршим результатом (відмова > не вирішено > оброблено),
        // щоб менеджер бачив стан заперечення без відкриття деталей.
        if(obOut){const worst=obOut.refused?'refused':obOut.open?'open':obOut.addressed?'addressed':'';if(worst)count.dataset.outcomes=worst;else delete count.dataset.outcomes;}else delete count.dataset.outcomes;
        button.setAttribute('aria-expanded',String(this.selected===data.id));button.dataset.baseLabel=data.label+' — '+nodeStatusLabel(data)+(data.id===this.currentId?', поточний фокус':'')+(data.waiting?.evidence_refs?.length?', очікування: '+data.waiting.label:'')+(valid?', умов '+count.textContent:repeats?', повторних згадок '+repeats:'');if(outcome)button.dataset.baseLabel+=' · '+outcome.label;button.setAttribute('aria-label',button.dataset.baseLabel);button.title=data.label+' · '+nodeStatusLabel(data)+(outcome?' · '+outcome.label:'');
      });
      this.renderOrderOutcomes();this.renderObjections();if(this.selected)this.renderPanel();
      if(this.modal){
        this.renderAccessibleList();
        const aftercare=this.mapMode==='short'&&this.possibleFamily==='after'&&this.graph.nodes.some(n=>n.post_purchase);
        this.mapCoverage.textContent=(aftercare?'Маршрут після покупки · умови кроків показані під блоками. Продовження — нижче. ':'')+pathCoverageText(this.graph);
        if(this.edges.some(interpreted)&&this.graph.coverage?.semantic_path?.state!=='available')this.mapCoverage.textContent+=' Кольоровий пунктир позначає інтерпретацію за перепискою.';
      }
      this.queueLayout();this.updateTimers();
    }
    possibleCatalogue(catalogue){
      if(!catalogue)return catalogue;
      // Fold a possible objection into the affected route. Retain both registry
      // transition IDs; this is a catalogue path, never a witnessed client edge.
      const incoming=catalogue.transitions.filter(e=>e.target_key==='objection_case'&&e.source_key!=='objection_case');
      const outgoing=catalogue.transitions.filter(e=>e.source_key==='objection_case'&&e.target_key!=='objection_case');
      const transitions=catalogue.transitions.filter(e=>e.source_key!=='objection_case'&&e.target_key!=='objection_case').map(e=>({...e,condition_label:e.condition_label||CONDITIONS[e.outcome]||''}));
      for(const a of incoming)for(const b of outgoing)transitions.push({
        ...b,id:'objection-route:'+a.id+':'+b.id,source_key:a.source_key,
        via_objection:true,source_transition_ids:[a.id,b.id],
        condition_label:'Якщо є заперечення · '+(b.condition_label||CONDITIONS[b.outcome]||'уточнити й продовжити'),
      });
      return {...catalogue,definitions:catalogue.definitions.filter(d=>d.key!=='objection_case'),transitions};
    }

    renderObjections(){
      // v6 · Секція заперечень — лише коли є СПРАВЖНІ заперечення. Порожня «можливість» більше не
      // займає пів екрана: вона не є подією клієнта, а заперечення тепер видно на самій лінії.
      // Картки стали компактними чіпами в один рядок — деталі відкриваються кліком.
      const cases=this.graph.nodes.filter(n=>n.semantic_key==='journey_case');
      const legacy=cases.length?[]:this.graph.nodes.filter(n=>n.semantic_key==='objection_case'&&n.presentation_kind!=='possible'&&n.presentation_event?.mode!=='detail');
      const items=[...cases,...legacy];this.objections.hidden=!items.length;this.objections.replaceChildren();this.objectionButtons.clear();
      if(this.objections.hidden)return;
      const head=el('div','twc-journey-objections-head');head.append(el('strong','','Питання та перешкоди · '+items.length));
      if(cases.length){const counts={};for(const item of cases){const key=caseOutcome(item).key;counts[key]=(counts[key]||0)+1;}head.append(el('span','',Object.entries({open:'не вирішено',addressed:'обговорено',resolved:'вирішено',refused:'відмова',unknown:'невідомо',manager:'менеджеру'}).filter(([key])=>counts[key]).map(([key,label])=>counts[key]+' '+label).join(' · ')));}
      const list=el('div','twc-journey-objection-list');
      for(const item of items){
        const outcome=caseOutcome(item),card=el('button','twc-journey-objection-card');card.type='button';card.dataset.tone=outcome?.tone||'warning';
        card.append(el('span','twc-journey-objection-diamond','◆'),el('span','twc-journey-objection-name',item.label),el('span','twc-journey-objection-outcome',outcome?.label||'Є збережені події'));
        const anchor=this.graph.nodes.find(n=>n.id===item.presentation_event?.anchor_ids?.[0]);if(anchor)card.append(el('span','twc-journey-objection-anchor','біля «'+(anchor.short_label||anchor.label)+'»'));
        card.setAttribute('aria-expanded',String(this.selected===item.id));card.addEventListener('click',()=>this.toggleNode(item.id));this.objectionButtons.set(item.id,card);list.append(card);
      }
      const disclosure=el('details','twc-journey-concern-details'),summary=el('summary');summary.append(head);disclosure.append(summary,list);this.objections.append(disclosure);
      if(cases.some(n=>n.status==='handled'))disclosure.append(el('p','twc-journey-objections-note','Обговорено ≠ вирішено: згоду клієнта не підтверджено.'));
    }
    // v6 · Малює сегментне кільце навколо ядра вузла. SVG-дуги масштабуються без втрати чіткості
    // і однаково читаються в компактній карті й у повній. Сегменти розділені зазорами, тож видно
    // КОЖЕН крок окремо — власник просив «в одному елементі, але щоб кожен був зрозумілий».
    renderSegments(button,data){
      const core=button.querySelector('.twc-journey-core');let ring=core.querySelector('.twc-journey-segments');
      let parts=null,title='',centre='';
      if(data.semantic_key==='story_interactions'){
        const latest=data.story_interactions?.items?.[0];parts=['Отримано','Медіа','Аналіз','Відповідь'].map((label,i)=>({label,state:[Boolean(latest),latest?.media_available,latest?.inspected,Boolean(latest?.replies?.length)][i]?'done':'todo'}));title='Остання сторис · '+parts.filter(p=>p.state==='done').length+'/4';centre=data.story_interactions?'×'+data.story_interactions.count:'↻';
      }else if(data.moderation_view){
        parts=data.moderation_view.items;title='Модерація · '+data.moderation_view.label;centre=parts.filter(p=>p.state==='done').length+'/3';
      }else if(data.consent_progress){
        const view=consentView(data.consent_progress);parts=view.parts;title=(data.label||'Дозвіл')+' · '+view.label;centre=parts.filter(p=>p.state==='done').length+'/4';
      }else if(data.selection_progress){
        const view=data.selection_progress;parts=view.parts;title='Підбір товару · '+view.label;centre=view.known?view.completed+'/'+view.total:'?';
      }else if(data.payment_progress){
        parts=data.payment_progress.items;title='Оплата · '+parts.map(p=>p.label).join(' → ');centre=data.payment_progress.paid?'✓':data.payment_progress.counts_known===false?'?':parts.filter(p=>p.state==='done').length+'/'+parts.length;
      }else if(data.delivery_progress){
        const p=data.delivery_progress;
        parts=p.stages.map((label,i)=>({label,state:p.planned?'todo':p.cancelled?'cancelled':i<p.step?'done':i===p.step?'next':'todo'}));
        title=p.cancelled?'Замовлення скасовано':'Доставка · '+p.label+' ('+p.step+'/4)';
        centre=p.cancelled?'×':p.step+'/4';
      }else{
        const req=data.requirements;
        const list=req&&Array.isArray(req.items)?req.items.filter(i=>i.required===true).slice(0,8):[];
        if(list.length){
          parts=list.map(i=>({label:i.label,state:i.status==='complete'?'done':i.status==='not_applicable'?'na':'need'}));
          const done=parts.filter(p=>p.state==='done').length,need=parts.filter(p=>p.state==='need').length;
          title='Обов’язкові умови: '+done+' з '+parts.length+(need?' · бракує: '+parts.filter(p=>p.state==='need').map(p=>p.label).join(', '):' · ці умови заповнено');
          centre=done+'/'+parts.length;
        }
      }
      if(!parts){ring?.remove();button.querySelector('.twc-journey-segment-count')?.remove();delete button.dataset.segments;delete button.dataset.segmentDone;return;}
      const signature=JSON.stringify(parts);
      if(!ring){ring=svg('svg',{viewBox:'0 0 44 44','aria-hidden':'true',focusable:'false'});ring.classList.add('twc-journey-segments');core.append(ring);}
      if(ring.dataset.signature!==signature){
        const first=!ring.dataset.signature;ring.dataset.signature=signature;ring.replaceChildren();
        const n=parts.length,gap=n>1?10:0,span=n===1?359.99:(360-gap*n)/n,r=20,cx=22,cy=22;
        const point=deg=>{const a=(deg-90)*Math.PI/180;return (cx+r*Math.cos(a)).toFixed(2)+' '+(cy+r*Math.sin(a)).toFixed(2);};
        parts.forEach((part,i)=>{
          const start=i*(span+gap)+gap/2,end=start+span;
          const arc=svg('path',{d:'M'+point(start)+' A'+r+' '+r+' 0 '+(span>180?1:0)+' 1 '+point(end)});
          arc.classList.add('twc-journey-segment');arc.dataset.state=part.state;
          // Послідовне «запалювання» при першому показі — посилка ніби рухається по кроках.
          if(first&&part.state==='done')arc.style.animationDelay=(i*120)+'ms';
          const t=svg('title');t.textContent=part.label+' · '+({done:'готово',next:'наступний крок',todo:'ще попереду',need:'треба заповнити',na:'не застосовується',cancelled:'скасовано'})[part.state];arc.append(t);
          ring.append(arc);
        });
      }
      let count=button.querySelector('.twc-journey-segment-count');
      if(!count){count=el('span','twc-journey-segment-count');button.append(count);}
      count.textContent=centre;count.title=title;
      button.dataset.consentPurpose=data.consent_progress?.purpose||'';
      button.dataset.segments=data.moderation_view?'moderation':data.selection_progress?'selection':data.consent_progress?'consent':data.payment_progress?'payment':data.delivery_progress?'delivery':'requirements';
      button.dataset.segmentDone=String(parts.every(p=>p.state==='done'||p.state==='na'));
    }
    presentEvents(graph){
      // Objections are annotations on the affected stage, never funnel stages.
      const isObjection=n=>n.semantic_key==='objection_case';
      const nodes=graph.nodes.filter(n=>!isObjection(n)||(n.evidence_refs||[]).length||graph.edges.some(e=>edgePriority(e)>0&&(e.from_node_id===n.id||e.to_node_id===n.id)));
      const ids=new Set(nodes.map(n=>n.id)),edges=graph.edges.filter(e=>ids.has(e.from_node_id)&&ids.has(e.to_node_id));
      for(const node of nodes.filter(n=>isObjection(n))){
        const incident=edges.filter(e=>edgePriority(e)>0&&(e.from_node_id===node.id||e.to_node_id===node.id));
        if(node.presentation_schema_version==='journey-presentation.v1'&&node.presentation_kind==='interpretation'){
          node.label='Деталі переписки';node.short_label='Деталі';node.tone='recorded';
          node.presentation_event={edge_ids:incident.map(e=>e.id),anchor_ids:[],mode:'detail',count:1,tone:'recorded'};continue;
        }
        if(!(node.evidence_refs||[]).length&&!incident.length)continue;
        const incoming=incident.filter(e=>e.to_node_id===node.id&&e.from_node_id!==node.id),outgoing=incident.filter(e=>e.from_node_id===node.id&&e.to_node_id!==node.id);
        const origins=[...new Set(incoming.map(e=>e.from_node_id))],targets=[...new Set(outgoing.map(e=>e.to_node_id))];
        const unambiguous=origins.length<=1&&targets.length<=1;
        const anchors=unambiguous?[...new Set([...origins,...targets])]:[];
        const count=(node.facts||[]).find(f=>f.id?.endsWith(':repeat_count'))?.value||incident.filter(e=>e.to_node_id===node.id&&e.reason_code!=='objection_addressed').reduce((sum,e)=>sum+edgeCount(e),0)||1;
        // F3-FUN-02 (план 3.0, P2-1.2): власник просив бачити не лише «×N», а й чим закінчилось —
        // скільки разів заперечення оброблено, скільки лишилось невирішеним і де був відмов.
        // Розкладаємо інцидентні ребра за reason_code; невідомий код не вигадуємо, а тримаємо в «інші».
        const outcomeOf=e=>{const r=String(e.reason_code||'');if(r==='objection_addressed'||r==='objection_resolved')return'addressed';if(r==='objection_dismissed'||r==='objection_refused'||r==='refusal')return'refused';if(r==='objection_open'||r==='objection_unresolved'||r==='objection_partial')return'open';return'other';};
        const outcomes=incident.reduce((acc,e)=>{const k=outcomeOf(e);acc[k]=(acc[k]||0)+edgeCount(e);return acc;},{});
        node.objection_outcomes={addressed:outcomes.addressed||0,open:outcomes.open||0,refused:outcomes.refused||0};
        node.presentation_event={edge_ids:incident.map(e=>e.id),anchor_ids:anchors,mode:anchors.length===2?'between':anchors.length===1?'attached':'unplaced',count,tone:incident.some(e=>edgeTone(e)==='danger')?'danger':'warning'};
      }
      for(const item of graph.trace_cases||[]){
        if(item.marker_eligible!==true||!item.source_node_id||!ids.has(item.source_node_id))continue;
        const labels={manager:'Уточнення менеджера',delivery:'Питання доставки',price:'Заперечення щодо ціни',payment:'Складність з оплатою',configuration:'Уточнення параметрів',purchase:'Перешкода замовленню'};
        const outcome=caseOutcome({...item,semantic_key:'journey_case'});
        nodes.push({...item,id:item.id,semantic_key:'journey_case',label:labels[item.topic]||'Важливе уточнення',state:'partial',current:false,presentation_kind:'interpretation',facts:[],
          presentation_event:{edge_ids:[...new Set([...(item.source_edge_ids||[]),...(item.attempt_edge_ids||[])])],anchor_ids:[item.source_node_id],mode:'attached',count:1,tone:item.topic==='manager'?'manager':outcome.key==='addressed'?'recorded':outcome.key==='resolved'?'success':item.materiality==='technical_failure'||outcome.key==='refused'?'danger':'warning'}});
      }
      for(const n of nodes)if(n.semantic_key==='spam_confirmed'){n.moderation_view=moderationView(n.moderation_progress);n.short_label='Спам';}
      const processIds=new Set(nodes.filter(n=>!eventNode(n)).map(n=>n.id));
      return Journey.prototype.mergeSelection.call(this,Journey.prototype.mergeConsent.call(this,Journey.prototype.mergePayment.call(this,Journey.prototype.mergeDelivery.call(this,{...graph,nodes,edges,inline_main_ids:graph.inline_main_ids?.filter(id=>processIds.has(id)),inline_alternative_ids:graph.inline_alternative_ids?.filter(id=>processIds.has(id))}))));
    }
    // v6 · Доставка — ОДИН вузол замість трьох («Замовлення · отримано», «Відправлено», «Отримано»).
    // Сервер (ig_journey_client_orders._context_path) віддає 3 окремі контекстні вузли, які раніше
    // малювались окремим рядом без зв'язку зі шляхом. Тут вони згортаються в батьківський вузол
    // замовлення: діти ховаються, їхні факти лишаються в панелі, а крок обчислюється з реальних
    // фактів замовлення (статус, ТТН, підтвердження перевізника). Нічого не вигадується.
    mergeDelivery(graph){
      for(const node of graph.nodes.filter(n=>(n.structural_key||n.semantic_key)==='fulfillment')){
        const p=node.fulfillment_progress,valid=p&&Number.isInteger(p.step)&&p.step>=0&&p.step<=4&&p.evidence_refs?.length;
        if(!node.delivery_progress)node.delivery_progress={step:valid?p.step:0,planned:!valid,cancelled:valid&&p.cancelled===true,label:valid?['Скасовано','Оформлено','Відправлено','На відділенні','Отримано'][p.step]:'Кроки доставки',stages:['Оформлено','Відправлено','На відділенні','Отримано']};
        node.short_label=valid&&p.step===4?'Отримано':'Доставка';if(node.presentation_kind==='possible')node.label='Доставка';
      }
      const parents=graph.nodes.filter(n=>n.semantic_key==='client_order_context');
      if(!parents.length)return graph;
      const hidden=new Set();
      for(const parent of parents){
        const orderId=parent.contextual_binding?.order_id??parent.id.split(':')[1];
        const children=graph.nodes.filter(n=>['client_order_shipping','client_order_delivery'].includes(n.semantic_key)&&String(n.contextual_binding?.order_id??n.id.split(':')[1])===String(orderId));
        children.forEach(c=>hidden.add(c.id));
        const fact=suffix=>(parent.facts||[]).find(f=>f.id?.endsWith(':'+suffix));
        const status=String(fact('status')?.value||'');
        const delivered=fact('delivery')?.state==='complete';
        const tracking=Boolean(fact('tracking')?.value);
        const cancelled=parent.fulfillment_progress?.cancelled===true||parent.state==='invalidated'||/Скасовано/.test(status);
        // Крок 1 — оформлено (замовлення існує), 2 — відправлено (є ТТН або статус «Відправлено»),
        // 3 — у дорозі / в перевізника (відправлено й ще не отримано), 4 — отримано перевізником.
        const shipped=/^Відправлено$/.test(status)||children.some(c=>c.semantic_key==='client_order_shipping'&&c.state==='complete');
        const structured=parent.fulfillment_progress;
        const step=structured&&Number.isInteger(structured.step)&&structured.step>=0&&structured.step<=4&&structured.evidence_refs?.length?structured.step:cancelled?0:delivered?4:shipped?2:1;
        const labels=['Скасовано','Оформлено','Відправлено','На відділенні','Отримано'];
        parent.delivery_progress={step,cancelled,label:structured?.completion_unverified?'Отримання не звірено':labels[step],stages:['Оформлено','Відправлено','На відділенні','Отримано']};
        parent.short_label=structured?.completion_unverified?'Отримання не звірено':cancelled?'Скасовано':step===4?'Отримано':step===3?'На відділенні':step===2?'Відправлено':'Доставка';
        parent.label=(parent.label||'Замовлення')+' · '+labels[step].toLowerCase();
        parent.delivery_facts=children.flatMap(c=>c.facts||[]);
        if(children.some(c=>c.waiting))parent.waiting=children.find(c=>c.waiting).waiting;
      }
      for(const parent of parents){
        const orderId=parent.contextual_binding?.order_id??parent.id.split(':')[1];
        const children=graph.nodes.filter(n=>hidden.has(n.id)&&String(n.contextual_binding?.order_id??n.id.split(':')[1])===String(orderId));
        graph=Journey.prototype.collapseComposition.call(this,graph,parent,children);
      }
      return graph;
    }

    collapseComposition(graph,parent,children){
      if(!children.length)return graph;
      const members=[parent,...children],ids=new Set(members.map(n=>n.id)),hidden=new Set(children.map(n=>n.id));
      parent.composite_nodes=members.map(n=>({...n}));
      parent.composite_edges=graph.edges.filter(e=>ids.has(e.from_node_id)&&ids.has(e.to_node_id));
      parent.current=members.some(n=>n.current);parent.route_focus=members.some(n=>n.route_focus);
      parent.timers=members.flatMap(n=>n.timers||[]);
      if(!parent.waiting)parent.waiting=children.find(n=>n.waiting)?.waiting;
      if(!hasChannelHandoff(parent))parent.channel_handoff=children.find(hasChannelHandoff)?.channel_handoff;
      const remap=id=>hidden.has(id)?parent.id:id;
      const edges=graph.edges.filter(e=>!(e.from_node_id!==e.to_node_id&&ids.has(e.from_node_id)&&ids.has(e.to_node_id))).map(e=>({...e,from_node_id:remap(e.from_node_id),to_node_id:remap(e.to_node_id),composite_source_endpoints:e.composite_source_endpoints||[e.from_node_id,e.to_node_id]}));
      const nodes=graph.nodes.filter(n=>!hidden.has(n.id)).map(n=>n.presentation_event?{...n,presentation_event:{...n.presentation_event,anchor_ids:[...new Set(n.presentation_event.anchor_ids.map(remap))]}}:n);
      const result={...graph,nodes,edges};
      for(const key of ['inline_main_ids','inline_alternative_ids','overview_node_ids','trace_node_ids'])if(graph[key])result[key]=[...new Set(graph[key].map(remap))];
      if(graph.display_focus)result.display_focus={...graph.display_focus,node_id:remap(graph.display_focus.node_id)};
      return result;
    }
    mergeSelection(graph){
      for(const node of graph.nodes.filter(n=>(n.structural_key||n.semantic_key)==='quoted_offer')){node.label='Ціна та умови';node.short_label='Ціна та умови';}
      const members=graph.nodes.filter(n=>['catalog_discovery','configured_line'].includes(n.structural_key||n.semantic_key));
      if(!members.length||new Set(members.map(n=>n.episode_id).filter(id=>id!=null)).size>1)return graph;
      const parent=members.find(n=>n.current)||members.find(n=>n.selection_fields)||members.find(n=>n.requirements)||members.find(n=>n.presentation_kind!=='possible')||members[0];
      const fieldSources=members.filter(n=>n.selection_fields),requirementSources=members.filter(n=>n.requirements);
      graph=Journey.prototype.collapseComposition.call(this,graph,parent,members.filter(n=>n!==parent));
      if(!parent.composite_nodes)parent.composite_nodes=[{...parent}];
      parent.selection_fields=fieldSources.length===1?fieldSources[0].selection_fields:undefined;
      parent.requirements=requirementSources.length===1?requirementSources[0].requirements:undefined;
      const cartSources=members.filter(n=>n.selection_cart);
      parent.selection_cart=cartSources.length===1?cartSources[0].selection_cart:undefined;
      parent.selection_progress=cartView(parent.selection_cart,parent.selection_fields,parent.requirements);
      parent.label='Підбір товару';parent.short_label='Підбір товару';parent.structural_key='catalog_discovery';
      parent.summary='Окремі позиції, кількість речей, параметри та попередня сума.';
      return graph;
    }
    mergePayment(graph){
      const members=graph.nodes.filter(n=>['awaiting_payment','settlement'].includes(n.structural_key||n.semantic_key));
      if(!members.length)return graph;
      // The current scoped journey may contain guide, trace and possible views
      // of the same payment. Keep every source inside one block, never join episodes.
      if(new Set(members.map(n=>n.episode_id).filter(id=>id!=null)).size>1)return graph;
      const parent=members.find(n=>n.current)||members.find(n=>n.presentation_kind!=='possible')||members[0];
      const factual=n=>!['possible','interpretation'].includes(n.presentation_kind)&&n.state==='complete'&&((n.evidence_refs||[]).length||(n.facts||[]).some(f=>f.state==='complete'&&f.source));
      const paid=members.some(n=>(n.structural_key||n.semantic_key)==='settlement'&&factual(n));
      const pending=members.filter(n=>!['possible','interpretation'].includes(n.presentation_kind)
        &&n.waiting?.kind==='manager_review'&&(n.waiting.evidence_refs||[]).some(ref=>ref.kind==='payment_review'&&Number.isInteger(ref.id)&&ref.id>0
          &&(n.facts||[]).some(f=>f.source==='payment_review.current'&&f.state==='open'
            &&(f.evidence_refs||[]).some(source=>source.kind==='payment_review'&&source.id===ref.id))));
      const reviewIds=new Set(pending.flatMap(n=>(n.waiting.evidence_refs||[]).filter(ref=>ref.kind==='payment_review'&&Number.isInteger(ref.id)&&ref.id>0).map(ref=>ref.id)));
      const waiting=reviewIds.size===1?pending[0]?.waiting:null;
      const receiptRefs=(Array.isArray(waiting?.receipt_evidence_refs)?waiting.receipt_evidence_refs:[]).filter(ref=>['message','source_message'].includes(ref.kind)&&Number.isInteger(ref.id)&&ref.id>0);
      const progress={paid,current:members.some(n=>n.current),discussion:members.some(n=>n.presentation_kind==='interpretation'),
        manager_review_pending:Boolean(waiting),review_evidence_refs:waiting?.evidence_refs||[],
        receipt_received:waiting?.receipt_received===true&&receiptRefs.length>0,receipt_evidence_refs:receiptRefs};
      graph=Journey.prototype.collapseComposition.call(this,graph,parent,members.filter(n=>n!==parent));
      if(!parent.composite_nodes)parent.composite_nodes=[{...parent}];
      parent.label='Оплата';parent.short_label='Оплата';parent.structural_key='awaiting_payment';
      parent.payment_progress={...progress,...paymentView(progress,parent.timers||[],this.serverTime)};
      parent.summary=!paid&&waiting?'Очікує перевірки менеджером; зарахування оплати ще не підтверджено.':'Посилання → очікування → оплата. Якщо строк минув без підтвердження — допомога з оплатою нижче.';
      parent.evidence_refs=members.flatMap(n=>n.evidence_refs||[]);
      return graph;
    }

    mergeConsent(graph){
      const consent=graph.nodes.filter(n=>(n.structural_key||n.semantic_key)==='channel_consent');
      const grants=graph.nodes.filter(n=>(n.structural_key||n.semantic_key)==='channel_grant_checked');
      if(consent.length===1&&grants.length===1&&(!consent[0].episode_id||!grants[0].episode_id||consent[0].episode_id===grants[0].episode_id)){
        const parent=consent[0];graph=Journey.prototype.collapseComposition.call(this,graph,parent,[grants[0]]);
        parent.consent_progress=parent.consent_progress||graph.marketing_consent||{};
        parent.label='Маркетинг opt-in';parent.short_label='Маркетинг opt-in';
      }
      for(const node of graph.nodes.filter(n=>n.semantic_key==='client_order_contact'))node.consent_progress=node.consent_progress||{};
      const reminder=graph.nodes.filter(n=>(n.structural_key||n.semantic_key)==='payment_reminder_consent'),notify=graph.nodes.filter(n=>(n.structural_key||n.semantic_key)==='payment_reminder');
      if(reminder.length===1&&notify.length===1){graph=Journey.prototype.collapseComposition.call(this,graph,reminder[0],[notify[0]]);reminder[0].label='Нагадати про оплату';reminder[0].short_label='Нагадування';reminder[0].consent_progress=reminder[0].consent_progress||{purpose:'payment_reminder'};}
      for(const node of graph.nodes.filter(n=>(n.structural_key||n.semantic_key)==='restock_consent')){
        node.consent_progress=node.consent_progress||{purpose:'restock_notification'};node.label='Сповістити про наявність';node.short_label='Наявність · opt-in';
      }
      return graph;
    }
    presentGraph(source,snapshot){
      snapshot={...snapshot,catalogue:this.possibleCatalogue(snapshot.catalogue)};
      // Cycle metadata belongs in the selector, not a disconnected graph circle.
      const actualMode=this.modal&&this.mapMode==='actual';
      // Server aftercare already contains scenario nodes. Filter both layers
      // before composites so possible stages cannot survive inside a fact.
      const nodes=(source.nodes||[]).filter(n=>!n.id?.startsWith('episode:')&&(!actualMode||n.presentation_kind!=='possible')).map(n=>({...n,structural_key:n.structural_key||GUIDE_STRUCTURE[n.id]||n.semantic_key}));
      const ids=new Set(nodes.map(n=>n.id));const edges=(source.edges||[]).filter(e=>ids.has(e.from_node_id)&&ids.has(e.to_node_id)&&(!actualMode||!['route','prerequisite'].includes(e.relation))).map(e=>({...e}));
      if(this.modal&&this.mapMode==='short'&&this.possibleFamily==='after'&&nodes.some(n=>n.post_purchase)){
        const selected=nodes.find(n=>n.id===this.afterPurchaseOrderId&&n.post_purchase)||nodes.find(n=>n.post_purchase?.parent_id===source.display_focus?.node_id)||nodes.find(n=>n.post_purchase);
        const tail=nodes.filter(n=>n.post_purchase?.order_id===selected.post_purchase.order_id),keep=new Set([selected.post_purchase.parent_id,...tail.map(n=>n.id)]);
        return {...source,nodes:nodes.filter(n=>keep.has(n.id)),edges:edges.filter(e=>keep.has(e.from_node_id)&&keep.has(e.to_node_id))};
      }
      if(!this.modal&&snapshot.catalogue&&!snapshot.is_history)return this.inlineGraph(source,nodes,edges,snapshot.catalogue);
      if(!this.modal||!this.showPossible||!snapshot.catalogue)return {...source,nodes,edges};
      const catalogue=snapshot.catalogue,family=this.mapMode==='all'?'all':this.possibleFamily||'inbound';
      const groups=family==='catalog'?['catalog','commerce','payment','objection']:family==='custom'?['custom','dtf','photo','commerce','payment','objection']:family==='after'?['commerce','post_sale','consent','ugc','reward','repeat']:[family];
      const shortChain=family==='after'?['fulfillment',...POST_SALE_TAIL]:(INLINE_CHAINS[family]||INLINE_CHAINS.catalog).filter(key=>!POST_SALE_TAIL.includes(key));
      const shortKeys=this.mapMode==='short'?new Set(shortChain):null;
      const definitions=catalogue.definitions.filter(d=>shortKeys?shortKeys.has(d.key):family==='all'||(family==='inbound'?(d.semantic_kind==='entry'||d.key==='spam_confirmed'):d.key==='inbound'||d.route_keys.some(k=>groups.includes(k))));
      const anchors=new Map();
      definitions.forEach(d=>{
        const deliveries=d.key==='fulfillment'?nodes.filter(n=>n.post_purchase_node_ids?.length):[];if(deliveries.length===1){anchors.set(d.key,deliveries[0].id);return;}
        // Only exact, unique guide or witnessed semantic nodes in this scoped snapshot can
        // serve as an anchor. A conversation topic is NOT a business milestone.
        const actual=nodes.filter(n=>n.structural_key===d.key&&!(d.key==='objection_case'&&n.presentation_kind==='interpretation')&&(n.id.startsWith('guide:')||n.post_purchase||n.producer==='story_context'||n.presentation_kind==='interpretation'||(n.id.startsWith('semantic:')&&n.episode_id===snapshot.viewed_episode_id&&recorded(n))));
        if(actual.length===1){anchors.set(d.key,actual[0].id);return;}
        const id='possible:'+d.key;anchors.set(d.key,id);nodes.push({id,semantic_key:d.key,label:d.label,state:null,current:false,presentation_kind:'possible',implementation_status:d.implementation_status,implementation_note:d.implementation_note,summary:'Сценарій передбачає цей етап. Подій цього клієнта тут не зафіксовано.',facts:[],evidence_refs:[],timers:[]});
      });
      catalogue.transitions.forEach(e=>{if((!shortKeys||family==='after'||shortChain.indexOf(e.target_key)===shortChain.indexOf(e.source_key)+1)&&anchors.has(e.source_key)&&anchors.has(e.target_key)&&!edges.some(existing=>existing.post_purchase_order_id&&existing.from_node_id===anchors.get(e.source_key)&&existing.to_node_id===anchors.get(e.target_key)))edges.push({id:e.id,from_node_id:anchors.get(e.source_key),to_node_id:anchors.get(e.target_key),relation:'route',via_objection:e.via_objection,source_transition_ids:e.source_transition_ids,outcome:e.outcome,evidence_refs:[],tone:'neutral',condition_label:e.condition_label||CONDITIONS[e.outcome]||(e.source_key==='inbound'&&e.target_key==='ad_resolved_product'?'Якщо товар відомий':e.source_key==='inbound'&&e.target_key==='catalog_discovery'?'Якщо потрібен підбір':''),structural_path:[e.source_key,e.target_key]});});
      return {...source,nodes,edges};
    }
    inlineGraph(source,nodes,edges,catalogue){
      if(source.transcript_reconstruction&&source.trace_node_ids?.length){
        // Only cited edges connect this discussion slice; gaps stay gaps.
        const main=[...new Set([...source.trace_node_ids,...nodes.filter(n=>n.route_focus||n.current||n.post_purchase||n.semantic_key==='client_order_context'||['persisted_case_records','website_order_report','channel_contact_report','advertising_attribution','moderation_context','story_context'].includes(n.producer)).map(n=>n.id)])];
        return {...source,nodes,edges,inline_main_ids:sourceFirst(main,nodes),inline_alternative_ids:[],inline_family:'transcript',other_directions:catalogue.transitions.filter(e=>e.source_key==='inbound').length};
      }
      const topic=nodes.find(n=>n.route_focus),kind=topic?.route_kind;
      if(kind&&!TOPIC_STRUCTURE[kind])return {...source,nodes,edges};
      const topicKey=topic&&(topic.route_kind==='collaboration'&&topic.route_subtype?'collaboration_'+topic.route_subtype:TOPIC_STRUCTURE[topic.route_kind]);
      if(topicKey)topic.structural_context_key=topicKey;
      // Accepted discussion intent has priority over an old generic sale episode.
      const family=kind?({custom_print:'custom',dtf:'dtf',support:'support'}[kind]||kind):(nodes.some(n=>['guide:selection','guide:terms','guide:offer','guide:payment','guide:fulfillment'].includes(n.id)&&(n.facts||[]).length)?'catalog':'inbound');
      const chain=[...(INLINE_CHAINS[family]||INLINE_CHAINS.inbound)];
      if(family==='collaboration'&&topic?.route_subtype)chain.splice(2,0,'collaboration_'+topic.route_subtype);
      const extras={catalog:['photo_reference','availability_question','payment_help','objection_case'],custom:['photo_reference','dtf_only','objection_case'],dtf:['custom_print','objection_case'],employment:['collaboration'],collaboration:['collaboration_designer','collaboration_partnership','collaboration_dropship','collaboration_wholesale_store','collaboration_creator','collaboration_other'],information:['collaboration'],support:['information_question'],inbound:['information_question','collaboration','employment','custom_print','dtf_only','post_sale_request']}[family]||[];
      const wanted=new Set([...chain,...extras]),anchors=new Map(),definitions=catalogue.definitions.filter(d=>wanted.has(d.key));
      definitions.forEach(d=>{
        const actual=nodes.filter(n=>n.structural_key===d.key&&n.semantic_key!=='conversation_intent');
        if(actual.length===1){anchors.set(d.key,actual[0].id);return;}
        // A topic can anchor a possible path without becoming a visited business
        // stage: retain its own ID, semantic kind, facts and acceptance status.
        if(topic&&topicKey===d.key){anchors.set(d.key,topic.id);topic.structural_context_key=d.key;return;}
        const id='possible:'+d.key;anchors.set(d.key,id);nodes.push({id,semantic_key:d.key,structural_key:d.key,label:d.label,presentation_kind:'possible',implementation_status:d.implementation_status,implementation_note:d.implementation_note,state:null,current:false,summary:'Можливий шлях. Цей етап не підтверджений подією клієнта.',facts:[],evidence_refs:[],timers:[]});
      });
      catalogue.transitions.forEach(e=>{if(anchors.has(e.source_key)&&anchors.has(e.target_key))edges.push({id:e.id,from_node_id:anchors.get(e.source_key),to_node_id:anchors.get(e.target_key),relation:'route',via_objection:e.via_objection,source_transition_ids:e.source_transition_ids,outcome:e.outcome,evidence_refs:[],tone:'neutral',condition_label:e.condition_label||'',structural_path:[e.source_key,e.target_key]});});
      // An accepted topic and a business fact may coexist at the same semantic
      // location. Keep both identities; only canonical possible neighbors can
      // connect the topic. This is never a topic→business completion edge.
      if(topicKey&&anchors.get(topicKey)!==topic.id)catalogue.transitions.forEach(e=>{
        const from=e.source_key===topicKey?topic.id:anchors.get(e.source_key),to=e.target_key===topicKey?topic.id:anchors.get(e.target_key);
        if((e.source_key===topicKey||e.target_key===topicKey)&&from&&to)edges.push({id:'topic-context:'+e.id,from_node_id:from,to_node_id:to,relation:'route',via_objection:e.via_objection,source_transition_ids:e.source_transition_ids,outcome:e.outcome,evidence_refs:[],tone:'neutral',condition_label:e.condition_label||'',structural_path:[e.source_key,e.target_key]});
      });
      const otherDirections=catalogue.transitions.filter(e=>e.source_key==='inbound'&&e.target_key!=='spam_confirmed'&&!wanted.has(e.target_key)).length;
      return {...source,nodes,edges,inline_main_ids:sourceFirst([...chain.map(k=>anchors.get(k)).filter(Boolean),...nodes.filter(n=>n.semantic_key==='client_order_context'||['persisted_case_records','website_order_report','channel_contact_report','advertising_attribution','moderation_context','story_context'].includes(n.producer)).map(n=>n.id)],nodes),inline_alternative_ids:extras.map(k=>anchors.get(k)).filter(Boolean),inline_family:family,other_directions:otherDirections};
    }
    syncVisibility(width){
      if(!this.graph)return;const eventNodes=this.graph.nodes.filter(eventNode),contextNodes=this.graph.nodes.filter(contextNode);let nodes=this.graph.nodes.filter(n=>!eventNode(n)&&(this.modal||!contextNode(n)));
      const assignment=!this.modal&&this.edges.find(e=>e.relation==='client_scope_assignment'&&contextNodes.some(n=>n.id===e.to_node_id));
      const focus=this.graph.nodes.find(n=>n.id===this.currentId),layoutFocus=eventNode(focus)?focus.presentation_event.anchor_ids[0]:this.currentId;
      this.displayEdges=this.edges;
      if(!this.modal&&this.graph.inline_main_ids){
        const overview=window.TwcJourneyGeometry.overview({nodes,edges:this.edges,mainIds:assignment?[...new Set([...this.graph.inline_main_ids,layoutFocus,assignment.from_node_id])]:this.graph.inline_main_ids,alternativeIds:this.graph.inline_alternative_ids,currentId:layoutFocus,priorityIds:[...(assignment?[assignment.from_node_id]:[]),...nodes.filter(n=>['website_order_report','channel_contact_report','advertising_attribution','moderation_context','story_context'].includes(n.producer)).map(n=>n.id)],width});
        nodes=overview.nodes;this.inlineSlots=overview.slots;this.displayEdges=overview.edges;
      }else if(!this.modal){
        this.inlineSlots=null;
        const current=this.currentId||this.graph.overview_node_ids?.find(id=>this.nodeIds.includes(id))||nodes[0]?.id;
        const direct=this.edges.filter(e=>witnessed(e)&&(e.from_node_id===current||e.to_node_id===current));
        if(width<560){const before=direct.find(e=>e.to_node_id===current),after=direct.find(e=>e.from_node_id===current);const ids=width<330?[current]:[before?.from_node_id,current,after?.to_node_id].filter(Boolean);nodes=nodes.filter(n=>ids.includes(n.id));}
        else{const cap=Math.max(3,Math.min(8,Math.floor(width/72)));const priorityNodes=nodes.filter(n=>n.waiting?.evidence_refs?.length||(n.semantic_key==='objection_case'&&n.state==='partial')).map(n=>n.id);const priorities=[current,...priorityNodes,...direct.flatMap(e=>[e.from_node_id,e.to_node_id]),...(this.graph.overview_node_ids||[])];const ids=[...new Set(priorities.filter(Boolean))].slice(0,cap);nodes=nodes.filter(n=>ids.includes(n.id));const lanes=[...new Set(nodes.map(n=>n.layout?.lane||0))].sort((a,b)=>a-b);if(lanes.length>3){const activeLane=nodes.find(n=>n.id===current)?.layout?.lane||0;const allowed=[activeLane,...lanes.filter(l=>l!==activeLane)].slice(0,3);nodes=nodes.filter(n=>allowed.includes(n.layout?.lane||0));}}
      }
      // v6 · Доставка стоїть у тому ж ряду, що й шлях, — у кінці, як завершення покупки, а не окремим
      // «другим рядом». Зв'язок зі зверненням малюється лише як чесна ручна прив'язка (пунктир),
      // бо сервер прямо каже: ця прив'язка не є переходом у розмові (episode_binding: absent).
      if(!this.modal&&contextNodes.length){
        const parent=contextNodes.find(n=>n.id===assignment?.to_node_id)||contextNodes.find(n=>n.semantic_key==='client_order_context');
        if(parent){
          if(!this.inlineSlots)this.inlineSlots=new Map(nodes.map((n,col)=>[n.id,{col,row:0}]));
          nodes=nodes.filter(n=>n.id!==parent.id&&this.inlineSlots.get(n.id)?.row===0);this.inlineSlots=new Map([...this.inlineSlots].filter(([id])=>nodes.some(n=>n.id===id)));
          const cap=Math.max(3,Math.min(7,Math.floor(width/80)));
          // Якщо ряд уже повний — стискаємо найменш важливі можливі етапи, щоб доставка влізла.
          while(nodes.length>=cap){const drop=[...nodes].reverse().find(n=>n.presentation_kind==='possible'&&n.id!==this.currentId);if(!drop)break;nodes=nodes.filter(n=>n.id!==drop.id);this.inlineSlots.delete(drop.id);}
          [...nodes].sort((a,b)=>this.inlineSlots.get(a.id).col-this.inlineSlots.get(b.id).col).forEach((n,col)=>this.inlineSlots.set(n.id,{col,row:0}));
          nodes.push(parent);this.inlineSlots.set(parent.id,{col:nodes.length-1,row:0});
          const contact=contextNodes.find(n=>n.semantic_key==='client_order_contact'&&n.contextual_binding?.order_id===parent.contextual_binding?.order_id);
          if(contact&&!nodes.some(n=>n.id===contact.id)){nodes.push(contact);this.inlineSlots.set(contact.id,{col:nodes.length-1,row:0});}

        }
      }
      if(!this.modal&&this.inlineSlots&&width<560){
        const columns=Math.max(3,Math.floor(width/86));
        nodes.forEach((n,i)=>this.inlineSlots.set(n.id,{col:i%columns,row:Math.floor(i/columns)}));
      }
      this.visibleIds=nodes.map(n=>n.id);
      this.eventVisibleIds=eventNodes.filter(n=>!n.presentation_event.anchor_ids.length||n.presentation_event.anchor_ids.some(id=>this.visibleIds.includes(id))).map(n=>n.id);
      this.visibleIds.push(...this.eventVisibleIds);
      const displayed=new Set(this.displayEdges.map(e=>e.id));this.displayEdges=[...this.displayEdges,...this.edges.filter(e=>!displayed.has(e.id)&&this.visibleIds.includes(e.from_node_id)&&this.visibleIds.includes(e.to_node_id)&&(contextual(e)||eventNode(this.graph.nodes.find(n=>n.id===e.from_node_id))||eventNode(this.graph.nodes.find(n=>n.id===e.to_node_id))))];
      this.contextKey.hidden=!nodes.some(contextNode);this.contextKey.textContent=assignment?.condition_label||'Пов’язане замовлення';
      this.cells.forEach((c,id)=>{c.hidden=!this.visibleIds.includes(id);});
      if(this.selected&&!this.visibleIds.includes(this.selected)&&!this.objectionButtons.has(this.selected))this.closePanel(false);
      const omitted=this.nodeIds.length-this.visibleIds.length;const attention=nodes.length<this.graph.nodes.length?this.graph.nodes.filter(n=>!this.visibleIds.includes(n.id)&&(n.waiting?.evidence_refs?.length||(n.semantic_key==='objection_case'&&n.state==='partial'))):[];const traceStale=this.graph.transcript_reconstruction?.freshness==='new_messages';this.note.hidden=!attention.length&&!traceStale;this.note.textContent=traceStale?'Є нові повідомлення; шлях потребує оновлення':attention.length?'Поза оглядом: '+attention.map(n=>n.waiting?.label||n.label).slice(0,2).join(' · ')+(attention.length>2?' · ще '+(attention.length-2):''):'';this.expand.textContent=omitted?'Карта · '+this.nodeIds.length+' ↗':'Карта ↗';this.expand.title=omitted?'Ще '+omitted+' етапів у повній карті':'Відкрити повну карту';
      this.expand.disabled=!this.nodeIds.length;
      this.inlineKey.hidden=Boolean(this.modal)||!this.graph.inline_main_ids;
      this.directions.hidden=!this.graph.inline_main_ids;this.directions.textContent='Інші напрями ↗';
    }
    toggleNode(id){if(this.selected===id&&!this.selectedEdge){this.closePanel(false);return;}this.selected=id;this.selectedEdge=null;this.panelKey='';this.renderPanel();this.buttons.forEach((b,key)=>b.setAttribute('aria-expanded',String(key===id)));this.objectionButtons.forEach((b,key)=>b.setAttribute('aria-expanded',String(key===id)));this.queueLayout();}
    selectEdge(id){const e=this.edges.find(e=>e.id===id);if(!e)return;if(this.selectedEdge===id){this.closePanel(false);return;}this.selected=e.from_node_id;this.selectedEdge=id;this.panelKey='';this.renderPanel();this.queueLayout();}
    parallelEdges(edge){return this.edges.filter(item=>edgePair(item)===edgePair(edge)&&(witnessed(item)||interpreted(item))).sort((a,b)=>edgePriority(b)-edgePriority(a)||(b.last_step_index??-1)-(a.last_step_index??-1));}
    edgeControl(id){const edge=this.edges.find(item=>item.id===id);return this.edgeButtons.get(id)||this.edgeButtons.get('possible-concern:'+id)||(edge&&this.parallelEdges(edge).map(item=>this.edgeButtons.get(item.id)).find(Boolean))||(!this.returnReason.hidden&&this.returnReason.dataset.edgeId===id?this.returnReason:null);}
    closePanel(returnFocus){const target=this.selectedEdge?(this.edgeControl(this.selectedEdge)||this.buttons.get(this.selected)):(this.concernControls?.get(this.selected)||this.objectionButtons.get(this.selected)||this.detailButtons.get(this.selected)||this.buttons.get(this.selected));this.selected=null;this.selectedEdge=null;this.panelKey='';this.panel?.remove();this.panel=null;this.buttons.forEach(b=>b.setAttribute('aria-expanded','false'));this.edgeButtons.forEach(b=>b.setAttribute('aria-expanded','false'));this.detailButtons.forEach(b=>b.setAttribute('aria-expanded','false'));this.objectionButtons.forEach(b=>b.setAttribute('aria-expanded','false'));this.queueLayout();if(returnFocus)target?.focus({preventScroll:true});}
    renderPanel(){
      const node=this.graph.nodes.find(item=>item.id===this.selected);if(!node)return;
      const edge=this.selectedEdge?this.edges.find(item=>item.id===this.selectedEdge):null;
      const branch=null;
      const observed=edge&&witnessed(edge),reconstructed=edge&&interpreted(edge);
      const assignment=!edge&&contextNode(node)?this.edges.find(e=>e.relation==='client_scope_assignment'&&e.to_node_id===node.id):null;
      const data=edge?{...node,label:node.label+(contextual(edge)?' — ':' → ')+(this.graph.nodes.find(n=>n.id===edge.to_node_id)?.label||''),summary:(reconstructed?'За перепискою · ':'')+(edge.summary||edge.reason_label||(contextual(edge)?(edge.condition_label||'Контекст замовлення')+' · '+(edge.summary||'') :observed?'Зафіксований зв’язок':(edge.condition_label?edge.condition_label+' · ':'')+'Можливий зв’язок; не підтверджує перехід')),facts:edge.facts||[],evidence_refs:edge.evidence_refs||[]}:assignment?{...node,summary:assignment.condition_label+' · '+(node.summary||'Контекст замовлення клієнта')}:node.consent_progress?{...node,summary:node.consent_progress.note||({payment_reminder:'Окрема згода на нагадування про цю оплату в узгоджений час.',restock_notification:'Окрема згода на сповіщення про конкретний товар, розмір і колір.'}[node.consent_progress.purpose]||'Запит згоди після оплати · маркетинг після отримання замовлення.')}:planned(node)?{...node,summary:(node.presentation_kind==='interpretation'?'За перепискою · '+(node.summary?node.summary+' · ':''):'')+'Заплановано'+(node.implementation_note?' · '+node.implementation_note:'')}:node;
      const events=((this.graph.history||this.snapshot.history||{}).events||[]).filter(item=>edge?(edge.event_ids||[]).includes(item.id):(node.composite_nodes||[node]).some(n=>n.id===item.node_id));
      const returns=!this.modal&&edge&&returnEdge(edge)?this.edges.filter(item=>returnEdge(item)&&(witnessed(item)||interpreted(item))&&this.positions?.has(item.from_node_id)&&this.positions.has(item.to_node_id)):[];
      const choices=returns.length?returns:edge&&edgePriority(edge)>0?this.parallelEdges(edge):[];
      const key=JSON.stringify([this.snapshot.viewed_episode_id,data,events,this.selectedEdge,choices]);if(this.panelKey===key)return;this.panelKey=key;
      const oldStories=[...this.panel?.querySelectorAll('.twc-journey-story-card[open]')||[]].map(n=>n.dataset.storyId);
      const oldOpen=this.panel?.querySelector('details')?.open||false,oldScroll=this.panel?.querySelector('.twc-journey-panel-body')?.scrollTop||0;
      const hadCloseFocus=this.panel?.querySelector('.twc-journey-close')===document.activeElement;
      const hadSummaryFocus=this.panel?.querySelector('summary')===document.activeElement;
      if(this.panel)this.panel.remove();this.panel=el('section','twc-journey-panel');this.panel.id='twc-journey-panel-'+this.snapshot.client_id;this.panel.setAttribute('aria-label',data.label+' — подробиці');
      this.buttons.forEach(button=>button.removeAttribute('aria-controls'));this.buttons.get(this.selected)?.setAttribute('aria-controls',this.panel.id);
      const head=el('div','twc-journey-panel-head'),copy=el('div');copy.append(el('h4','',data.label+(branch?' · '+branch.label:'')));if(data.summary)copy.append(el('p','twc-journey-panel-summary',data.summary));const close=el('button','twc-journey-close','×');close.type='button';close.setAttribute('aria-label','Закрити підетапи');close.addEventListener('click',()=>this.closePanel(true));head.append(copy,close);this.panel.append(head);
      const body=el('div','twc-journey-panel-body'),facts=el('div','twc-journey-facts');
      const outcome=!edge&&caseOutcome(node);if(outcome)body.append(el('p','twc-journey-case-outcome','За перепискою · '+outcome.label));
      if(choices.length>1){
        const pickerLabel=returns.length?'Повернення з причиною':'Переходи та причини · '+choices.reduce((sum,item)=>sum+edgeCount(item),0);
        const label=el('label','twc-journey-return-picker',pickerLabel),picker=el('select');picker.setAttribute('aria-label',pickerLabel);
        choices.forEach(item=>{const from=this.graph.nodes.find(n=>n.id===item.from_node_id),to=this.graph.nodes.find(n=>n.id===item.to_node_id);picker.append(new Option((from?.label||'')+' → '+(to?.label||'')+' · '+(interpreted(item)?'За перепискою · ':'Збережений перехід · ')+(item.summary||item.reason_label||'Уточнення')+(edgeCount(item)>1?' ×'+edgeCount(item):''),item.id));});picker.value=edge.id;
        picker.addEventListener('change',()=>{this.selectEdge(picker.value);this.panel?.querySelector('.twc-journey-return-picker select')?.focus({preventScroll:true});});label.append(picker);body.append(label);
      }
      if(!edge&&node.semantic_key==='journey_case'){
        const anchorId=node.presentation_event.anchor_ids[0],siblings=this.graph.nodes.filter(n=>n.semantic_key==='journey_case'&&n.presentation_event.anchor_ids.includes(anchorId));
        const at=this.graph.nodes.find(n=>n.id===anchorId);if(at)body.append(el('p','twc-journey-concern-location','На етапі «'+at.label+'»'));
        if(node.topic==='manager')body.append(el('p','twc-journey-fact-note','У переписці є запит або обіцянка уточнення. Доставку сповіщення менеджеру потрібно підтверджувати окремо.'));
        if(node.owner==='carrier')body.append(el('p','twc-journey-fact-note','Питання до перевізника · зі слів клієнта. Це не підтверджений збій магазину.'));
        if(siblings.length>1){const label=el('label','twc-journey-return-picker','Уточнення на цьому етапі'),picker=el('select');picker.setAttribute('aria-label','Уточнення на цьому етапі');siblings.forEach(n=>picker.append(new Option(n.label+' · '+caseOutcome(n).label,n.id)));picker.value=node.id;picker.addEventListener('change',()=>this.toggleNode(picker.value));label.append(picker);body.append(label);}
      }
      if(!edge&&eventNode(node))this.appendEventDetails(body,node);
      if(reconstructed||(!edge&&node.transcript_interpretation&&!eventNode(node))){const note=el('div','twc-journey-trace-note');note.append(el('p','','За перепискою. Підтвердження оплати й дозволів перевіряються окремо.'));if(!edge)this.appendSources(note,node.transcript_interpretation.evidence_refs||[],64);note.append(el('p','','Частина текстової переписки; давніші повідомлення й медіа можуть бути відсутні.'));body.append(note);}
      if(!edge&&recorded(node)){const visits=node.recorded_visits;const heading=(visits.has_backfilled?'Є відновлені записи подій':'Є збережені події')+' · '+(visits.history_truncated?'≥':'')+visits.count;body.append(el('p','twc-journey-visit-note',heading));if(visits.last_at)body.append(el('p','twc-journey-fact-note','Остання подія: '+date(visits.last_at)));}
      if(!edge&&['website_order_report','channel_contact_report'].includes(node.producer))this.appendSources(body,node.evidence_refs||[]);
      if(edge?.via_objection)body.append(el('p','twc-journey-report-note','Можлива робота із запереченням між етапами. Це умова маршруту, а не подія клієнта.'));
      if(!edge&&node.semantic_key==='advertising_entry')this.appendAdEntry(body,node);
      if(!edge&&node.semantic_key==='inbound'&&!node.ad_entry)body.append(el('p','twc-journey-fact-note','Джерело входу не передано. Це не підтверджує, що звернення було без реклами.'));
      if(!edge&&node.moderation_view)this.appendModeration(body,node);
      if(!edge&&node.post_purchase){const hint=el('section','twc-journey-aftercare-note');hint.dataset.readiness=node.post_purchase.readiness;hint.append(el('strong','',node.post_purchase.label),el('p','',node.post_purchase.note),el('small','','Замовлення №'+node.post_purchase.order_id+' · це умова маршруту, не виконана дія'));this.appendSources(hint,node.post_purchase.evidence_refs);body.append(hint);}
      if(!edge&&node.semantic_key==='story_interactions')this.appendStories(body,node);
      if(!edge){if(node.selection_progress)this.appendSelection(body,node);else this.appendRequirements(body,node);}
      if(!edge)this.appendDelivery(body,node);
      if(!edge&&node.fulfillment_progress?.completion_unverified)body.append(el('p','twc-journey-report-note','Замовлення завершено у системі, але отримання перевізником не підтверджено. Позначку «Отримано» не домислюємо.'));
      if(!edge&&node.fulfillment_progress?.carrier_pending_while_shipped)body.append(el('p','twc-journey-report-note','Замовлення позначено відправленим. Останній запис перевізника — створена ТТН; приймання посилки ще не підтверджено.'));
      if(!edge&&node.consent_progress)this.appendConsent(body,node);
      if(!edge&&node.payment_progress)this.appendPayment(body,node);
      if(!edge&&this.modal){const options=this.edges.filter(e=>e.from_node_id===node.id&&e.relation==='route');if(options.length){const section=el('div','twc-journey-options');section.append(el('h5','','Можливі продовження'));options.forEach(e=>{const target=this.graph.nodes.find(n=>n.id===e.to_node_id),part=node.payment_progress&&node.composite_nodes.find(n=>n.id===e.composite_source_endpoints?.[0]),button=el('button','', (part?part.label+' · ':'')+(e.condition_label?e.condition_label+' → ':'→ ')+(target?.label||''));button.type='button';button.addEventListener('click',()=>this.selectEdge(e.id));section.append(button);});body.append(section);}}
      (data.facts||[]).filter(fact=>!(!edge&&node.semantic_key==='advertising_entry')).filter(fact=>!branch||branch.facts.includes(fact.id)).forEach(fact=>{const row=el('div','twc-journey-fact');row.dataset.factId=fact.id;row.dataset.tone=['success','warning'].includes(fact.tone)?fact.tone:'neutral';row.dataset.state=STATES.has(fact.state)?fact.state:'partial';const label=el('div','twc-journey-fact-label');label.append(el('i','twc-journey-fact-mark'),el('span','',fact.label));const value=el('div','twc-journey-fact-value',fact.format==='datetime'?(date(fact.value)||'Час не зафіксовано'):valueText(fact.value));if(fact.captured_at)value.append(el('div','twc-journey-fact-note',date(fact.captured_at)));row.append(label,value);facts.append(row);});
      if(!facts.children.length&&!eventNode(node)&&!node.selection_fields?.evidence_refs?.length&&!node.payment_progress&&!node.consent_progress&&!node.ad_entry&&!node.selection_cart&&!node.moderation_view&&node.semantic_key!=='story_interactions')facts.append(el('p','twc-journey-empty','Підтверджених даних цього етапу ще немає.'));body.append(facts);if(!edge)this.appendTimerDetails(body,node);
      const details=el('details');details.open=oldOpen;details.append(el('summary','','Історія та джерела'));const history=el('ol','twc-journey-history');
      events.forEach(event=>{const item=el('li'),copy=el('div','',event.label||'Подія');const when=el('time','',date(event.occurred_at||event.recorded_at));if(event.occurred_at)when.dateTime=event.occurred_at;copy.append(when);this.appendSources(copy,event.evidence_refs||[]);item.append(copy);history.append(item);});
      if(!events.length)history.append(el('li','','Окремих подій для цього етапу ще не зафіксовано.'));details.append(history);
      const refs=data.evidence_refs||[];if(refs.length){const sources=el('div');this.appendSources(sources,refs,reconstructed?64:8);details.append(sources);}
      if((this.snapshot.history||{}).has_more)details.append(el('p','twc-journey-coverage','Показано останні події. Повна історія зберігається у джерелах.'));body.append(details);this.panel.append(body);(this.modal||this.root).append(this.panel);for(const card of body.querySelectorAll('.twc-journey-story-card'))if(oldStories.includes(card.dataset.storyId))card.open=true;body.scrollTop=oldScroll;
      if(hadCloseFocus)close.focus({preventScroll:true});if(hadSummaryFocus)details.querySelector('summary').focus({preventScroll:true});
    }
    appendEventDetails(body,node){
      const section=el('div','twc-journey-event-details'),events=this.edges.filter(e=>node.presentation_event.edge_ids.includes(e.id)).sort((a,b)=>(a.last_step_index??0)-(b.last_step_index??0));
      if(events.some(interpreted))section.append(el('p','twc-journey-fact-note','За перепискою'));
      if(node.presentation_event.mode!=='detail'&&(node.presentation_event.unplaced||node.presentation_event.mode==='unplaced'))section.append(el('p','twc-journey-fact-note','Зв’язок із конкретним етапом не визначено.'));
      for(const event of events){
        const item=el('div','twc-journey-event-detail');item.dataset.tone=edgeTone(event);
        item.append(el('b','',event.reason_label||'Уточнення'),el('p','',event.summary||'Подробиці — у повідомленні'+(edgeCount(event)>1?' · ×'+edgeCount(event):'')));
        const times=[...new Set((event.evidence_refs||[]).map(ref=>date(ref.message_at)).filter(Boolean))];
        item.append(el('p','twc-journey-fact-note','Коли: '+(times.join(' · ')||'час повідомлення не зафіксовано')));
        if(edgeCount(event)>1)item.append(el('span','twc-journey-fact-note','Повторень: '+edgeCount(event)+' · '));
        this.appendSources(item,event.evidence_refs||[],64);section.append(item);
      }
      if(!events.length){const refs=[...(node.evidence_refs||[]),...(node.trace_details||[]).flatMap(item=>item.evidence_refs||[])],times=[...new Set(refs.map(ref=>date(ref.message_at)).filter(Boolean))];section.append(el('p','twc-journey-fact-note','Коли: '+(times.join(' · ')||'час події не зафіксовано')));}
      body.append(section);
    }
    appendSources(root,refs,limit=8){
      const seen=new Set(),unique=(refs||[]).filter(ref=>{const key=ref.kind+':'+ref.id;if(!ref.id||seen.has(key))return false;seen.add(key);return true;});
      const render=(parent,ref)=>{const labels={message:'Повідомлення',source_message:'Повідомлення',order:'Замовлення',funnel_event:'Подія',episode_event:'Подія',episode:'Покупка',payment_projection:'Оплата',payment_review:'Перевірка оплати',prize_case:'Призовий випадок',post_sale_case:'Сервісний випадок'};const label=(labels[ref.kind]||'Джерело')+' №'+ref.id;const actionable=this.options.onEvidence&&['message','source_message'].includes(ref.kind);const link=el(actionable?'button':'span','twc-journey-source',label);if(actionable){link.type='button';link.addEventListener('click',()=>{if(this.modal)this.closeMap();this.closePanel(false);this.options.onEvidence(ref,link);});}parent.append(link);};
      unique.slice(0,limit).forEach(ref=>render(root,ref));
      if(unique.length>limit){const more=el('details','twc-journey-source-overflow');more.append(el('summary','','Ще '+(unique.length-limit)+' джерел'));unique.slice(limit).forEach(ref=>render(more,ref));root.append(more);}
    }
    // v6 · Панель вузла доставки: чотири кроки вертикальною стрічкою (як трекінг посилки),
    // ТТН, дата отримання та дозвіл на подальший контакт — усе з фактів замовлення.


    appendConsent(body,node){
      const view=consentView(node.consent_progress),section=el('div','twc-journey-consent-steps');
      const caption=el('p','twc-journey-consent-state',view.label);caption.dataset.tone=view.tone;section.append(caption);
      for(const part of view.parts){const row=el('div','twc-journey-consent-row');row.dataset.state=part.state;row.append(el('span','twc-journey-payment-dot'),el('strong','',part.label),el('span','twc-journey-consent-note',part.note));const sources=el('div','twc-journey-consent-sources');this.appendSources(sources,part.evidence_refs||[]);row.append(sources);section.append(row);}
      section.append(el('p','twc-journey-fact-note',node.consent_progress.note||'Потрібні нативні події запрошення, відповіді та чинного дозволу для цього каналу й мети.'));
      section.append(el('p','twc-journey-fact-note',({payment_reminder:'Лише нагадування про оплату. Не підписка на акції.',restock_notification:'Лише наявність цього варіанта. Не підписка на акції.'}[node.consent_progress.purpose]||'Акції та післяпродажні пропозиції — після отримання. Чинність каналу перевіряється перед відправкою.')));
      for(const child of node.composite_nodes||[]){
        const details=el('details','twc-journey-delivery-source');details.append(el('summary','',child.label));
        for(const fact of child.facts||[])details.append(el('p','twc-journey-fact-note',fact.label+': '+valueText(fact.value)));
        this.appendSources(details,child.evidence_refs||[]);this.appendTimerDetails(details,child);section.append(details);
      }
      for(const edge of node.composite_edges||[])this.appendSources(section,edge.evidence_refs||[]);
      body.append(section);
    }
    appendPayment(body,node){
      const section=el('section','twc-journey-payment');section.append(el('h5','','Оплата · '+node.payment_progress.label));
      for(const item of node.payment_progress.items){
        const step=el('details','twc-journey-payment-step');step.dataset.state=item.state;
        const head=el('summary');head.append(el('span','twc-journey-payment-dot'),el('strong','',item.label),el('span','',({done:'Підтверджено',next:'Зараз',cancelled:'Потребує уваги',discussion:'За перепискою',todo:'Ще попереду'})[item.state]));step.append(head,el('p','',item.note));this.appendSources(step,item.evidence_refs||[]);section.append(step);
      }
      for(const child of node.composite_nodes||[]){
        const details=el('details','twc-journey-payment-step');details.append(el('summary','',child.label+' · джерела'));
        if(child.summary)details.append(el('p','',child.summary));
        for(const fact of child.facts||[])details.append(el('p','',fact.label+': '+valueText(fact.value)));
        this.appendRequirements(details,child);this.appendTimerDetails(details,child);this.appendSources(details,child.evidence_refs||[]);section.append(details);
      }
      for(const edge of node.composite_edges||[]){section.append(el('p','twc-journey-fact-note',(edge.relation==='route'?'Можливий внутрішній перехід: ':'Внутрішній перехід: ')+(edge.condition_label||edge.reason_label||'наступний крок')));this.appendSources(section,edge.evidence_refs||[]);}
      body.append(section);
    }
    appendDelivery(body,node){
      const p=node.delivery_progress;if(!p)return;
      const section=el('section','twc-journey-delivery');section.append(el('h5','','Доставка · '+p.label));
      const track=el('ol','twc-journey-delivery-track');
      p.stages.forEach((label,i)=>{const item=el('li','',label);item.dataset.state=p.cancelled?'cancelled':p.planned?'todo':i<p.step?'done':i===p.step?'next':'todo';track.append(item);});
      section.append(track);
      const facts=[...(node.facts||[]),...(node.delivery_facts||[])];
      const ttn=facts.find(f=>f.id?.endsWith(':tracking'));if(ttn)section.append(el('p','twc-journey-fact-note','ТТН: '+valueText(ttn.value)));
      const got=facts.find(f=>f.id?.endsWith(':delivery'));if(got)section.append(el('p','twc-journey-fact-note','Отримання: '+valueText(got.value)+(got.captured_at?' · '+date(got.captured_at):'')));
      if(node.contact_permission){const c=el('p','twc-journey-delivery-contact');c.dataset.state=node.contact_permission.status==='confirmed'?'confirmed':'pending';c.textContent=node.contact_permission.status==='confirmed'?'🔔 Дозвіл на повідомлення після покупки підтверджено':'🔔 Дозволу на повідомлення після покупки ще немає — потрібне окреме підтвердження каналу й мети контакту';section.append(c);}
      for(const child of node.composite_nodes||[]){
        if(child.id===node.id)continue;
        const details=el('details','twc-journey-delivery-source');details.append(el('summary','',child.label));
        for(const fact of child.facts||[])details.append(el('p','twc-journey-fact-note',fact.label+': '+valueText(fact.value)));
        this.appendSources(details,child.evidence_refs||[]);section.append(details);
      }
      for(const edge of node.composite_edges||[])this.appendSources(section,edge.evidence_refs||[]);
      body.append(section);
    }
    appendAdEntry(body,node){
      const data=node.ad_entry||{},section=el('section','twc-journey-ad-card');
      section.append(el('span','twc-journey-ad-eyebrow',node.ad_entry?'ДЖЕРЕЛО ЗВЕРНЕННЯ':node.presentation_kind==='interpretation'?'РЕКЛАМА У ПЕРЕПИСЦІ':'МОЖЛИВИЙ РЕКЛАМНИЙ ВХІД'),el('h5','',data.title||'Реклама Instagram'));
      const grid=el('div','twc-journey-field-grid');
      for(const [label,value]of [['Оголошення',data.ad_id?'Ad ID · '+data.ad_id:data.ref||'Ідентифікатор не передано'],['Предмет',data.product_title||data.theme||'Потрібно уточнити'],['Прив’язка',data.product_id?'Однозначний товар каталогу':data.theme?'Тема без конкретного товару':data.resolution==='ambiguous'?'Кілька прив’язок — не вгадуємо':'Товар не визначено'],['Намір клієнта','Визначається з діалогу']]){
        const tile=el('div','twc-journey-field');tile.append(el('span','twc-journey-field-label',label),el('strong','',value));grid.append(tile);
      }
      section.append(grid,el('p','twc-journey-fact-note',data.product_id?'На «яка ціна?» є контекст саме цього товару. Точна сума залежить від комплектації; рекламний вхід не підтверджує намір купити.':'Якщо товар не визначено, спочатку уточнюємо предмет запиту. Загальний заголовок реклами не є достатнім джерелом ціни.'));
      if(data.scope==='client')section.append(el('p','twc-journey-fact-note','Збережений рекламний контекст картки; конкретне повідомлення входу не прив’язане.'));
      const detail=el('details');detail.append(el('summary','','Дані джерела'));
      for(const fact of node.facts||[]){detail.append(el('p','',fact.label+': '+valueText(fact.value)));this.appendSources(detail,fact.evidence_refs||[]);}section.append(detail);
      this.appendSources(section,data.evidence_refs||[]);body.append(section);
    }
    appendSelection(body,node){
      const section=el('section','twc-journey-selection-fields'),fields=node.selection_fields;
      if(node.selection_cart){this.appendCart(section,node.selection_cart);const sources=el('details');sources.append(el('summary','','Початкові етапи та джерела'));for(const child of node.composite_nodes||[]){sources.append(el('h5','',child.label));for(const fact of child.facts||[])sources.append(el('p','',fact.label+': '+valueText(fact.value)));this.appendRequirements(sources,child);this.appendSources(sources,child.evidence_refs||[]);}for(const edge of node.composite_edges||[])this.appendSources(sources,edge.evidence_refs||[]);section.append(sources);body.append(section);return;}
      section.append(el('h5','','Параметри товару · '+node.selection_progress.label));
      const source=fields?.items||node.requirements?.items||node.selection_progress.parts.map(p=>({label:p.label,status:'open',required:true}));
      const grid=el('div','twc-journey-field-grid');
      for(const item of source){
        const tile=el('div','twc-journey-field');tile.dataset.state=item.status;tile.dataset.required=String(item.required);
        tile.append(el('span','twc-journey-field-label',item.label),el('strong','',item.value||(item.status==='complete'?'Визначено':item.required===false?'Не потребує вибору':'Ще уточнюємо')));
        if(item.note)tile.append(el('small','',item.note));grid.append(tile);
      }
      section.append(grid,el('p','twc-journey-fact-note',fields?.note||(fields?'Параметри поточної комплектації; дозвіл на оплату перевіряється окремо.':'Точний прогрес з’явиться після підтвердження поточної комплектації. Дані лише з переписки не видаються за готовність до оплати.')));
      if(fields?.scope?.line_count>1)section.append(el('p','twc-journey-fact-note','Позиція '+fields.scope.active_position+' із '+fields.scope.line_count));
      this.appendShipping(section,null);
      this.appendSources(section,fields?.evidence_refs||node.requirements?.evidence_refs||[]);
      for(const child of node.composite_nodes||[]){const detail=el('details');detail.append(el('summary','',child.label+' · джерела'));for(const fact of child.facts||[])detail.append(el('p','',fact.label+': '+valueText(fact.value)));this.appendRequirements(detail,child);this.appendSources(detail,child.evidence_refs||[]);section.append(detail);}
      for(const edge of node.composite_edges||[])this.appendSources(section,edge.evidence_refs||[]);
      body.append(section);
    }
    money(value){return Number.isFinite(Number(value))?new Intl.NumberFormat('uk-UA',{maximumFractionDigits:2}).format(Number(value)):String(value);}
    renderOrderOutcomes(){
      if(!this.orderOutcomes)return;
      this.orderOutcomes.replaceChildren();
      const orders=this.graph.nodes.filter(n=>n.fulfillment_progress?.evidence_refs?.length&&n.presentation_kind!=='possible');
      this.orderOutcomes.hidden=!orders.length;
      for(const n of orders){const p=n.fulfillment_progress,number=(n.facts||[]).find(f=>f.id?.endsWith(':number'))?.value;
        const label=p.completion_unverified?'Отримання не звірено':p.cancelled?'Скасовано':['','Оформлено','Відправлено','На відділенні','Отримано'][p.step];
        const button=el('button','',label+(number?' · '+number:''));button.type='button';button.dataset.tone=p.completion_unverified?'warning':p.step===4?'success':p.cancelled?'danger':'recorded';
        button.title=(n.semantic_key==='client_order_context'?'Пов’язане замовлення клієнта; прив’язку до поточної покупки не підтверджено. ':'Замовлення поточної покупки. ')+(p.completion_unverified?'Завершено у системі без підтвердження перевізника.':p.carrier_pending_while_shipped?'Позначено відправленим; останній статус перевізника — створена ТТН.':'Відкрити етапи та джерела.');button.setAttribute('aria-label',button.textContent+'. '+button.title);
        button.addEventListener('click',()=>{if(!this.modal)this.openMap();this.toggleNode(n.id);});this.orderOutcomes.append(button);
        if(n.post_purchase_node_ids?.length){const after=el('button','twc-journey-aftercare-link','Після покупки ↗');after.type='button';after.title='Сервіс, контакт, UGC, нагорода та повторна покупка · '+(number||'замовлення');after.addEventListener('click',()=>{this.afterPurchaseOrderId=n.post_purchase_node_ids[0];if(!this.modal)this.openMap('after');else{this.possibleFamily='after';this.setMapMode('short');}});this.orderOutcomes.append(after);}
      }
    }
    appendStories(body,node){
      const data=node.story_interactions,items=data?.items||[];
      const section=el('section','twc-journey-stories');
      const intro=el('div','twc-journey-story-intro');intro.append(el('strong','',items.length?(data.truncated?'≥ ':'')+data.count+' взаємодій · '+data.reply_count+' з відповіддю':'Відмітка · репост · відповідь на сторис'),el('p','','Купівля в Instagram, Telegram чи на сайті не є умовою для відмітки. Канал покупки звіряється окремо.'));section.append(intro);
      const stages=['Отримано','Медіа','Аналіз','Відповідь'];
      if(!items.length){const list=el('ol','twc-journey-story-plan');['Отримати відмітку, репост або відповідь на сторис','Перевірити доступність фото / відео','Зрозуміти сюжет, текст і посил; перевірити відмітку','Відповісти за змістом: подякувати, підтримати або уточнити'].forEach(t=>list.append(el('li','',t)));section.append(list,el('p','twc-journey-fact-note','Можливий сценарій. Нові сторис повторюють цей цикл; перевірка права на UGC-нагороду — окрема гілка.'));}
      items.forEach((item,index)=>{const card=el('details','twc-journey-story-card');card.dataset.storyId=item.id;card.open=items.length===1;const summary=el('summary');const title=el('span');title.append(el('strong','',item.label),el('small','',date(item.received_at)));summary.append(el('span','twc-journey-story-index',String(items.length-index)),title,el('span','twc-journey-story-result',item.replies.length?'↗':item.outcome==='understood'?'✓':'◌'));card.append(summary);
        const progress=el('ol','twc-journey-story-progress');[true,item.media_available,item.inspected,Boolean(item.replies.length)].forEach((done,i)=>{const step=el('li');step.dataset.done=String(done);step.append(el('i','',done?'✓':String(i+1)),el('span','',stages[i]));if(i===2&&item.inspected&&item.outcome!=='understood')step.dataset.uncertain='true';progress.append(step);});card.append(progress);
        const details=el('div','twc-journey-story-content');const badges=el('div','twc-journey-story-tags');badges.append(el('span','',item.native_mention?'@twocomms · підтверджено Meta':'Відмітку нашого акаунта не підтверджено'));if(item.theme)badges.append(el('span','',item.theme+' · оцінка AI'));details.append(badges);
        if(!item.media_available)details.append(el('p','twc-journey-fact-note','Медіа недоступне для перегляду. Сюжет не домислюємо.'));else if(!item.inspected)details.append(el('p','twc-journey-fact-note','Медіа збережено; виконаний аналіз не зафіксовано.'));else if(item.outcome!=='understood')details.append(el('p','twc-journey-fact-note',item.outcome==='unreadable'?'AI не зміг прочитати вміст.':'Зміст визначено невпевнено — потрібне уточнення.'));
        if(item.customer_text){details.append(el('small','','Текст клієнта / контекст'),el('blockquote','',item.customer_text));}
        item.replies.forEach(reply=>{details.append(el('small','',reply.actor==='manager'?'Відповідь менеджера · надіслано':'Відповідь бота · надіслано'),el('blockquote','twc-journey-story-reply',reply.text||'Медіавідповідь'));this.appendSources(details,reply.evidence_refs);});
        if(!item.replies.length)details.append(el('p','twc-journey-fact-note','Пов’язану надіслану відповідь не зафіксовано.'));this.appendSources(details,item.evidence_refs);card.append(details);section.append(card);
      });if(data?.truncated)section.append(el('p','twc-journey-fact-note','Показано останні доступні взаємодії. Старіші повідомлення — у переписці.'));body.append(section);
    }
    appendModeration(body,node){
      const section=el('section','twc-journey-moderation');
      node.moderation_view.items.forEach((item,i)=>{const row=el('div','twc-journey-moderation-step');row.dataset.done=String(item.state==='done');row.dataset.step=String(i);row.append(el('span','twc-journey-moderation-number',item.state==='done'?'✓':String(i+1)));const text=el('div');text.append(el('strong','',item.label),el('p','',item.note));row.append(text);section.append(row);});
      for(const key of ['marked','warning','processing'])this.appendSources(section,node.moderation_progress?.[key]?.evidence_refs||[]);
      body.append(section);
    }
    appendShipping(body,cart){
      const shipping=cart?.shipping,threshold=shipping?.threshold||this.snapshot?.catalogue?.free_shipping_threshold;
      if(!threshold)return;
      const box=el('section','twc-journey-shipping-goal');box.dataset.reached=String(shipping?.eligible_estimate===true);
      box.append(el('strong','',shipping?.eligible_estimate?'Доставка від '+this.money(threshold)+' грн · поріг досягнуто':'Безкоштовна доставка від '+this.money(threshold)+' грн'));
      if(shipping?.remaining!=null){const meter=document.createElement('progress');meter.max=Number(threshold);meter.value=Math.max(0,Number(threshold)-Number(shipping.remaining));meter.setAttribute('aria-label','Сума до порога безкоштовної доставки');box.append(meter);}
      box.append(el('small','',shipping?.eligible_estimate?'За попередньою сумою. Перевіряємо підсумок після знижок.':shipping?.remaining!=null?'Ще '+this.money(shipping.remaining)+' грн за поточним складом.':'Залишок порахуємо, коли визначені ціни й кількість усіх речей.'));
      body.append(box);
    }
    appendCart(body,cart){
      const summary=el('div','twc-journey-cart-summary');
      for(const [value,label] of [[cart.line_count,'позицій'],[cart.item_count??'?', 'речей'],[cart.ready_count+'/'+cart.line_count,'готові параметри']]){const stat=el('div');stat.append(el('strong','',String(value)),el('small','',label));summary.append(stat);}body.append(summary);
      for(const line of cart.lines){
        const card=el('details','twc-journey-cart-line');card.open=cart.lines.length===1;card.dataset.active=String(line.active);
        const fields=line.fields,view=selectionView(fields),title=fields?.items?.find(f=>f.key==='product')?.value||'Товар уточнюємо';
        const heading=el('summary'),name=el('span','twc-journey-cart-name');name.append(el('small','',String(line.position).padStart(2,'0')+(line.active?' · зараз підбираємо':'')),el('strong','',title));
        const compact=(fields?.items||[]).filter(f=>['color','size','fit'].includes(f.key)&&(f.required||f.value)).map(f=>f.value||f.label+' ?').join(' · ');name.append(el('span','twc-journey-cart-mini',compact||'Параметри уточнюємо'),el('span','twc-journey-cart-mini',view.label+(line.subtotal!==null?' · '+this.money(line.subtotal)+' грн':'')));
        const quantity=el('span','twc-journey-cart-quantity',line.quantity===null?'× ?':'× '+line.quantity);quantity.title='Кількість речей у цій позиції';heading.append(name,quantity);card.append(heading);
        const variants=fields?.items?.filter(f=>['fit','color','size'].includes(f.key)||f.key?.startsWith('option:')).filter(f=>f.required||f.value)||[];
        card.append(el('p','twc-journey-cart-variant',variants.length?variants.map(f=>f.label+': '+(f.value||'?')).join(' · '):'Комплектація ще не підтверджена джерелами.'));
        const segments=el('div','twc-journey-cart-segments');segments.setAttribute('aria-label',view.label);
        view.parts.forEach(p=>{const segment=el('span');segment.dataset.state=p.state;segment.title=p.label;segments.append(segment);});card.append(segments,el('small','twc-journey-cart-progress',view.label));
        const grid=el('div','twc-journey-field-grid');
        for(const item of fields?.items||[]){const tile=el('div','twc-journey-field');tile.dataset.state=item.status;tile.dataset.required=String(item.required);tile.append(el('span','twc-journey-field-label',item.label),el('strong','',item.value||(item.required?'Ще уточнюємо':'Не потребує вибору')));if(item.note)tile.append(el('small','',item.note));grid.append(tile);}card.append(grid);
        card.append(el('p','twc-journey-cart-price',line.subtotal!==null?this.money(line.unit_price)+' грн × '+line.quantity+' = '+this.money(line.subtotal)+' грн':line.unit_price!==null?this.money(line.unit_price)+' грн / шт. · кількість уточнюємо':'Вартість уточнюється'));
        this.appendSources(card,line.evidence_refs||[]);body.append(card);
      }
      const total=el('div','twc-journey-cart-total');total.append(el('span','','Попередньо за речі'),el('strong','',cart.estimated_total===null?'Уточнюється':this.money(cart.estimated_total)+' грн'));body.append(total,el('p','twc-journey-fact-note',cart.note));this.appendShipping(body,cart);
    }
    appendRequirements(body,node){
      const progress=node.requirements;if(!progress||!Number.isInteger(progress.completed)||!Number.isInteger(progress.total)||progress.total<=0||progress.completed<0||progress.completed>progress.total||!Array.isArray(progress.items))return;
      const section=el('section','twc-journey-requirements');section.append(el('h5','',(progress.label||'Обов’язкові умови')+' · '+progress.completed+'/'+progress.total));
      const scope=progress.scope||{};if(Number.isInteger(scope.active_position)&&Number.isInteger(scope.line_count)&&scope.active_position>0&&scope.active_position<=scope.line_count&&scope.line_count>1)section.append(el('p','twc-journey-fact-note','Позиція '+scope.active_position+' із '+scope.line_count));
      const other=el('details');other.append(el('summary','','Необов’язкові та незастосовні умови'));
      for(const item of progress.items.slice(0,30)){
        if(!item||typeof item.label!=='string')continue;
        const row=el('div','twc-journey-requirement');row.dataset.complete=String(item.status==='complete');
        row.append(item.status==='complete'?icon('check'):el('span','twc-journey-requirement-open','○'),el('span','',item.label));
        if(item.status==='not_applicable')row.append(el('small','','Не застосовується'));
        // Reason codes are backend diagnostics, never customer-facing copy.
        (item.required===true&&item.status!=='not_applicable'?section:other).append(row);
      }
      if(other.children.length>1)section.append(other);body.append(section);
    }
    timerDescription(timer){
      const now=Number.isFinite(this.serverTime)?this.serverTime+(performance.now()-this.serverAnchor):null;
      const due=Date.parse(timer.due_at||'');
      const labels={paused:'Призупинено',cancelled:'Скасовано',completed:'Завершено',expired:'Строк минув'};
      if(labels[timer.status])return labels[timer.status];
      if(Number.isFinite(due)&&now!==null){
        if(now>=due)return 'Строк минув';
        const minutes=Math.ceil((due-now)/60000);
        return 'Залишилось '+(minutes>=60?Math.floor(minutes/60)+' год '+minutes%60+' хв':minutes+' хв');
      }
      return 'Очікування без визначеного строку';
    }
    appendTimerDetails(body,node){
      if(node.waiting?.evidence_refs?.length){const waiting=el('div','twc-journey-timer-detail');waiting.append(icon('clock'),el('span','',node.waiting.label));body.append(waiting);}
      for(const timer of (node.timers||[])){
        const countdown=invoiceCountdown(timer,Number.isFinite(this.serverTime)?this.serverTime+(performance.now()-this.serverAnchor):null);
        if(countdown?.expired)body.append(el('p','twc-journey-report-note','Час посилання минув. Перевірте оплату та уточніть плани клієнта. Для нагадування на іншу дату потрібні окрема згода й нове посилання.'));

        const line=el('div','twc-journey-timer-detail');line.append(icon('clock'),el('span','',timer.label||'Очікування'));
        const state=el('span','twc-journey-timer-state',this.timerDescription(timer));state.dataset.timerState=timer.id||timer.kind||'';line.append(state);
        const due=date(timer.due_at);if(due)line.append(el('time','','До '+due));body.append(line);
      }
    }
    // Replay only evidence-backed edges that are actually drawn. Catalogue
    // order and missing links must never become an observed customer path.
    walkEdges(){
      const drawn=new Set([...this.svg.querySelectorAll('.twc-journey-edge')].map(p=>p.dataset.edgeId));
      return (this.edges||[]).filter(e=>edgePriority(e)>0&&drawn.has(e.id)).sort((a,b)=>{
        if(Number.isInteger(a.last_step_index)&&Number.isInteger(b.last_step_index))return a.last_step_index-b.last_step_index;
        return 0;
      });
    }
    // v6 · «Комета» — хід клієнта як історія. Точка з хвостом-слідом проходить лише реальні
    // (з джерелами) намальовані переходи. На кожному вузлі — коротка пауза-«вдих» і спалах ядра;
    // на вузлі із запереченнями — стільки обертів, скільки заперечень (1 заперечення = 1 оберт).
    // Фінал — сплеск на поточному вузлі, щоб око зупинилося там, де клієнт зараз.
    toggleWalk(){
      if(this.walking){this.stopWalk();return;}
      if(matchMedia('(prefers-reduced-motion: reduce)').matches)return;
      const edges=this.walkEdges();if(!edges.length)return;
      const paths=new Map([...this.svg.querySelectorAll('.twc-journey-edge')].map(p=>[p.dataset.edgeId,p]));
      this.walking=true;this.play.setAttribute('aria-pressed','true');this.play.textContent='⏸ Зупинити';this.root.dataset.walking='true';this.modal?.setAttribute('data-walking','true');
      // Слід: полілінія, що «тягнеться» за кометою й поступово згасає.
      this.trail?.remove();this.trail=svg('polyline',{points:''});this.trail.classList.add('twc-journey-trail');this.svg.append(this.trail);
      const trailPoints=[];
      let index=0;
      const ease=t=>t<.5?2*t*t:1-Math.pow(-2*t+2,2)/2;
      const move=point=>{this.walker.style.left=point.x+'px';this.walker.style.top=point.y+'px';this.walker.dataset.active='true';trailPoints.push(point.x.toFixed(1)+','+point.y.toFixed(1));if(trailPoints.length>26)trailPoints.shift();this.trail.setAttribute('points',trailPoints.join(' '));};
      const flash=id=>{const b=this.buttons.get(id);if(!b)return;b.dataset.visited='true';b.classList.remove('twc-journey-flash');void b.offsetWidth;b.classList.add('twc-journey-flash');};
      const finish=()=>{const id=this.currentId;if(id){const b=this.buttons.get(id);b?.classList.add('twc-journey-arrive');this.arriveTimer=setTimeout(()=>b?.classList.remove('twc-journey-arrive'),1400);}this.walkTimer=setTimeout(()=>this.stopWalk(),900);};
      const step=()=>{
        if(!this.walking||this.destroyed)return;
        if(index>=edges.length){finish();return;}
        trailPoints.length=0;const edge=edges[index++],path=paths.get(edge.id);if(!path?.isConnected){finish();return;}
        if(index===1)flash(edge.from_node_id);
        const length=path.getTotalLength(),start=performance.now(),duration=Math.max(520,Math.min(1100,length*4.5));
        const frame=now=>{
          if(!this.walking||this.destroyed)return;
          const progress=Math.min(1,(now-start)/duration);move(path.getPointAtLength(length*ease(progress)));
          if(progress<1){this.walkFrame=requestAnimationFrame(frame);return;}
          flash(edge.to_node_id);
          const cases=this.graph.nodes.filter(n=>n.semantic_key==='journey_case'&&(n.presentation_event?.anchor_ids||[]).includes(edge.to_node_id));
          const onEdge=edge.objection_band?.count||0;
          const turns=cases.length||onEdge;
          const center=this.positions.get(edge.to_node_id);
          if(turns&&center){
            const orbitStart=performance.now(),duration=turns*700;this.walker.dataset.orbit='true';this.walker.dataset.tone=edge.objection_band?.tone||'warning';
            const orbit=now=>{
              if(!this.walking||this.destroyed)return;
              const angle=(now-orbitStart)/700*Math.PI*2-Math.PI/2;move({x:center.x+19*Math.cos(angle),y:center.y+19*Math.sin(angle)});
              if(now-orbitStart<duration){this.walkFrame=requestAnimationFrame(orbit);return;}
              this.walker.dataset.orbit='false';delete this.walker.dataset.tone;this.walkTimer=setTimeout(step,260);
            };this.walkFrame=requestAnimationFrame(orbit);
          }else this.walkTimer=setTimeout(step,260);
        };this.walkFrame=requestAnimationFrame(frame);
      };step();
    }
    stopWalk(){
      this.walking=false;clearTimeout(this.walkTimer);clearTimeout(this.arriveTimer);cancelAnimationFrame(this.walkFrame);
      if(this.walker){this.walker.dataset.active='false';this.walker.dataset.orbit='false';}
      this.trail?.remove();this.trail=null;if(this.root)delete this.root.dataset.walking;this.modal?.removeAttribute('data-walking');
      this.buttons?.forEach(b=>{delete b.dataset.visited;b.classList.remove('twc-journey-flash','twc-journey-arrive');});
      if(this.play){this.play.setAttribute('aria-pressed','false');this.play.textContent='▶ Показати хід';}
    }
    updateTimers(){
      if(!this.graph)return;const serverNow=Number.isFinite(this.serverTime)?this.serverTime+(performance.now()-this.serverAnchor):null;
      this.graph.nodes.forEach(node=>{
        const button=this.buttons.get(node.id);if(!button)return;button.dataset.waiting=String(hasActiveWait(node,serverNow));
        if(node.payment_progress){
          const view=paymentView(node.payment_progress,node.timers||[],serverNow),changed=node.payment_progress.label!==view.label;
          node.payment_progress={...node.payment_progress,...view};this.renderSegments(button,node);
          let caption=button.querySelector('.twc-journey-payment-caption');if(!caption){caption=el('span','twc-journey-payment-caption');button.append(caption);}caption.textContent=view.label;caption.dataset.tone=view.tone;
          button.dataset.paymentState=view.expired?'expired':node.payment_progress.paid?'paid':'waiting';
          if(view.expired||node.payment_progress.paid)button.dataset.tone=view.tone;
          if(changed&&this.selected===node.id&&!this.selectedEdge){this.panelKey='';this.renderPanel();}
        }
        const timer=node.payment_progress?.paid?null:(node.timers||[]).find(t=>invoiceCountdown(t,serverNow))||(node.timers||[]).find(t=>['running','scheduled','paused','expired','unknown'].includes(t.status));
        let ring=button.querySelector('.twc-journey-timer');
        const countdown=invoiceCountdown(timer,serverNow);let fuse=button.querySelector('.twc-journey-fuse');
        if(countdown&&!node.payment_progress?.paid){
          ring?.remove();
          if(!fuse){
            fuse=svg('svg',{viewBox:'0 0 56 56','aria-hidden':'true',focusable:'false'});fuse.classList.add('twc-journey-fuse');
            fuse.append(svg('circle',{cx:28,cy:28,r:24,class:'twc-journey-fuse-track'}),svg('circle',{cx:28,cy:28,r:24,pathLength:100,class:'twc-journey-fuse-remaining'}),svg('circle',{cx:52,cy:28,r:2.1,class:'twc-journey-fuse-ember'}));
            button.querySelector('.twc-journey-core').append(fuse);button.append(el('span','twc-journey-fuse-label'));
          }
          fuse.dataset.expired=String(countdown.expired);fuse.querySelector('.twc-journey-fuse-remaining').setAttribute('stroke-dasharray',countdown.remaining*100+' 100');
          const angle=countdown.remaining*Math.PI*2,ember=fuse.querySelector('.twc-journey-fuse-ember');ember.setAttribute('cx',28+24*Math.cos(angle));ember.setAttribute('cy',28+24*Math.sin(angle));
          button.querySelector('.twc-journey-fuse-label').textContent=countdown.label;
          button.dataset.timer=countdown.expired?'expired':'running';button.title=node.label+' · '+countdown.label+' · до '+date(timer.due_at);
          button.setAttribute('aria-label',(button.dataset.baseLabel||node.label)+' · '+countdown.label);
          return;
        }else{fuse?.remove();button.querySelector('.twc-journey-fuse-label')?.remove();}

        if(!timer){ring?.remove();delete button.dataset.timer;if(node.payment_progress)button.setAttribute('aria-label',(button.dataset.baseLabel||node.label)+' · '+node.payment_progress.label);return;}
        button.setAttribute('aria-label',(button.dataset.baseLabel||node.label)+' · '+(timer.label||'Очікування')+' · '+this.timerDescription(timer));
        const start=Date.parse(timer.started_at||''),due=Date.parse(timer.due_at||'');
        const valid=Number.isFinite(start)&&Number.isFinite(due)&&due>start&&serverNow!==null;
        const expired=timer.status==='expired'||(valid&&serverNow>=due),paused=timer.status==='paused';
        const status=button.querySelector('.twc-journey-status');if(!['complete','invalidated'].includes(node.state)&&!['danger','manager'].includes(node.tone)){status.hidden=false;status.replaceChildren(icon('clock'));}
        if(!valid||paused){ring?.remove();button.dataset.timer=paused?'paused':'unknown';button.title=node.label+' · '+(paused?'Призупинено':timer.due_at?'До '+date(timer.due_at):'Строк не задано');return;}
        if(!ring){ring=svg('svg',{viewBox:'0 0 38 38','aria-hidden':'true'});ring.classList.add('twc-journey-timer');ring.append(svg('circle',{cx:19,cy:19,r:17,class:'twc-journey-timer-track'}),svg('circle',{cx:19,cy:19,r:17,class:'twc-journey-timer-remaining',pathLength:100}));button.querySelector('.twc-journey-core').append(ring);}
        const remaining=Math.max(0,Math.min(1,(due-serverNow)/(due-start)));
        ring.lastChild.setAttribute('stroke-dasharray',(remaining*100)+' 100');button.dataset.timer=expired?'expired':'running';
        const minutes=Math.ceil(Math.max(0,due-serverNow)/60000),text=expired?'Строк минув':minutes>=60?Math.floor(minutes/60)+' год '+minutes%60+' хв':minutes+' хв';
        button.title=node.label+' · '+(timer.label||'Очікування')+' · '+text+' · до '+date(timer.due_at);
      });
      if(this.panel&&!this.selectedEdge){const node=this.graph.nodes.find(n=>n.id===this.selected);for(const state of this.panel.querySelectorAll('[data-timer-state]')){const timer=node?.timers?.find(t=>(t.id||t.kind||'')===state.dataset.timerState);if(timer)state.textContent=this.timerDescription(timer);}}
    }
    openMap(family=null){
      if(this.modal||!this.snapshot)return;this.closePanel(false);
      const own=this.graph?.inline_family;
      this.mapMode=family==='after'?'short':!family&&window.matchMedia('(max-width: 880px)').matches?'actual':'all';this.showPossible=this.mapMode!=='actual';
      this.possibleFamily=family&&family!=='all'?family:({catalog:'catalog',custom:'custom',dtf:'custom',collaboration:'collaboration',employment:'employment',information:'information',support:'after'}[own]||'catalog');
      const dialog=el('dialog','twc-journey twc-journey-dialog');dialog.setAttribute('aria-label','Повний шлях клієнта');
      const top=el('header','twc-journey-dialog-head'),copy=el('div');copy.append(el('h3','','Шлях клієнта'),el('p','',this.title.textContent+this.mode.textContent));
      const close=el('button','twc-journey-close','×');close.type='button';close.setAttribute('aria-label','Закрити повну карту');close.addEventListener('click',()=>this.closeMap());top.append(copy,close);
      const tools=el('div','twc-journey-tools');tools.append(this.play);const action=(label,fn)=>{const b=el('button','',label);b.type='button';b.addEventListener('click',fn);tools.append(b);return b;};
      const minus=action('−',()=>this.setZoom(this.zoom/1.2));minus.setAttribute('aria-label','Зменшити карту');const plus=action('+',()=>this.setZoom(this.zoom*1.2));plus.setAttribute('aria-label','Збільшити карту');this.zoomLabel=el('span','','100%');tools.append(this.zoomLabel);action('Огляд',()=>this.fitMap(true));action('До поточного',()=>this.centerCurrent());
      const modes=el('div','twc-journey-modes');modes.setAttribute('role','group');modes.setAttribute('aria-label','Вигляд карти');
      this.modeButtons=new Map();
      for(const [key,label]of [['actual','Шлях клієнта'],['short','Коротко'],['all','Усі можливості']]){
        const button=el('button','',label);button.type='button';button.setAttribute('aria-pressed',String(key===this.mapMode));
        button.addEventListener('click',()=>this.setMapMode(key));modes.append(button);this.modeButtons.set(key,button);
      }tools.append(modes);
      this.familySelect=el('select');this.familySelect.setAttribute('aria-label','Напрям короткого шляху');
      [['catalog','Одяг і оплата'],['custom','Власний принт / DTF'],['collaboration','Співпраця'],['employment','Робота'],['information','Інформація'],['after','Після покупки']].forEach(([value,label])=>this.familySelect.append(new Option(label,value)));
      this.familySelect.value=this.possibleFamily;this.familySelect.hidden=this.mapMode!=='short';
      this.familySelect.addEventListener('change',()=>{this.possibleFamily=this.familySelect.value;this.setMapMode('short');});tools.append(this.familySelect);
      this.mapHint=el('p','twc-journey-mode-hint');
      this.viewport=el('div','twc-journey-viewport');this.viewport.tabIndex=0;this.viewport.setAttribute('aria-label','Карта. Стрілки для переміщення; кнопки плюс і мінус для масштабу.');this.canvas=el('div','twc-journey-canvas');this.canvas.append(this.map);this.viewport.append(this.canvas);
      this.accessible=el('details','twc-journey-accessible');this.accessible.append(el('summary','','Етапи й переходи списком'));this.accessibleBody=el('div');this.accessible.append(this.accessibleBody);
      const legend=el('div','twc-journey-map-legend');[['recorded','━ Перехід'],['recorded','┄ За перепискою'],['success','● Підтверджено'],['warning','! Питання / перешкода'],['manager','◇? Уточнення менеджера'],['manager','┄ Контекст клієнта'],['neutral','◌ Можливий етап']].forEach(([tone,label])=>{const item=el('span','',label);item.dataset.tone=tone;legend.append(item);});
      this.mapCoverage=el('p','twc-journey-map-coverage');dialog.append(top,this.orderOutcomes,tools,this.mapHint,legend,this.mapCoverage,this.objections,this.viewport,this.discussionDetails,this.accessible);this.modal=dialog;document.body.append(dialog);
      dialog.addEventListener('keydown',this.escape);dialog.addEventListener('cancel',event=>{event.preventDefault();if(this.selected)this.closePanel(true);else this.closeMap();});dialog.addEventListener('click',event=>{if(event.target===dialog){const r=dialog.getBoundingClientRect();if(event.clientX<r.left||event.clientX>r.right||event.clientY<r.top||event.clientY>r.bottom)this.closeMap();}});
      this.viewport.addEventListener('wheel',event=>{if(event.ctrlKey){event.preventDefault();this.setZoom(this.zoom*Math.exp(-event.deltaY*.01));}},{passive:false});
      this.bindPan();this.modalResize=new ResizeObserver(()=>this.queueLayout());this.modalResize.observe(this.viewport);this.oldBodyOverflow=document.body.style.overflow;document.body.style.overflow='hidden';dialog.showModal();this.zoom=1;this.update(this.snapshot,{force:true});this.layout();this.fitMap();this.updateMapHint();close.focus({preventScroll:true});
    }
    setMapMode(mode){
      this.closePanel(false);this.mapMode=mode;this.showPossible=mode!=='actual';
      this.modeButtons.forEach((b,k)=>b.setAttribute('aria-pressed',String(k===mode)));
      this.familySelect.hidden=mode!=='short';this.update(this.snapshot,{force:true});this.layout();this.fitMap();this.updateMapHint();
    }
    updateMapHint(){
      if(!this.mapHint)return;
      this.mapHint.textContent=this.mapMode==='all'
        ?'Повна карта · '+this.graph.nodes.filter(n=>!eventNode(n)).length+' блоків · '+this.edges.length+' зв’язків. Усі розвилки доступні; «Огляд» уміщує всю карту. Оберіть вузол, щоб підсвітити його входи й виходи; пунктир — можливість, а не факт.'
        :this.mapMode==='short'&&this.possibleFamily==='after'&&this.graph.nodes.some(n=>n.post_purchase)?'Після покупки · сервіс, згода, UGC та повторний інтерес мають окремі умови. Прокрутіть карту вниз; отримання не означає, що всі дії вже виконані.':this.mapMode==='short'?'Основні можливі етапи обраного напряму. Факти клієнта збережено; відгалуження й альтернативи — у «Усі можливості».'
        :'Події та звернення цього клієнта. Пунктир за перепискою — інтерпретація, пов’язане замовлення — окремий контекст.';
    }
    bindPan(){
      const points=new Map();let drag=null,pinch=null;
      this.viewport.addEventListener('pointerdown',event=>{if(event.target.closest('button'))return;points.set(event.pointerId,{x:event.clientX,y:event.clientY});this.viewport.setPointerCapture(event.pointerId);if(points.size===1)drag={x:event.clientX,y:event.clientY,left:this.viewport.scrollLeft,top:this.viewport.scrollTop};if(points.size===2){const [a,b]=[...points.values()];pinch={distance:Math.hypot(a.x-b.x,a.y-b.y),zoom:this.zoom};drag=null;}});
      this.viewport.addEventListener('pointermove',event=>{if(!points.has(event.pointerId))return;points.set(event.pointerId,{x:event.clientX,y:event.clientY});if(points.size===2&&pinch){const [a,b]=[...points.values()];if(pinch.distance>0)this.setZoom(pinch.zoom*Math.hypot(a.x-b.x,a.y-b.y)/pinch.distance);}else if(drag){this.viewport.scrollLeft=drag.left-(event.clientX-drag.x);this.viewport.scrollTop=drag.top-(event.clientY-drag.y);}});
      const end=event=>{points.delete(event.pointerId);drag=null;pinch=null;};this.viewport.addEventListener('pointerup',end);this.viewport.addEventListener('pointercancel',end);
    }
    closeMap(){
      if(!this.modal)return;this.closePanel(false);this.modalResize.disconnect();this.inlineKey.append(this.play);this.root.insertBefore(this.orderOutcomes,this.error);this.root.insertBefore(this.map,this.inlineKey);this.root.insertBefore(this.discussionDetails,this.note);this.root.insertBefore(this.objections,this.discussionDetails);this.map.style.transform='';this.modal.close();this.modal.remove();this.modal=null;document.body.style.overflow=this.oldBodyOverflow;this.zoom=1;this.update(this.snapshot,{force:true});this.queueLayout();this.expand.focus({preventScroll:true});
    }
    setZoom(value){
      if(!this.modal)return;const next=Math.max(.1,Math.min(2.2,value));const cx=(this.viewport.scrollLeft+this.viewport.clientWidth/2)/this.zoom,cy=(this.viewport.scrollTop+this.viewport.clientHeight/2)/this.zoom;this.zoom=next;this.map.style.transform='scale('+next+')';this.canvas.style.width=this.mapWidth*next+'px';this.canvas.style.height=this.mapHeight*next+'px';this.zoomLabel.textContent=Math.round(next*100)+'%';this.viewport.scrollLeft=cx*next-this.viewport.clientWidth/2;this.viewport.scrollTop=cy*next-this.viewport.clientHeight/2;
    }
    fitMap(overview=false){if(this.modal&&this.mapMode==='short'&&this.possibleFamily==='after'&&this.graph.nodes.some(n=>n.post_purchase)){this.setZoom(Math.min(1,(this.viewport.clientWidth-12)/this.mapWidth));this.viewport.scrollTop=0;this.viewport.scrollLeft=0;return;}if(this.modal&&this.mapMode==='all'){const fit=Math.min(1,(this.viewport.clientWidth-24)/this.mapWidth,(this.viewport.clientHeight-24)/this.mapHeight);this.setZoom(overview?fit:Math.max(.8,fit));const p=this.positions.get(this.currentId);this.viewport.scrollTop=overview?0:Math.max(0,(p?.y||0)*this.zoom-this.viewport.clientHeight/2);this.viewport.scrollLeft=0;return;}if(this.modal)this.setZoom(Math.min(1,(this.viewport.clientWidth-24)/this.mapWidth,(this.viewport.clientHeight-24)/this.mapHeight));}
    centerCurrent(){if(!this.modal)return;const pos=this.positions.get(this.currentId)||this.positions.values().next().value;if(!pos)return;this.setZoom(1);this.viewport.scrollLeft=Math.max(0,pos.x-this.viewport.clientWidth/2);this.viewport.scrollTop=Math.max(0,pos.y-this.viewport.clientHeight/2);}
    renderAccessibleList(){
      if(!this.accessibleBody)return;this.accessibleBody.replaceChildren();this.graph.nodes.forEach(node=>{const b=el('button','',node.label+' · '+nodeStatusLabel(node));b.type='button';b.addEventListener('click',()=>this.toggleNode(node.id));this.accessibleBody.append(b);});
      this.edges.forEach(edge=>{const a=this.graph.nodes.find(n=>n.id===edge.from_node_id),b=this.graph.nodes.find(n=>n.id===edge.to_node_id);const item=el('button','',a.label+(contextual(edge)?' — ':' → ')+b.label+' · '+(contextual(edge)?'Пов’язано':(interpreted(edge)?'За перепискою · ':'')+(edge.reason_label||(witnessed(edge)?'Зафіксований перехід':(edge.condition_label?edge.condition_label+' · ':'')+'Можливий шлях'))));item.type='button';item.addEventListener('click',()=>this.selectEdge(edge.id));this.accessibleBody.append(item);});
      if(!this.edges.length)this.accessibleBody.append(el('p','','Переходи ще не зафіксовано. Положення етапів не підтверджує послідовність подій.'));
    }
    queueLayout(){cancelAnimationFrame(this.frame);this.frame=requestAnimationFrame(()=>this.layout());}
    layout(){
      if(this.destroyed||!this.root.isConnected||!this.graph)return;
      const width=this.modal?this.viewport.clientWidth:this.root.getBoundingClientRect().width;
      // A control label can notify ResizeObserver without changing map geometry.
      if(this.walking&&this.layoutWidth===width&&this.layoutModal===Boolean(this.modal))return;
      this.stopWalk();this.layoutWidth=width;this.layoutModal=Boolean(this.modal);this.syncVisibility(width);
      const nodes=this.graph.nodes.filter(n=>this.visibleIds.includes(n.id)&&!eventNode(n));const narrow=!this.modal&&width<560;this.map.dataset.narrow=String(narrow);this.map.dataset.single=String(nodes.length===1&&!this.eventVisibleIds?.length);
      this.positions=new Map();this.bands.replaceChildren();
      const atlasMode=Boolean(this.modal&&this.mapMode==='all');this.map.dataset.atlas=String(atlasMode);this.map.dataset.focused=String(Boolean(this.selected));
      if(this.modal&&this.mapMode==='short'&&this.possibleFamily==='after'&&nodes.some(n=>n.post_purchase)){
        const geometry=window.TwcJourneyGeometry.aftercare({nodes,width});this.positions=geometry.positions;this.mapWidth=geometry.width;this.mapHeight=geometry.height;
        nodes.forEach(node=>{const p=this.positions.get(node.id),cell=this.cells.get(node.id);cell.style.left=p.x+'px';cell.style.top=p.y+'px';cell.style.width=p.cardWidth+'px';});
      }else if(atlasMode){
        const geometry=window.TwcJourneyGeometry.atlas({nodes,edges:this.edges,width});this.positions=geometry.positions;this.mapWidth=geometry.width;this.mapHeight=geometry.height;
        for(const band of geometry.bands){const box=el('section','twc-journey-band');box.style.cssText='left:'+band.x+'px;top:'+band.y+'px;width:'+band.width+'px;height:'+band.height+'px';box.append(el('h4','',band.label),el('span','',String(band.count)));this.bands.append(box);}
        nodes.forEach(node=>{const p=this.positions.get(node.id),cell=this.cells.get(node.id);cell.style.left=p.x+'px';cell.style.top=p.y+'px';cell.style.width=p.cardWidth+'px';});
      }else if(!this.modal&&this.inlineSlots){
        const columns=Math.max(1,...[...this.inlineSlots.values()].map(p=>p.col+1)),step=width/columns;
        const rowHeight=nodes.some(n=>n.moderation_view||n.semantic_key==='advertising_entry'||n.selection_progress||n.consent_progress||n.payment_progress||(n.timers||[]).some(t=>t.kind==='invoice_expiry'))?100:70;
        const loopSpace=this.edges.some(e=>e.from_node_id===e.to_node_id&&nodes.some(n=>n.id===e.from_node_id))||this.eventVisibleIds?.length?20:16;
        nodes.forEach(node=>{const slot=this.inlineSlots.get(node.id),x=step*(slot.col+.5),y=30+loopSpace+slot.row*rowHeight;this.positions.set(node.id,{x,y,...slot});const cell=this.cells.get(node.id);cell.style.left=x+'px';cell.style.top=y+'px';cell.style.width=Math.max(44,step-12)+'px';});
        this.mapWidth=width;this.mapHeight=(nodes.some(n=>this.inlineSlots.get(n.id).row)?142:68)+loopSpace;
      }else if(!narrow&&window.TwcJourneyGeometry){
        const geometry=window.TwcJourneyGeometry.layout({nodes,edges:this.edges.filter(e=>this.visibleIds.includes(e.from_node_id)&&this.visibleIds.includes(e.to_node_id)),width,full:Boolean(this.modal)});
        this.positions=geometry.positions;this.mapWidth=geometry.width;this.mapHeight=geometry.height;
        const rankCount=new Set([...this.positions.values()].map(p=>p.col)).size;
        const cellWidth=this.modal?100:Math.max(44,(width-32)/Math.max(1,rankCount)-12);
        nodes.forEach(node=>{const pos=this.positions.get(node.id),cell=this.cells.get(node.id);cell.style.left=pos.x+'px';cell.style.top=pos.y+'px';cell.style.width=cellWidth+'px';});
      }else{
        nodes.forEach((node,index)=>{const x=width/Math.max(1,nodes.length)*(index+.5),y=22;this.positions.set(node.id,{x,y,col:index,row:0});const cell=this.cells.get(node.id);cell.style.left=x+'px';cell.style.top=y+'px';cell.style.width=Math.min(width-8,150)+'px';});
        this.mapWidth=width;this.mapHeight=58;
      }
      // If rank collisions exceed the overview, retain the focused slice rather
      // than squeeze labels or wrap the path. The full map retains every node.
      if(!this.modal){
        if([...this.positions.values()].some(p=>p.x>width-24||p.y>150)){
          const id=this.currentId&&this.positions.has(this.currentId)?this.currentId:nodes[0]?.id;
          this.positions.clear();if(id){this.positions.set(id,{x:width/2,y:22,col:0,row:0});this.visibleIds=[id];this.cells.forEach((cell,key)=>{cell.hidden=key!==id;});const cell=this.cells.get(id);cell.style.left=width/2+'px';cell.style.top='22px';cell.style.width=(width-8)+'px';}
          this.map.dataset.single='true';this.map.dataset.narrow='true';this.mapHeight=58;this.expand.textContent='Карта · '+this.nodeIds.length+' ↗';
        }this.mapHeight=Math.min(192,this.mapHeight);
      }
      // Composite captions and invoice lifetime need their own vertical space.
      for(const node of nodes){const p=this.positions.get(node.id);if(p&&(node.semantic_key==='advertising_entry'||node.selection_progress||node.consent_progress||node.payment_progress||(node.timers||[]).some(t=>t.kind==='invoice_expiry')))this.mapHeight=Math.max(this.mapHeight,p.y+78);}
      this.placeEvents();
      this.map.style.setProperty('--journey-touch-scale',String(this.modal?1/this.zoom:1));
      this.map.style.width=this.mapWidth+'px';this.map.style.height=this.mapHeight+'px';
      if(this.modal){this.map.style.transform='scale('+this.zoom+')';this.canvas.style.width=this.mapWidth*this.zoom+'px';this.canvas.style.height=this.mapHeight*this.zoom+'px';}
      this.drawGuides();const reduced=matchMedia('(prefers-reduced-motion: reduce)').matches;this.play.disabled=reduced||!this.walkEdges().length;this.play.title=reduced?'Анімацію вимкнено у налаштуваннях руху':this.play.disabled?'Немає видимих переходів із джерелами':'Показати наявні переходи. Пунктир за перепискою лишається інтерпретацією.';this.positionPanel();
    }
    placeEvents(){
      const events=this.graph.nodes.filter(n=>this.eventVisibleIds?.includes(n.id)),details=[];
      const obstacles=this.graph.nodes.filter(n=>!eventNode(n)&&this.positions.has(n.id)).flatMap(n=>{const p=this.positions.get(n.id);return [{left:p.x-23,right:p.x+23,top:p.y-23,bottom:p.y+23},{left:p.x-50,right:p.x+50,top:p.y+21,bottom:p.y+(n.moderation_view||n.semantic_key==='advertising_entry'||n.selection_progress||n.consent_progress||n.payment_progress||(n.timers||[]).some(t=>t.kind==='invoice_expiry')?72:this.modal?53:39)}];});
      for(const node of events){
        if(node.semantic_key==='journey_case'){this.cells.get(node.id).hidden=true;this.positions.delete(node.id);continue;}
        const info=node.presentation_event,anchors=info.anchor_ids.map(id=>this.positions.get(id)).filter(Boolean);let point;
        if(anchors.length&&info.mode!=='detail'&&!(this.modal&&this.mapMode==='all')){
          const a=anchors[0],b=anchors[1],candidates=[];
          if(b)candidates.push({x:(a.x+b.x)/2,y:(a.y+b.y)/2});
          candidates.push({x:a.x+40,y:a.y-22},{x:a.x-40,y:a.y-22});
          point=candidates.find(p=>p.x>=16&&p.x<=this.mapWidth-16&&p.y>=16&&p.y<=this.mapHeight-16&&!obstacles.some(r=>p.x-8<r.right&&p.x+8>r.left&&p.y-8<r.bottom&&p.y+8>r.top));
        }
        info.unplaced=!point;
        if(!point){this.cells.get(node.id).hidden=true;this.positions.delete(node.id);details.push(node);continue;}
        this.cells.get(node.id).hidden=false;
        this.positions.set(node.id,{...point,col:(anchors[0]?.col||0)+.35,row:anchors[0]?.row||0});
        obstacles.push({left:point.x-16,right:point.x+16,top:point.y-16,bottom:point.y+16});
        const cell=this.cells.get(node.id);cell.style.left=point.x+'px';cell.style.top=point.y+'px';cell.style.width='32px';
      }
      this.discussionDetails.hidden=!details.length;
      const shown=new Set(details.map(n=>n.id));for(const [id,button]of this.detailButtons)if(!shown.has(id)){button.remove();this.detailButtons.delete(id);}
      if(details.length&&!this.discussionDetails.firstChild)this.discussionDetails.append(el('span','twc-journey-fact-note','Деталі поза схемою:'));
      for(const node of details){let button=this.detailButtons.get(node.id);if(!button){button=el('button','twc-journey-detail-link');button.type='button';button.addEventListener('click',()=>this.toggleNode(node.id));this.detailButtons.set(node.id,button);this.discussionDetails.append(button);}button.setAttribute('aria-controls','twc-journey-panel-'+this.snapshot.client_id);button.textContent=node.label;button.dataset.tone=node.presentation_event.tone;button.title=node.presentation_event.mode==='detail'?'Подробиці та джерела переписки':'Немає однозначного місця на схемі; зв’язок не домислюється';button.setAttribute('aria-expanded',String(this.selected===node.id));}

    }
    drawGuides(){
      this.svg.setAttribute('viewBox','0 0 '+this.mapWidth+' '+this.mapHeight);this.svg.replaceChildren();const tones={neutral:'#64748b',recorded:'#60a5fa',success:'#34d399',warning:'#fbbf24',danger:'#fb7185',manager:'#a78bfa'},prefix='journey-'+this.snapshot.client_id;
      const defs=svg('defs');Object.entries(tones).forEach(([tone,color])=>{const marker=svg('marker',{id:prefix+'-'+tone,viewBox:'0 0 6 6',refX:5,refY:3,markerWidth:5,markerHeight:5,orient:'auto'});marker.append(svg('path',{d:'M1 1L5 3L1 5',fill:'none',stroke:color,'stroke-width':1}));defs.append(marker);});this.svg.append(defs);
      const shown=new Set();
      const inlineReturns=[];
      const visibleNodes=this.graph.nodes.filter(n=>this.positions.has(n.id));
      const candidates=this.displayEdges||this.edges,representedPairs=new Set(candidates.filter(e=>edgePriority(e)>0).map(edgePair));
      const unplaced=new Set(this.graph.nodes.filter(n=>eventNode(n)&&(n.presentation_event.mode==='unplaced'||n.presentation_event.unplaced)).map(n=>n.id));
      const drawingEdges=candidates.filter(e=>!unplaced.has(e.from_node_id)&&!unplaced.has(e.to_node_id)&&!(e.relation==='route'&&representedPairs.has(edgePair(e))));
      drawingEdges.forEach(e=>{delete e.objection_band;});
      const geometryRoutes=window.TwcJourneyGeometry?.routeEdges({nodes:visibleNodes,edges:drawingEdges,positions:this.positions,width:this.mapWidth,height:this.mapHeight,full:Boolean(this.modal)});
      [...drawingEdges].sort((a,b)=>edgePriority(a)-edgePriority(b)).forEach(edge=>{
        const a=this.positions.get(edge.from_node_id),b=this.positions.get(edge.to_node_id);if(!a||!b)return;
        const observed=witnessed(edge),reconstructed=interpreted(edge),tone=contextual(edge)?'manager':edgeTone(edge);
        const route=geometryRoutes?.get(edge.id);if(!route)return;
        const d=route.d,mx=route.markerX,my=route.markerY;
        const path=svg('path',{d,stroke:tones[tone],...(contextual(edge)?{}:{'marker-end':'url(#'+prefix+'-'+tone+')'})});path.classList.add('twc-journey-edge');path.dataset.edgeId=edge.id;path.dataset.observed=String(observed);path.dataset.interpreted=String(reconstructed);path.dataset.contextual=String(contextual(edge));path.dataset.selected=String(this.selectedEdge===edge.id);path.dataset.related=String(Boolean(this.selected&&(edge.from_node_id===this.selected||edge.to_node_id===this.selected)));
        if(edge.via_objection&&this.selectedEdge===edge.id){
          const key='possible-concern:'+edge.id;shown.add(key);let chip=this.edgeButtons.get(key);
          if(!chip){chip=el('button','twc-journey-band-chip twc-journey-possible-concern','◇');chip.type='button';chip.addEventListener('click',()=>this.selectEdge(edge.id));this.edgeButtons.set(key,chip);this.edgeLayer.append(chip);}
          chip.style.left=mx+'px';chip.style.top=my+'px';chip.title=edge.condition_label;
          chip.setAttribute('aria-label','Можливе заперечення на переході · '+edge.condition_label);
        }
        this.svg.append(path);
        path.append(svg('title'));path.lastChild.textContent=contextual(edge)?'Пов’язано з '+(this.graph.nodes.find(n=>n.id===edge.to_node_id)?.label||'контекстом замовлення'):edge.condition_label||edge.reason_label||(observed?'Збережений перехід':'Можливий шлях');

        if(route.conditionPosition&&!(this.modal&&this.mapMode==='all')){const p=route.conditionPosition,label=svg('text',{x:p.x,y:p.y,'text-anchor':'middle','dominant-baseline':'middle'});label.classList.add('twc-journey-condition');label.textContent=edge.outcome==='wait_for_attempt'?'Нова спроба':'Допомога';this.svg.append(label);}
        if(this.newEdges?.has(edge.id)&&!matchMedia('(prefers-reduced-motion: reduce)').matches){const dot=svg('circle',{r:2,fill:tones[tone]});const motion=svg('animateMotion',{dur:'.6s',path:d,repeatCount:1,fill:'freeze'});dot.append(motion);this.svg.append(dot);motion.addEventListener('endEvent',()=>dot.remove(),{once:true});}
        const count=Number.isInteger(edge.repeated_count)&&edge.repeated_count>1?edge.repeated_count:0;
        if(eventNode(this.graph.nodes.find(n=>n.id===edge.from_node_id))||eventNode(this.graph.nodes.find(n=>n.id===edge.to_node_id)))return;
        if(!exceptional(edge)||this.graph.nodes.some(n=>n.id===edge.case_id&&eventNode(n)))return;
        if(!this.modal&&this.inlineSlots&&(observed||reconstructed)&&returnEdge(edge)){
          // A 44px marker on the narrow outer return corridor would overlap the
          // chat heading. Keep the actual arrow and its disclosure in the key.
          inlineReturns.push(edge);
          return;
        }
        const peers=this.parallelEdges(edge).filter(item=>exceptional(item)&&drawingEdges.some(shownEdge=>shownEdge.id===item.id)&&(!this.inlineSlots||this.modal||!returnEdge(item)));
        if(peers.length&&peers[0].id!==edge.id)return;
        const total=peers.reduce((sum,item)=>sum+edgeCount(item),0)||edgeCount(edge),selected=peers.some(item=>item.id===this.selectedEdge)||this.selectedEdge===edge.id;
        if(edge.structural_path||(!edge.reason_label&&!count&&edge.relation!=='return'&&peers.length<2))return;shown.add(edge.id);let button=this.edgeButtons.get(edge.id);
        if(!button){button=el('button','twc-journey-edge-marker');button.type='button';button.addEventListener('click',()=>this.selectEdge(edge.id));this.edgeButtons.set(edge.id,button);this.edgeLayer.append(button);}
        button.replaceChildren(icon(returnEdge(edge)?'return':tone==='danger'?'cross':edge.interpretation_kind==='objection'?'question':tone==='warning'?'repeat':'question'));if(total>1)button.append(el('span','','×'+total));button.style.left=mx+'px';button.style.top=my+'px';button.dataset.tone=tone;button.title=edge.summary||edge.reason_label||'Подробиці переходу';button.setAttribute('aria-label',(edge.summary||edge.reason_label||'Подробиці переходу')+(total>1?' · переходів '+total:''));button.setAttribute('aria-expanded',String(selected));
      });
      this.drawConcernMarkers(shown,geometryRoutes);
      this.inlineKey.querySelector('.twc-journey-key-possible').hidden=!drawingEdges.some(e=>e.relation==='route'&&this.positions.has(e.from_node_id)&&this.positions.has(e.to_node_id));
      this.inlineReturnIds=inlineReturns.map(edge=>edge.id);
      const inlineReturn=inlineReturns.find(edge=>edge.id===this.selectedEdge)||inlineReturns[0];
      if(inlineReturn){const count=Number.isInteger(inlineReturn.repeated_count)&&inlineReturn.repeated_count>1?inlineReturn.repeated_count:0;this.returnReason.dataset.edgeId=inlineReturn.id;this.returnReason.dataset.tone=edgeTone(inlineReturn);this.returnReason.textContent='↶ '+(inlineReturn.reason_label||'Уточнення')+(count?' ×'+count:'')+(inlineReturns.length>1?' · ще '+(inlineReturns.length-1):'');this.returnReason.title=this.returnReason.textContent;this.returnReason.setAttribute('aria-expanded',String(this.selectedEdge===inlineReturn.id));}
      this.returnReason.hidden=!inlineReturn;this.inlineKey.dataset.hasReturn=String(Boolean(inlineReturn));
      this.newEdges?.clear();for(const [id,b]of this.edgeButtons)if(!shown.has(id)){b.remove();this.edgeButtons.delete(id);}
    }

    drawConcernMarkers(shown,routes){
      const groups=new Map(),occupied=[];
      for(const n of this.graph.nodes){
        const p=this.positions.get(n.id);if(!p||eventNode(n))continue;
        occupied.push({left:p.x-28,right:p.x+28,top:p.y-28,bottom:p.y+24},{left:p.x-53,right:p.x+53,top:p.y+23,bottom:p.y+((n.selection_progress||n.payment_progress||n.consent_progress||n.moderation_view||n.semantic_key==='advertising_entry')?76:this.modal?65:40)});
        if(n.current)occupied.push({left:p.x-23,right:p.x+23,top:p.y-49,bottom:p.y-24});
      }
      for(const n of this.graph.nodes.filter(n=>n.semantic_key==='journey_case')){
        const id=n.presentation_event.anchor_ids.find(id=>this.positions.has(id));if(!id)continue;
        const key=id+'|'+(n.topic==='manager'?'manager':'concern');if(!groups.has(key))groups.set(key,[]);groups.get(key).push(n);
      }
      this.concernControls=new Map();
      for(const [groupKey,cases]of groups){
        const anchorId=cases[0].presentation_event.anchor_ids.find(id=>this.positions.has(id));
        cases.sort((a,b)=>['danger','warning','manager','neutral','recorded','success'].indexOf(caseOutcome(a).tone)-['danger','warning','manager','neutral','recorded','success'].indexOf(caseOutcome(b).tone));
        const anchor=this.positions.get(anchorId),first=cases[0],outcome=caseOutcome(first),source=this.graph.nodes.find(n=>n.id===anchorId);
        // Use only this case's cited route; otherwise attach beside its recorded source.
        const route=cases.flatMap(n=>n.presentation_event.edge_ids).map(id=>routes?.get(id)).find(Boolean);
        const point=window.TwcJourneyGeometry.annotationPoint({anchor,route,occupied,width:this.mapWidth,height:this.mapHeight});if(!point)continue;
        occupied.push({left:point.x-19,right:point.x+19,top:point.y-15,bottom:point.y+15});
        const key='concern:'+groupKey;shown.add(key);let chip=this.edgeButtons.get(key);
        if(!chip){chip=el('button','twc-journey-concern');chip.type='button';chip.addEventListener('click',()=>this.toggleNode(chip.dataset.caseId));this.edgeLayer.append(chip);this.edgeButtons.set(key,chip);}
        chip.dataset.caseId=cases.some(n=>n.id===this.selected)?this.selected:first.id;chip.dataset.tone=outcome.tone;
        chip.dataset.kind=first.topic==='manager'?'manager':'concern';
        chip.textContent=first.topic==='manager'?'?':cases.length>1?'! '+cases.length:outcome.key==='resolved'?'✓':outcome.key==='addressed'?'↩':'!';
        if(first.topic==='manager'){chip.replaceChildren(el('span','',cases.length>1?'? '+cases.length:'?'));}
        chip.style.left=point.x+'px';chip.style.top=point.y+'px';
        chip.title=cases.map(n=>n.label+' · '+caseOutcome(n).label).join('\n');
        chip.setAttribute('aria-label','Уточнення біля «'+(source.short_label||source.label)+'» · '+cases.length+' · '+outcome.label);
        chip.setAttribute('aria-expanded',String(cases.some(n=>n.id===this.selected)));
        cases.forEach(n=>this.concernControls.set(n.id,chip));
        if(!point.onRoute){const line=svg('path',{d:'M'+(anchor.x+(point.x>=anchor.x?23:-23))+' '+anchor.y+' L'+point.x+' '+point.y});line.classList.add('twc-journey-concern-tether');line.dataset.tone=outcome.tone;this.svg.append(line);}
      }
    }
    positionPanel(){
      if(!this.panel)return;const mobile=innerWidth<600;this.panel.dataset.mobile=String(mobile);if(mobile){this.panel.style.left='12px';this.panel.style.right='12px';this.panel.style.bottom='12px';this.panel.style.top='auto';return;}
      this.panel.dataset.cart=String(Boolean(this.graph.nodes.find(n=>n.id===this.selected)?.selection_cart));
      const target=this.selectedEdge?(this.edgeControl(this.selectedEdge)||this.buttons.get(this.selected)):(this.concernControls?.get(this.selected)||this.objectionButtons.get(this.selected)||this.detailButtons.get(this.selected)||this.buttons.get(this.selected));if(!target)return;const r=target.getBoundingClientRect(),w=Math.min(this.panel.dataset.cart==='true'?400:340,innerWidth-24);this.panel.style.width=w+'px';this.panel.style.left=Math.max(12,Math.min(innerWidth-w-12,r.left+r.width/2-w/2))+'px';this.panel.style.right='auto';this.panel.style.bottom='auto';const h=this.panel.getBoundingClientRect().height;this.panel.style.top=Math.max(12,Math.min(innerHeight-h-12,r.bottom+10))+'px';
    }
    destroy(){this.stopWalk();this.closeMap();this.destroyed=true;this.sequence++;clearInterval(this.timerInterval);cancelAnimationFrame(this.frame);this.resize.disconnect();document.removeEventListener('visibilitychange',this.visibility);document.removeEventListener('pointerdown',this.outside);window.removeEventListener('scroll',this.reposition,true);this.root.remove();}
  }
  window.TwcJourney={create:options=>new Journey(options)};
})();
