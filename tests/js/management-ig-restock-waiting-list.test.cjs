const test=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const source=fs.readFileSync(path.join(__dirname,'../../twocomms/management/static/management/js/ig_restock_waiting_list.js'),'utf8');
const REQUEST='11111111-1111-4111-8111-111111111111';
class Node {
  constructor(tag,doc){this.tagName=tag;this.ownerDocument=doc;this.children=[];this.attributes={};this.listeners={};this.className='';this._text='';this.open=false;this.disabled=false;this.classList={add:name=>this.className+=' '+name};}
  append(...nodes){this.children.push(...nodes);}appendChild(node){this.append(node);return node;}
  replaceChildren(...nodes){this._text='';this.children=nodes;}
  setAttribute(name,value){this.attributes[name]=String(value);}
  addEventListener(name,fn){this.listeners[name]=fn;}
  get textContent(){return this._text+this.children.map(child=>child.textContent).join(' ');}
  set textContent(value){this._text=String(value);this.children=[];}
  all(){return [this,...this.children.flatMap(child=>child.all())];}
  visible(){return this._text+' '+(this.tagName==='details'&&!this.open?this.children.filter(child=>child.tagName==='summary'):this.children).map(child=>child.visible()).join(' ');}
  click(){if(!this.disabled&&this.listeners.click)this.listeners.click();}
}
function waiting(overrides={}){
  return {version:2,coverage_complete:true,native_future_capability:'unverified',lists:[{id:'private-list',version:2,completion:'all',locale:'uk',items:[
    {line_id:'line-A',recipient_id:'self',title:'Футболка А',color:'Чорний',size:'M',fit:'classic',options:{fit:'classic',print:'Сонце'},quantity:2,item_kind:'catalog_apparel'},
    {line_id:'line-B',recipient_id:'private-recipient-uuid',title:'Особливе худі Б',color:'Бузковий',size:'L',fit:'oversize',options:{},quantity:1,item_kind:'custom_apparel'}],
    agreement:{state:'accepted',accepted_at:'2026-10-15T09:15:00Z',expires_at:'2027-01-13T09:15:00Z',invitation_receipt_confirmed:true},
    manager_review:{ready:true,request_id:REQUEST,request_digest:'a'.repeat(64),state:'reviewed',checked_at:'2026-10-15T10:00:00Z',all_ready:false,lines:[
      {line_id:'line-A',availability:'available',checked_quantity:2,supply_basis:'accessible_unrecorded',checked_at:'2026-10-15T10:00:00Z'},
      {line_id:'line-B',availability:'wait',checked_quantity:null,supply_basis:'incoming_confirmed',checked_at:'2026-10-15T10:00:00Z'}]},
    notification:{state:'pending',reason:'waiting_window',sent_at:null,native_receipt_confirmed:false},source_refs:[{kind:'message',id:51,event_at:'2026-10-15T09:15:00Z'}],...overrides}]};
}
function fixture(options={}){
  const document={createElement(tag){return new Node(tag,this);}},root=new Node('section',document),calls=[],replies=[];
  const window={document,location:{origin:'https://manager.test',href:'https://manager.test/bot/?section=clients'},fetch:async(url,options)=>{
    calls.push({url,options});if(!replies.length)throw new Error('unexpected fetch');const reply=replies.shift();return await (typeof reply==='function'?reply():reply);
  }};
  vm.runInNewContext(source,{window,URL,AbortController,Date,Intl,module:{exports:{}}});
  const controller=window.IgRestockWaitingList.create(root,{url:id=>'/bot/api/clients/'+id+'/restock-waiting-list/',canOperate:true,reviewUrl:(id,requestId)=>'/bot/?section=clients&client_id='+id+'&fulfillment_request='+requestId,...options});
  const response=(value,status=200)=>({ok:status>=200&&status<300,status,json:async()=>value});
  const queue=(value,status=200)=>replies.push(response({success:status===200,waiting_list:value},status));
  const load=async(value=waiting())=>{queue(value);await controller.render(12);};
  const links=()=>root.all().filter(node=>node.tagName==='a');
  return {root,controller,calls,replies,response,queue,load,links,visible:()=>root.visible()};
}

