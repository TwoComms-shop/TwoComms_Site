const test=require('node:test');
const assert=require('node:assert/strict');
const {Controller}=require('../../twocomms/management/static/management/js/ig_assisted_partial.js');
// Only the DOM surface used by the real controller. Events bubble so input
// edits exercise the production form listeners, not a test-only state setter.
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


const A='11111111-1111-4111-8111-111111111111',B='22222222-2222-4222-8222-222222222222',D='33333333-3333-4333-8333-333333333333',TASK=71,PUBLIC='55555555-5555-4555-8555-555555555555';
const hash=n=>n.repeat(64),clone=value=>JSON.parse(JSON.stringify(value));
function context(){return {status:'current',request:{id:A},cas:{expected_request_version:1,expected_request_digest:hash('a'),expected_full_capture_digest:hash('b'),expected_decision_version:1,expected_draft_digest:hash('c')},editable_items:[{line_id:'line-a',title:'Футболка',quantity:2,availability:'ready',unit_price:'250.00',recipient_id:'self',original_index:0},{line_id:'line-b',title:'Особливе худі',quantity:1,availability:'wait',unit_price:null,recipient_id:'gift',original_index:1}],can_approve:false,decision_head:{id:D,status:'approved'},approved_head:{id:D,physical_checked_at:'2026-10-14T09:00:00Z',approved_total:'1500.00'},partial_intention:{ready:true,reason:'',reason_label:'',request_id:A,request_digest:hash('a'),decision_id:D,decision_digest:hash('d'),intention_id:B,coverage_digest:hash('e'),terms_digest:hash('f'),lines:[{line_id:'line-a',recipient_id:'self',recipient_label:'Клієнт',title:'Футболка',color:'Чорний',size:'M',quantity:2,disposition:'ready',preparation:{min_days:0,max_days:0,unit:'calendar'},dispatch:{min_days:1,max_days:3,unit:'calendar'}},{line_id:'line-b',recipient_id:'gift',recipient_label:'Для подарунка',title:'Особливе худі',color:'Фіолетовий',size:'L',quantity:1,disposition:'deferred',preparation:null,dispatch:null}],coverage:{selected:['line-a'],deferred:['line-b'],settled:[],inflight:[]},scoped_quote:{scope:'selected',total:'500.00',currency:'UAH',selected_line_ids:['line-a'],deferred_line_ids:['line-b'],scope_digest:hash('1'),terms_digest:hash('f')},shipping_terms:null,can_approve_terms:false,can_issue:false,can_prepare_selected:false,can_resume:false,can_prepare_remainder:false,offer:null,continuation_offer:null,orders:[]}};}
function offer(extra={}){return {task_id:TASK,operation_id:'66666666-6666-4666-8666-000000000001',card_digest:hash('2'),locale:'uk',text:'Готові зараз: футболка. Решта ще не придбана.',buttons:[{title:'Оформити готові зараз'},{title:'Дочекатися всіх товарів'}],expires_at:'2026-10-15T09:00:00Z',delivery:{status:'sent',receipt_verified:true,native_mid:'native-synthetic',sent_at:'2026-10-14T09:01:00Z'},choice:{action:'partial',choice_digest:hash('3'),source_id:27},...extra};}
function saved(data,terms){const next=clone(data);next.partial_intention.shipping_terms=clone(terms);next.partial_intention.scoped_quote.shipping_terms=clone(terms);next.partial_intention.can_approve_terms=true;next.can_approve=true;next.decision_head.status='draft';next.cas.expected_decision_version=2;next.cas.expected_draft_digest=hash('4');return next;}
function fixture(canOperate=true){const document={createElement(tag){return new Node(tag,this);}},root=new Node('section',document),calls=[],replies=[],uuids=[],opened=[],updates=[],refreshes=[];let peer=true;const fetch=async(url,options)=>{const call={url:String(url),method:options.method,body:options.body,headers:options.headers};calls.push(call);assert.ok(replies.length,'every actual POST needs an explicit native response');let reply=replies.shift();if(reply instanceof Error)throw reply;if(typeof reply==='function')reply=reply(call);return {status:reply.status||200,ok:(reply.status||200)>=200&&(reply.status||200)<300,json:async()=>reply.data};};const controller=new Controller(root,{apiUrl:'/bot/api/clients/12/assisted-commerce/',canOperate,fetch,csrfToken:'synthetic-only',canAct:()=>peer,onCommerce:data=>updates.push(data),onRefresh:info=>refreshes.push(info),onOpenProposal:id=>opened.push(id),operationId:()=>{const id='66666666-6666-4666-8666-'+String(uuids.length+1).padStart(12,'0');uuids.push(id);return id;}});const response=(commerce,extra={})=>({data:{success:true,commerce,...extra}});const button=text=>root.all().find(node=>node.tagName==='button'&&node.textContent===text);const control=label=>root.all().find(node=>node.attributes['aria-label']===label);const change=(node,value)=>{assert.equal(node.disabled,false);node.value=value;node.emit('change');};return {root,controller,calls,replies,uuids,opened,updates,refreshes,response,button,control,change,update:data=>controller.update(data),flush:async()=>{await new Promise(setImmediate);await new Promise(setImmediate);},peer:value=>{peer=value;controller.syncActions();}};}
function issued(f,data,call){const body=JSON.parse(call.body),receipt={...offer({delivery:{status:'pending',receipt_verified:false},choice:null}),...body,task_id:TASK,card_digest:hash('2')},next=clone(data);if(body.action==='issue_continuation_offer'){receipt.kind='continuation';receipt.buttons=[{title:'Повернутися до решти товарів'}];}next.partial_intention[body.action==='issue_continuation_offer'?'continuation_offer':'offer']=receipt;return f.response(next,{offer:receipt});}
function terms(f){for(const [title,value]of [['Готові зараз','12,50'],['Решта пізніше','25.00'],['Усі товари разом','35.00']]){f.change(f.control('Платник — '+title),'customer');f.control('Максимальна вартість для клієнта — '+title).input(value);}}

