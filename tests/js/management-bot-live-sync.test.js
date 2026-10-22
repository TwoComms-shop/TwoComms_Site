const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const template = fs.readFileSync(path.join(__dirname, '../../twocomms/management/templates/management/bot.html'), 'utf8');
function block(start, end) { return template.slice(template.indexOf(start), template.indexOf(end, template.indexOf(start))); }
function deferred() { let resolve; const promise = new Promise(r => { resolve = r; }); return { promise, resolve }; }
function harness() {
  const calls = [], renders = [], increments = [];
  const mc = { offsetParent: {}, isConnected: true };
  const pane = { replaceChildren() {}, children: [], scrollTop: 0 };
  const sandbox = {
    AbortController, Set, Date, console,
    fetch(url, options) { const wait = deferred(); calls.push({ url, options, wait }); return wait.promise; },
    document: { hidden: false, activeElement: null, getElementById() { return mc; } },
    window: {}, base: '/clients/', contextEl: pane, conversationEl: pane,
    canLeaveAssistedCommerce: () => true, canLeaveHumanDocuments: () => true,
    fulfillmentReviewController: null, splitShippingController: null, sizeCorrectionController: null,
    clearDecisionTrace() {}, node() {}, journeyQuery: () => '', journeyView: null,
    readJsonResponse: response => response.json(), globalFeedback: null, setGlobalFeedback() {},
    OrderAssignmentDrawer: { close() {}, refresh() {} }, clientsPanelIsVisible: () => !sandbox.document.hidden,
    appendConvMessages: items => increments.push(items), applyIncrementalConversationState() {},
    calls, renders, increments,
  };
  vm.createContext(sandbox);
  vm.runInContext(`
    let activeId=1,convId=1,convLastId=0,convInFlight=null,convAbortController=null;
    let detailRequestGeneration=0,detailAbortController=null,renderedDetailId=1;
    let detailInFlight=null,detailInFlightId=null,detailRevision='r1',detailRefreshedAt=Date.now();
    let contextPreservedRoots=new Set(),convPollFailures=0;
    const CLIENT_DETAIL_RECONCILE_MS=120000;
    function renderConversation(d){renders.push(d.client.id);convId=d.client.id;}
    function renderContext(){}
    ${block('    async function detail(id,', '    function appendConvMessages(items)')}
    ${block('    function pollConv(){', '    let convTimer=null;')}
    globalThis.api={detail,pollConv,expire(){detailRefreshedAt=0;},select(id){activeId=id;},state(){return {activeId,convId,detailRevision,convLastId};}};
  `, sandbox);
  function respond(index, data) { calls[index].wait.resolve({ ok: true, json: async () => ({ success: true, ...data }) }); }
  return { ...sandbox, api: sandbox.api, respond };
}

test('same-client clicks coalesce; every later click fetches current detail again', async () => {
  const h = harness(); const first = h.api.detail(1); const repeated = h.api.detail(1);
  assert.equal(h.calls.length, 1);
  h.respond(0, { client: { id: 1 }, detail_revision: 'r2' });
  await Promise.all([first, repeated]);
  assert.deepEqual(h.renders, [1]);
  const next = h.api.detail(1); assert.equal(h.calls.length, 2);
  h.respond(1, { client: { id: 1 }, detail_revision: 'r3' }); await next;
  assert.equal(h.api.state().detailRevision, 'r3');
});

test('A to B aborts A and rejects a late A response even if fetch ignores abort', async () => {
  const h = harness(); const first = h.api.detail(1);
  h.api.select(2); const second = h.api.detail(2);
  assert.equal(h.calls[0].options.signal.aborted, true);
  h.respond(1, { client: { id: 2 }, detail_revision: 'b' }); await second;
  h.respond(0, { client: { id: 1 }, detail_revision: 'a' }); await first;
  assert.deepEqual(h.renders, [2]); assert.equal(h.api.state().detailRevision, 'b');
});

test('empty transcript polls remain incremental; changed state refreshes full card without messages', async () => {
  const h = harness(); const poll = h.api.pollConv();
  assert.match(h.calls[0].url, /poll=1&after_id=0/);
  assert.equal(h.api.pollConv(), poll); assert.equal(h.calls.length, 1);
  h.respond(0, { messages: [], detail_revision: 'r2' });
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(h.calls.length, 2); assert.match(h.calls[1].url, /detail=1/);
  h.respond(1, { client: { id: 1 }, detail_revision: 'r2' }); await poll;
  assert.deepEqual(h.renders, [1]);
  const unchanged = h.api.pollConv(); h.respond(2, { messages: [], detail_revision: 'r2' }); await unchanged;
  assert.equal(h.calls.length, 3);
});

