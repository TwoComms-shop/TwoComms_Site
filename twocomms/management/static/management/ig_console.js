/* Metadata-only bounded console. Root wiring owns endpoint and capabilities. */
(function(global){
  'use strict';
  const SCHEMA=1,DOM_CAP=400,PAGES_PER_TICK=5;
  const KINDS={inbound_received:'Вхідне повідомлення',source_updated:'Джерело оновлено',payment_updated:'Стан оплати',decision_completed:'Рішення',reply_sent:'Відповідь надіслано',reply_failed:'Помилка відповіді',provider_attempt:'Спроба провайдера',manager_action:'Дія менеджера',manager_notification:'Сповіщення менеджеру',memory_updated:'Памʼять оновлено',error:'Помилка',cron:'Періодичне завдання',health:'Спостереження стану',worker_state:'Стан обробника',legacy_event:'Подія без структурованих даних'};
  const REASONS=new Set(['accepted', 'bot_paused', 'cancelled', 'completed', 'deadline', 'delivery_unknown', 'failed', 'healthy', 'lease_busy', 'legacy_unstructured', 'maintenance', 'manager_owned', 'manager_takeover', 'no_reply', 'observation_unavailable', 'owner_changed', 'partial_delivery', 'pending', 'permission_changed', 'processing', 'provider_error', 'provider_timeout', 'provider_unavailable', 'quota_cooldown', 'quota_denied', 'quota_exhausted', 'recovered', 'retry_deferred', 'sent', 'source_admission_denied', 'source_admission_unavailable', 'source_changed', 'source_erased', 'source_scope_changed', 'stalled', 'unknown']);
  const TASKS=new Set(['analysis','reply_recovery','conversation_refresh','journey_trace_refresh','permission_transition','inbox_refresh','checkout_lifecycle','follow_intelligence','typed_memory','memory_summary','instagram','instagram_daemon','instagram_periodic','manager_notifications']);
  const SCOPE_IDS=['client_id','revision_id','message_id','attempt_id','notification_id'];
  const iso=value=>value===null||typeof value==='string'&&/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})$/.test(value);
  const CATEGORIES=['all','client','decision','manager','errors','routine','unknown'];
  const LEVELS=['debug','info','success','warning','error'];
  const REASON_LABELS={accepted:'Прийнято',bot_paused:'Відповіді призупинено',cancelled:'Скасовано',completed:'Завершено',deadline:'Час очікування вичерпано',delivery_unknown:'Результат доставки невідомий',failed:'Не вдалося виконати',healthy:'Є свіжий поступ',lease_busy:'Інший процес уже працює',legacy_unstructured:'Історична подія без метаданих',maintenance:'Технічна пауза',manager_owned:'У роботі менеджера',manager_takeover:'Керування передано менеджеру',no_reply:'Відповідь не потрібна',observation_unavailable:'Спостереження недоступне',owner_changed:'Відповідальний процес змінився',partial_delivery:'Доставлено частково',pending:'Очікує',permission_changed:'Дозвіл змінився',processing:'Виконується',provider_error:'Помилка провайдера',provider_timeout:'Провайдер не підтвердив результат вчасно',provider_unavailable:'Провайдер недоступний',quota_cooldown:'Очікуємо відновлення квоти',quota_denied:'Допуск за квотою відхилено',quota_exhausted:'Квоту вичерпано',recovered:'Відновлено',retry_deferred:'Повтор відкладено',sent:'Доставлено',source_admission_denied:'Допуск джерела змінився',source_admission_unavailable:'Допуск джерела недоступний',source_changed:'Джерело змінилося',source_erased:'Джерело видалено',source_scope_changed:'Область джерела змінилася',stalled:'Поступ не підтверджено',unknown:'Причину не встановлено'};
  const SCOPE_LABELS={client_id:'Клієнт',revision_id:'Ревізія',message_id:'Повідомлення',attempt_id:'Спроба',notification_id:'Сповіщення',request_ref:'Запит',task_key:'Процес'};
  function validId(value,zero){return Number.isSafeInteger(value)&&value>=(zero?0:1);}
  function validate(data){
    if(!data||data.schema_version!==SCHEMA||!Array.isArray(data.items)||data.items.length>120||!validId(data.next_after_id,true)||typeof data.has_more!=='boolean'||typeof data.retention_gap!=='boolean'||!data.range||!data.retention||!['allowed','denied'].includes(data.access))return false;
    if(![data.range.oldest_available_id,data.range.newest_available_id].every(value=>value===null||validId(value,false))||!(data.range.retained_rows===null||validId(data.range.retained_rows,true))||!(data.retention.target_rows===null||validId(data.retention.target_rows,false)))return false;
    return data.items.every(item=>item&&validId(item.id,false)&&Object.hasOwn(KINDS,item.kind)&&LEVELS.includes(item.level)&&REASONS.has(item.reason)&&iso(item.last_at)&&item.scope&&typeof item.scope==='object'&&!Array.isArray(item.scope)&&Object.entries(item.scope).every(([key,value])=>SCOPE_IDS.includes(key)?validId(value,false):key==='request_ref'?typeof value==='string'&&/^greq_[a-f0-9]{20}$/.test(value):key==='task_key'&&TASKS.has(value))&&Array.isArray(item.ids)&&item.ids.length<=120&&item.ids.every(id=>validId(id,false))&&validId(item.count,false)&&item.count===item.ids.length&&item.id===item.ids[item.ids.length-1]);
  }
  function safeFilters(raw){
    raw=raw&&typeof raw==='object'?raw:{};
    const client=String(raw.client_id||'');
    const validClient=/^[1-9][0-9]{0,15}$/.test(client);
    return {category:CATEGORIES.includes(raw.category)?raw.category:'all',reason:REASONS.has(raw.reason)?raw.reason:'',client_id:validClient?client:'',include_routine:raw.include_routine===true,invalid:raw.invalid===true||Boolean(client&&!validClient)};
  }
  function create(options){
    const container=options.container;
    if(!container)throw new Error('console_container_missing');
    const endpoint=new URL(options.endpoint,global.location.href);
    if(endpoint.origin!==global.location.origin||!['http:','https:'].includes(endpoint.protocol))throw new Error('console_endpoint_invalid');
    const allowed=options.enabled===true;
    const storageKey=options.storageKey||'ig-console.filters.v1';
    let filters=safeFilters(options.filters),cursor=0,paused=false,loading=false,destroyed=false,generation=0,timer=0,freshTimer=0,controller=null,lastSnapshot=null,lastSuccess=null,streamIssue='',failure=false,explicitRefreshGeneration=null;
    const seen=new Set();
    try{const saved=global.localStorage.getItem(storageKey);if(saved)filters=safeFilters(JSON.parse(saved));}catch(_error){}
    function updateStatus(){
      const age=lastSuccess===null?null:Math.max(0,Math.floor((Date.now()-lastSuccess)/1000));
      const text=!allowed?'Консоль недоступна за поточними правами':(paused?'Пауза · ':'')+(failure?'Оновлення недоступне · ':'')+(explicitRefreshGeneration===generation?'Оновлення стрічки очікує наступного читання':age===null?'Дані ще не отримано':'Останнє успішне читання '+age+' с тому')+(streamIssue?' · '+streamIssue:'');
      if(options.statusElement)options.statusElement.textContent=text;
      if(options.pauseButton){options.pauseButton.textContent=paused?'Продовжити':'Пауза';options.pauseButton.setAttribute('aria-pressed',String(paused));options.pauseButton.disabled=!allowed;}
      if(options.onState)options.onState({paused,cursor,loading,lastSnapshot,ageSeconds:age,failure,streamIssue});
    }
    function schedule(delay){if(timer)global.clearTimeout(timer);if(options.autoPoll!==false&&!destroyed&&!paused&&allowed&&!global.document.hidden)timer=global.setTimeout(()=>drain(),delay);}
    function row(item){
      const div=global.document.createElement('div');div.className='bot-line '+item.level;div.dataset.consoleId=String(item.id);
      const time=global.document.createElement('time');time.className='t';time.textContent=item.last_at?new Date(item.last_at).toLocaleTimeString('uk-UA',{hour:'2-digit',minute:'2-digit',second:'2-digit'}):'—';if(item.last_at){time.setAttribute('datetime',item.last_at);time.setAttribute('title',item.last_at);}
      const kind=global.document.createElement('span');kind.className='e';kind.textContent=KINDS[item.kind]+(item.count>1?' ×'+item.count:'');
      const meta=global.document.createElement('span');meta.className='d';
      const scope=Object.entries(item.scope).filter(([key,value])=>['client_id','revision_id','message_id','attempt_id','notification_id','request_ref','task_key'].includes(key)&&(validId(value,false)||typeof value==='string'&&/^(greq_[a-f0-9]{20}|[a-z][a-z0-9_]{0,63})$/.test(value))).map(([key,value])=>SCOPE_LABELS[key]+': '+String(value));
      meta.textContent=REASON_LABELS[item.reason]||REASON_LABELS.unknown;
      if(scope.length){const details=global.document.createElement('details');details.className='bot-console-source';const summary=global.document.createElement('summary');summary.textContent='Джерело';const source=global.document.createElement('small');source.textContent=scope.join(' · ');details.append(summary,source);meta.append(details);}
      div.append(time,kind,meta);return div;
    }
    function append(data){
      const atBottom=container.scrollHeight-container.scrollTop-container.clientHeight<40;
      if(data.items.length)Array.from(container.children).filter(child=>child.dataset.consoleEmpty).forEach(child=>container.removeChild(child));
      data.items.forEach(item=>{if(item.ids.some(id=>seen.has(id)))return;item.ids.forEach(id=>seen.add(id));container.appendChild(row(item));});
      if(!container.children.length){const empty=global.document.createElement('div');empty.className='bot-console-empty';empty.dataset.consoleEmpty='true';empty.textContent='За цими фільтрами нових подій немає.';container.appendChild(empty);}
      const beforeTrimHeight=container.scrollHeight;
      while(container.children.length>DOM_CAP)container.removeChild(container.firstChild);
      const removedHeight=beforeTrimHeight-container.scrollHeight;
      if(removedHeight){streamIssue='На екрані до '+DOM_CAP+' груп; старі рядки прибрано';if(!atBottom)container.scrollTop=Math.max(0,container.scrollTop-removedHeight);}
      if(atBottom)container.scrollTop=container.scrollHeight;
      // Dedup is bounded independently of total session length.
      if(seen.size>DOM_CAP*120){seen.clear();}
      if(data.retention_gap)streamIssue='Пропуск до найстарішого доступного ID; кількість втрачених подій невідома';
      if(data.gap_reason==='cursor_ahead_of_available_stream')streamIssue='Курсор поза доступною стрічкою; потрібне явне оновлення';
      if(options.rangeElement){const range=data.range;options.rangeElement.textContent='Доступні ID: '+String(range.oldest_available_id??'—')+'…'+String(range.newest_available_id??'—')+' · збережено '+String(range.retained_rows??'невідомо')+' · орієнтир збереження '+String(data.retention.target_rows??'невідомий');}
    }
    function url(captureConsole){const result=new URL(endpoint);result.searchParams.set('include_console',captureConsole?'1':'0');result.searchParams.set('after_id',String(cursor));result.searchParams.set('category',filters.category);if(filters.client_id)result.searchParams.set('client_id',filters.client_id);else result.searchParams.delete('client_id');if(filters.reason)result.searchParams.set('reason',filters.reason);else result.searchParams.delete('reason');result.searchParams.set('include_routine',filters.include_routine?'1':'0');return result;}
    async function drain(){
      // The outer status loop is the sole owner. Leave an explicit refresh
      // pending if its old, aborted poll has not finished yet.
      if(loading||destroyed)return false;
      const explicitRefresh=explicitRefreshGeneration===generation;
      if((global.document.hidden||paused&&!explicitRefresh||!allowed)&&options.statusPolling!==true)return false;
      if(filters.invalid){failure=true;streamIssue='Вкажіть додатній числовий ID клієнта';updateStatus();if(options.statusPolling!==true)return false;}
      const captureConsole=allowed&&(!paused||explicitRefresh)&&!global.document.hidden&&!filters.invalid;
      // A failed/denied explicit read is not silently retried by a status tick.
      explicitRefreshGeneration=null;
      loading=true;const capturedGeneration=generation;controller=new AbortController();let more=false;
      try{
        for(let page=0;page<PAGES_PER_TICK;page++){
          const response=await global.fetch(url(captureConsole),{method:'GET',credentials:'same-origin',headers:{Accept:'application/json','X-Requested-With':'XMLHttpRequest'},signal:controller.signal,cache:'no-store'});
          const body=await response.json();const data=body&&body.console?body.console:body;
          if(destroyed||controller.signal.aborted||capturedGeneration!==generation||captureConsole&&paused&&!explicitRefresh)return false;
          if(!response.ok||body.success===false||!validate(data))throw new Error('console_snapshot_invalid');
          if(!captureConsole){if(options.onSnapshot)options.onSnapshot(body);updateStatus();return true;}
          if(data.access!=='allowed')throw new Error('console_access_denied');
          if(data.next_after_id<cursor||data.items.some(item=>item.ids.some(id=>id<=cursor||id>data.next_after_id)))throw new Error('console_cursor_regressed');
          append(data);const previous=cursor;cursor=data.next_after_id;lastSnapshot=data;lastSuccess=Date.now();failure=false;more=data.has_more;
          if(options.onSnapshot)options.onSnapshot(body);
          updateStatus();
          if(!more)break;
          if(cursor===previous)throw new Error('console_cursor_stalled');
        }
        return true;
      }catch(error){if(!destroyed&&capturedGeneration===generation&&!controller.signal.aborted&&error.name!=='AbortError'){failure=true;updateStatus();}return false;}
      finally{loading=false;controller=null;updateStatus();schedule(more?50:Math.max(1000,Number(options.pollInterval||5000)));}
    }
    function setPaused(value){paused=value===true;if(paused){if(timer)global.clearTimeout(timer);if(controller)controller.abort();}else schedule(0);updateStatus();}
    function setFilters(value){filters=safeFilters(value);generation++;explicitRefreshGeneration=null;cursor=0;seen.clear();container.replaceChildren();lastSnapshot=null;lastSuccess=null;streamIssue='';failure=false;if(controller)controller.abort();try{global.localStorage.setItem(storageKey,JSON.stringify(filters));}catch(_error){}updateStatus();schedule(0);}
    function refresh(){setFilters(filters);explicitRefreshGeneration=generation;updateStatus();}
    function togglePause(){setPaused(!paused);}
    function visibility(){if(global.document.hidden){if(timer)global.clearTimeout(timer);if(controller)controller.abort();}else schedule(0);}
    if(options.pauseButton)options.pauseButton.addEventListener('click',togglePause);
    global.document.addEventListener('visibilitychange',visibility);
    freshTimer=global.setInterval(updateStatus,1000);updateStatus();schedule(0);
    return {poll:drain,pause:()=>setPaused(true),resume:()=>setPaused(false),refresh,setFilters,getState:()=>({cursor,paused,loading,filters:{...filters},lastSnapshot}),destroy:()=>{destroyed=true;if(controller)controller.abort();if(timer)global.clearTimeout(timer);if(freshTimer)global.clearInterval(freshTimer);if(options.pauseButton)options.pauseButton.removeEventListener('click',togglePause);global.document.removeEventListener('visibilitychange',visibility);}};
  }
  const api={create,validate,safeFilters,reasonLabel:code=>REASON_LABELS[code]||REASON_LABELS.unknown};global.IgConsole=api;
  if(typeof module!=='undefined'&&module.exports)module.exports=api;
})(typeof window!=='undefined'?window:globalThis);