test('passive mount and commerce updates display all whole lines and exact selected amount with no fetch or customer action',()=>{const f=fixture();assert.equal(f.root.hidden,true);f.update(context());assert.equal(f.calls.length,0);assert.equal(f.uuids.length,0);assert.match(f.root.textContent,/Футболка.*2 шт/);assert.match(f.root.textContent,/Особливе худі.*1 шт/);assert.match(f.root.textContent,/Товари до оплати зараз: 500,00 UAH/);assert.doesNotMatch(f.root.textContent,/1500|11111111|coverage_digest/);assert.equal(f.button('Оформити готові зараз'),undefined);assert.equal(f.control('Максимальна вартість для клієнта — Готові зараз').value,'');assert.match(f.root.textContent,/У день перевірки.*14\.10\.2026/);});

test('all three explicit shipping caps save exact full draft then approve exact stored CAS without items',async()=>{const data=context(),f=fixture();f.update(data);assert.equal(f.button('Зберегти умови доставки').disabled,true);terms(f);assert.equal(f.controller.canLeave(),false);let draft;f.replies.push(call=>{const sent=JSON.parse(call.body);draft=saved(data,sent.shipping_terms);return f.response(draft);});f.button('Зберегти умови доставки').click();await f.flush();const sent=JSON.parse(f.calls[0].body);assert.equal(sent.action,'draft');assert.deepEqual(sent.items,data.editable_items);assert.equal(sent.shipping_terms.now.customer_maximum,'12.50');assert.equal(sent.shipping_terms.later.customer_maximum,'25.00');assert.equal(sent.shipping_terms.together.customer_maximum,'35.00');assert.equal(Object.hasOwn(sent,'selected_line_ids'),false);assert.equal(f.button('Затвердити умови й ціну готових товарів').disabled,false);f.replies.push(f.response({...draft,can_approve:false,decision_head:{id:D,status:'approved'},partial_intention:{...draft.partial_intention,can_approve_terms:false,can_issue:true}}));f.button('Затвердити умови й ціну готових товарів').click();await f.flush();const approve=JSON.parse(f.calls[1].body);assert.equal(approve.action,'approve');assert.equal(approve.expected_decision_version,2);assert.equal(approve.expected_draft_digest,hash('4'));assert.equal(Object.hasOwn(approve,'items'),false);assert.equal(Object.hasOwn(approve,'shipping_terms'),false);assert.equal(f.calls.length,2);assert.equal(f.updates.length,2);});

test('merchant delivery is explicit zero; absent customer maximum never becomes an invented carrier price',()=>{const f=fixture();f.update(context());f.change(f.control('Платник — Готові зараз'),'merchant');assert.equal(f.control('Максимальна вартість для клієнта — Готові зараз').value,'0.00');assert.equal(f.control('Максимальна вартість для клієнта — Готові зараз').disabled,true);assert.equal(f.button('Зберегти умови доставки').disabled,true);assert.match(f.root.textContent,/а не розрахунок перевізника/);});

test('together is only waiting and UNKNOWN delivery cannot admit purchase even with an inconsistent backend flag',()=>{const data=context(),f=fixture();data.partial_intention.can_prepare_selected=true;data.partial_intention.offer=offer({choice:{action:'together',choice_digest:hash('3'),source_id:27}});f.update(data);assert.equal(f.button('Підготувати оформлення готових товарів').disabled,true);assert.match(f.root.textContent,/обрав дочекатися всіх товарів/);data.partial_intention.offer=offer({delivery:{status:'unknown'},choice:{action:'partial',choice_digest:hash('3'),source_id:27}});f.update(data);assert.equal(f.button('Підготувати оформлення готових товарів').disabled,true);assert.doesNotMatch(f.root.textContent,/Клієнт підтвердив оформлення готових товарів/);assert.equal(f.calls.length,0);});

test('selected checkout binds actual choice and delivered task without manager-chosen line IDs or deferred charges',async()=>{const data=context(),f=fixture();data.partial_intention.can_prepare_selected=true;data.partial_intention.offer=offer();f.update(data);f.replies.push(f.response(data,{checkout:{ready:true,public_id:PUBLIC,state:'awaiting_payment'}}));f.button('Підготувати оформлення готових товарів').click();await f.flush();const body=JSON.parse(f.calls[0].body);assert.deepEqual(Object.keys(body).sort(),['action','decision_id','expected_choice_digest','expected_coverage_digest','expected_decision_digest','expected_request_digest','expected_terms_digest','operation_id','request_id','task_id']);assert.equal(body.task_id,TASK);assert.equal(body.expected_choice_digest,hash('3'));assert.equal(body.expected_terms_digest,hash('f'));assert.equal(f.calls.length,1);f.button('Відкрити пропозицію').click();assert.deepEqual(f.opened,[PUBLIC]);assert.match(f.root.textContent,/Посилання не підтверджує оплату/);});

test('UNKNOWN offer replays original exact A operation after update B and pins refresh to original request',async()=>{const data=context(),f=fixture();data.partial_intention.can_issue=true;f.update(data);f.replies.push(new Error('timeout'));f.button('Надіслати варіанти клієнту').click();await f.flush();const original=f.calls[0].body;assert.equal(f.controller.canLeave(),false);const newer=clone(data);newer.partial_intention.request_id=B;newer.partial_intention.request_digest=hash('9');f.update(newer);f.button('Оновити').click();assert.deepEqual(f.refreshes,[{requestId:A}]);assert.equal(f.controller.busy,true);await f.flush();assert.equal(f.controller.pending.body,original);f.replies.push(call=>issued(f,data,call));f.button('Звірити ту саму операцію').click();await f.flush();assert.equal(f.calls[1].body,original);assert.equal(f.uuids.length,1);assert.equal(f.controller.pending,null);});