test('late incremental A cannot mutate messages or card during B selection', async () => {
  const h = harness(); const poll = h.api.pollConv();
  h.api.select(2); const detail = h.api.detail(2);
  assert.equal(h.calls[0].options.signal.aborted, true);
  h.respond(0, { messages: [{ id: 9 }], detail_revision: 'a2' }); await poll;
  assert.deepEqual(h.increments, []);
  h.respond(1, { client: { id: 2 }, detail_revision: 'b' }); await detail;
  assert.deepEqual(h.renders, [2]);
});

test('old-client mutation callbacks do not cancel the newly selected request', async () => {
  const h = harness(); h.api.select(2); const detail = h.api.detail(2);
  assert.equal(await h.api.detail(1), false); assert.equal(h.calls.length, 1);
  assert.equal(h.calls[0].options.signal.aborted, false);
  h.respond(0, { client: { id: 2 }, detail_revision: 'b' }); await detail;
});

test('visible card performs a bounded reconciliation even when related revision is unchanged', async () => {
  const h = harness(); h.api.expire(); const poll = h.api.pollConv();
  h.respond(0, { messages: [], detail_revision: 'r1' });
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(h.calls.length, 2);
  h.respond(1, { client: { id: 1 }, detail_revision: 'r1' }); await poll;
  assert.deepEqual(h.renders, [1]);
});

test('hidden document does not poll or build a full card', async () => {
  const h = harness(); h.document.hidden = true;
  await h.api.pollConv(); assert.equal(h.calls.length, 0);
});

test('overlapping list triggers share one request for identical filter/page/query', async () => {
  const calls = []; const sandbox = { currentView: 'all', currentPage: 1, requestedClientId: 0,
    listInFlight: null, listRequestKey: '', fetchClients(q, options) { const wait = deferred(); calls.push({q,options,wait}); return wait.promise; },
  };
  vm.createContext(sandbox);
  vm.runInContext(`${block('    function load(q,{background=false}={})', '    async function fetchClients(q,')}globalThis.load=load;`, sandbox);
  const first = sandbox.load('hoodie'), second = sandbox.load('hoodie', {background:true});
  assert.equal(first, second); assert.equal(calls.length, 1);
  calls[0].wait.resolve(true); await first;
  const later = sandbox.load('hoodie'); assert.equal(calls.length, 2); calls[1].wait.resolve(true); await later;
});

function actionHarness() {
  const calls=[],feedback=[],panes=[],lists=[];
  const sandbox={FormData,csrf:'test',base:'/clients/',window:{confirm:()=>true},
    currentView:'all',currentQuery:()=>'',actionSuccessText:()=> 'Saved',
    setActionFeedback:(...args)=>feedback.push(args),clearDecisionTrace(){},setMobilePane(){},node:()=>({}),
    conversationEl:{replaceChildren(){panes.push('conversation');}},contextEl:{replaceChildren(){panes.push('context');}},
    fetch(){const wait=deferred();calls.push(wait);return wait.promise;},
    load(){const wait=deferred();lists.push(wait);return wait.promise;},
  };
  vm.createContext(sandbox);
  vm.runInContext(`let activeId=1,convId=1,convLastId=8,detailRequestGeneration=1;
    async function detail(){detailRequestGeneration++;return true;}
    ${block('    async function runClientAction(id, action, button){','    function section(root,title,count)')}
    globalThis.api={runClientAction,select(id){activeId=id;convId=id;detailRequestGeneration++;},state(){return {activeId,convId};}};`,sandbox);
  return {api:sandbox.api,calls,feedback,panes,lists,sandbox,button:{innerHTML:'Action',textContent:'Action',disabled:false},
    respond(data,ok=true){calls[0].resolve({ok,json:async()=>data});}};
}

test('delayed hide or lost success for A reloads global list without clearing selected B',async()=>{
  for(const action of ['hide','lost']){
    const h=actionHarness();if(action==='lost')h.sandbox.currentView='active';
    const work=h.api.runClientAction(1,action,h.button);h.api.select(2);h.respond({success:true});
    await new Promise(resolve=>setImmediate(resolve));assert.equal(h.lists.length,1);
    h.lists[0].resolve(true);await work;
    assert.equal(h.api.state().activeId,2);assert.equal(h.api.state().convId,2);
    assert.deepEqual(h.panes,[]);assert.deepEqual(h.feedback,[]);
  }
});