test('actual GET renders every exact item and independent agreement/check/channel states without purchase authority',async()=>{
  const f=fixture();await f.load();
  assert.match(f.visible(),/Футболка А/);assert.match(f.visible(),/Особливе худі Б/);
  assert.match(f.visible(),/Кількість: 2 шт/);assert.match(f.visible(),/Кількість: 1 шт/);assert.match(f.visible(),/Одержувач 1/);
  assert.doesNotMatch(f.visible(),/private-recipient-uuid|private-list|line-A/);
  assert.match(f.visible(),/Разову згоду підтверджено/);assert.match(f.visible(),/Ще не всі позиції доступні/);
  assert.match(f.visible(),/Очікуємо дозволеного контакту в Instagram/);assert.match(f.visible(),/усіх позицій у потрібній кількості/);
  assert.match(f.visible(),/Доступно поза обліком.*залишки цим рішенням не змінювались/);
  assert.match(f.visible(),/15\.10\.2026.*12:15.*Київ/);
  assert.equal(f.calls.length,1);assert.equal(f.calls[0].options.method,'GET');assert.equal(f.calls[0].options.cache,'no-store');
  assert.equal(f.root.all().filter(node=>['input','select','textarea','form'].includes(node.tagName)).length,0);
  assert.equal(f.root.all().filter(node=>node.tagName==='button').length,1);
});

test('review link requires verified actual request and operator permission, preserves exact case in another tab',async()=>{
  const f=fixture();await f.load();assert.equal(f.links().length,1);const link=f.links()[0],url=new URL(link.href);
  assert.equal(url.searchParams.get('client_id'),'12');assert.equal(url.searchParams.get('fulfillment_request'),REQUEST);assert.equal(link.target,'_blank');assert.equal(link.rel,'noopener');
  const viewer=fixture({canOperate:false});await viewer.load();assert.equal(viewer.links().length,0);assert.match(viewer.visible(),/Наявність підтверджено/);
  for(const override of [{ready:false},{request_id:'bad'},{request_digest:''}]){
    const value=waiting();Object.assign(value.lists[0].manager_review,override);const f=fixture();await f.load(value);assert.equal(f.links().length,0);
  }
  const foreign=fixture({reviewUrl:()=> 'https://foreign.test/bot/'});await foreign.load();assert.equal(foreign.links().length,0);
});

test('send receipt remains distinct from accepted consent and uncertain sent flags fail closed',async()=>{
  for(const notification of [{state:'unknown',native_receipt_confirmed:false},{state:'sent',native_receipt_confirmed:false}]){
    const f=fixture();await f.load(waiting({notification}));assert.match(f.visible(),/Разову згоду підтверджено/);assert.match(f.visible(),/Результат надсилання невідомий/);assert.match(f.visible(),/Не надсилайте повторно/);
    assert.doesNotMatch(f.visible(),/Надсилання підтверджено Instagram|Прочитано/);
  }
  const f=fixture();await f.load(waiting({notification:{state:'sent',native_receipt_confirmed:true,sent_at:'2026-10-15T10:30:00Z'}}));
  assert.match(f.visible(),/Надсилання підтверджено Instagram/);assert.match(f.visible(),/13:30/);assert.doesNotMatch(f.visible(),/Прочитано/);
});

test('invited/revoked/expired and absent native receipt never imply subscription from WAIT',async()=>{
  for(const [state,label] of [['invited','Очікуємо згоди клієнта'],['revoked','Згоду відкликано'],['expired','Строк погодження минув']]){
    const f=fixture();await f.load(waiting({agreement:{state,invitation_receipt_confirmed:true}}));assert.match(f.visible(),new RegExp(label));assert.doesNotMatch(f.visible(),/Разову згоду підтверджено/);
  }
  const f=fixture();await f.load(waiting({agreement:{state:'accepted',invitation_receipt_confirmed:false}}));assert.match(f.visible(),/Згоду не підтверджено/);assert.doesNotMatch(f.visible(),/Разову згоду підтверджено/);
  const empty=fixture();await empty.load({version:2,lists:[],coverage_complete:true});assert.match(empty.visible(),/саме по собі не означає згоду/);
});