test('only exact native offer receipt settles UNKNOWN issue without another POST and pending delivery stays honest',async()=>{const data=context(),f=fixture();data.partial_intention.can_issue=true;f.update(data);f.replies.push(new Error('timeout'));f.button('Надіслати варіанти клієнту').click();await f.flush();const body=JSON.parse(f.calls[0].body),receipt={...offer({delivery:{status:'pending'},choice:null}),...body,task_id:TASK,card_digest:hash('2')};const wrong=clone(data);wrong.partial_intention.offer={...receipt,expected_decision_digest:hash('0')};f.update(wrong);assert.ok(f.controller.pending);const right=clone(data);right.partial_intention.offer=receipt;f.update(right);assert.equal(f.controller.pending,null);assert.equal(f.controller.canLeave(),true);assert.equal(f.calls.length,1);assert.match(f.root.textContent,/Повідомлення очікує надсилання/);assert.doesNotMatch(f.root.textContent,/Нативне надсилання підтверджено/);});

test('409 changed coverage retains terms but requires explicit discard instead of implicit rebasing',async()=>{const data=context(),f=fixture();f.update(data);terms(f);const fresh=clone(data);fresh.partial_intention.coverage_digest=hash('8');fresh.cas.expected_decision_version=2;f.replies.push({status:409,data:{success:false,reason:'coverage_changed',commerce:fresh}});f.button('Зберегти умови доставки').click();await f.flush();assert.equal(f.control('Максимальна вартість для клієнта — Готові зараз').value,'12,50');assert.equal(f.button('Зберегти умови доставки').disabled,true);assert.equal(f.controller.canLeave(),false);f.button('Відкинути чернетку умов').click();assert.equal(f.controller.canLeave(),true);assert.equal(f.control('Максимальна вартість для клієнта — Готові зараз').value,'');assert.equal(f.calls.length,1);});

test('residual invitation preserves historical order and performs no readiness claim, cart resume, subscription or invoice',async()=>{const data=context(),f=fixture();data.partial_intention.ready=false;data.partial_intention.lines[0].disposition='settled';data.partial_intention.can_resume=true;data.partial_intention.orders=[{id:51,order_number:'TC-0051',lines:[{title:'Футболка',quantity:2}],payment_status_label:'Оплату підтверджено',shipping_status_label:'Ще не відправлено'}];f.update(data);assert.doesNotMatch(f.root.textContent,/Товари до оплати зараз/);f.replies.push(call=>issued(f,data,call));f.button('Запросити повернутися до решти товарів').click();await f.flush();const body=JSON.parse(f.calls[0].body);assert.deepEqual(Object.keys(body).sort(),['action','expected_coverage_digest','expected_request_digest','operation_id','request_id']);assert.equal(body.action,'issue_continuation_offer');assert.match(f.root.textContent,/Замовлення TC-0051/);assert.match(f.root.textContent,/Особливе худі/);assert.equal(f.calls.length,1);});

test('viewer and sibling-dirty guards prevent terms edits or commands and preserve non-destructive lifecycle',()=>{const data=context(),viewer=fixture(false);data.partial_intention.can_issue=true;viewer.update(data);assert.equal(viewer.button('Зберегти умови доставки'),undefined);assert.equal(viewer.button('Надіслати варіанти клієнту'),undefined);assert.equal(viewer.control('Платник — Готові зараз').disabled,true);const f=fixture();f.update(data);f.peer(false);assert.equal(f.control('Платник — Готові зараз').disabled,true);assert.equal(f.button('Надіслати варіанти клієнту').disabled,true);f.peer(true);terms(f);assert.equal(f.controller.destroy(),false);f.button('Відкинути чернетку умов').click();assert.equal(f.controller.destroy(),true);assert.equal(f.calls.length,0);});


test('ordinary FULL39 stays visually unchanged while resumed remainder with history remains visible',()=>{const data=context(),f=fixture();data.partial_intention.lines[1].disposition='ready';f.update(data);assert.equal(f.root.hidden,true);data.partial_intention.orders=[{id:51,order_number:'TC-0051',lines:[{title:'Футболка',quantity:2}]}];data.partial_intention.lines=[data.partial_intention.lines[1]];data.partial_intention.can_prepare_remainder=true;f.update(data);assert.equal(f.root.hidden,false);assert.match(f.root.textContent,/Особливе худі/);assert.match(f.root.textContent,/Замовлення TC-0051/);assert.equal(f.calls.length,0);});


test('saved offer displays its immutable original price and caps after current draft terms change',()=>{const data=context(),f=fixture();data.partial_intention.scoped_quote.total='700.00';data.partial_intention.shipping_terms={now:{payer:'customer',customer_maximum:'55.00',currency:'UAH'},later:{payer:'customer',customer_maximum:'60.00',currency:'UAH'},together:{payer:'customer',customer_maximum:'80.00',currency:'UAH'}};data.partial_intention.offer=offer({scoped_quote:{total:'500.00',currency:'UAH'},shipping_terms:{now:{payer:'customer',customer_maximum:'12.50',currency:'UAH'},later:{payer:'merchant',customer_maximum:'0.00',currency:'UAH'},together:{payer:'customer',customer_maximum:'35.00',currency:'UAH'}}});f.update(data);const saved=f.root.querySelector('.ig-ap-offer');assert.match(saved.textContent,/збереженою пропозицією: товари готової частини 500,00 UAH/);assert.match(saved.textContent,/Максимум для клієнта: 12,50 UAH/);assert.doesNotMatch(saved.textContent,/55,00|700,00/);assert.equal(f.calls.length,0);});