test('pause success cannot paint A feedback onto B after awaited list refresh',async()=>{
  const h=actionHarness();const work=h.api.runClientAction(1,'pause',h.button);h.respond({success:true});
  await new Promise(resolve=>setImmediate(resolve));assert.equal(h.lists.length,1);
  h.api.select(2);h.lists[0].resolve(true);await work;assert.deepEqual(h.feedback,[]);
});

test('late action error restores its own old button without painting selected B',async()=>{
  const h=actionHarness();const work=h.api.runClientAction(1,'resume',h.button);
  h.api.select(2);h.respond({success:false,error:'A failure'},false);await work;
  assert.equal(h.button.disabled,false);assert.equal(h.button.innerHTML,'Action');assert.deepEqual(h.feedback,[]);
});

test('missing optional motion asset retains a functional keyed client list',()=>{
  const opened=[];
  const list={children:[],scrollTop:80,get firstChild(){return this.children[0]||null;},querySelectorAll(){return this.children.filter(row=>row.dataset.clientId);},
    insertBefore(row,before){const old=this.children.indexOf(row);if(old>=0)this.children.splice(old,1);const index=before?this.children.indexOf(before):this.children.length;this.children.splice(index,0,row);},
    appendChild(row){this.children.push(row);},contains(row){return this.children.includes(row);}};
  function render(row,c){row.dataset.renderSignature=JSON.stringify(c);row.name=c.name;}
  function create(c){const row={dataset:{clientId:String(c.id)},classList:{toggle(){}},click(){opened.push(c.id);},
    get nextSibling(){return list.children[list.children.indexOf(this)+1]||null;},remove(){list.children.splice(list.children.indexOf(this),1);}};render(row,c);return row;}
  const sandbox={window:{},document:{activeElement:null},listEl:list,createClientRow:create,renderClientRow:render,clientSignature:JSON.stringify,activeId:1,node:()=>({dataset:{},remove(){}})};
  vm.createContext(sandbox);
  vm.runInContext(`${block('    function createStaticClientList(){','    function reconcileClients(clients,')}globalThis.list=clientList;`,sandbox);
  sandbox.list.reconcile([{id:1,name:'A'},{id:2,name:'B'}]);const original=list.children[0];
  sandbox.list.reconcile([{id:2,name:'Fresh B'},{id:1,name:'Fresh A'}]);
  assert.equal(list.children[1],original);assert.equal(original.name,'Fresh A');list.children[0].click();assert.deepEqual(opened,[2]);assert.equal(list.scrollTop,80);
});

test('older ID page inserted between delayed import and visible event preserves the visible reader',async()=>{
  const reading=require('../../twocomms/management/static/management/bot_chat_reading.js');
  for(const mobile of [false,true]){
    const wait=deferred();
    const view={scrollY:1120,innerHeight:600,document:{documentElement:{scrollHeight:3000}},
      scrollTo({top}){this.scrollY=top;},scrollBy({top}){this.scrollY+=top;}};
    const messages={rows:[],isConnected:true,scrollTop:120,clientHeight:100,scrollHeight:600,
      querySelectorAll(){return this.rows;},contains(row){return this.rows.includes(row);},
      get firstElementChild(){return this.rows[0];},get firstChild(){return this.rows[0];},get lastElementChild(){return this.rows.at(-1);},
      getBoundingClientRect(){return {top:mobile?1000-view.scrollY:0};},
      insertBefore(row,before){this.rows.splice(this.rows.indexOf(row),1);const index=before?this.rows.indexOf(before):this.rows.length;this.rows.splice(index,0,row);}};
    const append=(root,item)=>root.rows.push({dataset:{mid:String(item.id),messageTime:item.time,messageSignature:JSON.stringify(item)},
      getBoundingClientRect(){const top=(mobile?1000-view.scrollY:-messages.scrollTop)+messages.rows.indexOf(this)*100;return {top,bottom:top+100};}});
    append(messages,{id:101,time:'2020-01-01T00:00:00Z'});append(messages,{id:102,time:'2026-01-01T00:00:00Z'});
    const visible=messages.rows[1];const liveButton={};
    const sandbox={document:{getElementById:()=>messages},window:{TwcBotChatReading:{...reading,capture:(root,options)=>reading.capture(root,{...options,scrollWindow:view})}},
      convId:1,convOldestId:101,convHasOlder:true,base:'/clients/',fetch:()=>wait.promise,
      conversationIsDocumentScrolled:()=>mobile,appendMessage:append,refreshMessage(){},
      conversationEl:{querySelector:()=>liveButton},preserveConversationAnchor(){assert.fail('must preserve visible anchor using shared helper');}};
    vm.createContext(sandbox);vm.runInContext(`${block('    async function fetchOlder(button){','    function renderFollowupResolution(row,item,id){')}globalThis.fetchOlder=fetchOlder;`,sandbox);
    const work=sandbox.fetchOlder({disabled:false});
    // The user scrolls during the network wait; preserve the latest position.
    messages.scrollTop=110;view.scrollY=1110;const before=visible.getBoundingClientRect().top;
    wait.resolve({ok:true,json:async()=>({success:true,messages:[{id:1,time:'2025-01-01T00:00:00Z'}],oldest_message_id:1,has_older:false})});
    assert.equal(await work,true);assert.equal(visible.getBoundingClientRect().top,before);
    assert.deepEqual(messages.rows.map(row=>Number(row.dataset.mid)),[101,1,102]);
    assert.equal(sandbox.convOldestId,1);assert.equal(sandbox.convHasOlder,false);
  }
});