test('customer titles/options render only as escaped text and proofs stay closed',async()=>{
  const data=waiting();data.lists[0].items[0].title='<img src=x onerror=alert(1)>';data.lists[0].items[0].options={print:'<script>unsafe</script>'};
  const f=fixture();await f.load(data);assert.match(f.visible(),/<img src=x onerror=alert\(1\)>/);assert.match(f.visible(),/<script>unsafe<\/script>/);
  assert.equal(f.root.all().some(node=>['img','script'].includes(node.tagName)),false);
  assert.doesNotMatch(f.visible(),/Повідомлення №51/);const details=f.root.all().find(node=>node.tagName==='details');assert.equal(details.open,false);details.open=true;assert.match(f.visible(),/Повідомлення №51/);
});

test('late previous-client response cannot overwrite selected client and all requests remain GET',async()=>{
  const f=fixture();let finish;f.replies.push(()=>new Promise(resolve=>{finish=resolve;}));const first=f.controller.render(12);
  f.queue(waiting({items:[{line_id:'other',title:'Інший клієнт',quantity:1,recipient_id:'self'}]}));await f.controller.render(23);
  assert.equal(f.calls[0].options.signal.aborted,true);finish(f.response({success:true,waiting_list:waiting()}));await first;
  assert.match(f.visible(),/Інший клієнт/);assert.doesNotMatch(f.visible(),/Футболка А/);assert.equal(f.controller.clientId,23);
  assert.equal(f.calls.every(call=>call.options.method==='GET'),true);
});

test('privacy denial erases previous facts, refresh retries read only, destroyed response stays hidden',async()=>{
  const f=fixture();await f.load();f.queue(null,410);await f.controller.load();assert.match(f.visible(),/Дані клієнта видаляються/);assert.doesNotMatch(f.visible(),/Футболка А/);
  f.queue(waiting());f.root.all().find(node=>node.tagName==='button').click();await new Promise(setImmediate);assert.match(f.visible(),/Футболка А/);
  let finish;f.replies.push(()=>new Promise(resolve=>{finish=resolve;}));const pending=f.controller.load();f.controller.destroy();finish(f.response({success:true,waiting_list:waiting()}));await pending;assert.equal(f.root.textContent,'');assert.equal(f.controller.canLeave(),true);
});

test('bounded history explicitly discloses omissions, unknown schema never renders claimed item facts',async()=>{
  const f=fixture();const data=waiting();data.coverage_complete=false;await f.load(data);assert.match(f.visible(),/не повний перелік погоджень/);
  const bad=fixture();await bad.load({...waiting(),version:1});assert.match(bad.visible(),/не вдалося підтвердити/);assert.doesNotMatch(bad.visible(),/Футболка А/);
});