test('unverified provider status and malformed success cannot become consent or a committed offer',async()=>{const data=context(),f=fixture();data.partial_intention.can_prepare_selected=true;data.partial_intention.offer=offer({delivery:{status:'sent',receipt_verified:false,native_mid:'unverified'}});f.update(data);assert.equal(f.button('Підготувати оформлення готових товарів').disabled,true);assert.doesNotMatch(f.root.textContent,/Нативне надсилання підтверджено/);data.partial_intention.offer=null;data.partial_intention.can_issue=true;f.update(data);f.replies.push(f.response(data));f.button('Надіслати варіанти клієнту').click();await f.flush();assert.ok(f.controller.pending);assert.equal(f.controller.unknown,true);assert.equal(f.controller.canLeave(),false);assert.equal(f.calls.length,1);});

test('UNKNOWN selected checkout requires exact prepare replay when GET lacks full selection receipt metadata',async()=>{const data=context(),f=fixture();data.partial_intention.can_prepare_selected=true;data.partial_intention.offer=offer();f.update(data);f.replies.push(new Error('timeout'));f.button('Підготувати оформлення готових товарів').click();await f.flush();const original=f.calls[0].body,readback=clone(data);readback.prepared_checkout={ready:true,public_id:PUBLIC,operation_id:JSON.parse(original).operation_id,request_id:A,decision_id:D,expected_request_digest:hash('a'),expected_decision_digest:hash('d')};f.update(readback);assert.ok(f.controller.pending);f.replies.push(f.response(data,{checkout:{ready:true,public_id:PUBLIC,state:'awaiting_payment'}}));f.button('Звірити ту саму операцію').click();await f.flush();assert.equal(f.calls[1].body,original);assert.equal(f.uuids.length,1);assert.equal(f.controller.pending,null);});


test('completed two-order coverage never labels remaining goods unpaid or approves an old card for new terms',()=>{const data=context(),f=fixture();data.partial_intention.lines.forEach(line=>line.disposition='settled');data.partial_intention.coverage={settled_line_ids:['line-a','line-b'],deferred_line_ids:[],inflight_line_ids:[]};data.partial_intention.orders=[{id:51,order_number:'TC-0051',lines:[{title:'Футболка',quantity:2}]},{id:52,order_number:'TC-0052',lines:[{title:'Особливе худі',quantity:1}]}];data.partial_intention.offer=offer({request_id:B,expected_terms_digest:hash('9')});f.update(data);assert.match(f.root.textContent,/Відкладених позицій для нового оформлення немає/);assert.doesNotMatch(f.root.textContent,/Решта залишається невиконаним побажанням і ще не оплачена/);assert.match(f.root.textContent,/Це збережені попередні умови/);assert.match(f.root.textContent,/TC-0051/);assert.match(f.root.textContent,/TC-0052/);assert.equal(f.calls.length,0);});


test('initial sibling loading does not permanently disable term handlers after the shared gate clears',()=>{const f=fixture();f.peer(false);f.update(context());assert.equal(f.control('Платник — Готові зараз').disabled,true);f.peer(true);terms(f);assert.equal(f.controller.dirty,true);assert.equal(f.button('Зберегти умови доставки').disabled,false);assert.equal(f.calls.length,0);});


test('state notifications are transition-only and async refresh catches failure without losing dirty terms',async()=>{const f=fixture();let notifications=0;f.controller.options.onStateChange=()=>{notifications++;f.controller.syncActions();};f.update(context());terms(f);assert.equal(notifications,1);f.controller.syncActions();assert.equal(notifications,1);f.controller.options.onRefresh=async()=>{throw new Error('Не вдалося звірити нативну відповідь');};f.button('Оновити').click();assert.equal(f.controller.busy,true);await f.flush();assert.equal(f.controller.busy,false);assert.equal(f.controller.dirty,true);assert.equal(f.control('Максимальна вартість для клієнта — Готові зараз').value,'12,50');assert.match(f.root.textContent,/Не вдалося звірити нативну відповідь/);assert.equal(notifications,3);});

test('exact partial prepared GET receipt settles UNKNOWN only with full choice and original coverage proof',async()=>{const data=context(),f=fixture();data.partial_intention.can_prepare_selected=true;data.partial_intention.offer=offer();f.update(data);f.replies.push(new Error('timeout'));f.button('Підготувати оформлення готових товарів').click();await f.flush();const payload=JSON.parse(f.calls[0].body),right=clone(data);right.prepared_checkout={...payload,ready:true,public_id:PUBLIC,state:'awaiting_payment'};const wrong=clone(right);wrong.prepared_checkout.expected_choice_digest=hash('9');f.update(wrong);assert.ok(f.controller.pending);f.update(right);assert.equal(f.controller.pending,null);assert.equal(f.controller.canLeave(),true);assert.equal(f.calls.length,1);f.button('Відкрити пропозицію').click();assert.deepEqual(f.opened,[PUBLIC]);});


test('shared relevance keeps fully paid FULL39 history unchanged while real partial history stays visible',()=>{const {isRelevant}=require('../../twocomms/management/static/management/js/ig_assisted_partial.js');const p=context().partial_intention;p.lines.forEach(line=>line.disposition='settled');p.orders=[{order_id:51}];p.coverage.executions=[{scope:'full'}];assert.equal(isRelevant(p),false);p.coverage.executions=[{scope:'partial'}];assert.equal(isRelevant(p),true);});

