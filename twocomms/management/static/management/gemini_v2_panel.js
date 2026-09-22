(function(){
  'use strict';

  const root=document.getElementById('gemini-v2-panel');
  if(!root){
    window.GeminiV2Panel={load:function(){return Promise.resolve(false);},syncTimers:function(){}};
    return;
  }

  const EXPECTED_SCHEMA=Number(root.dataset.schemaVersion||0);
  const MODEL_ORDER=['gemini-3.8-flash','gemini-3.7-flash','gemini-3.6-flash','gemini-3.5-flash','gemini-3.5-flash-lite'];
  const SLOT_ORDER=['gslot_7f3a','gslot_c921','gslot_18de','gslot_a604','gslot_52bb','gslot_e17c'];
  const ROUTE_ORDER=['no_model','ordinary_live','complex_live','durable_analysis'];
  const VIEW_ORDER=['quotas','routes','attempts'];
  const MAX_RENDERED_ATTEMPTS=100;
  const MODEL_LABELS={
    'gemini-3.8-flash':'Gemini 3.8 Flash',
    'gemini-3.7-flash':'Gemini 3.7 Flash',
    'gemini-3.6-flash':'Gemini 3.6 Flash',
    'gemini-3.5-flash':'Gemini 3.5 Flash',
    'gemini-3.5-flash-lite':'Gemini 3.5 Flash Lite',
    unknown:'Модель невідома',
  };
  const LANE_LABELS={
    live:'Відповідь наживо',analysis:'Фоновий аналіз',recovery:'Відновлення',holding:'Утримання',
    followup:'Повторний контакт',call:'Виклик',checker:'Перевірка',diagnostic:'Діагностика',
    metadata_probe:'Діагностика метаданих',unknown:'Лінія невідома',
  };
  const TASK_LABELS={
    no_model:'Без моделі',ordinary_live:'Звичайна відповідь наживо',complex_live:'Складна відповідь наживо',
    durable_analysis:'Довгий аналіз',diagnostic:'Діагностика',unknown:'Клас невідомий',
  };
  const ROUTE_PRESENTATION={
    no_model:{
      title:'Без моделі',
      definition:'Відповідь або дія повністю визначена перевіреними даними бекенда; генерація Gemini не потрібна.',
    },
    ordinary_live:{
      title:'Звичайна відповідь наживо',
      definition:'Факти бекенда повні; Gemini лише формулює коротку відповідь клієнту.',
    },
    complex_live:{
      title:'Складна відповідь наживо',
      definition:'Неоднозначність або медіа впливають на вибір товару, конфігурацію чи крок воронки.',
    },
    durable_analysis:{
      title:'Фоновий довгий аналіз',
      definition:'Фоновий CRM-аналіз зберігає структуровані пропозиції й не затримує відповідь клієнту.',
    },
  };
  const STATUS_LABELS={
    confirmed_recent_success:'Підтверджений недавній успіх',available_assumed:'Доступність лише припускається',
    in_flight:'Запит виконується',rpm_limited:'RPM тимчасово обмежено',tpm_limited:'TPM тимчасово обмежено',
    rpd_exhausted_until_reset:'RPD вичерпано до скидання',provider_degraded:'Провайдер деградував',
    auth_failed:'Помилка авторизації',model_unavailable_for_project:'Модель недоступна для проєкту',
    not_configured:'Проєкт не налаштовано',accounting_unknown:'Облік невідомий',
  };
  const EXECUTION_LABELS={
    attempted:'Спроба виконана',not_attempted:'Не викликано',pending:'Очікує',not_recorded:'Не зафіксовано',
  };
  const FSM_LABELS={
    planned:'Заплановано',reserved:'Резерв створено',provider_started:'Провайдер викликаний',
    succeeded:'Успіх',succeeded_late:'Пізній успіх',failed:'Помилка',
    timeout_ambiguous:'Таймаут · результат невідомий',cancelled_pre_dispatch:'Скасовано до відправлення',unknown:'Стан невідомий',
  };
  const FAILURE_LABELS={
    blocked:'Заблоковано',empty:'Порожня відповідь',forbidden:'Доступ заборонено',http_408:'HTTP 408',
    http_5xx:'HTTP 5xx',invalid_key:'Ключ відхилено',invalid_payload:'Некоректні вхідні дані',
    invalid_response:'Некоректна відповідь',lease_busy:'Ресурс зайнятий',malformed_response:'Пошкоджена відповідь',
    model_not_found:'Модель не знайдена',model_overload:'Модель перевантажена',model_unavailable:'Модель недоступна',
    overload:'Перевантаження',permission_denied:'Немає дозволу',provider_error:'Помилка провайдера',
    provider_overload:'Провайдер перевантажений',quarantined:'Проєкт у карантині',quota_429:'Квота 429',
    read_timeout:'Таймаут читання',request_error:'Помилка запиту',stale_provider_boundary:'Застаріла межа провайдера',
    transport:'Транспортна помилка',other:'Інша типізована помилка',
  };
  const SKIP_LABELS={
    circuit_open:'Контур захисту відкритий',deadline:'Дедлайн вичерпано',duplicate_credential:'Дублікат облікових даних',
    duplicate_project:'Дублікат проєкту',fatal_payload:'Фатальна помилка вхідних даних',lease_busy:'Ресурс зайнятий',
    model_overload:'Модель перевантажена',model_terminal:'Модель зупинила ланцюг',model_unavailable:'Модель недоступна',
    not_available_plan:'Немає доступного плану',policy_stop:'Зупинка політикою',quarantine:'Карантин',
    quota_cooldown:'Період очікування квоти',quota_exhausted:'Квоту вичерпано',sla_model_budget:'Бюджет SLA вичерпано',
    unconfigured:'Не налаштовано',winner_found:'Переможця вже знайдено',
  };
  const RESOLUTION_LABELS={succeeded:'Завершено успішно',failed:'Завершено помилкою',pending:'Ще виконується',other:'Результат невідомий'};
  const RESOLUTION_REASON_LABELS={
    deadline:'вичерпано дедлайн',exhausted:'вичерпано кандидатів',fatal_payload:'фатальна помилка вхідних даних',
    model_terminal:'модель завершила ланцюг',model_unavailable:'модель недоступна',no_candidates:'немає кандидатів',
    provider_success:'успішна відповідь провайдера',quota_cooldown:'період очікування квоти',
    quota_exhausted:'квоту вичерпано',sla_model_budget:'вичерпано бюджет SLA',winner_found:'переможця зафіксовано',
  };
  const REPLY_LABELS={
    persisted:'Відповідь збережена',not_linked:'Відповідь не прив’язана',missing:'Пов’язаний запис не знайдено',
    link_conflict:'Конфлікт зв’язку відповіді',
  };
  const SEND_STATE_LABELS={
    sent:'надіслано',sending:'надсилається',failed:'не надіслано',cancelled:'скасовано',
    duplicate:'дубль не надсилався',unknown:'результат доставки невідомий','':'стан доставки відсутній',
  };
  const MESSAGE_STATUS_LABELS={
    done:'запис завершено',failed:'запис із помилкою',pending:'запис очікує',processing:'запис обробляється',
    unknown:'стан запису невідомий','':'стан запису відсутній',
  };
  const ACCOUNTING_MODE_LABELS={
    off:'вимкнений',shadow:'тіньовий',enforced:'примусовий',emergency:'аварійний',invalid:'невідомий',unknown:'невідомий',
  };
  const PROBE_STATUS_LABELS={
    ok:'Метадані доступні',metadata_ok:'Метадані доступні',invalid_key:'Ключ відхилено',permission_denied:'Немає дозволу',
    model_not_found:'Модель не знайдена',quota_429:'Провайдер повернув 429',read_timeout:'Таймаут',
    transport:'Транспортна помилка',provider_error:'Помилка провайдера',invalid_response:'Некоректна відповідь',
  };

  const tabs=VIEW_ORDER.map(name=>root.querySelector('[data-gemini-tab="'+name+'"]'));
  const views=Object.fromEntries(VIEW_ORDER.map(name=>[name,root.querySelector('[data-gemini-view="'+name+'"]')]));
  const content={
    quotas:document.getElementById('gemini-v2-quotas-content'),
    routes:document.getElementById('gemini-v2-routes-content'),
    attempts:document.getElementById('gemini-v2-attempts-content'),
  };
  const statusRegion=document.getElementById('gemini-v2-status');
  const notice=document.getElementById('gemini-v2-notice');
  const refreshButton=document.getElementById('gemini-v2-refresh');
  const policyValue=document.getElementById('gemini-v2-policy');
  const accountingValue=document.getElementById('gemini-v2-accounting');
  const snapshotValue=document.getElementById('gemini-v2-snapshot');
  const moreWrap=document.getElementById('gemini-v2-attempts-more');
  const moreButton=document.getElementById('gemini-v2-load-more');
  const attemptsError=document.getElementById('gemini-v2-attempts-error');
  const probeButton=document.getElementById('gemini-v2-probe');
  const probeProject=document.getElementById('gemini-v2-probe-project');
  const probeModel=document.getElementById('gemini-v2-probe-model');
  const probeResult=document.getElementById('gemini-v2-diagnostic-result');
  const urls={
    quotas:sameOriginUrl(root.dataset.quotasUrl),
    routes:sameOriginUrl(root.dataset.routesUrl),
    attempts:sameOriginUrl(root.dataset.attemptsUrl),
    probe:sameOriginUrl(root.dataset.probeUrl),
  };
  const state={
    active:'quotas',
    snapshots:{quotas:null,routes:null,attempts:null},
    attemptsItems:[],
    nextCursor:null,
    controllers:{quotas:null,routes:null,attempts:null,probe:null},
    refreshTimer:0,
    lastRequestAt:{quotas:0,routes:0,attempts:0},
  };
  const refreshInterval=Math.max(30000,Number(root.dataset.refreshInterval||60000));

  function sameOriginUrl(value){
    try{
      const parsed=new URL(String(value||''),window.location.origin);
      return parsed.origin===window.location.origin?parsed.href:'';
    }catch(_error){return '';}
  }

  function node(tag,className,text){
    const element=document.createElement(tag);
    if(className)element.className=className;
    if(text!==undefined)element.textContent=String(text);
    return element;
  }

  function isObject(value){return Boolean(value)&&typeof value==='object'&&!Array.isArray(value);}
  function isNumberOrNull(value){return value===null||(typeof value==='number'&&Number.isFinite(value)&&value>=0);}
  function isSafeModel(value){return MODEL_ORDER.includes(value)||value==='unknown';}
  function isSafeSlot(value){return value===null||SLOT_ORDER.includes(value);}
  function hasExpectedSchema(data){return isObject(data)&&Number(data.schema_version)===EXPECTED_SCHEMA;}
  function validMetric(metric){
    return isObject(metric)&&typeof metric.complete==='boolean'&&['used','limit','remaining','reserved','uncertain'].every(key=>isNumberOrNull(metric[key]));
  }
  function validateQuotas(data){
    if(!hasExpectedSchema(data)||!isObject(data.accounting)||!Array.isArray(data.models)||data.models.length!==MODEL_ORDER.length)return false;
    return data.models.every((model,index)=>{
      if(!isObject(model)||model.model!==MODEL_ORDER[index]||!Array.isArray(model.projects)||model.projects.length!==SLOT_ORDER.length)return false;
      if(!validMetric(model.rpm)||!validMetric(model.input_tpm)||!validMetric(model.rpd)||!isObject(model.coverage)||!isObject(model.latency_ms))return false;
      return model.projects.every((project,projectIndex)=>{
        return isObject(project)&&project.model===MODEL_ORDER[index]&&project.slot_id===SLOT_ORDER[projectIndex]
          &&typeof project.configured==='boolean'&&typeof project.identity_mapping==='string'&&typeof project.status==='string'
          &&validMetric(project.rpm)&&validMetric(project.input_tpm)&&validMetric(project.rpd)
          &&Array.isArray(project.provider_blocks)&&isObject(project.latency_ms);
      });
    });
  }
  function validateRoutes(data){
    if(!hasExpectedSchema(data)||!isObject(data.accounting)||!isObject(data.emergency_pin)||!Array.isArray(data.routes)||data.routes.length!==ROUTE_ORDER.length)return false;
    return data.routes.every((route,index)=>isObject(route)&&route.task_class===ROUTE_ORDER[index]
      &&typeof route.title==='string'&&typeof route.definition==='string'&&Array.isArray(route.base_chain)
      &&Array.isArray(route.effective_chain)&&route.base_chain.every(isSafeModel)&&route.effective_chain.every(isSafeModel)
      &&Array.isArray(route.escalation_chain)&&route.escalation_chain.every(isSafeModel));
  }
  function validPublicAttempt(attempt){
    return isObject(attempt)&&isSafeModel(attempt.model)&&isSafeSlot(attempt.slot_id)
      &&typeof attempt.fsm_state==='string'&&typeof attempt.outcome==='string'&&typeof attempt.winner==='boolean'
      &&(attempt.quota_block===null||isObject(attempt.quota_block));
  }
  function validateAttempts(data){
    if(!hasExpectedSchema(data)||!Array.isArray(data.items)||data.items.length>50||!(data.next_cursor===null||typeof data.next_cursor==='string'))return false;
    return data.items.every(item=>{
      if(!isObject(item)||typeof item.request_ref!=='string'||(item.request_ref&&!/^greq_[a-f0-9]{20}$/.test(item.request_ref)))return false;
      if(!Array.isArray(item.candidate_plan)||!Array.isArray(item.attempts)||!isObject(item.resolution)||!isObject(item.reply))return false;
      if(item.winner!==null&&!validPublicAttempt(item.winner))return false;
      return item.attempts.every(validPublicAttempt)&&item.candidate_plan.every(candidate=>isObject(candidate)
        &&isSafeModel(candidate.model)&&isSafeSlot(candidate.slot_id)&&Array.isArray(candidate.outcomes)&&candidate.outcomes.every(validPublicAttempt));
    });
  }

  function formatNumber(value){return typeof value==='number'&&Number.isFinite(value)?new Intl.NumberFormat('uk-UA').format(value):'—';}
  function formatDate(value){
    if(!value)return '—';
    const date=new Date(value);
    if(Number.isNaN(date.getTime()))return '—';
    return date.toLocaleString('uk-UA',{day:'2-digit',month:'2-digit',year:'2-digit',hour:'2-digit',minute:'2-digit'});
  }
  function formatDuration(ms){
    if(typeof ms!=='number'||!Number.isFinite(ms)||ms<0)return '—';
    if(ms<1000)return Math.round(ms)+' мс';
    return (ms/1000).toLocaleString('uk-UA',{maximumFractionDigits:1})+' с';
  }
  function formatSeconds(seconds){
    if(typeof seconds!=='number'||!Number.isFinite(seconds)||seconds<=0)return '—';
    if(seconds<60)return Math.round(seconds)+' с';
    if(seconds<3600)return Math.round(seconds/60)+' хв';
    return Math.round(seconds/3600)+' год';
  }
  function modelLabel(value){return MODEL_LABELS[value]||MODEL_LABELS.unknown;}
  function projectLabel(slotId){const index=SLOT_ORDER.indexOf(slotId);return index>=0?'Проєкт '+String(index+1):'Проєкт невідомий';}
  function laneLabel(value){return LANE_LABELS[value]||LANE_LABELS.unknown;}
  function taskLabel(value){return TASK_LABELS[value]||TASK_LABELS.unknown;}
  function mapLabel(map,value,fallback){return Object.prototype.hasOwnProperty.call(map,value)?map[value]:fallback;}
  function accountingModeLabel(value){return ACCOUNTING_MODE_LABELS[value]||ACCOUNTING_MODE_LABELS.unknown;}

  function statusTone(status){
    if(status==='confirmed_recent_success'||status==='in_flight')return 'is-live';
    if(['rpm_limited','tpm_limited','available_assumed'].includes(status))return 'is-warning';
    if(['rpd_exhausted_until_reset','provider_degraded','auth_failed','model_unavailable_for_project'].includes(status))return 'is-danger';
    return '';
  }
  function executionTone(value){
    if(value==='attempted')return 'is-live';
    if(value==='pending'||value==='not_recorded')return 'is-warning';
    if(value==='not_attempted')return 'is-danger';
    return '';
  }
  function resolutionTone(value){
    if(value==='succeeded')return 'is-live';
    if(value==='failed')return 'is-danger';
    return 'is-warning';
  }

  function setStatus(message,tone){
    statusRegion.textContent=message||'';
    statusRegion.className='gemini-v2-status'+(tone?' '+tone:'');
  }
  function setNotice(kind,title,copy){
    notice.className='gemini-v2-notice '+(kind||'is-neutral');
    const paragraph=node('p');
    paragraph.append(node('strong','',title));
    if(copy)paragraph.append(document.createTextNode(' '+copy));
    notice.replaceChildren(node('span','gemini-v2-notice-mark'),paragraph);
    notice.firstElementChild.setAttribute('aria-hidden','true');
  }
  function setBusy(viewName,busy){
    const view=views[viewName];
    if(view)view.setAttribute('aria-busy',busy?'true':'false');
    const activeBusy=busy&&state.active===viewName;
    refreshButton.disabled=activeBusy;
    refreshButton.classList.toggle('is-loading',activeBusy);
  }
  function showSkeleton(viewName){
    const shell=content[viewName];
    if(!shell)return;
    const skeleton=node('div','gemini-v2-skeleton');
    const rows=viewName==='routes'?4:3;
    for(let index=0;index<rows;index+=1)skeleton.append(node('div','gemini-v2-skeleton-row'));
    skeleton.setAttribute('aria-label','Завантаження локального зрізу');
    shell.replaceChildren(skeleton);
  }
  function showFatal(viewName,title,copy){
    const empty=node('div','gemini-v2-empty is-error');
    empty.append(node('strong','',title),node('span','',copy));
    content[viewName].replaceChildren(empty);
  }
  function updateSnapshotTime(value){
    snapshotValue.textContent=value?formatDate(value):'—';
    if(value)snapshotValue.setAttribute('datetime',String(value));else snapshotValue.removeAttribute('datetime');
  }
  function updateAccounting(accounting){
    const data=isObject(accounting)?accounting:{};
    const mode=String(data.mode||'unknown');
    const active=Boolean(data.runtime_active);
    accountingValue.textContent=accountingModeLabel(mode)+(active?' · активний':' · неактивний');
    if(Object.prototype.hasOwnProperty.call(ACCOUNTING_MODE_LABELS,mode))accountingValue.title='Технічний режим: '+mode;
    else accountingValue.removeAttribute('title');
    if(!active||!['shadow','enforced','emergency'].includes(mode)){
      setNotice('is-neutral','Облік вимкнений або невідомий.','Показані значення є локальними чи невідомими; «—» не означає нульове використання.');
    }else{
      setNotice('is-live','Локальний облік активний.','Це локальний доказ маршрутизатора, а не зовнішній баланс Google.');
    }
  }
  function updateHeader(data,viewName){
    updateSnapshotTime(data.generated_at);
    if(isObject(data.accounting))updateAccounting(data.accounting);
    if(viewName==='routes')policyValue.textContent=data.policy_version||'—';
  }

  function metricParts(metric){
    if(!isObject(metric)||metric.complete!==true){return {value:'—',detail:'Облік невідомий',unknown:true};}
    const used=formatNumber(metric.used);
    const limit=formatNumber(metric.limit);
    const detail='зал. '+formatNumber(metric.remaining)+' · резерв '+formatNumber(metric.reserved)+' · невизн. '+formatNumber(metric.uncertain);
    return {value:used+' / '+limit,detail:detail,unknown:false};
  }
  function railMetric(label,value,detail,unknown){
    const wrapper=node('div');
    const dd=node('dd',unknown?'is-unknown':'',value);
    wrapper.append(node('dt','',label),dd,node('small','',detail));
    return wrapper;
  }
  function projectMetric(label,metric){
    const parts=metricParts(metric);
    const wrapper=node('div','gemini-v2-project-metric');
    wrapper.append(node('dt','',label),node('dd',parts.unknown?'is-unknown':'',parts.value),node('small','',parts.detail));
    return wrapper;
  }
  function scalarMetric(label,value,detail){
    const known=typeof value==='number'&&Number.isFinite(value);
    return railMetric(label,known?formatNumber(value):'—',known?detail:'Облік невідомий',!known);
  }
  function latencyMetric(label,value){
    const known=typeof value==='number'&&Number.isFinite(value);
    return railMetric(label,known?formatDuration(value):'—',known?'за локальними спробами':'Облік невідомий',!known);
  }

  function counterText(values,labels){
    if(!isObject(values)||!Object.keys(values).length)return 'Немає локальних спроб';
    return Object.entries(values).map(([key,value])=>(labels[key]||'Інше')+': '+formatNumber(value)).join(' · ');
  }
  function evidenceText(evidence){
    if(!isObject(evidence))return 'Немає реального спостереження.';
    const outcome=evidence.success?'успішне':'неуспішне';
    const failure=evidence.failure_kind?' · '+mapLabel(FAILURE_LABELS,evidence.failure_kind,'Типізована помилка'):'';
    const http=evidence.http_code?' · HTTP '+String(evidence.http_code):'';
    const latency=evidence.latency_ms?' · '+formatDuration(evidence.latency_ms):'';
    return formatDate(evidence.at)+' · '+outcome+failure+http+latency+(evidence.request_ref?' · '+evidence.request_ref:'');
  }
  function dimensionsText(dimensions){
    if(!isObject(dimensions)||!Object.keys(dimensions).length)return '';
    const labels={location:'локація',model:'модель',region:'регіон',tier:'рівень'};
    return Object.entries(dimensions).filter(([key])=>Object.prototype.hasOwnProperty.call(labels,key)).map(([key,value])=>labels[key]+' '+String(value)).join(' · ');
  }
  function blocksText(project,pacificReset){
    const complete=project.rpm.complete&&project.input_tpm.complete&&project.rpd.complete;
    if(!complete)return ['Блокування провайдера невідоме.'];
    if(!project.provider_blocks.length)return ['Активного типізованого блокування провайдера немає.','Скидання за тихоокеанським часом: '+formatDate(pacificReset)];
    return project.provider_blocks.map(block=>{
      const metric=String(block.metric||'unknown').toUpperCase();
      const quota=block.quota_id?' · '+String(block.quota_id):'';
      const dimensions=dimensionsText(block.dimensions);
      const retry=block.retry_after_seconds?' · повтор через '+formatSeconds(block.retry_after_seconds):'';
      const until=block.until?' · до '+formatDate(block.until):'';
      return metric+quota+(dimensions?' · '+dimensions:'')+retry+until;
    }).concat('Скидання за тихоокеанським часом: '+formatDate(pacificReset));
  }

  function projectRow(project,index,pacificReset){
    const article=node('article','gemini-v2-project');
    const header=node('header','gemini-v2-project-header');
    const title=node('div','gemini-v2-project-title');
    const titleCopy=node('div');
    const mappingLabels={explicit:'мапінг підтверджено',missing:'мапінг відсутній',duplicate:'дубль мапінгу'};
    titleCopy.append(node('h4','',projectLabel(project.slot_id)),node('p','',(project.configured?'Налаштовано':'Не налаштовано')+' · '+(mappingLabels[project.identity_mapping]||'мапінг невідомий')));
    title.append(node('span','gemini-v2-project-number',String(index+1).padStart(2,'0')),titleCopy);
    const status=String(project.status||'accounting_unknown');
    header.append(title,node('span','gemini-v2-state '+statusTone(status),STATUS_LABELS[status]||STATUS_LABELS.accounting_unknown));
    article.append(header);

    const metrics=node('dl','gemini-v2-project-metrics');
    metrics.append(projectMetric('RPM',project.rpm),projectMetric('Input TPM',project.input_tpm),projectMetric('RPD',project.rpd));
    const flightKnown=typeof project.in_flight==='number'&&Number.isFinite(project.in_flight);
    const flight=node('div','gemini-v2-project-metric');
    flight.append(node('dt','','In-flight'),node('dd',flightKnown?'':'is-unknown',flightKnown?formatNumber(project.in_flight):'—'),node('small','',flightKnown?'поточні локальні дозволи':'Облік невідомий'));
    metrics.append(flight);
    article.append(metrics);

    const details=node('div','gemini-v2-project-detail');
    const evidence=node('section');
    evidence.append(node('h5','','Останній реальний доказ'),node('p','',evidenceText(project.last_real_evidence)));
    const usage=node('section');
    usage.append(node('h5','','Лінії та класи'));
    usage.append(node('p','',counterText(project.usage_by_lane,LANE_LABELS)));
    usage.append(node('p','',counterText(project.usage_by_task_class,TASK_LABELS)));
    const latency=(project.latency_ms&&isNumberOrNull(project.latency_ms.p50)&&isNumberOrNull(project.latency_ms.p95))
      ?'p50 '+formatDuration(project.latency_ms.p50)+' · p95 '+formatDuration(project.latency_ms.p95)
      :'Латентність невідома';
    usage.append(node('p','',latency+' · резервних перемог '+formatNumber(project.fallback_wins)));
    const blocks=node('section');
    blocks.append(node('h5','','Блокування провайдера / скидання'));
    blocksText(project,pacificReset).forEach(copy=>blocks.append(node('p','',copy)));
    if(project.external_usage_suspected)blocks.append(node('p','gemini-v2-warning-copy','Є ознака зовнішнього використання. Це попередження, а не доведена причина.'));
    details.append(evidence,usage,blocks);
    article.append(details);
    return article;
  }

  function modelRow(model,index,pacificReset){
    const details=node('details','gemini-v2-model gemini-v2-model--'+String(index+1));
    const summary=node('summary');
    const identity=node('span','gemini-v2-model-identity');
    const coverage=isObject(model.coverage)?model.coverage:{};
    const configured=Number(coverage.configured||0);
    const accounted=Number(coverage.accounted||0);
    const danger=model.projects.some(project=>statusTone(project.status)==='is-danger');
    let stateClass='';
    let stateText='Облік невідомий · '+formatNumber(configured)+' налашт.';
    if(accounted&&accounted===configured&&model.rpm.complete&&model.input_tpm.complete&&model.rpd.complete){stateClass=danger?'is-danger':'is-live';stateText='Облік '+String(accounted)+'/'+String(configured)+' проєктів';}
    else if(accounted){stateClass='is-warning';stateText='Неповний облік '+String(accounted)+'/'+String(configured);}
    if(model.external_usage_suspected){stateClass='is-warning';stateText+=' · зовнішня активність';}
    identity.append(node('span','gemini-v2-model-name',modelLabel(model.model)),node('span','gemini-v2-model-state '+stateClass,stateText));

    const metrics=node('dl','gemini-v2-rail-metrics');
    const rpm=metricParts(model.rpm);const tpm=metricParts(model.input_tpm);const rpd=metricParts(model.rpd);
    metrics.append(
      railMetric('RPM',rpm.value,rpm.detail,rpm.unknown),
      railMetric('Input TPM',tpm.value,tpm.detail,tpm.unknown),
      railMetric('RPD',rpd.value,rpd.detail,rpd.unknown),
      scalarMetric('In-flight',model.in_flight,'активні дозволи'),
      latencyMetric('p50',model.latency_ms.p50),
      latencyMetric('p95',model.latency_ms.p95),
      railMetric('Резерв',typeof model.fallbacks_from==='number'&&typeof model.fallbacks_to==='number'?formatNumber(model.fallbacks_from)+' / '+formatNumber(model.fallbacks_to):'—',typeof model.fallbacks_from==='number'?'перехід / перемога':'Облік невідомий',typeof model.fallbacks_from!=='number')
    );
    summary.append(identity,metrics,node('span','gemini-v2-chevron','⌄'));
    const body=node('div','gemini-v2-model-body');
    model.projects.forEach((project,projectIndex)=>body.append(projectRow(project,projectIndex,pacificReset)));
    details.append(summary,body);
    details.addEventListener('toggle',()=>{
      if(!details.open)return;
      details.parentElement.querySelectorAll('details.gemini-v2-model[open]').forEach(other=>{if(other!==details)other.open=false;});
    });
    return details;
  }

  function renderQuotas(data){
    const models=node('div','gemini-v2-models');
    data.models.forEach((model,index)=>models.append(modelRow(model,index,data.pacific_reset_at)));
    content.quotas.replaceChildren(models);
    const hasExternal=data.models.some(model=>model.external_usage_suspected);
    if(hasExternal)setNotice('is-warning','Зафіксовано ознаку зовнішнього використання.','Вона не доводить причину вичерпання квоти й не змінює локальні факти.');
    else if(data.accounting.traffic_truncated)setNotice('is-warning','Локальний зріз обрізано захисним лімітом.','Показані значення можуть бути неповними.');
  }

  function chainList(models,className){
    const list=node('ol','gemini-v2-chain'+(className?' '+className:''));
    if(!models.length){list.append(node('li','','Без моделі'));return list;}
    models.forEach(model=>list.append(node('li','',modelLabel(model))));
    return list;
  }
  function routeRow(route,index){
    const article=node('article','gemini-v2-route'+(route.task_class==='durable_analysis'?' is-secondary':''));
    const summary=node('div');
    const presentation=ROUTE_PRESENTATION[route.task_class];
    const classLine=node('div','gemini-v2-route-class');
    classLine.append(node('span','',String(index+1).padStart(2,'0')),document.createTextNode(route.task_class.toUpperCase()));
    summary.append(classLine,node('h4','',presentation.title),node('p','gemini-v2-route-definition',presentation.definition));
    const facts=node('div','gemini-v2-route-facts');
    facts.append(node('span','',laneLabel(route.lane)),node('span','',route.deadline_ms?'дедлайн '+formatDuration(route.deadline_ms):'без model-дедлайну'));
    summary.append(facts);
    const chains=node('div','gemini-v2-route-chains');
    const base=node('section','gemini-v2-route-chain');base.append(node('h5','','Базова черга'),chainList(route.base_chain));
    const effective=node('section','gemini-v2-route-chain');effective.append(node('h5','','Ефективна черга'),chainList(route.effective_chain,'is-effective'));
    chains.append(base,effective);
    if(route.escalation_chain.length){
      const escalation=node('div','gemini-v2-escalation','Вторинна ескалація аналізу: '+route.escalation_chain.map(modelLabel).join(' → '));
      chains.append(escalation);
    }
    article.append(summary,chains);
    return article;
  }
  function renderRoutes(data){
    const fragment=document.createDocumentFragment();
    const pin=node('div','gemini-v2-pin'+(data.emergency_pin.active?' is-active':''));
    const pinCopy=node('div');
    if(data.emergency_pin.active){
      pinCopy.append(node('strong','','Аварійне закріплення активне · '+modelLabel(data.emergency_pin.model)),node('p','','Модель переміщено на перше місце зі збереженням усіх резервних маршрутів. Це не ексклюзивне блокування.'));
    }else{
      pinCopy.append(node('strong','','Аварійне закріплення неактивне'),node('p','','Ефективні черги відповідають базовій політиці; резервні маршрути збережені.'));
    }
    const pinTime=node('time','',data.emergency_pin.active?'до '+formatDate(data.emergency_pin.expires_at):'без строку');
    if(data.emergency_pin.expires_at)pinTime.setAttribute('datetime',String(data.emergency_pin.expires_at));
    pin.append(node('span','gemini-v2-pin-index','PIN'),pinCopy,pinTime);
    fragment.append(pin);
    const authority=node('div','gemini-v2-request-context');
    authority.append(node('span','','Політика '+(data.policy_version||'—')),node('span','','Версія правил '+(data.authority_snapshot_version||'—')),node('span','','Діє від '+formatDate(data.accounting.effective_from)));
    fragment.append(authority);
    const ledger=node('div','gemini-v2-route-ledger');
    data.routes.forEach((route,index)=>ledger.append(routeRow(route,index)));
    fragment.append(ledger);
    content.routes.replaceChildren(fragment);
  }

  function replyText(reply){
    const stateLabel=REPLY_LABELS[reply.state]||'Стан відповіді невідомий';
    const receipt=reply.provider_receipt_present?'квитанцію провайдера зафіксовано':'квитанції провайдера немає';
    if(reply.state!=='persisted')return stateLabel+' · '+receipt;
    const delivery=mapLabel(SEND_STATE_LABELS,String(reply.send_state||'unknown'),SEND_STATE_LABELS.unknown);
    const messageStatus=mapLabel(MESSAGE_STATUS_LABELS,String(reply.message_status||'unknown'),MESSAGE_STATUS_LABELS.unknown);
    const chunks=Number(reply.planned_chunks||0)?' · доставлено частин '+formatNumber(reply.delivered_chunks)+'/'+formatNumber(reply.planned_chunks):'';
    return stateLabel+' · '+messageStatus+' · '+delivery+' · '+receipt+chunks;
  }
  function quotaBlockText(block){
    if(!isObject(block))return '';
    const metric=String(block.metric||'unknown').toUpperCase();
    const quota=block.quota_id?' · '+String(block.quota_id):'';
    const dimensions=dimensionsText(block.dimensions);
    const retry=block.retry_after_seconds?' · повтор через '+formatSeconds(block.retry_after_seconds):'';
    const until=block.until?' · до '+formatDate(block.until):'';
    return 'Блокування квоти '+metric+quota+(dimensions?' · '+dimensions:'')+retry+until;
  }
  function outcomeRow(outcome){
    let classes='gemini-v2-outcome';
    if(outcome.winner)classes+=' is-winner';
    else if(outcome.outcome==='succeeded_late')classes+=' is-late';
    else if(outcome.failure_kind||outcome.outcome==='failed')classes+=' is-danger';
    const row=node('div',classes);
    const stateCopy=outcome.winner?'Переможець · '+(FSM_LABELS[outcome.fsm_state]||'Стан невідомий'):(FSM_LABELS[outcome.fsm_state]||'Стан невідомий');
    const reason=outcome.not_attempted_reason
      ?mapLabel(SKIP_LABELS,outcome.not_attempted_reason,'Не викликано')
      :outcome.failure_kind?mapLabel(FAILURE_LABELS,outcome.failure_kind,'Типізована помилка')
      :outcome.outcome==='succeeded_late'?'Пізній результат · не замінює переможця':'Без типізованої помилки';
    const timing=(outcome.provider_started_at?formatDate(outcome.provider_started_at):'не відправлено')+' · '+formatDuration(outcome.latency_ms);
    row.append(node('strong','',stateCopy),node('span','',reason),node('span','',timing));
    const quota=quotaBlockText(outcome.quota_block);
    if(quota)row.append(node('span','gemini-v2-quota-block',quota));
    return row;
  }
  function candidateRow(candidate){
    const item=node('li','gemini-v2-candidate');
    const head=node('div','gemini-v2-candidate-head');
    const copy=node('div');
    const project=candidate.project_state==='mapped'?projectLabel(candidate.slot_id):'Проєкт невідомий';
    copy.append(node('span','gemini-v2-candidate-title',modelLabel(candidate.model)+' · '+project));
    const skip=candidate.initial_skip_reason?' · початково: '+mapLabel(SKIP_LABELS,candidate.initial_skip_reason,'Не викликано'):'';
    copy.append(node('span','gemini-v2-candidate-meta','Кандидат '+formatNumber(candidate.candidate_index)+skip));
    const execution=String(candidate.execution_state||'not_recorded');
    head.append(copy,node('span','gemini-v2-execution '+executionTone(execution),EXECUTION_LABELS[execution]||'Стан невідомий'));
    item.append(head);
    const outcomes=node('div','gemini-v2-outcomes');
    if(candidate.outcomes.length)candidate.outcomes.forEach(outcome=>outcomes.append(outcomeRow(outcome)));
    else outcomes.append(node('div','gemini-v2-outcome',execution==='not_recorded'?'Подію спроби не зафіксовано.':'Виклику провайдера не було.'));
    item.append(outcomes);
    return item;
  }
  function attemptFact(label,value,tone){
    const wrapper=node('span','gemini-v2-attempt-fact');
    wrapper.append(node('small','',label),node('strong',tone||'',value));
    return wrapper;
  }
  function attemptRow(item){
    const details=node('details','gemini-v2-attempt');
    const summary=node('summary');
    const request=node('span');
    request.append(node('span','gemini-v2-request-ref',item.request_ref||'Непрозоре посилання відсутнє'),node('time','gemini-v2-request-time',formatDate(item.created_at)));
    if(item.created_at)request.lastElementChild.setAttribute('datetime',String(item.created_at));
    const resolution=String(item.resolution.state||'pending');
    const winner=item.winner?modelLabel(item.winner.model)+' · '+projectLabel(item.winner.slot_id):'Переможця немає';
    summary.append(
      request,
      attemptFact('Лінія / клас',laneLabel(item.lane)+' · '+taskLabel(item.task_class)),
      attemptFact('Результат',RESOLUTION_LABELS[resolution]||RESOLUTION_LABELS.other,resolutionTone(resolution)),
      attemptFact('Переможець',winner,item.winner?'is-live':''),
      attemptFact('Відповідь',replyText(item.reply),item.reply.provider_receipt_present?'is-live':''),
      node('span','gemini-v2-chevron','⌄')
    );
    const body=node('div','gemini-v2-attempt-body');
    const context=node('div','gemini-v2-request-context');
    context.append(
      node('span','','Політика '+(item.policy_version||'—')),
      node('span','','Облік '+accountingModeLabel(item.accounting_mode)),
      node('span','','Дедлайн '+formatDuration(item.deadline_ms)),
      node('span','','Причина '+(item.resolution.reason?mapLabel(RESOLUTION_REASON_LABELS,item.resolution.reason,'типізовану причину не визначено'):'—'))
    );
    body.append(context);
    const candidates=node('ol','gemini-v2-candidates');
    if(item.candidate_plan.length)item.candidate_plan.forEach(candidate=>candidates.append(candidateRow(candidate)));
    else candidates.append(node('li','gemini-v2-candidate','План кандидатів не зафіксовано.'));
    body.append(candidates);
    if(item.candidate_plan_truncated||item.attempts_truncated)body.append(node('p','gemini-v2-truncated','Граф обрізано захисним серверним лімітом; невидимі рядки не інтерпретуються.'));
    details.append(summary,body);
    return details;
  }
  function renderAttempts(data,append){
    if(append)state.attemptsItems=state.attemptsItems.concat(data.items).slice(0,MAX_RENDERED_ATTEMPTS);
    else state.attemptsItems=data.items.slice(0,MAX_RENDERED_ATTEMPTS);
    state.nextCursor=data.next_cursor||null;
    const ledger=node('div','gemini-v2-attempt-ledger');
    if(!state.attemptsItems.length){
      const empty=node('div','gemini-v2-empty');
      empty.append(node('strong','','Запитів ще немає'),node('span','','Локальний журнал V2 не містить графів спроб.'));
      content.attempts.replaceChildren(empty);
    }else{
      state.attemptsItems.forEach(item=>ledger.append(attemptRow(item)));
      content.attempts.replaceChildren(ledger);
    }
    const atCap=state.attemptsItems.length>=MAX_RENDERED_ATTEMPTS;
    moreWrap.hidden=!state.nextCursor||atCap;
    if(atCap){
      const cap=node('p','gemini-v2-truncated','Показано захисний максимум '+String(MAX_RENDERED_ATTEMPTS)+' запитів. Оновіть зріз, щоб повернутися до найновіших.');
      content.attempts.append(cap);
    }
  }

  function renderData(viewName,data,append){
    updateHeader(data,viewName);
    root.classList.remove('is-stale');
    if(viewName==='quotas')renderQuotas(data);
    else if(viewName==='routes')renderRoutes(data);
    else renderAttempts(data,append);
  }

  function endpointFor(viewName,append){
    const base=urls[viewName];
    if(!base)return '';
    if(viewName!=='attempts')return base;
    const url=new URL(base);
    url.searchParams.set('cursor',append&&state.nextCursor?state.nextCursor:'');
    url.searchParams.set('limit','25');
    return url.href;
  }
  function abortView(viewName){
    const controller=state.controllers[viewName];
    if(controller)controller.abort();
    state.controllers[viewName]=null;
  }
  function abortInactive(){VIEW_ORDER.forEach(name=>{if(name!==state.active)abortView(name);});}
  function invalidSchemaMessage(){return 'Схема локальної адреси не підтримується цією версією панелі. Оновлення зупинено без припущень про дані.';}

  async function loadView(viewName,{passive=false,force=false,append=false}={}){
    if(!VIEW_ORDER.includes(viewName)||document.hidden||!isOuterActive())return false;
    if(!force&&!append&&state.snapshots[viewName])return true;
    if(append&&(!state.nextCursor||state.attemptsItems.length>=MAX_RENDERED_ATTEMPTS))return false;
    const now=Date.now();
    if(passive&&now-state.lastRequestAt[viewName]<refreshInterval-1000)return false;
    const endpoint=endpointFor(viewName,append);
    if(!endpoint){showFatal(viewName,'Локальна адреса недоступна','Адреса V2 не пройшла перевірку спільного походження.');setStatus('Локальна адреса V2 недоступна.','is-error');return false;}
    abortView(viewName);
    const controller=new AbortController();
    state.controllers[viewName]=controller;
    state.lastRequestAt[viewName]=now;
    setBusy(viewName,true);
    if(!append&&!state.snapshots[viewName])showSkeleton(viewName);
    if(!passive)setStatus(append?'Завантажуємо наступні графи спроб…':'Завантажуємо локальний зріз…','');
    if(viewName==='attempts'&&!append)attemptsError.hidden=true;
    try{
      const response=await fetch(endpoint,{method:'GET',credentials:'same-origin',headers:{'Accept':'application/json','X-Requested-With':'XMLHttpRequest'},signal:controller.signal});
      const data=await response.json().catch(()=>null);
      if(!response.ok){
        const error=new Error(data&&data.error==='invalid_cursor'?'invalid_cursor':'endpoint_error');
        error.kind=data&&data.error==='invalid_cursor'?'cursor':'endpoint';
        throw error;
      }
      const valid=viewName==='quotas'?validateQuotas(data):viewName==='routes'?validateRoutes(data):validateAttempts(data);
      if(!valid){const error=new Error('unsupported_schema');error.kind='schema';throw error;}
      if(controller.signal.aborted)return false;
      state.snapshots[viewName]=data;
      renderData(viewName,data,append);
      if(viewName==='attempts')attemptsError.hidden=true;
      setStatus(append?'Додано '+String(data.items.length)+' графів.':'Локальний зріз оновлено · '+formatDate(data.generated_at),'is-success');
      return true;
    }catch(error){
      if(error&&error.name==='AbortError')return false;
      if(error&&error.kind==='schema'){
        state.snapshots[viewName]=null;
        if(viewName==='attempts'){state.attemptsItems=[];state.nextCursor=null;moreWrap.hidden=true;}
        showFatal(viewName,'Непідтримувана схема',invalidSchemaMessage());
        setNotice('is-danger','Схему відхилено.','Панель не інтерпретує невідомі поля або версії.');
        setStatus(invalidSchemaMessage(),'is-error');
        return false;
      }
      if(viewName==='attempts'&&append&&state.attemptsItems.length){
        attemptsError.textContent=error&&error.kind==='cursor'
          ?'Курсор застарів або недійсний. Уже завантажені графи залишено без змін.'
          :'Наступну сторінку не завантажено. Уже завантажені графи залишено без змін.';
        attemptsError.hidden=false;
        setStatus(attemptsError.textContent,'is-error');
        return false;
      }
      if(state.snapshots[viewName]){
        root.classList.add('is-stale');
        setNotice('is-warning','Новий зріз недоступний.','Показано останні валідні локальні дані; вони позначені як застарілі.');
        setStatus('Новий зріз недоступний · останні валідні дані залишено на екрані.','is-stale');
      }else{
        showFatal(viewName,'Локальні дані недоступні','Спробуйте оновити активний розділ пізніше. Панель не виконує жодних запитів до провайдера.');
        setNotice('is-danger','Локальна адреса не відповіла.','Дані не замінено нульовими або припущеними значеннями.');
        setStatus('Не вдалося завантажити локальний зріз.','is-error');
      }
      return false;
    }finally{
      if(state.controllers[viewName]===controller){
        state.controllers[viewName]=null;
        setBusy(viewName,false);
        if(moreButton)moreButton.disabled=false;
      }
    }
  }

  function selectView(viewName,{focus=false}={}){
    if(!VIEW_ORDER.includes(viewName))return;
    state.active=viewName;
    tabs.forEach((tab,index)=>{
      const active=VIEW_ORDER[index]===viewName;
      tab.classList.toggle('is-active',active);
      tab.setAttribute('aria-selected',active?'true':'false');
      tab.tabIndex=active?0:-1;
      if(active&&focus)tab.focus();
    });
    VIEW_ORDER.forEach(name=>{
      const active=name===viewName;
      views[name].hidden=!active;
      views[name].classList.toggle('is-active',active);
      if(active){views[name].classList.remove('is-entering');requestAnimationFrame(()=>views[name].classList.add('is-entering'));}
    });
    abortInactive();
    loadView(viewName,{force:false});
    syncTimers();
  }

  function isOuterActive(){
    const outer=root.closest('.bot-panel');
    return Boolean(outer&&outer.classList.contains('active')&&outer.getAttribute('aria-hidden')!=='true');
  }
  function load(options){return loadView(state.active,{force:true,...(options||{})});}
  function syncTimers(){
    if(state.refreshTimer){window.clearInterval(state.refreshTimer);state.refreshTimer=0;}
    if(document.hidden||!isOuterActive()){
      VIEW_ORDER.forEach(abortView);
      return;
    }
    state.refreshTimer=window.setInterval(()=>{
      if(!document.hidden&&isOuterActive())loadView(state.active,{passive:true,force:true});
    },refreshInterval);
  }

  async function runProbe(){
    if(!urls.probe||!SLOT_ORDER.includes(probeProject.value)||!MODEL_ORDER.includes(probeModel.value)){
      probeResult.textContent='Параметри ручної діагностики не пройшли allowlist.';
      probeResult.className='gemini-v2-diagnostic-result is-error';
      setStatus(probeResult.textContent,'is-error');
      return;
    }
    abortView('probe');
    const controller=new AbortController();state.controllers.probe=controller;
    probeButton.disabled=true;
    const selectedProject=projectLabel(probeProject.value);const selectedModel=modelLabel(probeModel.value);
    probeResult.textContent='Перевіряємо '+selectedProject+' · '+selectedModel+'…';
    probeResult.className='gemini-v2-diagnostic-result';
    setStatus('Запущено ручну capability/auth діагностику. Вона не перевіряє генераційну квоту.','');
    const body=new FormData();body.append('slot_id',probeProject.value);body.append('model',probeModel.value);
    const csrf=(document.querySelector('input[name=csrfmiddlewaretoken]')||{}).value||'';
    try{
      const response=await fetch(urls.probe,{method:'POST',credentials:'same-origin',headers:{'X-CSRFToken':csrf,'X-Requested-With':'XMLHttpRequest','Accept':'application/json'},body:body,signal:controller.signal});
      const data=await response.json().catch(()=>null);
      if(!response.ok||!isObject(data)||data.success!==true||!isObject(data.probe)||data.probe.model!==probeModel.value||!Object.prototype.hasOwnProperty.call(PROBE_STATUS_LABELS,data.probe.status))throw new Error(isObject(data)?String(data.code||data.error||'probe_failed'):'probe_failed');
      const status=String(data.probe.status);const success=status==='ok'||status==='metadata_ok';
      probeResult.textContent=selectedProject+' · '+selectedModel+' · '+PROBE_STATUS_LABELS[status]+' · '+formatDuration(Number(data.probe.latency_ms));
      probeResult.className='gemini-v2-diagnostic-result '+(success?'is-success':'is-error');
      setStatus(success?'Capability/auth підтверджено. Це не доказ залишку генераційної квоти.':'Capability/auth не підтверджено. Генераційний баланс залишається невідомим.',success?'is-success':'is-error');
    }catch(error){
      if(error&&error.name==='AbortError')return;
      const labels={key_unconfigured:'Проєкт не налаштовано.',key_cooldown:'Проєкт у періоді очікування.',key_busy:'Проєкт зайнятий.',probe_in_progress:'Перевірка вже виконується.',probe_rate_limited:'Ручну перевірку тимчасово обмежено.'};
      probeResult.textContent=labels[error.message]||'Ручну capability/auth діагностику не завершено.';
      probeResult.className='gemini-v2-diagnostic-result is-error';
      setStatus(probeResult.textContent,'is-error');
    }finally{
      if(state.controllers.probe===controller)state.controllers.probe=null;
      probeButton.disabled=false;
    }
  }

  tabs.forEach((tab,index)=>tab.addEventListener('click',()=>selectView(VIEW_ORDER[index])));
  document.getElementById('gemini-v2-tabs').addEventListener('keydown',event=>{
    if(!['ArrowLeft','ArrowRight','Home','End'].includes(event.key))return;
    let index=tabs.indexOf(document.activeElement);
    if(index<0)index=VIEW_ORDER.indexOf(state.active);
    if(event.key==='Home')index=0;
    else if(event.key==='End')index=tabs.length-1;
    else index=(index+(event.key==='ArrowRight'?1:-1)+tabs.length)%tabs.length;
    event.preventDefault();selectView(VIEW_ORDER[index],{focus:true});
  });
  refreshButton.addEventListener('click',()=>loadView(state.active,{force:true}));
  moreButton.addEventListener('click',()=>{moreButton.disabled=true;loadView('attempts',{force:true,append:true});});
  probeButton.addEventListener('click',runProbe);
  document.addEventListener('visibilitychange',()=>{
    syncTimers();
    if(!document.hidden&&isOuterActive())loadView(state.active,{passive:true,force:true});
  });

  window.GeminiV2Panel={load:load,syncTimers:syncTimers};
})();