function rowUrlHarness({active=1,requested=0}={}){
  const requests=[],replacements=[],rows=[];
  const sandbox={URL,window:{location:{href:'https://example.test/bot/?section=clients&client_id=1&message_id=99&fulfillment_request=old#old'},
    history:{state:{keep:true},replaceState(state,_,url){assert.equal(state.keep,true);replacements.push(url);sandbox.window.location.href=new URL(url,sandbox.window.location.href).href;}}},
    listEl:{querySelectorAll:()=>rows},setMobilePane(){},request(id){const wait=deferred();requests.push({id,wait});return wait.promise;}};
  vm.createContext(sandbox);vm.runInContext(`let activeId=${JSON.stringify(active)},convId=${JSON.stringify(active)},requestedClientId=${requested},requestedMessageId=99,detailRequestGeneration=0;
    function detail(id){detailRequestGeneration++;return request(id);}
    ${block('    function bindClientRow(row){','    function renderClientRow(row,c){')}
    globalThis.api={bindClientRow,select(id){activeId=id;convId=id;detailRequestGeneration++;},conversation(id){convId=id;},message(){return requestedMessageId;}};`,sandbox);
  function row(id){const handlers={};const item={dataset:{clientId:String(id)},classList:{add(){},remove(){}},addEventListener(name,handler){handlers[name]=handler;}};rows.push(item);sandbox.api.bindClientRow(item);return handlers;}
  return {row,api:sandbox.api,requests,replacements,sandbox};
}

test('successful explicit client switch updates share URL and clears previous client scope',async()=>{
  const h=rowUrlHarness();const work=h.row(2).click();assert.deepEqual(h.replacements,[]);assert.equal(h.api.message(),0);
  h.api.conversation(2);h.requests[0].wait.resolve(true);await work;
  assert.deepEqual(h.replacements,['/bot/?section=clients&client_id=2']);
});

test('initial deep link and same-client refresh preserve message and fulfillment URL scope',async()=>{
  for(const options of [{active:null,requested:1},{active:1,requested:0}]){
    const h=rowUrlHarness(options),work=h.row(1).click();h.api.conversation(1);h.requests[0].wait.resolve(true);await work;
    assert.deepEqual(h.replacements,[]);assert.equal(h.api.message(),99);
  }
});

test('failed or canceled switch cannot update URL; stale earlier switch cannot overwrite newer client',async()=>{
  const failed=rowUrlHarness(),failure=failed.row(2).click();failed.requests[0].wait.resolve(false);await failure;assert.deepEqual(failed.replacements,[]);
  const canceled=rowUrlHarness(),cancel=canceled.row(2).click();canceled.api.select(1);canceled.requests[0].wait.resolve(false);await cancel;assert.deepEqual(canceled.replacements,[]);
  const h=rowUrlHarness(),earlier=h.row(2).click(),later=h.row(3).click();
  h.api.conversation(3);h.requests[1].wait.resolve(true);await later;h.requests[0].wait.resolve(true);await earlier;
  assert.deepEqual(h.replacements,['/bot/?section=clients&client_id=3']);
});