function shippingContext(){const data=context();data.partial_intention.lines[0].disposition='settled';data.partial_intention.orders=[{order_id:51,order_number:'TC-0051',lines:[{title:'Футболка',quantity:2}],payment_status_label:'Оплату підтверджено',shipping_status_label:'Потребує звірки умов доставки',payer:'customer',customer_maximum:'35.00',currency:'UAH',shipping_review:{state:'requires_review',reason:'assisted_customer_shipping_maximum_exceeded',can_resolve:true,order_id:51,expected_document_ref:'native-document-reference',expected_terms_digest:hash('7')}}];return data;}
function reviewed(f,data,call){const body=JSON.parse(call.body),receipt={ready:true,reason:'',order_id:body.order_id,operation_id:body.operation_id,expected_document_ref:body.expected_document_ref,expected_terms_digest:body.expected_terms_digest,replayed:false},next=clone(data);next.partial_intention.orders[0].shipping_review={...next.partial_intention.orders[0].shipping_review,state:'resolved',can_resolve:false,operation_receipt:receipt};return f.response(next,{shipping_review:receipt});}

test('shipping review binds existing paid Order and accepted cap without creating a TTN or overriding price',async()=>{const data=shippingContext(),f=fixture();f.update(data);assert.match(f.root.textContent,/Клієнту не можна додати різницю/);assert.doesNotMatch(f.root.textContent,/native-document-reference|expected_terms_digest/);f.replies.push(call=>reviewed(f,data,call));f.button('Звірити існуючу ТТН').click();await f.flush();const body=JSON.parse(f.calls[0].body);assert.deepEqual(Object.keys(body).sort(),['action','expected_document_ref','expected_terms_digest','operation_id','order_id']);assert.equal(body.action,'resolve_shipping_review');assert.equal(body.order_id,51);assert.equal(body.expected_document_ref,'native-document-reference');assert.equal(body.expected_terms_digest,hash('7'));assert.equal(Object.hasOwn(body,'carrier_cost'),false);assert.equal(Object.hasOwn(body,'customer_maximum'),false);assert.equal(f.calls.length,1);assert.equal(f.controller.canLeave(),true);assert.match(f.root.textContent,/Існуючу ТТН звірено/);assert.equal(f.button('Звірити існуючу ТТН'),undefined);});

test('shipping review over-cap failure explains native error and keeps accepted customer maximum unchanged',async()=>{const data=shippingContext(),f=fixture();f.update(data);f.replies.push({status:409,data:{success:false,shipping_review:{ready:false,reason:'assisted_customer_shipping_maximum_exceeded',order_id:51},commerce:data}});f.button('Звірити існуючу ТТН').click();await f.flush();assert.equal(f.controller.pending,null);assert.match(f.root.textContent,/Клієнту не можна додати різницю/);assert.match(f.root.textContent,/Максимальна вартість доставки для клієнта: 35,00 UAH/);assert.equal(f.controller.projection.orders[0].customer_maximum,'35.00');assert.doesNotMatch(f.root.textContent,/assisted_customer_shipping_maximum_exceeded/);assert.equal(f.calls.length,1);});

test('UNKNOWN shipping review only settles exact signed order document terms operation receipt',async()=>{const data=shippingContext(),f=fixture();f.update(data);f.replies.push(new Error('timeout'));f.button('Звірити існуючу ТТН').click();await f.flush();const body=JSON.parse(f.calls[0].body);assert.equal(f.controller.canLeave(),false);const right=reviewed(f,data,f.calls[0]).data.commerce,foreign=clone(right);foreign.partial_intention.orders[0].shipping_review.operation_receipt.order_id=52;f.update(foreign);assert.ok(f.controller.pending);const changed=clone(right);changed.partial_intention.orders[0].shipping_review.operation_receipt.expected_terms_digest=hash('8');f.update(changed);assert.ok(f.controller.pending);f.update(right);assert.equal(f.controller.pending,null);assert.equal(f.controller.canLeave(),true);assert.equal(f.calls.length,1);assert.equal(body.order_id,51);});

test('shipping UNKNOWN recovery reuses exact body and UUID while viewer and foreign-review descriptors cannot act',async()=>{const data=shippingContext(),f=fixture();f.update(data);f.replies.push(new Error('timeout'));f.button('Звірити існуючу ТТН').click();await f.flush();const original=f.calls[0].body;f.replies.push(call=>reviewed(f,data,call));f.button('Звірити ту саму операцію').click();await f.flush();assert.equal(f.calls[1].body,original);assert.equal(f.uuids.length,1);assert.equal(f.controller.pending,null);const viewer=fixture(false);viewer.update(data);assert.equal(viewer.button('Звірити існуючу ТТН'),undefined);const foreign=clone(data);foreign.partial_intention.orders[0].shipping_review.order_id=52;const guard=fixture();guard.update(foreign);assert.equal(guard.button('Звірити існуючу ТТН').disabled,true);assert.equal(guard.calls.length,0);});


test('a delayed failed POST cannot undo an exact native receipt already observed by shared GET',async()=>{const data=context(),f=fixture();data.partial_intention.can_issue=true;f.update(data);let release,body;f.controller.fetch=async(_url,options)=>{body=options.body;await new Promise(resolve=>{release=resolve;});return {status:500,ok:false,json:async()=>({success:false})};};f.button('Надіслати варіанти клієнту').click();const payload=JSON.parse(body),readback=clone(data);readback.partial_intention.offer={...offer({delivery:{status:'pending'},choice:null}),...payload,task_id:TASK,card_digest:hash('2')};f.update(readback);assert.equal(f.controller.pending,null);release();await f.flush();assert.equal(f.controller.unknown,false);assert.equal(f.controller.canLeave(),true);assert.equal(f.button('Звірити ту саму операцію'),undefined);assert.match(f.root.textContent,/Нативний результат операції вже звірено/);});


