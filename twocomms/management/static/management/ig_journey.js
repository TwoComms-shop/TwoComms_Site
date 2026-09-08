/* Read-only client journey, mounted above the existing management chat. */
(function () {
  'use strict';
  const NS='http://www.w3.org/2000/svg';
  const STATES=new Set(['open','complete','partial','skipped','not_applicable','invalidated','superseded']);
  const LABELS={possible:'Можливий етап',open:'Ще немає підтвердження',complete:'Підтверджено',partial:'Є частина даних',skipped:'Пропущено з причиною',not_applicable:'Не потрібен',invalidated:'Потрібно уточнити',superseded:'Є новіше значення'};
  const ICONS={
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
  function iconFor(n){const mapped=window.TwcJourneyGeometry?.visualFor(n);if(mapped?.icon)return mapped.icon;const key=n.route_kind||n.semantic_key||n.id.split(':')[1];return ({inbound:'message',inquiry:'message',catalog:'shirt',selection:'shirt',brief:'brief',custom_print:'image',dtf:'image',quoted_offer:'tag',terms:'tag',offer:'image',settlement:'money',payment:'money',fulfillment:'package',support:'return',objection_case:'question',employment:'work',collaboration:'handshake',information:'info',community:'gift',consent:'bell',reward:'gift'})[key]||(n.id.startsWith('episode:')?'repeat':'info');}
  function date(value){const d=new Date(value);return value&&!Number.isNaN(d.getTime())?new Intl.DateTimeFormat('uk-UA',{day:'numeric',month:'short',hour:'2-digit',minute:'2-digit'}).format(d):'';}
  function valueText(value){if(value===null||value===undefined||value==='')return 'Не визначено';if(typeof value==='boolean')return value?'Так':'Ні';if(typeof value!=='object')return String(value);if(Array.isArray(value))return value.map(valueText).filter(Boolean).join(' · ');return [value.title,value.size,value.fit_option_label,value.qty?('×'+value.qty):'',value.order_id?('Замовлення №'+value.order_id):'',value.status_label].filter(Boolean).join(' · ')||'Дані збережено';}
  function witnessed(edge){return !['route','prerequisite'].includes(edge.relation)&&(edge.evidence_refs||[]).length>0;}
  const RETURN_OUTCOMES=new Set(['configuration_correction','offer_correction','settlement_correction','new_selection','amended_offer','choose_alternative']);
  const NEGATIVE_OUTCOMES=new Set(['declined','rejected','blocked','cancelled','restock_consent_not_granted']);
  const RETRY_OUTCOMES=new Set(['new_attempt','wait_for_attempt']);
  function edgeTone(edge){
    if(!witnessed(edge))return 'neutral';
    if(edge.tone==='danger'||edge.relation==='return'||RETURN_OUTCOMES.has(edge.outcome)||NEGATIVE_OUTCOMES.has(edge.outcome))return 'danger';
    if(edge.tone==='warning'||edge.relation==='retry'||RETRY_OUTCOMES.has(edge.outcome))return 'warning';
    return edge.tone==='success'?'success':'recorded';
  }
  function planned(node){return node.presentation_kind==='possible'&&node.implementation_status==='planned'&&['stock_wait','restock_consent'].includes(node.semantic_key);}
  function nodeStatusLabel(node){return planned(node)?'Заплановано':node.presentation_kind==='possible'?LABELS.possible:node.state==='complete'&&node.tone==='danger'?'Завершено з негативним результатом':LABELS[node.state]||LABELS.open;}
  function recorded(node){const visits=node.recorded_visits;return visits&&Number.isInteger(visits.count)&&visits.count>0&&Array.isArray(visits.evidence_refs)&&visits.evidence_refs.length>0;}
  const CONDITIONS={confirmed_coverage:'Коли оплату підтверджено',settlement_correction:'Якщо змінилися дані розрахунку',eligible_opt_in:'Запропонувати потрібну згоду',consent_recorded:'Коли згоду зафіксовано',send_capable:'Якщо контакт дозволено',catalog_match:'Якщо це товар каталогу',custom_reference:'Якщо потрібен власний принт',availability:'Перевірити доступність',eligible_follow_up:'Якщо потрібна допомога',wait_for_attempt:'Повторити оплату',offer_correction:'Змінити умови',configuration_correction:'Змінити склад',payment_objection:'Обговорити заперечення',new_attempt:'Нова спроба оплати',current_mockup_accepted:'Після погодження чинного макета',payment_required:'Якщо потрібна оплата',verified_entitlement_covers_total:'Якщо підтверджене право покриває суму',permission_check:'Перевірити дозвіл на контакт',new_selection:'Підібрати інший товар',current_configuration_confirmed:'Якщо чинний склад підтверджено',authorised_reward_grant:'Після дозволу на нагороду'};
  function toneFor(node){if(['success','warning','danger','manager'].includes(node.tone))return node.tone;return node.state==='complete'?'success':node.state==='invalidated'?'warning':'neutral';}
  Object.assign(CONDITIONS,{check_selected_availability:'Перевірити наявність обраного',stock_rechecked_available:'Коли потрібний варіант є в наявності',unavailable_wait:'Якщо варіанта немає — очікувати',choose_alternative:'Підібрати інший варіант',offer_restock_consent:'Запропонувати дозвіл сповістити',restock_consent_granted:'Дозвіл отримано',restock_consent_not_granted:'Без дозволу на повідомлення'});
  const GUIDE_STRUCTURE={'guide:inquiry':'inbound','guide:selection':'catalog_discovery','guide:terms':'quoted_offer','guide:offer':'awaiting_payment','guide:payment':'settlement','guide:fulfillment':'fulfillment'};
  const TOPIC_STRUCTURE={catalog:'catalog_discovery',custom_print:'custom_print',dtf:'dtf_only',employment:'employment',collaboration:'collaboration',information:'information_question',support:'post_sale_request'};
  const INLINE_CHAINS={catalog:['inbound','catalog_discovery','configured_line','quoted_offer','awaiting_payment','settlement','fulfillment'],custom:['inbound','custom_print','custom_brief','mockup_current_acceptance','configured_line','quoted_offer','awaiting_payment','settlement'],dtf:['inbound','dtf_only','custom_brief','mockup_current_acceptance','configured_line','quoted_offer','awaiting_payment'],employment:['inbound','employment','employment_response'],collaboration:['inbound','collaboration','business_decision'],information:['inbound','information_question','information_resolved'],support:['inbound','post_sale_request','post_sale_case'],inbound:['inbound','catalog_discovery','configured_line']};
  class Journey {
    constructor(options={}){
      this.options=options;this.snapshot=null;this.selected=null;this.selectedEdge=null;this.buttons=new Map();this.cells=new Map();this.edgeButtons=new Map();this.sequence=0;this.destroyed=false;this.panelKey='';this.nodeIds=[];this.zoom=1;this.modal=null;
      this.root=el('section','twc-journey');this.root.setAttribute('aria-label','Шлях клієнта');
      const top=el('div','twc-journey-top');this.title=el('h3','twc-journey-heading','Звернення');this.mode=el('span','twc-journey-mode');const heading=el('div','twc-journey-context');heading.append(this.title,this.mode);
      this.select=el('select','twc-journey-picker');this.select.setAttribute('aria-label','Покупка для перегляду');this.select.hidden=true;
      this.expand=el('button','twc-journey-expand','Весь шлях ↗');this.expand.type='button';this.expand.setAttribute('aria-haspopup','dialog');this.expand.addEventListener('click',()=>this.openMap());top.append(heading,this.select,this.expand);
      this.error=el('p','twc-journey-error');this.error.hidden=true;this.error.setAttribute('role','status');
      this.map=el('div','twc-journey-map');this.svg=svg('svg',{'aria-hidden':'true',focusable:'false'});this.svg.classList.add('twc-journey-guides');this.grid=el('div','twc-journey-grid');this.edgeLayer=el('div','twc-journey-edge-layer');this.map.append(this.svg,this.grid,this.edgeLayer);
      this.note=el('p','twc-journey-note');this.note.hidden=true;
      this.inlineKey=el('div','twc-journey-inline-key');this.inlineKey.append(el('span','twc-journey-key-actual','Фактичний перехід'),el('span','twc-journey-key-possible','Можливий шлях'));
      this.returnReason=el('button','twc-journey-return-reason');this.returnReason.type='button';this.returnReason.hidden=true;this.returnReason.addEventListener('click',()=>this.selectEdge(this.returnReason.dataset.edgeId));this.inlineKey.append(this.returnReason);
      this.directions=el('button','twc-journey-directions');this.directions.type='button';this.directions.setAttribute('aria-haspopup','dialog');this.directions.addEventListener('click',()=>{this.showPossible=true;this.possibleFamily='inbound';this.openMap();});this.inlineKey.append(this.directions);
      this.root.append(top,this.error,this.map,this.inlineKey,this.note);
      this.select.addEventListener('change',()=>this.selectEpisode(this.select.value));
      this.escape=event=>{if(event.key==='Escape'&&this.selected){event.preventDefault();event.stopPropagation();this.closePanel(true);}};
      this.root.addEventListener('keydown',this.escape);
      this.reposition=()=>this.positionPanel();window.addEventListener('scroll',this.reposition,true);
      this.outside=event=>{if(this.panel&&!this.panel.contains(event.target)&&!event.target.closest('.twc-journey-step,.twc-journey-edge-marker'))this.closePanel(false);};document.addEventListener('pointerdown',this.outside);
      this.resize=new ResizeObserver(()=>this.queueLayout());this.resize.observe(this.root);
      this.visibility=()=>{if(!document.hidden)this.updateTimers();};document.addEventListener('visibilitychange',this.visibility);
      this.timerInterval=setInterval(()=>{if(!document.hidden)this.updateTimers();},15000);
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
      this.snapshot=snapshot;
      const source=snapshot.graph?.schema_version===1?snapshot.graph:{nodes:snapshot.nodes,edges:[]};
      const incoming=this.presentGraph(source,snapshot);
      const seen=new Set();this.graph={...incoming,nodes:(incoming.nodes||[]).filter(n=>{if(!n||typeof n.id!=='string'||seen.has(n.id))return false;seen.add(n.id);return true;})};this.nodeIds=[...seen];
      this.edges=(incoming.edges||[]).filter(e=>e&&typeof e.id==='string'&&seen.has(e.from_node_id)&&seen.has(e.to_node_id));
      this.newEdges=previousEdges?new Set(this.edges.filter(e=>witnessed(e)&&!previousEdges.has(e.id)).map(e=>e.id)):new Set();
      for(const [id,button]of this.buttons)if(!seen.has(id)){button.remove();this.cells.get(id)?.remove();this.buttons.delete(id);this.cells.delete(id);}
      if(this.selected&&!seen.has(this.selected))this.closePanel(false);
      if(this.selectedEdge&&!this.edges.some(e=>e.id===this.selectedEdge))this.closePanel(false);
      this.root.dataset.clientId=String(snapshot.client_id);this.root.dataset.episodeId=String(snapshot.viewed_episode_id||'');
      const current=this.graph.nodes.find(n=>n.route_focus)||this.graph.nodes.find(n=>n.current);this.currentId=current?.id;
      const conversational=current?.route_kind||!snapshot.viewed_episode_id||!current||current.id==='guide:inquiry';
      this.title.textContent=conversational?'Звернення':(snapshot.is_history?'Історія · ':'')+(snapshot.viewed_episode?.label||'Покупка '+(snapshot.viewed_episode?.sequence||''));
      this.mode.textContent=current&&current.label!==this.title.textContent?' · '+current.label:'';
      const items=[...(snapshot.episodes?.items||[])];if(snapshot.viewed_episode&&!items.some(e=>e.id===snapshot.viewed_episode.id))items.unshift(snapshot.viewed_episode);
      const optionKey=JSON.stringify(items.map(e=>[e.id,e.label,e.current]));if(optionKey!==this.optionKey){this.optionKey=optionKey;this.select.replaceChildren();if(!snapshot.current_episode_id)this.select.append(new Option('Поточний діалог',''));items.forEach(e=>this.select.append(new Option((e.label||'Покупка '+e.sequence)+(e.current?' · поточна':''),String(e.id))));}
      this.select.value=snapshot.viewed_episode_id?String(snapshot.viewed_episode_id):'';this.select.hidden=this.select.options.length<2;
      this.graph.nodes.forEach(data=>{
        let button=this.buttons.get(data.id);
        if(!button){button=el('button','twc-journey-step');button.type='button';button.dataset.nodeId=data.id;const core=el('span','twc-journey-core');core.append(el('span','twc-journey-icon'),el('span','twc-journey-status'));button.append(core,el('span','twc-journey-step-label'),el('span','twc-journey-count'));button.addEventListener('click',()=>this.toggleNode(data.id));this.buttons.set(data.id,button);const cell=el('div','twc-journey-cell');cell.append(button);this.cells.set(data.id,cell);this.grid.append(cell);}
        const key=iconFor(data);if(button.dataset.icon!==key){button.dataset.icon=key;button.querySelector('.twc-journey-icon').replaceChildren(icon(key));}
        const state=data.presentation_kind==='possible'?'possible':STATES.has(data.state)?data.state:'open';button.dataset.state=state;button.dataset.tone=toneFor(data);button.dataset.current=String(data.id===this.currentId);button.querySelector('.twc-journey-step-label').textContent=data.short_label||window.TwcJourneyGeometry?.visualFor(data)?.short_label||data.label;
        button.dataset.recorded=String(Boolean(recorded(data)));
        const statusKey=planned(data)?'brief':data.tone==='danger'?'cross':state==='complete'?'check':state==='invalidated'?'return':data.tone==='manager'?'person':data.waiting?.evidence_refs?.length?'clock':null;const status=button.querySelector('.twc-journey-status');status.hidden=!statusKey;if(statusKey)status.replaceChildren(icon(statusKey));
        const progress=data.requirements;const valid=progress&&Number.isInteger(progress.completed)&&Number.isInteger(progress.total)&&progress.total>0&&progress.completed>=0&&progress.completed<=progress.total;
        const mentions=data.semantic_key==='objection_case'?(data.facts||[]).find(f=>f.id?.endsWith(':repeat_count')):null;const repeats=Number.isInteger(mentions?.value)&&mentions.value>1?mentions.value:0;const count=button.querySelector('.twc-journey-count');count.hidden=!valid&&!repeats;count.textContent=valid?progress.completed+'/'+progress.total:repeats?'×'+repeats:'';count.title=valid?'Виконано обов’язкових умов: '+count.textContent:repeats?'Повторних згадок: '+repeats:'';
        button.setAttribute('aria-expanded',String(this.selected===data.id));button.dataset.baseLabel=data.label+' — '+nodeStatusLabel(data)+(data.id===this.currentId?', поточний фокус':'')+(data.waiting?.evidence_refs?.length?', очікування: '+data.waiting.label:'')+(valid?', умов '+count.textContent:repeats?', повторних згадок '+repeats:'');button.setAttribute('aria-label',button.dataset.baseLabel);button.title=data.label+' · '+nodeStatusLabel(data);
      });
      if(this.selected)this.renderPanel();if(this.modal){this.renderAccessibleList();this.mapCoverage.textContent=this.graph.coverage?.semantic_transitions==='missing_source'?(this.graph.nodes.some(recorded)?'Є збережені події етапів. Переходи між ними не зафіксовані.':'Переходи між етапами не зафіксовані.'):'Суцільні стрілки — збережені переходи; пунктир — можливі шляхи.';}this.queueLayout();this.updateTimers();
    }
    presentGraph(source,snapshot){
      // Cycle metadata belongs in the selector, not a disconnected graph circle.
      const nodes=(source.nodes||[]).filter(n=>!n.id?.startsWith('episode:')).map(n=>({...n,structural_key:GUIDE_STRUCTURE[n.id]||n.semantic_key}));
      const ids=new Set(nodes.map(n=>n.id));const edges=(source.edges||[]).filter(e=>ids.has(e.from_node_id)&&ids.has(e.to_node_id)).map(e=>({...e}));
      if(!this.modal&&snapshot.catalogue&&!snapshot.is_history)return this.inlineGraph(source,nodes,edges,snapshot.catalogue);
      if(!this.modal||!this.showPossible||!snapshot.catalogue)return {...source,nodes,edges};
      const catalogue=snapshot.catalogue,family=this.possibleFamily||'inbound';
      const groups=family==='catalog'?['catalog','commerce','payment']:family==='custom'?['custom','dtf','photo','commerce','payment']:family==='after'?['commerce','post_sale','consent','ugc','reward','repeat']:[family];
      const definitions=catalogue.definitions.filter(d=>family==='all'||(family==='inbound'?(d.semantic_kind==='entry'||d.key==='spam_confirmed'):d.key==='inbound'||d.route_keys.some(k=>groups.includes(k))));
      const anchors=new Map();
      definitions.forEach(d=>{
        // Only exact, unique guide or witnessed semantic nodes in this scoped snapshot can
        // serve as an anchor. A conversation topic is NOT a business milestone.
        const actual=nodes.filter(n=>n.structural_key===d.key&&(n.id.startsWith('guide:')||(n.id.startsWith('semantic:')&&n.episode_id===snapshot.viewed_episode_id&&recorded(n))));
        if(actual.length===1){anchors.set(d.key,actual[0].id);return;}
        const id='possible:'+d.key;anchors.set(d.key,id);nodes.push({id,semantic_key:d.key,label:d.label,state:null,current:false,presentation_kind:'possible',implementation_status:d.implementation_status,implementation_note:d.implementation_note,summary:'Сценарій передбачає цей етап. Подій цього клієнта тут не зафіксовано.',facts:[],evidence_refs:[],timers:[]});
      });
      catalogue.transitions.forEach(e=>{if(anchors.has(e.source_key)&&anchors.has(e.target_key))edges.push({id:e.id,from_node_id:anchors.get(e.source_key),to_node_id:anchors.get(e.target_key),relation:'route',outcome:e.outcome,evidence_refs:[],tone:'neutral',condition_label:CONDITIONS[e.outcome]||(e.source_key==='inbound'&&e.target_key==='ad_resolved_product'?'Якщо товар відомий':e.source_key==='inbound'&&e.target_key==='catalog_discovery'?'Якщо потрібен підбір':''),structural_path:[e.source_key,e.target_key]});});
      return {...source,nodes,edges};
    }
    inlineGraph(source,nodes,edges,catalogue){
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
      catalogue.transitions.forEach(e=>{if(anchors.has(e.source_key)&&anchors.has(e.target_key))edges.push({id:e.id,from_node_id:anchors.get(e.source_key),to_node_id:anchors.get(e.target_key),relation:'route',outcome:e.outcome,evidence_refs:[],tone:'neutral',structural_path:[e.source_key,e.target_key]});});
      // An accepted topic and a business fact may coexist at the same semantic
      // location. Keep both identities; only canonical possible neighbors can
      // connect the topic. This is never a topic→business completion edge.
      if(topicKey&&anchors.get(topicKey)!==topic.id)catalogue.transitions.forEach(e=>{
        const from=e.source_key===topicKey?topic.id:anchors.get(e.source_key),to=e.target_key===topicKey?topic.id:anchors.get(e.target_key);
        if((e.source_key===topicKey||e.target_key===topicKey)&&from&&to)edges.push({id:'topic-context:'+e.id,from_node_id:from,to_node_id:to,relation:'route',outcome:e.outcome,evidence_refs:[],tone:'neutral',structural_path:[e.source_key,e.target_key]});
      });
      const otherDirections=catalogue.transitions.filter(e=>e.source_key==='inbound'&&e.target_key!=='spam_confirmed'&&!wanted.has(e.target_key)).length;
      return {...source,nodes,edges,inline_main_ids:[...chain.map(k=>anchors.get(k)).filter(Boolean),...nodes.filter(n=>n.semantic_key==='client_order_context').map(n=>n.id)],inline_alternative_ids:extras.map(k=>anchors.get(k)).filter(Boolean),inline_family:family,other_directions:otherDirections};
    }
    syncVisibility(width){
      if(!this.graph)return;let nodes=this.graph.nodes;
      this.displayEdges=this.edges;
      if(!this.modal&&this.graph.inline_main_ids){
        const overview=window.TwcJourneyGeometry.overview({nodes,edges:this.edges,mainIds:this.graph.inline_main_ids,alternativeIds:this.graph.inline_alternative_ids,currentId:this.currentId,width});
        nodes=overview.nodes;this.inlineSlots=overview.slots;this.displayEdges=overview.edges;
      }else if(!this.modal){
        this.inlineSlots=null;
        const current=this.currentId||this.graph.overview_node_ids?.find(id=>this.nodeIds.includes(id))||nodes[0]?.id;
        const direct=this.edges.filter(e=>witnessed(e)&&(e.from_node_id===current||e.to_node_id===current));
        if(width<560){const before=direct.find(e=>e.to_node_id===current),after=direct.find(e=>e.from_node_id===current);const ids=width<330?[current]:[before?.from_node_id,current,after?.to_node_id].filter(Boolean);nodes=nodes.filter(n=>ids.includes(n.id));}
        else{const cap=Math.max(3,Math.min(8,Math.floor(width/72)));const priorityNodes=nodes.filter(n=>n.waiting?.evidence_refs?.length||(n.semantic_key==='objection_case'&&n.state==='partial')).map(n=>n.id);const priorities=[current,...priorityNodes,...direct.flatMap(e=>[e.from_node_id,e.to_node_id]),...(this.graph.overview_node_ids||[])];const ids=[...new Set(priorities.filter(Boolean))].slice(0,cap);nodes=nodes.filter(n=>ids.includes(n.id));const lanes=[...new Set(nodes.map(n=>n.layout?.lane||0))].sort((a,b)=>a-b);if(lanes.length>3){const activeLane=nodes.find(n=>n.id===current)?.layout?.lane||0;const allowed=[activeLane,...lanes.filter(l=>l!==activeLane)].slice(0,3);nodes=nodes.filter(n=>allowed.includes(n.layout?.lane||0));}}
      }
      this.visibleIds=nodes.map(n=>n.id);this.cells.forEach((c,id)=>{c.hidden=!this.visibleIds.includes(id);});
      if(this.selected&&!this.visibleIds.includes(this.selected))this.closePanel(false);
      const omitted=this.nodeIds.length-nodes.length;const attention=nodes.length<this.graph.nodes.length?this.graph.nodes.filter(n=>!this.visibleIds.includes(n.id)&&(n.waiting?.evidence_refs?.length||(n.semantic_key==='objection_case'&&n.state==='partial'))):[];this.note.hidden=!attention.length;this.note.textContent=attention.length?'Поза оглядом: '+attention.map(n=>n.waiting?.label||n.label).slice(0,2).join(' · ')+(attention.length>2?' · ще '+(attention.length-2):''):'';this.expand.textContent=omitted?'Весь шлях · '+this.nodeIds.length+' ↗':'Весь шлях ↗';this.expand.title=omitted?'Ще '+omitted+' етапів у повній карті':'Відкрити повну карту';
      this.expand.disabled=!this.nodeIds.length;
      this.inlineKey.hidden=Boolean(this.modal)||!this.graph.inline_main_ids;
      this.directions.hidden=!this.graph.inline_main_ids;this.directions.textContent='Інші напрями ↗';
    }
    toggleNode(id){if(this.selected===id&&!this.selectedEdge){this.closePanel(false);return;}this.selected=id;this.selectedEdge=null;this.panelKey='';this.renderPanel();this.buttons.forEach((b,key)=>b.setAttribute('aria-expanded',String(key===id)));this.queueLayout();}
    selectEdge(id){const e=this.edges.find(e=>e.id===id);if(!e)return;if(this.selectedEdge===id){this.closePanel(false);return;}this.selected=e.from_node_id;this.selectedEdge=id;this.panelKey='';this.renderPanel();this.queueLayout();}
    closePanel(returnFocus){const target=this.selectedEdge?(this.edgeButtons.get(this.selectedEdge)||(!this.returnReason.hidden&&this.returnReason.dataset.edgeId===this.selectedEdge?this.returnReason:this.buttons.get(this.selected))):this.buttons.get(this.selected);this.selected=null;this.selectedEdge=null;this.panelKey='';this.panel?.remove();this.panel=null;this.buttons.forEach(b=>b.setAttribute('aria-expanded','false'));this.edgeButtons.forEach(b=>b.setAttribute('aria-expanded','false'));this.queueLayout();if(returnFocus)target?.focus({preventScroll:true});}
    renderPanel(){
      const node=this.graph.nodes.find(item=>item.id===this.selected);if(!node)return;
      const edge=this.selectedEdge?this.edges.find(item=>item.id===this.selectedEdge):null;
      const branch=null;
      const observed=edge&&edge.relation!=='route'&&edge.relation!=='prerequisite'&&(edge.evidence_refs||[]).length>0;
      const data=edge?{...node,label:node.label+' → '+(this.graph.nodes.find(n=>n.id===edge.to_node_id)?.label||''),summary:edge.reason_label||(observed?'Зафіксований зв’язок':(edge.condition_label?edge.condition_label+' · ':'')+'Можливий зв’язок; не підтверджує перехід'),facts:edge.facts||[],evidence_refs:edge.evidence_refs||[]}:planned(node)?{...node,summary:'Заплановано'+(node.implementation_note?' · '+node.implementation_note:'')}:node;
      const events=((this.graph.history||this.snapshot.history||{}).events||[]).filter(item=>edge?(edge.event_ids||[]).includes(item.id):item.node_id===this.selected);
      const returns=!this.modal&&edge?.relation==='return'?this.edges.filter(item=>item.relation==='return'&&witnessed(item)&&this.positions?.has(item.from_node_id)&&this.positions.has(item.to_node_id)):[];
      const key=JSON.stringify([this.snapshot.viewed_episode_id,data,events,this.selectedEdge,returns]);if(this.panelKey===key)return;this.panelKey=key;
      const oldOpen=this.panel?.querySelector('details')?.open||false,oldScroll=this.panel?.querySelector('.twc-journey-panel-body')?.scrollTop||0;
      const hadCloseFocus=this.panel?.querySelector('.twc-journey-close')===document.activeElement;
      const hadSummaryFocus=this.panel?.querySelector('summary')===document.activeElement;
      if(this.panel)this.panel.remove();this.panel=el('section','twc-journey-panel');this.panel.id='twc-journey-panel-'+this.snapshot.client_id;this.panel.setAttribute('aria-label',data.label+' — подробиці');
      this.buttons.forEach(button=>button.removeAttribute('aria-controls'));this.buttons.get(this.selected)?.setAttribute('aria-controls',this.panel.id);
      const head=el('div','twc-journey-panel-head'),copy=el('div');copy.append(el('h4','',data.label+(branch?' · '+branch.label:'')));if(data.summary)copy.append(el('p','twc-journey-panel-summary',data.summary));const close=el('button','twc-journey-close','×');close.type='button';close.setAttribute('aria-label','Закрити підетапи');close.addEventListener('click',()=>this.closePanel(true));head.append(copy,close);this.panel.append(head);
      const body=el('div','twc-journey-panel-body'),facts=el('div','twc-journey-facts');
      if(returns.length>1){
        const label=el('label','twc-journey-return-picker','Повернення з причиною'),picker=el('select');picker.setAttribute('aria-label','Повернення з причиною');
        returns.forEach(item=>{const from=this.graph.nodes.find(n=>n.id===item.from_node_id),to=this.graph.nodes.find(n=>n.id===item.to_node_id);picker.append(new Option((from?.label||'')+' → '+(to?.label||'')+' · '+(item.reason_label||'Уточнення'),item.id));});picker.value=edge.id;
        picker.addEventListener('change',()=>{this.selectEdge(picker.value);this.panel?.querySelector('.twc-journey-return-picker select')?.focus({preventScroll:true});});label.append(picker);body.append(label);
      }
      if(!edge&&recorded(node)){const visits=node.recorded_visits;const heading=(visits.has_backfilled?'Є відновлені записи подій':'Є збережені події')+' · '+(visits.history_truncated?'≥':'')+visits.count;body.append(el('p','twc-journey-visit-note',heading));if(visits.last_at)body.append(el('p','twc-journey-fact-note','Остання подія: '+date(visits.last_at)));}
      if(!edge)this.appendRequirements(body,node);
      if(!edge&&this.modal){const options=this.edges.filter(e=>e.from_node_id===node.id&&e.relation==='route');if(options.length){const section=el('div','twc-journey-options');section.append(el('h5','','Можливі продовження'));options.forEach(e=>{const target=this.graph.nodes.find(n=>n.id===e.to_node_id),button=el('button','', (e.condition_label?e.condition_label+' → ':'→ ')+(target?.label||''));button.type='button';button.addEventListener('click',()=>this.selectEdge(e.id));section.append(button);});body.append(section);}}
      (data.facts||[]).filter(fact=>!branch||branch.facts.includes(fact.id)).forEach(fact=>{const row=el('div','twc-journey-fact');row.dataset.factId=fact.id;row.dataset.tone=['success','warning'].includes(fact.tone)?fact.tone:'neutral';row.dataset.state=STATES.has(fact.state)?fact.state:'partial';const label=el('div','twc-journey-fact-label');label.append(el('i','twc-journey-fact-mark'),el('span','',fact.label));const value=el('div','twc-journey-fact-value',fact.format==='datetime'?(date(fact.value)||'Час не зафіксовано'):valueText(fact.value));if(fact.captured_at)value.append(el('div','twc-journey-fact-note',date(fact.captured_at)));row.append(label,value);facts.append(row);});
      if(!facts.children.length)facts.append(el('p','twc-journey-empty','Підтверджених даних цього етапу ще немає.'));body.append(facts);if(!edge)this.appendTimerDetails(body,node);
      const details=el('details');details.open=oldOpen;details.append(el('summary','','Історія та джерела'));const history=el('ol','twc-journey-history');
      events.forEach(event=>{const item=el('li'),copy=el('div','',event.label||'Подія');const when=el('time','',date(event.occurred_at||event.recorded_at));if(event.occurred_at)when.dateTime=event.occurred_at;copy.append(when);this.appendSources(copy,event.evidence_refs||[]);item.append(copy);history.append(item);});
      if(!events.length)history.append(el('li','','Окремих подій для цього етапу ще не зафіксовано.'));details.append(history);
      const refs=data.evidence_refs||[];if(refs.length){const sources=el('div');this.appendSources(sources,refs);details.append(sources);}
      if((this.snapshot.history||{}).has_more)details.append(el('p','twc-journey-coverage','Показано останні події. Повна історія зберігається у джерелах.'));body.append(details);this.panel.append(body);(this.modal||this.root).append(this.panel);body.scrollTop=oldScroll;
      if(hadCloseFocus)close.focus({preventScroll:true});if(hadSummaryFocus)details.querySelector('summary').focus({preventScroll:true});
    }
    appendSources(root,refs){const seen=new Set();refs.slice(0,8).forEach(ref=>{const key=ref.kind+':'+ref.id;if(seen.has(key)||!ref.id)return;seen.add(key);const labels={message:'Повідомлення',source_message:'Повідомлення',order:'Замовлення',funnel_event:'Подія',episode_event:'Подія',episode:'Покупка',payment_projection:'Оплата',payment_review:'Перевірка оплати'};const label=(labels[ref.kind]||'Джерело')+' №'+ref.id;const actionable=this.options.onEvidence&&['message','source_message'].includes(ref.kind);const link=el(actionable?'button':'span','twc-journey-source',label);if(actionable){link.type='button';link.addEventListener('click',()=>{if(this.modal)this.closeMap();this.closePanel(false);this.options.onEvidence(ref,link);});}root.append(link);});}
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
        const line=el('div','twc-journey-timer-detail');line.append(icon('clock'),el('span','',timer.label||'Очікування'));
        const state=el('span','twc-journey-timer-state',this.timerDescription(timer));state.dataset.timerState=timer.id||timer.kind||'';line.append(state);
        const due=date(timer.due_at);if(due)line.append(el('time','','До '+due));body.append(line);
      }
    }
    updateTimers(){
      if(!this.graph)return;const serverNow=Number.isFinite(this.serverTime)?this.serverTime+(performance.now()-this.serverAnchor):null;
      this.graph.nodes.forEach(node=>{
        const button=this.buttons.get(node.id);if(!button)return;
        const timer=(node.timers||[]).find(t=>['running','scheduled','paused','expired','unknown'].includes(t.status));
        let ring=button.querySelector('.twc-journey-timer');
        if(!timer){ring?.remove();delete button.dataset.timer;return;}
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
    openMap(){
      if(this.modal||!this.snapshot)return;this.closePanel(false);
      this.showPossible=true;this.possibleFamily='all';
      const dialog=el('dialog','twc-journey twc-journey-dialog');dialog.setAttribute('aria-label','Повний шлях клієнта');
      const top=el('header','twc-journey-dialog-head'),copy=el('div');copy.append(el('h3','','Шлях клієнта'),el('p','',this.title.textContent+this.mode.textContent));
      const close=el('button','twc-journey-close','×');close.type='button';close.setAttribute('aria-label','Закрити повну карту');close.addEventListener('click',()=>this.closeMap());top.append(copy,close);
      const tools=el('div','twc-journey-tools');const action=(label,fn)=>{const b=el('button','',label);b.type='button';b.addEventListener('click',fn);tools.append(b);return b;};
      const minus=action('−',()=>this.setZoom(this.zoom/1.2));minus.setAttribute('aria-label','Зменшити карту');const plus=action('+',()=>this.setZoom(this.zoom*1.2));plus.setAttribute('aria-label','Збільшити карту');this.zoomLabel=el('span','','100%');tools.append(this.zoomLabel);action('Умістити',()=>this.fitMap());action('До поточного',()=>this.centerCurrent());
      this.possibilities=el('button','','Можливі шляхи');this.possibilities.type='button';this.possibilities.setAttribute('aria-pressed',String(Boolean(this.showPossible)));this.possibilities.addEventListener('click',()=>{this.closePanel(false);this.showPossible=!this.showPossible;this.possibilities.setAttribute('aria-pressed',String(this.showPossible));this.familySelect.hidden=!this.showPossible;this.update(this.snapshot,{force:true});this.layout();this.centerCurrent();});tools.append(this.possibilities);
      this.familySelect=el('select');this.familySelect.setAttribute('aria-label','Напрям можливих шляхів');[['inbound','Напрями звернень'],['catalog','Одяг і оплата'],['custom','Власний принт / DTF'],['collaboration','Співпраця'],['employment','Робота в команді'],['information','Інформація'],['prize','Приз'],['after','Після покупки'],['all','Усі напрями']].forEach(([value,label])=>this.familySelect.append(new Option(label,value)));this.familySelect.value=this.possibleFamily||'inbound';this.familySelect.hidden=!this.showPossible;this.familySelect.addEventListener('change',()=>{this.closePanel(false);this.possibleFamily=this.familySelect.value;this.update(this.snapshot,{force:true});this.layout();this.centerCurrent();});tools.append(this.familySelect);
      this.viewport=el('div','twc-journey-viewport');this.viewport.tabIndex=0;this.viewport.setAttribute('aria-label','Карта. Стрілки для переміщення; кнопки плюс і мінус для масштабу.');this.canvas=el('div','twc-journey-canvas');this.canvas.append(this.map);this.viewport.append(this.canvas);
      this.accessible=el('details','twc-journey-accessible');this.accessible.append(el('summary','','Етапи й переходи списком'));this.accessibleBody=el('div');this.accessible.append(this.accessibleBody);
      const legend=el('div','twc-journey-map-legend');[['recorded','○ Є збережені події'],['recorded','━ Збережений перехід'],['success','✓ Підтверджено'],['danger','↶ Повернення / негативний результат'],['warning','↻ Повторна спроба'],['neutral','┄ Можливий шлях']].forEach(([tone,label])=>{const item=el('span','',label);item.dataset.tone=tone;legend.append(item);});
      this.mapCoverage=el('p','twc-journey-map-coverage');dialog.append(top,tools,legend,this.mapCoverage,this.viewport,this.accessible);this.modal=dialog;document.body.append(dialog);
      dialog.addEventListener('keydown',this.escape);dialog.addEventListener('cancel',event=>{event.preventDefault();if(this.selected)this.closePanel(true);else this.closeMap();});dialog.addEventListener('click',event=>{if(event.target===dialog){const r=dialog.getBoundingClientRect();if(event.clientX<r.left||event.clientX>r.right||event.clientY<r.top||event.clientY>r.bottom)this.closeMap();}});
      this.viewport.addEventListener('wheel',event=>{if(event.ctrlKey){event.preventDefault();this.setZoom(this.zoom*Math.exp(-event.deltaY*.01));}},{passive:false});
      this.bindPan();this.modalResize=new ResizeObserver(()=>this.queueLayout());this.modalResize.observe(this.viewport);this.oldBodyOverflow=document.body.style.overflow;document.body.style.overflow='hidden';dialog.showModal();this.zoom=1;this.update(this.snapshot,{force:true});this.layout();this.fitMap();close.focus({preventScroll:true});
    }
    bindPan(){
      const points=new Map();let drag=null,pinch=null;
      this.viewport.addEventListener('pointerdown',event=>{if(event.target.closest('button'))return;points.set(event.pointerId,{x:event.clientX,y:event.clientY});this.viewport.setPointerCapture(event.pointerId);if(points.size===1)drag={x:event.clientX,y:event.clientY,left:this.viewport.scrollLeft,top:this.viewport.scrollTop};if(points.size===2){const [a,b]=[...points.values()];pinch={distance:Math.hypot(a.x-b.x,a.y-b.y),zoom:this.zoom};drag=null;}});
      this.viewport.addEventListener('pointermove',event=>{if(!points.has(event.pointerId))return;points.set(event.pointerId,{x:event.clientX,y:event.clientY});if(points.size===2&&pinch){const [a,b]=[...points.values()];if(pinch.distance>0)this.setZoom(pinch.zoom*Math.hypot(a.x-b.x,a.y-b.y)/pinch.distance);}else if(drag){this.viewport.scrollLeft=drag.left-(event.clientX-drag.x);this.viewport.scrollTop=drag.top-(event.clientY-drag.y);}});
      const end=event=>{points.delete(event.pointerId);drag=null;pinch=null;};this.viewport.addEventListener('pointerup',end);this.viewport.addEventListener('pointercancel',end);
    }
    closeMap(){
      if(!this.modal)return;this.closePanel(false);this.modalResize.disconnect();this.root.insertBefore(this.map,this.note);this.map.style.transform='';this.modal.close();this.modal.remove();this.modal=null;document.body.style.overflow=this.oldBodyOverflow;this.zoom=1;this.update(this.snapshot,{force:true});this.queueLayout();this.expand.focus({preventScroll:true});
    }
    setZoom(value){
      if(!this.modal)return;const next=Math.max(.1,Math.min(2.2,value));const cx=(this.viewport.scrollLeft+this.viewport.clientWidth/2)/this.zoom,cy=(this.viewport.scrollTop+this.viewport.clientHeight/2)/this.zoom;this.zoom=next;this.map.style.transform='scale('+next+')';this.canvas.style.width=this.mapWidth*next+'px';this.canvas.style.height=this.mapHeight*next+'px';this.zoomLabel.textContent=Math.round(next*100)+'%';this.viewport.scrollLeft=cx*next-this.viewport.clientWidth/2;this.viewport.scrollTop=cy*next-this.viewport.clientHeight/2;
    }
    fitMap(){if(this.modal)this.setZoom(Math.min(1,(this.viewport.clientWidth-24)/this.mapWidth,(this.viewport.clientHeight-24)/this.mapHeight));}
    centerCurrent(){if(!this.modal)return;const pos=this.positions.get(this.currentId)||this.positions.values().next().value;if(!pos)return;this.setZoom(1);this.viewport.scrollLeft=Math.max(0,pos.x-this.viewport.clientWidth/2);this.viewport.scrollTop=Math.max(0,pos.y-this.viewport.clientHeight/2);}
    renderAccessibleList(){
      if(!this.accessibleBody)return;this.accessibleBody.replaceChildren();this.graph.nodes.forEach(node=>{const b=el('button','',node.label+' · '+nodeStatusLabel(node));b.type='button';b.addEventListener('click',()=>this.toggleNode(node.id));this.accessibleBody.append(b);});
      this.edges.forEach(edge=>{const a=this.graph.nodes.find(n=>n.id===edge.from_node_id),b=this.graph.nodes.find(n=>n.id===edge.to_node_id);const item=el('button','',a.label+' → '+b.label+' · '+(edge.reason_label||(witnessed(edge)?'Зафіксований перехід':(edge.condition_label?edge.condition_label+' · ':'')+'Можливий шлях')));item.type='button';item.addEventListener('click',()=>this.selectEdge(edge.id));this.accessibleBody.append(item);});
      if(!this.edges.length)this.accessibleBody.append(el('p','','Переходи ще не зафіксовано. Положення етапів не підтверджує послідовність подій.'));
    }
    queueLayout(){cancelAnimationFrame(this.frame);this.frame=requestAnimationFrame(()=>this.layout());}
    layout(){
      if(this.destroyed||!this.root.isConnected||!this.graph)return;
      const width=this.modal?this.viewport.clientWidth:this.root.getBoundingClientRect().width;this.syncVisibility(width);
      const nodes=this.graph.nodes.filter(n=>this.visibleIds.includes(n.id));const narrow=!this.modal&&width<560;this.map.dataset.narrow=String(narrow);this.map.dataset.single=String(nodes.length===1);
      this.positions=new Map();
      if(!this.modal&&this.inlineSlots){
        const columns=Math.max(1,...[...this.inlineSlots.values()].map(p=>p.col+1)),step=width/columns;
        nodes.forEach(node=>{const slot=this.inlineSlots.get(node.id),x=step*(slot.col+.5),y=30+slot.row*70;this.positions.set(node.id,{x,y,...slot});const cell=this.cells.get(node.id);cell.style.left=x+'px';cell.style.top=y+'px';cell.style.width=Math.max(44,step-12)+'px';});
        this.mapWidth=width;this.mapHeight=nodes.some(n=>this.inlineSlots.get(n.id).row)?142:68;
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
          this.map.dataset.single='true';this.map.dataset.narrow='true';this.mapHeight=58;this.expand.textContent='Весь шлях · '+this.nodeIds.length+' ↗';
        }this.mapHeight=Math.min(192,this.mapHeight);
      }
      this.map.style.width=this.mapWidth+'px';this.map.style.height=this.mapHeight+'px';
      if(this.modal){this.map.style.transform='scale('+this.zoom+')';this.canvas.style.width=this.mapWidth*this.zoom+'px';this.canvas.style.height=this.mapHeight*this.zoom+'px';}
      this.drawGuides();this.positionPanel();
    }
    drawGuides(){
      this.svg.setAttribute('viewBox','0 0 '+this.mapWidth+' '+this.mapHeight);this.svg.replaceChildren();const tones={neutral:'#64748b',recorded:'#60a5fa',success:'#34d399',warning:'#fbbf24',danger:'#fb7185',manager:'#a78bfa'},prefix='journey-'+this.snapshot.client_id;
      const defs=svg('defs');Object.entries(tones).forEach(([tone,color])=>{const marker=svg('marker',{id:prefix+'-'+tone,viewBox:'0 0 6 6',refX:5,refY:3,markerWidth:5,markerHeight:5,orient:'auto'});marker.append(svg('path',{d:'M1 1L5 3L1 5',fill:'none',stroke:color,'stroke-width':1}));defs.append(marker);});this.svg.append(defs);
      const shown=new Set();
      const inlineReturns=[];
      const visibleNodes=this.graph.nodes.filter(n=>this.positions.has(n.id));
      const geometryRoutes=window.TwcJourneyGeometry?.routeEdges({nodes:visibleNodes,edges:this.displayEdges||this.edges,positions:this.positions,width:this.mapWidth,height:this.mapHeight,full:Boolean(this.modal)});
      [...(this.displayEdges||this.edges)].sort((a,b)=>Number(witnessed(a))-Number(witnessed(b))).forEach(edge=>{
        const a=this.positions.get(edge.from_node_id),b=this.positions.get(edge.to_node_id);if(!a||!b)return;
        const observed=witnessed(edge),tone=edgeTone(edge);
        const route=geometryRoutes?.get(edge.id);if(!route)return;
        const d=route.d,mx=route.markerX,my=route.markerY;
        const path=svg('path',{d,'marker-end':'url(#'+prefix+'-'+tone+')',stroke:tones[tone]});path.classList.add('twc-journey-edge');path.dataset.observed=String(observed);path.dataset.selected=String(this.selectedEdge===edge.id);this.svg.append(path);
        path.append(svg('title'));path.lastChild.textContent=edge.condition_label||edge.reason_label||(observed?'Збережений перехід':'Можливий шлях');
        if(route.conditionPosition){const p=route.conditionPosition,label=svg('text',{x:p.x,y:p.y,'text-anchor':'middle','dominant-baseline':'middle'});label.classList.add('twc-journey-condition');label.textContent=edge.outcome==='wait_for_attempt'?'Нова спроба':'Допомога';this.svg.append(label);}
        if(this.newEdges?.has(edge.id)&&!matchMedia('(prefers-reduced-motion: reduce)').matches){const dot=svg('circle',{r:2,fill:tones[tone]});const motion=svg('animateMotion',{dur:'.6s',path:d,repeatCount:1,fill:'freeze'});dot.append(motion);this.svg.append(dot);motion.addEventListener('endEvent',()=>dot.remove(),{once:true});}
        const count=Number.isInteger(edge.repeated_count)&&edge.repeated_count>1?edge.repeated_count:0;
        if(!this.modal&&this.inlineSlots&&observed&&edge.relation==='return'){
          // A 44px marker on the narrow outer return corridor would overlap the
          // chat heading. Keep the actual arrow and its disclosure in the key.
          inlineReturns.push(edge);
          return;
        }
        if(edge.structural_path||(!edge.reason_label&&!count&&edge.relation!=='return'))return;shown.add(edge.id);let button=this.edgeButtons.get(edge.id);
        if(!button){button=el('button','twc-journey-edge-marker');button.type='button';button.addEventListener('click',()=>this.selectEdge(edge.id));this.edgeButtons.set(edge.id,button);this.edgeLayer.append(button);}
        button.replaceChildren(icon(edge.relation==='return'?'return':observed?'question':'info'));if(count)button.append(el('span','','×'+count));button.style.left=mx+'px';button.style.top=my+'px';button.dataset.tone=tone;button.title=edge.reason_label||'Подробиці переходу';button.setAttribute('aria-label',(edge.reason_label||'Подробиці переходу')+(count?' · переходів '+count:''));button.setAttribute('aria-expanded',String(this.selectedEdge===edge.id));
      });
      this.inlineReturnIds=inlineReturns.map(edge=>edge.id);
      const inlineReturn=inlineReturns.find(edge=>edge.id===this.selectedEdge)||inlineReturns[0];
      if(inlineReturn){const count=Number.isInteger(inlineReturn.repeated_count)&&inlineReturn.repeated_count>1?inlineReturn.repeated_count:0;this.returnReason.dataset.edgeId=inlineReturn.id;this.returnReason.dataset.tone=edgeTone(inlineReturn);this.returnReason.textContent='↶ '+(inlineReturn.reason_label||'Уточнення')+(count?' ×'+count:'')+(inlineReturns.length>1?' · ще '+(inlineReturns.length-1):'');this.returnReason.title=this.returnReason.textContent;this.returnReason.setAttribute('aria-expanded',String(this.selectedEdge===inlineReturn.id));}
      this.returnReason.hidden=!inlineReturn;this.inlineKey.dataset.hasReturn=String(Boolean(inlineReturn));
      this.newEdges?.clear();for(const [id,b]of this.edgeButtons)if(!shown.has(id)){b.remove();this.edgeButtons.delete(id);}
    }
    positionPanel(){
      if(!this.panel)return;const mobile=innerWidth<600;this.panel.dataset.mobile=String(mobile);if(mobile){this.panel.style.left='12px';this.panel.style.right='12px';this.panel.style.bottom='12px';this.panel.style.top='auto';return;}
      const target=this.selectedEdge?(this.edgeButtons.get(this.selectedEdge)||(!this.returnReason.hidden&&this.returnReason.dataset.edgeId===this.selectedEdge?this.returnReason:this.buttons.get(this.selected))):this.buttons.get(this.selected);if(!target)return;const r=target.getBoundingClientRect(),w=Math.min(340,innerWidth-24);this.panel.style.width=w+'px';this.panel.style.left=Math.max(12,Math.min(innerWidth-w-12,r.left+r.width/2-w/2))+'px';this.panel.style.right='auto';this.panel.style.bottom='auto';const h=this.panel.getBoundingClientRect().height;this.panel.style.top=Math.max(12,Math.min(innerHeight-h-12,r.bottom+10))+'px';
    }
    destroy(){this.closeMap();this.destroyed=true;this.sequence++;clearInterval(this.timerInterval);cancelAnimationFrame(this.frame);this.resize.disconnect();document.removeEventListener('visibilitychange',this.visibility);document.removeEventListener('pointerdown',this.outside);window.removeEventListener('scroll',this.reposition,true);this.root.remove();}
  }
  window.TwcJourney={create:options=>new Journey(options)};
})();
