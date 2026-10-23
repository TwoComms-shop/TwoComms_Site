const test=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const crypto=require('node:crypto');
const source=fs.readFileSync(path.join(__dirname,'../../twocomms/management/static/management/js/ig_fulfillment_review.js'),'utf8');
const template=fs.readFileSync(path.join(__dirname,'../../twocomms/management/templates/management/bot.html'),'utf8');
const A='11111111-1111-4111-8111-111111111111',B='22222222-2222-4222-8222-222222222222';
// Shared lightweight DOM surface used by the existing native-controller tests.
class Node {
  constructor(tag, document) {
    this.tagName = tag; this.ownerDocument = document; this.children = [];
    this.parentNode = null; this.dataset = {}; this.attributes = {}; this.listeners = {};
    this.className = ''; this._text = ''; this.value = ''; this.disabled = false;
    this.hidden = false; this.checked = false;
    this.classList = {
      add: name => this.classList.toggle(name, true),
      contains: name => this.className.split(/\s+/).includes(name),
      toggle: (name, force) => {
        const values = new Set(this.className.split(/\s+/).filter(Boolean));
        const add = force === undefined ? !values.has(name) : Boolean(force);
        if (add) values.add(name); else values.delete(name);
        this.className = [...values].join(' '); return add;
      }
    };
  }
  append(...nodes) {
    nodes.forEach(node => {
      if (typeof node === 'string') { const text = new Node('#text', this.ownerDocument); text.textContent = node; node = text; }
      node.remove(); node.parentNode = this; this.children.push(node);
    });
  }
  appendChild(node) { this.append(node); return node; }
  remove() {
    if (this.parentNode) this.parentNode.children = this.parentNode.children.filter(node => node !== this);
    this.parentNode = null;
  }
  replaceChildren(...nodes) {
    this.children.forEach(node => { node.parentNode = null; }); this.children = []; this._text = ''; this.append(...nodes);
  }
  after(node) {
    const parent = this.parentNode; if (!parent) return;
    node.remove(); node.parentNode = parent; parent.children.splice(parent.children.indexOf(this) + 1, 0, node);
  }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  addEventListener(name, handler) { (this.listeners[name] ||= []).push(handler); }
  get textContent() { return this._text + this.children.map(node => node.textContent).join(''); }
  set textContent(value) { this.replaceChildren(); this._text = String(value); }
  get firstChild() {
    if (!this._text) return this.children[0];
    const node = this;
    return { get textContent() { return node._text; }, set textContent(value) { node._text = String(value); } };
  }
  all() { return [this, ...this.children.flatMap(node => node.all())]; }
  matches(selector) {
    if (selector.startsWith('.')) return this.classList.contains(selector.slice(1));
    const match = selector.match(/^([a-z]+)?(?:\[([\w-]+)(?:="([^"]*)")?\])?$/);
    if (!match || match[1] && this.tagName !== match[1]) return false;
    if (!match[2]) return Boolean(match[1]);
    const name = match[2];
    const value = name.startsWith('data-') ? this.dataset[name.slice(5).replace(/-([a-z])/g, (_, letter) => letter.toUpperCase())]
      : this.attributes[name] ?? this[name];
    return match[3] === undefined ? value !== undefined : String(value) === match[3];
  }
  querySelectorAll(selector) {
    const selectors = selector.split(',').map(value => value.trim());
    return this.all().slice(1).filter(node => selectors.some(value => node.matches(value)));
  }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  focus() { this.ownerDocument.activeElement = this; }
  emit(type) {
    const event = { target: this, preventDefault() {} };
    for (let node = this; node; node = node.parentNode) (node.listeners[type] || []).forEach(handler => handler(event));
  }
  input(value) {
    assert.equal(this.disabled, false, 'a user cannot edit a disabled count');
    this.value = String(value); this.emit('input');
  }
  click() {
    if (this.disabled || this.hidden) return;
    if (this.tagName === 'input' && this.type === 'checkbox') { this.checked = !this.checked; this.emit('change'); }
    this.emit('click');
    if (this.tagName === 'button' && this.type === 'submit') {
      let form = this.parentNode; while (form && form.tagName !== 'form') form = form.parentNode;
      form?.emit('submit');
    }
  }
}

Node.prototype.contains=function(node){return this.all().includes(node);};
Object.defineProperty(Node.prototype,'isConnected',{get(){for(let node=this;node;node=node.parentNode)if(node.connected)return true;return false;}});
Node.prototype.getAttribute=function(name){return this.attributes[name]??null;};
function pending(id=A){return {status:'pending',can_decide:true,decision_recorded:false,decision_mode:'initial',request_digest:'digest-'+id,
 source_excerpts:[{message_id:11,text:'Чи є чорна футболка L? <script>fake()</script>',event_at:'2026-10-23T10:00:00Z'}],
 current_request:{id,scope:'cart',sources:[{message_id:11}],customer_requirements:[],lines:[{line_id:'line-1',display_label:'Футболка Tokyo',label:'Футболка',target:{quantity:1,size:'L',fit:'classic',color_label:'Чорний'},observation:{status:'unavailable'}}]},decision:null};}
function reviewed(){const value=pending();return {...value,status:'reviewed',can_decide:false,decision_recorded:true,decision_mode:'none',decision:{id:'saved',version:1,request_digest:value.request_digest,lines:[{line_id:'line-1',availability:'unavailable'}]}};}
function http(value,status=200){return {status,ok:status>=200&&status<300,json:async()=>value};}
function fixture(options={}){
 const document={activeElement:null,createElement(tag){return new Node(tag,this);}};
 document.body=new Node('body',document);document.body.connected=true;
 document.getElementById=id=>document.body.all().find(node=>node.id===id)||null;
 const root=new Node('section',document);document.body.append(root);
 const calls=[],replies=[],states=[];let notify;
 const fetch=async(url,options={})=>{calls.push({url:String(url),method:options.method||'GET',options});assert.ok(replies.length,'queued native API response required');const value=replies.shift();if(value instanceof Error)throw value;if(typeof value==='function')return value();return value;};
 const window={location:{origin:'https://management.example',href:'https://management.example/bot/'},crypto};
 vm.runInNewContext(source,{window,document,fetch,URL,AbortController,Date,Intl,Uint8Array});
 const controller=window.IgFulfillmentReview.create(root,{canOperate:true,observeDirty:true,csrfToken:'fixture-only',url:id=>'/clients/'+id+'/fulfillment-review/',onStateChange:state=>{states.push(state);notify?.();},sourceUrl:(id,mid)=>'/bot/?client_id='+id+'&message_id='+mid,...options});
 const flush=async()=>{await new Promise(setImmediate);await new Promise(setImmediate);};
 async function load(review,id=12){replies.push(http({success:true,review}));controller.render(id);await flush();}
 return {document,root,controller,calls,replies,states,window,flush,load,posts:()=>calls.filter(call=>call.method==='POST'),setNotify:value=>{notify=value;}};
}
function control(root,label){const result=root.querySelector('[aria-label="'+label+'"]');assert.ok(result,label);return result;}
function choose(root,value){root.querySelector('[data-fr-availability="'+value+'"]').click();}
function submit(root){const button=root.querySelector('[type="submit"]');assert.ok(button);button.click();}
function placement(f){
 const context=new Node('div',f.document),conversation=new Node('div',f.document),head=new Node('header',f.document),messages=new Node('div',f.document);
 head.className='bot-conversation-head';messages.id='bot-msgs';f.document.body.append(context,conversation);context.append(f.root);conversation.append(head,messages);
 const restores=[];const sandbox={fulfillmentReviewController:f.controller,fulfillmentReviewRoot:f.root,fulfillmentReviewEntry:null,fulfillmentReviewEntryKey:'',activeId:12,convId:12,contextEl:context,conversationEl:conversation,document:f.document,
  node:(tag,cls,text)=>{const node=f.document.createElement(tag);node.className=cls||'';if(text!==undefined)node.textContent=text;return node;},
  globalFeedback:null,setGlobalFeedback(){},conversationIsDocumentScrolled:()=>false,window:{TwcBotChatReading:{capture:()=>({atBottom:false}),restore:(...args)=>restores.push(args)}}};
 vm.createContext(sandbox);const start=template.indexOf('    function placeFulfillmentReview(){'),end=template.indexOf('    function clearDecisionTrace(){',start);assert.ok(start>0&&end>start);vm.runInContext(template.slice(start,end),sandbox);
 f.setNotify(()=>sandbox.placeFulfillmentReview());
 return {sandbox,context,conversation,head,messages,restores,place:()=>sandbox.placeFulfillmentReview()};
}

test('one native pending form moves above chat and survives parent rebuild and dirty refresh without cloning',async()=>{
 const f=fixture(),p=placement(f);await f.load(pending());
 const entry=p.conversation.querySelector('[data-stock-review-entry]');assert.ok(entry);assert.equal(f.root.parentNode,entry);
 assert.match(entry.textContent,/Чи є чорна футболка L/);assert.match(entry.textContent,/Футболка Tokyo/);
 choose(f.root,'clarify');const field=control(f.root,'Пояснення або питання — Футболка');field.input('Уточніть бажаний колір');field.focus();
 const form=f.root.querySelector('form');p.conversation.replaceChildren(p.head,p.messages);p.context.replaceChildren();p.place();
 assert.equal(f.root.querySelector('form'),form);assert.equal(field.value,'Уточніть бажаний колір');assert.equal(f.document.activeElement,field);assert.equal(entry.open,true);
 f.replies.push(http({success:true,review:pending()}));f.controller.render(12);await f.flush();
 assert.equal(f.root.querySelector('form'),form);assert.equal(field.value,'Уточніть бажаний колір');assert.equal(f.posts().length,0);assert.ok(p.restores.length);
});

test('processed or expired decisions never leave a contextual initial-action entry even when status says pending',async()=>{
 const f=fixture(),p=placement(f);await f.load(pending());
 await f.load({...pending(),can_decide:false,decision_recorded:true,decision_mode:'none',decision_blocked_reason:'decision_already_recorded'});
 assert.equal(p.conversation.querySelector('[data-stock-review-entry]'),null);assert.equal(f.root.parentNode,p.context);assert.equal(f.root.querySelector('[type="submit"]'),null);
 await f.load({status:'none',can_decide:false,decision_mode:'none',current_request:null});assert.equal(p.conversation.querySelector('[data-stock-review-entry]'),null);
});

test('exact verified lining and other options remain visible before stock confirmation and in read-only review',async()=>{
 const f=fixture(),value=pending();value.current_request.lines[0].target.option_values={fit:'classic',lining:'fleece',material:'cotton <script>fictional()</script>',ignored:{nested:'not an option'}};
 await f.load(value);const target=f.root.querySelector('.ig-fr-line').querySelector('p');
 assert.match(target.textContent,/Утеплення: Фліс/);assert.match(target.textContent,/material: cotton <script>fictional\(\)<\/script>/);
 assert.doesNotMatch(target.textContent,/fit: classic|ignored/);assert.equal(target.querySelector('script'),null);
 const saved={...reviewed(),current_request:value.current_request};saved.current_request.lines[0].target.option_values.lining='no_fleece';await f.load(saved);
 assert.match(f.root.querySelector('.ig-fr-line').textContent,/Утеплення: Без флісу/);
 delete value.current_request.lines[0].target.option_values;value.current_request.lines[0].target.options={lining:'custom_lining'};await f.load(value);
 assert.match(f.root.querySelector('.ig-fr-line').textContent,/Утеплення: custom_lining/);
});

test('cases without excerpts reuse exact own source refs and preserve captured owner across client switches',async()=>{
 const jumps=[],compositions=[],f=fixture({onSource:(...args)=>jumps.push(args),onCompose:(...args)=>compositions.push(args)}),value=pending();
 value.source_excerpts=[];value.current_request.scope='restock_list_invitation';value.current_request.source_refs=[{message_id:22,url:'https://foreign.example/'}];
 await f.load(value);const link=f.root.querySelector('a'),compose=f.root.querySelectorAll('button').find(node=>node.textContent==='Написати окрему відповідь');
 assert.equal(link.href,'/bot/?client_id=12&message_id=22');assert.match(f.root.textContent,/Точний запит клієнта/);assert.doesNotMatch(f.root.textContent,/Чи є чорна футболка/);
 link.click();compose.click();assert.deepEqual(jumps,[[12,22]]);assert.deepEqual(compositions,[[12,22]]);
 await f.load(pending(B),13);link.click();compose.click();assert.deepEqual(jumps[1],[12,22]);assert.deepEqual(compositions[1],[12,22]);
 value.current_request.sources=[];value.current_request.source_refs=[];value.current_request.source_id=33;await f.load(value);
 assert.equal(f.root.querySelector('a').href,'/bot/?client_id=12&message_id=33');
});

test('available preset cannot save guessed physical stock; existing quantity and basis checks remain',async()=>{
 const f=fixture();await f.load(pending());choose(f.root,'available');submit(f.root);assert.equal(f.posts().length,0);
 control(f.root,'Перевірена кількість — Футболка').input('2');control(f.root,'Підстава наявності — Футболка').value='accessible_unrecorded';
 f.replies.push(http({success:true,review:reviewed()}));submit(f.root);await f.flush();
 const data=JSON.parse(f.posts()[0].options.body);assert.equal(data.action,undefined);assert.equal(data.expected_request_digest,'digest-'+A);
 assert.equal(data.decisions_by_line[0].checked_quantity,2);assert.equal(data.decisions_by_line[0].supply_basis,'accessible_unrecorded');assert.match(f.root.textContent,/Облік оновлюється окремою/);
});

test('one submission covers every mixed-cart line and preserves the requested quantity',async()=>{
 const f=fixture(),value=pending();value.current_request.lines.push({line_id:'line-2',display_label:'Худі · білий · M',label:'Худі',target:{quantity:2,size:'M'},observation:{status:'unavailable'}});
 await f.load(value);choose(f.root,'available');control(f.root,'Перевірена кількість — Футболка').input('2');control(f.root,'Підстава наявності — Футболка').value='warehouse_checked';
 submit(f.root);assert.equal(f.posts().length,0,'second line must be reviewed separately');
 f.root.querySelectorAll('[data-fr-availability="unavailable"]')[1].click();f.replies.push(http({success:true,review:reviewed()}));submit(f.root);await f.flush();
 const lines=JSON.parse(f.posts()[0].options.body).decisions_by_line;assert.equal(lines.length,2);assert.equal(lines[0].line_id,'line-1');assert.equal(lines[0].availability,'available');assert.equal(lines[1].line_id,'line-2');assert.equal(lines[1].availability,'unavailable');
});

test('existing private-draft entry preserves dirty work and creates no send command when opening a source-bound reply',()=>{
 let dirty=false,updates=[],newDocuments=0,focus=0,opened=0,notices=[];
 const sandbox={activeId:12,humanDocumentsClientId:12,csrf:'fixture-only',
  ClientContextDrawer:{open(){opened++;}},humanDocumentsDisclosure:{open:false},
  humanDocumentsController:{hasUnsavedChanges:()=>dirty,update:opts=>{updates.push(opts);return true;}},
  humanDocumentsRoot:{querySelector:selector=>selector==='[data-action="new"]'?{click(){newDocuments++;}}:{focus(){focus++;}}},
  setGlobalFeedback:(...args)=>notices.push(args),
  fetch:()=>assert.fail('opening existing draft UI must never send'),detail:()=>assert.fail('opening draft must not refresh/send'),
 };
 vm.createContext(sandbox);let start=template.indexOf('    function openFulfillmentReply('),end=template.indexOf('    function placeFulfillmentReview(',start);assert.ok(start>0&&end>start);
 const code=template.slice(start,end).replace(/\{% url "management_bot_clients_api" %\}/g,'/bot/api/clients/');vm.runInContext(code,sandbox);
 sandbox.openFulfillmentReply(12,11);assert.equal(opened,1);assert.equal(updates[0].contextMessageId,11);assert.equal(updates[0].clientId,12);assert.equal(newDocuments,1);assert.equal(focus,1);
 dirty=true;sandbox.openFulfillmentReply(12,22);assert.equal(updates.length,1);assert.equal(newDocuments,1);assert.equal(focus,2);
 assert.match(notices[0][0],/попередню приватну чернетку.*#22.*Нова відповідь ще не створена/);assert.equal(notices[0][1],'error');
 sandbox.activeId=13;sandbox.openFulfillmentReply(12,11);assert.equal(opened,2);assert.equal(newDocuments,1);
});

test('incoming-base 1/3/7-day estimates use only base_arrival and never become preparation, dispatch or accepted wait',async t=>{
 for(const days of ['1','3','7'])await t.test(days+' days',async()=>{
  const f=fixture();await f.load(pending());choose(f.root,'wait');const basis=control(f.root,'Підстава наявності — Футболка');basis.value='incoming_confirmed';basis.emit('change');
  control(f.root,'Очікуване надходження основи — термін').value=days;
  assert.equal(f.root.querySelector('[aria-label="Очікуване надходження основи — початок відліку"]'),null);
  f.replies.push(http({success:true,review:{...reviewed(),customer_waiting:{state:'proposed'}}}));submit(f.root);await f.flush();
  const line=JSON.parse(f.posts()[0].options.body).decisions_by_line[0];assert.deepEqual(line.base_arrival,{min_days:Number(days),max_days:Number(days),unit:'calendar'});
  assert.equal(line.preparation,null);assert.equal(line.dispatch,null);assert.equal(line.checked_quantity,undefined);assert.match(f.root.textContent,/Клієнт ще не погодив цей строк/);
 });
});

test('double-click and response-loss retry preserve one operation UUID and never issue a stock-sync action',async()=>{
 const f=fixture();await f.load(pending());choose(f.root,'unavailable');let reject;
 f.replies.push(()=>new Promise((_,r)=>{reject=r;}));submit(f.root);submit(f.root);assert.equal(f.posts().length,1);const body=f.posts()[0].options.body;
 reject(new Error('response lost'));await f.flush();f.replies.push(http({success:true,review:reviewed()}));submit(f.root);await f.flush();assert.equal(f.posts().length,2);assert.equal(f.posts()[1].options.body,body);
 assert.equal(JSON.parse(body).action,undefined);
});

test('dirty authority-only refresh removes processed entry while preserving and disabling old fields',async()=>{
 const f=fixture(),p=placement(f);await f.load(pending());choose(f.root,'clarify');const field=control(f.root,'Пояснення або питання — Футболка');field.input('Незбережений текст');
 f.replies.push(http({success:true,review:reviewed()}));f.controller.render(12);await f.flush();
 assert.equal(field.value,'Незбережений текст');assert.equal(field.disabled,true);assert.equal(f.controller.context.draftStale,true);assert.equal(p.conversation.querySelector('[data-stock-review-entry]'),null);
 assert.equal(f.posts().length,0);assert.equal(f.controller.canLeave(13),false);
});

test('late A request cannot mount its case or source text into selected B',async()=>{
 const f=fixture(),p=placement(f);let resolveA;
 f.replies.push(()=>new Promise(resolve=>{resolveA=resolve;}));f.controller.render(12);
 p.sandbox.activeId=13;p.sandbox.convId=13;await f.load({...pending(B),source_excerpts:[{message_id:22,text:'B exact question'}]},13);
 resolveA(http({success:true,review:pending()}));await f.flush();
 assert.equal(f.controller.clientId,13);assert.match(p.conversation.textContent,/B exact question/);assert.doesNotMatch(p.conversation.textContent,/Чи є чорна/);
});

test('deadline resolution preserves saved base arrival and strips only its captured anchor',async()=>{
 const f=fixture();const digest='a'.repeat(64),value=reviewed();value.can_decide=true;value.decision_mode='deadline_resolution';
 value.current_request.customer_requirements=[{deadline_hard:true,requirement_digest:digest,deadline_date:'2026-11-01',date_status:'resolved'}];value.current_request.active_hard_requirement_digest=digest;
 value.decision.lines=[{line_id:'line-1',availability:'wait',supply_basis:'incoming_confirmed',base_arrival:{min_days:7,max_days:7,unit:'calendar',anchor_at:'2026-10-23T10:00:00Z'},preparation:null,dispatch:null}];
 value.decision.deadline_review={outcome:'review_pending',requirement_digest:digest,line_ids:['line-1'],options:[]};await f.load(value);
 control(f.root,'Висновок щодо обов’язкового строку').value='impossible';f.replies.push(http({success:true,review:reviewed()}));submit(f.root);await f.flush();
 const data=JSON.parse(f.posts()[0].options.body).decisions_by_line;
 assert.deepEqual(data.lines[0].base_arrival,{min_days:7,max_days:7,unit:'calendar'});assert.equal(data.deadline_review.outcome,'impossible');
});

function configurationTask(id=71){return {id,kind:'manager_task',reason:'revision_case:product_configuration_review',status:'skipped',continue_url:'/existing/native/continue/',manager_review:{state:'pending',actionable:true,type:'product_configuration_review',client_id:12,task_id:id,source_message_ids:[44],question:'Чи є таке худі у зеленому кольорі? <script>fictional()</script>'}};}
function configurationFixture(){
 const f=fixture(),p=placement(f),frames=[],opened=[],revealed=[];
 f.document.getElementById=id=>f.document.body.all().find(node=>node.id===id)||null;
 p.sandbox.window.location={href:'https://manager.test/bot/?section=clients&client_id=12'};
 p.sandbox.URL=URL;p.sandbox.requestAnimationFrame=fn=>frames.push(fn);
 p.sandbox.ClientContextDrawer={open:trigger=>opened.push(trigger),close:()=>opened.push('closed')};
 p.sandbox.revealRequestedMessage=(...args)=>revealed.push(args);p.sandbox.requestedMessageId=null;
 const start=template.indexOf('    function isFollowupDeliveryReview('),end=template.indexOf('    function appendCapturedCheckoutOrder(',start);
 vm.runInContext(template.slice(start,end),p.sandbox);
 const urlStart=template.indexOf('    function fulfillmentSourceUrl('),urlEnd=template.indexOf('    function openFulfillmentReply(',urlStart);vm.runInContext(template.slice(urlStart,urlEnd),p.sandbox);
 const older=f.document.createElement('button');older.dataset.olderMessages='';p.conversation.appendChild(older);
 const row=f.document.createElement('div');row.dataset.followupId='71';row.tabIndex=-1;row.scrollIntoView=()=>{row.scrolled=true;};p.context.appendChild(row);
 return {...f,...p,frames,opened,revealed,row,draw:followups=>p.sandbox.renderConfigurationReviewShortcuts({client:{id:12},followups})};
}

test('current configuration case gets one contextual shortcut to its existing exact row and source, without posting',()=>{
 const f=configurationFixture(),task=configurationTask();f.draw([task,task]);
 const root=f.conversation.querySelector('[data-configuration-review-shortcuts]'),button=root.querySelector('button');
 assert.equal(root.querySelectorAll('button').length,1);assert.match(root.textContent,/зеленому кольорі/);assert.equal(root.querySelector('script'),null);
 f.draw([task]);assert.equal(f.conversation.querySelector('[data-configuration-review-shortcuts]'),root);assert.equal(root.querySelector('button'),button);
 const draft=f.document.createElement('textarea');draft.value='Незбережена чернетка';f.context.appendChild(draft);
 button.click();assert.equal(f.opened[0],button);f.frames.shift()();assert.equal(f.document.activeElement,f.row);assert.equal(f.row.scrolled,true);assert.equal(draft.value,'Незбережена чернетка');
 f.sandbox.renderConfigurationReviewSources(f.row,task,12);const link=f.row.querySelector('a');assert.equal(link.href,'/bot/?section=clients&client_id=12&message_id=44');link.click();assert.equal(f.sandbox.requestedMessageId,44);assert.equal(f.revealed.length,1);assert.equal(f.posts().length,0);
 const olderTasks=Array.from({length:12},(_,i)=>({id:100+i,status:'completed'}));const visible=f.sandbox.visibleFollowups([...olderTasks,task,task],12);assert.equal(visible[0],task);assert.equal(visible.filter(item=>item===task).length,1);assert.equal(visible.length,8);
 assert.match(template,/visibleFollowups\(d.followups,id\)/);assert.equal((template.match(/renderConfigurationReviewShortcuts\(d\);/g)||[]).length,2);
});

test('missing, historical, processed, unowned and malformed configuration projections never raise a shortcut',()=>{
 const f=configurationFixture();
 for(const change of [item=>{item.manager_review=null;},item=>{item.manager_review.state='historical';},item=>{item.manager_review.actionable=false;},item=>{item.status='completed';},item=>{item.status='cancelled';},item=>{item.manager_review.client_id=13;},item=>{item.manager_review.task_id=72;},item=>{item.manager_review.source_message_ids=[];},item=>{item.manager_review.source_message_ids=['44'];},item=>{item.reason='revision_case:product_information_review';}]){
  f.draw([configurationTask()]);const item=configurationTask();change(item);f.draw([item]);assert.equal(f.conversation.querySelector('[data-configuration-review-shortcuts]'),null);
 }
 assert.ok(f.restores.length,'removing controls restores an older reading position');assert.equal(f.calls.length,0);
});

test('deferred configuration navigation and source links cannot focus or inspect A after B selection or processing',()=>{
 const f=configurationFixture(),task=configurationTask();f.draw([task]);const button=f.conversation.querySelector('[data-configuration-review-shortcuts]').querySelector('button');button.click();
 f.sandbox.activeId=13;f.sandbox.convId=13;f.frames.shift()();assert.notEqual(f.document.activeElement,f.row);assert.equal(f.row.scrolled,undefined);
 f.sandbox.renderConfigurationReviewSources(f.row,task,12);f.row.querySelector('a').click();assert.equal(f.revealed.length,0);
 f.sandbox.activeId=12;f.sandbox.convId=12;f.draw([task]);button.click();f.draw([{...task,status:'completed',manager_review:null}]);f.frames.shift()();assert.notEqual(f.document.activeElement,f.row);
 const opened=f.opened.length;button.click();assert.equal(f.opened.length,opened);assert.equal(f.calls.length,0);
});