test('an existing card with absent native buttons does not invent operational choices',()=>{const data=context(),f=fixture();data.partial_intention.offer=offer({buttons:[]});f.update(data);const card=f.root.querySelector('.ig-ap-offer');assert.match(card.textContent,/Кнопки у нативному повідомленні ще не підтверджено/);assert.doesNotMatch(card.querySelector('.ig-ap-options').textContent,/Оформити готові зараз|Дочекатися всіх товарів/);assert.equal(f.calls.length,0);});

test('approved delivery caps stay compact and immutable while dirty or UNKNOWN edits remain visible',async()=>{
  const data=context(),f=fixture();
  data.partial_intention.shipping_terms={now:{payer:'customer',customer_maximum:'12.50',currency:'UAH'},later:{payer:'merchant',customer_maximum:'0.00',currency:'UAH'},together:{payer:'customer',customer_maximum:'35.00',currency:'UAH'}};
  data.partial_intention.scoped_quote.delivery_scope='now';data.partial_intention.can_issue=true;f.update(data);
  const details=f.root.querySelector('.ig-ap-terms-editor'),savedCaps=f.root.querySelector('.ig-ap-saved-terms');
  assert.equal(details.open,false);assert.equal(savedCaps.querySelectorAll('input,select').length,0);
  assert.match(savedCaps.textContent,/Готові зараз · Клієнт12,50 UAH/);assert.match(savedCaps.textContent,/Решта пізніше · TwoComms0,00 UAH/);
  assert.equal(savedCaps.querySelector('.is-current').textContent,'Готові зараз · Клієнт12,50 UAH');
  assert.equal(f.button('Надіслати варіанти клієнту').parentNode===details,false);
  details.open=true;details.emit('toggle');f.control('Максимальна вартість для клієнта — Готові зараз').input('18.00');
  details.open=false;details.emit('toggle');assert.equal(details.open,true);
  assert.match(savedCaps.textContent,/12,50 UAH/);assert.doesNotMatch(savedCaps.textContent,/18,00/);
  assert.equal(f.button('Надіслати варіанти клієнту').disabled,true);
  f.replies.push(new Error('timeout'));f.button('Зберегти умови доставки').click();await f.flush();
  assert.equal(f.root.querySelector('.ig-ap-terms-editor').open,true);
  assert.equal(f.control('Максимальна вартість для клієнта — Готові зараз').value,'18.00');
  assert.equal(f.controller.canLeave(),false);assert.equal(f.controller.unknown,true);
  assert.equal(JSON.parse(f.calls[0].body).shipping_terms.now.customer_maximum,'18.00');
  assert.equal(f.calls.length,1);assert.ok(f.button('Звірити ту саму операцію'));
});

test('saved replacement delivery draft exposes exact approval without retaining approved disclosure',async()=>{
  const data=context(),f=fixture();f.update(data);terms(f);
  f.replies.push(call=>f.response(saved(data,JSON.parse(call.body).shipping_terms)));
  f.button('Зберегти умови доставки').click();await f.flush();
  assert.equal(f.root.querySelector('.ig-ap-terms-editor'),null);
  assert.equal(f.root.querySelector('.ig-ap-saved-terms'),null);
  assert.equal(f.button('Затвердити умови й ціну готових товарів').disabled,false);
  assert.equal(f.control('Максимальна вартість для клієнта — Готові зараз').value,'12.50');
  assert.match(f.root.textContent,/Ціна готових товарів для затвердження: 500,00 UAH/);
  assert.equal(f.calls.length,1);
});

test('already consumed scope explains existing checkout then actual linked order without generic failure or duplicate action',()=>{
  const data=context(),f=fixture();data.partial_intention.reason='assisted_scope_overlap';data.partial_intention.reason_label='Дія зараз недоступна. Перевірте поточний запит, джерела та умови.';
  data.partial_intention.lines[0].disposition='inflight';
  data.prepared_checkout={ready:true,action:'prepare_selected',public_id:PUBLIC,state:'awaiting_payment'};
  f.update(data);assert.match(f.root.querySelector('.ig-ap-stage').textContent,/Оформлення цих товарів уже відкрите/);
  assert.doesNotMatch(f.root.textContent,/Дія зараз недоступна/);assert.ok(f.button('Відкрити пропозицію'));
  assert.equal(f.button('Підготувати оформлення готових товарів'),undefined);
  f.button('Відкрити пропозицію').click();assert.deepEqual(f.opened,[PUBLIC]);
  const settled=clone(data);settled.partial_intention.reason='coverage_changed';settled.partial_intention.lines[0].disposition='settled';
  settled.partial_intention.orders=[{order_id:51,order_number:'TC-0051',lines:[{title:'Футболка',quantity:2}],payment_status_label:'Оплату підтверджено',shipping_status_label:'Ще не відправлено'}];
  f.update(settled);assert.match(f.root.querySelector('.ig-ap-stage').textContent,/Повторне оформлення не потрібне/);
  assert.match(f.root.textContent,/Оплату підтверджено/);assert.doesNotMatch(f.root.querySelector('.ig-ap-stage').textContent,/Оплата ще не підтверджена/);
  assert.equal(f.calls.length,0);
  const changed=context();changed.partial_intention.reason='coverage_changed';changed.partial_intention.reason_label='Актуальний кошик змінився — потрібна звірка.';
  f.update(changed);assert.equal(f.root.querySelector('.ig-ap-stage').textContent,changed.partial_intention.reason_label);
});