test('actual bot mount reuses one read-only controller and passes the native exact-review URL',()=>{
  const template=fs.readFileSync(path.join(__dirname,'../../twocomms/management/templates/management/bot.html'),'utf8');
  let mount=template.slice(template.indexOf('    function renderRestockWaitingList(id){'),template.indexOf('    function renderFulfillmentReview(id){'));
  assert.ok(mount.length>100);mount=mount.replace('{% if bot_can_operate %}true{% else %}false{% endif %}','true')
    .replace('{% url "management_bot_restock_waiting_list_api" 0 %}','/bot/api/clients/0/restock-waiting-list/').replace(/{%[^%]*%}/g,'');
  const calls=[],created=[],parent={appendChild(root){calls.push({root});}},window={location:{href:'https://manager.test/bot/?section=clients&client_id=99#old'},IgRestockWaitingList:{create(root,options){created.push({root,options});return {render(id){calls.push({id});}};}}};
  vm.runInNewContext('let restockWaitingRoot=null,restockWaitingController=null;'+mount+'renderRestockWaitingList(12);renderRestockWaitingList(23);',{window,URL,contextEl:parent,node:()=>({})});
  assert.equal(created.length,1);assert.deepEqual(calls.filter(call=>call.id).map(call=>call.id),[12,23]);
  assert.equal(created[0].options.url(23),'/bot/api/clients/23/restock-waiting-list/');const review=new URL(created[0].options.reviewUrl(23,REQUEST));
  assert.equal(review.searchParams.get('client_id'),'23');assert.equal(review.searchParams.get('fulfillment_request'),REQUEST);assert.equal(review.hash,'');
  assert.match(template,/renderAssistedCommerce\(id\);\s+renderRestockWaitingList\(id\);\s+renderFulfillmentReview\(id\);/);
});

test('same-client parent renders share a pending request and cannot starve a slow read',async()=>{
  const f=fixture();let finish;f.replies.push(()=>new Promise(resolve=>{finish=resolve;}));
  const first=f.controller.render(12),heading=f.root.children[0];
  const second=f.controller.render(12),third=f.controller.render(12);
  assert.equal(first,second);assert.equal(second,third);assert.equal(f.calls.length,1);
  assert.equal(f.calls[0].options.signal.aborted,false);assert.equal(f.root.children[0],heading);
  finish(f.response({success:true,waiting_list:waiting()}));await Promise.all([first,second,third]);
  assert.match(f.visible(),/Футболка А/);assert.equal(f.root.attributes['aria-busy'],'false');
});

test('same-client refresh keeps verified DOM and open proofs, including unchanged final response',async()=>{
  const f=fixture();await f.load();const card=f.root.children[1],proof=f.root.all().find(node=>node.tagName==='details');proof.open=true;
  let finish;f.replies.push(()=>new Promise(resolve=>{finish=resolve;}));const refresh=f.controller.render(12);
  assert.equal(f.root.children[1],card);assert.equal(f.root.attributes['aria-busy'],'true');assert.match(f.visible(),/Футболка А/);assert.doesNotMatch(f.visible(),/Завантаження/);
  assert.equal(f.root.all().find(node=>node.tagName==='button').disabled,true);
  const repeated=f.controller.load();assert.equal(repeated,refresh);assert.equal(f.calls.length,2);
  finish(f.response({success:true,waiting_list:waiting()}));await refresh;
  assert.equal(f.root.children[1],card);assert.equal(proof.open,true);assert.equal(f.root.attributes['aria-busy'],'false');assert.equal(f.root.all().find(node=>node.tagName==='button').disabled,false);
});

test('switching clients clears verified facts immediately while replacement request remains pending',async()=>{
  const f=fixture();await f.load();let finishRefresh;f.replies.push(()=>new Promise(resolve=>{finishRefresh=resolve;}));const oldRefresh=f.controller.render(12);
  let finishNew;f.replies.push(()=>new Promise(resolve=>{finishNew=resolve;}));const selected=f.controller.render(23);
  assert.equal(f.controller.data,null);assert.doesNotMatch(f.visible(),/Футболка А|Особливе худі Б/);assert.match(f.visible(),/Завантаження/);assert.equal(f.calls[1].options.signal.aborted,true);
  finishRefresh(f.response({success:true,waiting_list:waiting()}));await oldRefresh;assert.doesNotMatch(f.visible(),/Футболка А/);assert.equal(f.root.attributes['aria-busy'],'true');
  finishNew(f.response({success:true,waiting_list:waiting({items:[{line_id:'new',title:'Вибраний клієнт',quantity:1}]})}));await selected;
  assert.match(f.visible(),/Вибраний клієнт/);assert.doesNotMatch(f.visible(),/Футболка А/);
});