/* Passive, actor-independent overview. The existing status poll owns updates. */
(function(global){
  'use strict';
  const LANES={customer_revisions:'Відповіді клієнтам',legacy_inbound:'Вхідні повідомлення',legacy_outbound:'Доставка відповідей',revision_delivery:'Квитанції доставки',manager_notifications:'Сповіщення менеджеру',conversation_analysis:'Аналіз діалогів',analysis_materialization:'Запис результатів аналізу',reply_recovery:'Відновлення відповідей',binotel_analysis:'Аналіз дзвінків',typed_memory:'Структурована памʼять',trace_refresh:'Оновлення воронки'};
  const TASKS={ig_checkout_reconcile:'Звірка checkout',ig_order_fulfillment:'Події замовлень',ig_deal_payments:'Звірка оплат',order_telegram_reconcile:'Картки замовлень у Telegram',nova_poshta_tracking:'Статуси Нової Пошти',ig_typed_memory_reconcile:'Звірка памʼяті',ig_trace_refresh:'Оновлення воронки',binotel_call_ai_analyses:'Аналіз дзвінків'};
  const STATES={healthy:'Є свіжий поступ',observed:'Є локальне спостереження',attention:'Потребує уваги',stalled:'Поступ не підтверджено',disabled:'Вимкнено',unavailable:'Дані недоступні',coverage_incomplete:'Покриття неповне',unobserved:'Ще не спостерігали',stale:'Застаріле спостереження',failed:'Помилка',degraded:'Обмежено',not_observed:'Немає спостереження',running:'Виконується',maintenance:'Технічна пауза',pause_pending:'Пауза застосовується',worker_stalled:'Поступ не підтверджено',starting:'Запускається',idle:'Очікує',available_assumed:'Доступність припускається',confirmed_recent_success:'Є недавній успіх генерації',local_validation_failed:'Чернетку відхилила локальна перевірка',accounting_unknown:'Облік невідомий',not_configured:'Не налаштовано',rpm_limited:'Обмежено RPM',tpm_limited:'Обмежено TPM',rpd_exhausted_until_reset:'Денна квота вичерпана',auth_failed:'Авторизація не підтверджена',model_unavailable_for_project:'Модель недоступна',provider_degraded:'Провайдер обмежений',in_flight:'Запит виконується'};
  const SLOTS={gslot_7f3a:'A',gslot_c921:'B',gslot_18de:'C',gslot_a604:'D',gslot_52bb:'E',gslot_e17c:'F'};
  const MODELS=new Set(['gemini-3.8-flash','gemini-3.7-flash','gemini-3.6-flash','gemini-3.5-flash','gemini-3.5-flash-lite']);
  const ROUTES={no_model:'Без генерації',ordinary_live:'Відповідь наживо',complex_live:'Складний запит',durable_analysis:'Фоновий аналіз'};
  const object=value=>value&&typeof value==='object'&&!Array.isArray(value)?value:{};
  const number=value=>typeof value==='number'&&Number.isFinite(value)&&value>=0?value:null;
  const count=value=>number(value)===null?'—':new Intl.NumberFormat('uk-UA').format(value);
  const state=value=>STATES[value]||'Стан невідомий';
  const age=value=>number(value)===null?'час невідомий':value<60?Math.round(value)+' с':value<3600?Math.round(value/60)+' хв':Math.round(value/3600)+' год';
  const model=value=>MODELS.has(value)?value.replace('gemini-','Gemini ').replace('-flash-lite',' Flash Lite').replace('-flash',' Flash'):'Модель невідома';
  function node(tag,className,text){const element=global.document.createElement(tag);if(className)element.className=className;if(text!==undefined)element.textContent=text;return element;}
  function render(container,payload){
    if(!container)return false;
    const opened=new Set(Array.from(container.querySelectorAll('details[open][data-overview-section]')).map(row=>row.dataset.overviewSection));
    const active=global.document.activeElement,focused=active&&active.closest?active.closest('[data-overview-section]'):null;
    const focusKey=focused&&container.contains(focused)?focused.dataset.overviewSection:null;
    const section=(key,title)=>{const details=node('details','bot-overview-section');details.dataset.overviewSection=key;details.open=opened.has(key);details.append(node('summary','',title));const body=node('div','bot-overview-section-body');details.append(body);return {details,body};};
    if(!payload||payload.schema_version!=='ig-overview.v1'||payload.available===false){container.replaceChildren(node('p','bot-hint','Операційний огляд недоступний за поточними правами або джерелом.'));return false;}
    const components=object(payload.components),cards=node('div','bot-overview-cards');
    const component=key=>object(components[key]);const data=key=>object(component(key).data);
    const fresh=key=>component(key).available===true&&component(key).freshness==='fresh';
    const stamp=key=>component(key).available===true?'Спостереження '+age(component(key).observation_age_seconds)+' тому'+(fresh(key)?'':' · застаріле'):'Спостереження недоступне';
    const daemon=data('daemon'),health=node('article','bot-overview-observation');
    health.append(node('span','bot-overview-eyebrow','Робочий цикл'),node('strong','',!fresh('daemon')?'Поступ не підтверджено':daemon.process_online&&daemon.main_healthy?'Є свіжий поступ':daemon.process_online?'Процес онлайн, поступ відсутній':'Процес не підтверджено'),node('p','bot-hint',stamp('daemon')));
    const attention=data('attention'),human=object(attention.human_unknown_cases),debt=node('article','bot-overview-observation'+(attention.required===true?' needs-attention':''));
    debt.append(node('span','bot-overview-eyebrow','Потрібна людина'),node('strong','',component('attention').available!==true?'Обсяг невідомий':attention.required===true?'Є відкриті випадки':'Зафіксованих випадків немає'),node('p','bot-hint','Борг відповіді: '+count(attention.count)+' · доставка з невідомим результатом: '+count(human.count)));
    if(attention.required===true)debt.append(node('p','bot-hint','Перевірте відповідь або квитанцію; очікування '+age(attention.waiting_age_seconds)+'.'));
    debt.append(node('small','bot-hint','Оплати й післяпродажні випадки мають окремий облік. '+stamp('attention')));cards.append(health,debt);
    const fragments=[cards],queues=section('queues','Черги та відповідальні процеси');
    if(component('lanes').available!==true)queues.body.append(node('p','bot-hint','Не вдалося прочитати черги. Це не означає нуль роботи.'));
    for(const [key,row] of Object.entries(object(data('lanes').lanes))){if(!Object.hasOwn(LANES,key))continue;const values=object(row),counts=object(values.counts),line=node('div','bot-overview-owner-row');
      line.append(node('strong','',LANES[key]),node('span','',state(values.state)),node('small','bot-hint','До виконання: '+count(counts.runnable)+' у вибірці'+(values.has_more?' · є ще':'')+' · у роботі: '+count(counts.processing)+' · вручну: '+count(counts.manual)+' · найстаріше очікування '+age(values.oldest_runnable_age_seconds)),node('small','bot-hint',values.attention_total_exact?'Потребують уваги: '+count(values.attention_total)+' (точний облік)':'Оцінка ризику обмежена вибіркою'));queues.body.append(line);}
    const tasks=data('tasks').tasks;if(Array.isArray(tasks))for(const row of tasks.slice(0,8)){if(!Object.hasOwn(TASKS,row.key))continue;const line=node('div','bot-overview-owner-row');line.append(node('strong','',TASKS[row.key]),node('span','',state(row.state)),node('small','bot-hint','Останній поступ '+age(row.age_seconds)+' тому · послідовних помилок '+count(row.consecutive_failures)));queues.body.append(line);}
    const memory=data('memory_generation'),memoryCounts=object(memory.counts);queues.body.append(node('div','bot-overview-owner-row',component('memory_generation').available!==true?'Памʼять: дані черги недоступні':'Памʼять: '+(memory.generation_enabled&&memory.provider_admission_accepted?'допуск перевіряється перед роботою':'автоматична генерація вимкнена')+' · до перевірки допуску '+count(memoryCounts.due_unclaimed)+' · у роботі '+count(memoryCounts.processing)));
    fragments.push(queues.details);
    const capacity=section('capacity','Gemini: поточні маршрути та локальний облік');capacity.body.append(node('p','bot-hint','Вікна рахуються окремо за проєктом і моделлю. Метадані та нульове використання не підтверджують генерацію; ці значення не дозволяють HTTP-виклик.'));
    if(component('routes').available===true&&Array.isArray(data('routes').routes))for(const row of data('routes').routes){if(!Object.hasOwn(ROUTES,row.task_class))continue;const chain=Array.isArray(row.effective_chain)?row.effective_chain.filter(value=>MODELS.has(value)):[];capacity.body.append(node('p','bot-overview-route',ROUTES[row.task_class]+': '+(chain.map(model).join(' → ')||'Без моделі')));}
    const quotas=data('quotas');capacity.body.append(node('p','bot-hint','Non-live: '+(quotas.nonlive_enforcement_active===true?'enforce активний':quotas.nonlive_admission_mode==='shadow'?'shadow — допуск не підтверджено':'enforce не підтверджено')+' · '+stamp('quotas')));
    if(component('quotas').available!==true)capacity.body.append(node('p','bot-hint','Локальний облік недоступний.'));
    const projects=Array.isArray(quotas.projects)?quotas.projects:[];
    for(const pair of projects.slice(0,30)){if(!Object.hasOwn(SLOTS,pair.slot_id)||!MODELS.has(pair.model))continue;const tpm=object(pair.input_tpm),binding=object(pair.nonlive_profile),row=node('div','bot-overview-owner-row');
      row.append(node('strong','',model(pair.model)+' · проєкт '+SLOTS[pair.slot_id]),node('span','',state(pair.status)),node('small','bot-hint','RPM '+count(object(pair.rpm).used)+' / '+count(object(pair.rpm).limit)+' · RPD '+count(object(pair.rpd).used)+' / '+count(object(pair.rpd).limit)+' · TPM '+count(tpm.used)+' / '+count(tpm.limit)),node('small','bot-hint',tpm.headroom_known===true?'Локальний залишок TPM: '+count(tpm.remaining):'Залишок TPM невідомий: калібрування або привʼязку профілю не підтверджено.'));
      row.append(node('small','bot-hint',pair.generation_evidence_present===true?'Останній фактичний виклик '+age(pair.last_generation_age_seconds)+' тому':'Фактична генерація в цьому спостереженні не підтверджена.'));capacity.body.append(row);}
    fragments.push(capacity.details);container.replaceChildren(...fragments);
    if(focusKey){const details=Array.from(container.querySelectorAll('[data-overview-section]')).find(row=>row.dataset.overviewSection===focusKey);const summary=details&&details.querySelector('summary');if(summary)summary.focus({preventScroll:true});}
    return true;
  }
  global.IgOverview={render};
})(typeof window!=='undefined'?window:globalThis);