test('native residual coverage change resolves to linked orders even after current selected quote is absent',()=>{
  const data=context(),f=fixture();data.partial_intention.reason='residual_coverage_changed';data.partial_intention.reason_label='Дія зараз недоступна. Перевірте поточний запит, джерела та умови.';
  data.partial_intention.lines.forEach(line=>line.disposition='settled');data.partial_intention.coverage.ready=true;
  data.partial_intention.orders=[{order_id:51,order_number:'TC-0051',payment_status_label:'Оплачено повністю',lines:[{title:'Футболка',quantity:2}]},{order_id:52,order_number:'TC-0052',payment_status_label:'Оплачено повністю',lines:[{title:'Особливе худі',quantity:1}]}];
  data.partial_intention.scoped_quote=null;data.partial_intention.offer=null;f.update(data);
  assert.match(f.root.querySelector('.ig-ap-stage').textContent,/Повторне оформлення не потрібне/);
  assert.doesNotMatch(f.root.textContent,/Дія зараз недоступна/);assert.match(f.root.textContent,/TC-0051/);assert.match(f.root.textContent,/TC-0052/);
  const residual=clone(data);residual.partial_intention.lines[1].disposition='deferred';residual.partial_intention.coverage.ready=false;f.update(residual);
  assert.equal(f.root.querySelector('.ig-ap-stage').textContent,residual.partial_intention.reason_label);
  assert.equal(f.calls.length,0);
});

test('one-shipment remainder displays only its sealed later cap and actual single purchase button',()=>{
  const data=context(),f=fixture(),caps={now:{payer:'customer',customer_maximum:'120.00',currency:'UAH'},later:{payer:'customer',customer_maximum:'150.00',currency:'UAH'},together:{payer:'merchant',customer_maximum:'0.00',currency:'UAH'}};
  data.partial_intention.lines[0].disposition='settled';data.partial_intention.lines[1].disposition='ready';
  data.partial_intention.orders=[{order_id:51,order_number:'TC-0051'}];data.partial_intention.shipping_terms=caps;
  data.partial_intention.scoped_quote={...data.partial_intention.scoped_quote,delivery_scope:'later',selected_line_ids:['line-b'],deferred_line_ids:[],shipping_terms:caps,total:'1490.00'};
  data.partial_intention.offer=offer({request_id:A,scoped_quote:clone(data.partial_intention.scoped_quote),shipping_terms:clone(caps),buttons:[{title:'Оформити готові'}]});
  f.update(data);const card=f.root.querySelector('.ig-ap-offer'),summary=f.root.querySelector('.ig-ap-saved-terms');
  assert.equal(card.querySelectorAll('.ig-ap-term').length,1);assert.match(card.textContent,/Ця відправка.*150,00 UAH/);
  assert.doesNotMatch(card.querySelector('.ig-ap-terms-list').textContent,/120,00|Максимум для клієнта: 0,00 UAH|Усі товари разом|Готові зараз/);
  assert.equal(summary.children.length,1);assert.match(summary.textContent,/Ця відправка.*150,00 UAH/);
  assert.equal(card.querySelector('.ig-ap-options').children.length,1);assert.equal(card.querySelector('.ig-ap-options').textContent,'Оформити готові');
  assert.equal(f.calls.length,0);
});

test('mixed R2 uses later cap for current and future separate parcels and preserves unused now term in strict draft',async()=>{
  const data=context(),f=fixture(),caps={now:{payer:'customer',customer_maximum:'120.00',currency:'UAH'},later:{payer:'customer',customer_maximum:'150.00',currency:'UAH'},together:{payer:'merchant',customer_maximum:'0.00',currency:'UAH'}};
  data.partial_intention.lines[0].disposition='settled';data.partial_intention.lines[1].disposition='ready';
  data.partial_intention.lines.push({line_id:'line-c',title:'Пізніша позиція C',quantity:1,disposition:'deferred'});
  data.partial_intention.orders=[{order_id:51,order_number:'TC-0051'}];data.partial_intention.shipping_terms=clone(caps);
  data.partial_intention.scoped_quote={...data.partial_intention.scoped_quote,delivery_scope:'later',selected_line_ids:['line-b'],deferred_line_ids:['line-c'],shipping_terms:clone(caps),total:'1490.00'};
  data.partial_intention.offer=offer({request_id:A,scoped_quote:clone(data.partial_intention.scoped_quote),shipping_terms:clone(caps)});
  f.update(data);const card=f.root.querySelector('.ig-ap-offer'),summary=f.root.querySelector('.ig-ap-saved-terms'),titles=card.querySelectorAll('.ig-ap-term').map(node=>node.children[0].textContent);
  assert.deepEqual(titles,['Готові зараз та наступні окремі відправки','Уся решта разом']);
  assert.match(card.querySelector('.ig-ap-terms-list').textContent,/окремі відправки.*150,00 UAH.*Уся решта разом.*0,00 UAH/);
  assert.doesNotMatch(card.querySelector('.ig-ap-terms-list').textContent,/120,00/);
  assert.equal(summary.children.length,2);assert.match(summary.querySelector('.is-current').textContent,/150,00 UAH/);
  assert.match(card.textContent,/наступних товарів потрібні свіжа перевірка менеджера, нове погодження умов і власний вибір клієнта/);
  assert.equal(f.control('Максимальна вартість для клієнта — Готові зараз'),undefined);
  const details=f.root.querySelector('.ig-ap-terms-editor');details.open=true;details.emit('toggle');
  f.control('Максимальна вартість для клієнта — Готові зараз та наступні окремі відправки').input('160.00');
  f.replies.push(call=>f.response(saved(data,JSON.parse(call.body).shipping_terms)));
  f.button('Зберегти умови доставки').click();await f.flush();const body=JSON.parse(f.calls[0].body);
  assert.deepEqual(Object.keys(body.shipping_terms).sort(),['later','now','together']);
  assert.deepEqual(body.shipping_terms.now,caps.now);assert.deepEqual(body.shipping_terms.together,caps.together);
  assert.equal(body.shipping_terms.later.customer_maximum,'160.00');assert.deepEqual(body.items,data.editable_items);
  assert.equal(f.calls.length,1);
});

test('settled exact scope collapses agreement while actual order cost and status remain visible',()=>{
  const data=context(),f=fixture();data.partial_intention.lines[0].disposition='settled';
  data.partial_intention.reason='coverage_changed';
  data.partial_intention.orders=[{order_id:51,line_ids:['line-a'],order_number:'TC-0051',lines:[{title:'Футболка',quantity:2}],payment_status_label:'Оплачено повністю',shipping_status_label:'ТТН ще не створено',payer:'customer',customer_maximum:'120.00',currency:'UAH'}];
  data.partial_intention.offer=offer({request_id:A,expected_decision_digest:hash('d'),expected_terms_digest:hash('f'),scoped_quote:clone(data.partial_intention.scoped_quote),shipping_terms:{now:{payer:'customer',customer_maximum:'120.00',currency:'UAH'}}});
  f.update(data);const disclosure=f.root.querySelector('.ig-ap-settled-offer');
  assert.ok(disclosure);assert.equal(disclosure.open,false);assert.equal(disclosure.children[0].textContent,'Погоджені умови та вибір клієнта');
  assert.equal(f.root.querySelector('.ig-ap-offer').parentNode,disclosure);
  const orders=f.root.querySelector('.ig-ap-orders');assert.equal(orders.parentNode,f.root);
  assert.match(orders.textContent,/TC-0051.*Оплачено повністю.*ТТН ще не створено/);assert.match(orders.textContent,/120,00 UAH/);
  disclosure.open=true;disclosure.emit('toggle');assert.match(disclosure.textContent,/Нативне надсилання підтверджено/);
  assert.match(disclosure.textContent,/Клієнт підтвердив оформлення готових товарів/);
  assert.equal(f.button('Підготувати оформлення готових товарів'),undefined);assert.equal(f.calls.length,0);
  for (const mutate of [
    value=>value.partial_intention.lines[0].disposition='inflight',
    value=>value.partial_intention.lines[0].disposition='ready',
    value=>value.partial_intention.orders[0].line_ids=['unrelated-line'],
    value=>value.partial_intention.offer.expected_decision_digest=hash('9'),
    value=>value.partial_intention.offer.delivery.status='unknown',
    value=>value.partial_intention.offer.choice=null
  ]) {const current=clone(data);mutate(current);f.update(current);assert.equal(f.root.querySelector('.ig-ap-settled-offer'),null);assert.equal(f.root.querySelector('.ig-ap-offer').parentNode,f.root);}
});

test('prepared checkout becomes actual linked-order readback only for exact settled execution',()=>{
 const data=context(),f=fixture(),execution='native-execution';data.partial_intention.lines[0].disposition='settled';
 data.partial_intention.coverage={ready:true,executions:[{execution_id:execution,request_id:A,decision_id:D,state:'settled',line_ids:['line-a']}]};
 data.partial_intention.orders=[{execution_id:execution,order_id:51,line_ids:['line-a'],order_number:'TC-0051',payment_status_label:'Оплачено повністю',shipping_status_label:'ТТН ще не створено'}];
 data.prepared_checkout={ready:true,action:'prepare_selected',public_id:PUBLIC,request_id:A,decision_id:D,expected_request_digest:hash('a'),expected_decision_digest:hash('d')};
 f.update(data);assert.match(f.root.textContent,/Товари у пов’язаному замовленні/);assert.doesNotMatch(f.root.textContent,/Оформлення підготовлено/);assert.ok(f.button('Відкрити пропозицію'));
 for(const mutate of [value=>value.partial_intention.coverage.executions[0].state='inflight',value=>value.partial_intention.orders[0].execution_id='foreign-execution',value=>value.prepared_checkout.expected_decision_digest=hash('9')]){
  const current=clone(data);mutate(current);f.update(current);assert.match(f.root.textContent,/Оформлення підготовлено/);assert.doesNotMatch(f.root.textContent,/Товари у пов’язаному замовленні/);
 }
 assert.equal(f.calls.length,0);
});

test('known current inflight invoice commitment gives neutral preparation status while stale and uncertain frames retain warning',()=>{
 const data=context(),f=fixture();data.status='prepared';data.partial_intention.lines[0].disposition='inflight';
 data.partial_intention.reason='existing_invoice_commitment_requires_review';data.partial_intention.reason_label='Дія зараз недоступна. Перевірте поточний запит, джерела та умови.';
 data.partial_intention.coverage={ready:true,executions:[{execution_id:'native-execution',request_id:A,decision_id:D,state:'inflight',line_ids:['line-a']}]};
 f.update(data);assert.match(f.root.querySelector('.ig-ap-stage').textContent,/Оформлення готової частини вже підготовлено/);assert.match(f.root.querySelector('.ig-ap-stage').textContent,/оплата ще не підтверджена/);
 for(const mutate of [value=>value.status='stale',value=>value.partial_intention.coverage.executions[0].request_id=B,value=>value.partial_intention.coverage.executions[0].state='resource_review']){
  const current=clone(data);mutate(current);f.update(current);assert.equal(f.root.querySelector('.ig-ap-stage').textContent,data.partial_intention.reason_label);
 }
 assert.equal(f.calls.length,0);
});
