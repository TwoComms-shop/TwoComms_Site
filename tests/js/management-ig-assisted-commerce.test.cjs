const test=require('node:test');
const assert=require('node:assert/strict');
const {Controller}=require('../../twocomms/management/static/management/js/ig_assisted_commerce.js');
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


const A='11111111-1111-4111-8111-111111111111',B='22222222-2222-4222-8222-222222222222';
const PUBLIC='33333333-3333-4333-8333-333333333333',IMAGE='44444444-4444-4444-8444-444444444444';
const hash=n=>n.repeat(64),clone=value=>JSON.parse(JSON.stringify(value));
function item(extra={}) {return {line_id:'custom-line',recipient_id:'self',original_index:0,item_kind:'custom_apparel',product_id:null,color_variant_id:null,title:'Футболка з написом',color:'Чорний',size:'M',fit_code:'',quantity:2,unit_price:'400.00',currency:'UAH',availability:'ready',checked_quantity:2,supply_basis:'accessible_unrecorded',source_location:'',option_values:{},preparation:null,dispatch:null,image_id:null,image_digest:'',question_answers:[],...extra};}
function context(extra={}) {const row=item();return {status:'current',request:{id:A,version:1,intention_id:B,request_digest:hash('a'),full_capture_digest:hash('b'),lines:[{line_id:row.line_id,recipient_id:'self',original_index:0,draft_title:'Клієнтська футболка',source_choices:{color:'Оригінальний чорний',size:'M',quantity:2}}],questions:[]},editable_items:[row],item_controls:[{line_id:row.line_id,can_edit_price:true}],draft:null,decision_head:null,approved_head:null,cas:{expected_request_version:1,expected_request_digest:hash('a'),expected_full_capture_digest:hash('b'),expected_decision_version:0,expected_draft_digest:''},can_approve:false,can_prepare:false,can_prepare_checkout:false,bot_effects_allowed:true,source_cas:{ready:true,revision_id:17,expected_revision_digest:hash('c'),expected_full_capture_digest:hash('b')},...extra};}
function decision(data,items,status,version) {const next=clone(data),head={id:'55555555-5555-4555-8555-555555555555',version,status,decision_digest:hash('d'),draft_digest:hash('e'),items:clone(items),physical_checked_at:status==='approved'?'2026-10-14T09:00:00+00:00':null,expires_at:'2026-10-15T09:00:00+00:00',approved_total:'800.00',currency:'UAH'};next.editable_items=clone(items);next.decision_head=head;next.draft=status==='draft'?head:next.draft;next.approved_head=status==='approved'?head:null;next.can_approve=status==='draft';next.can_prepare=status==='approved'&&items.every(row=>row.availability==='ready');next.cas.expected_decision_version=version;next.cas.expected_draft_digest=head.draft_digest;return next;}
class FakeFormData {constructor(){this.values=new Map();}append(key,value){this.values.set(key,value);}get(key){return this.values.get(key);}}
function fixture(canOperate=true) {
 const document={createElement(tag){return new Node(tag,this);}},root=new Node('section',document),calls=[],replies=[],uuids=[],opened=[];
 const fetch=async(url,options={})=>{const call={url:String(url),method:options.method||'GET',body:options.body,headers:options.headers};calls.push(call);assert.ok(replies.length,'every controller request has an explicit response');let reply=replies.shift();if(reply instanceof Error)throw reply;if(typeof reply==='function')reply=reply(call);return {status:reply.status||200,ok:(reply.status||200)>=200&&(reply.status||200)<300,json:async()=>reply.data};};
 const controller=new Controller(root,{apiUrl:'/bot/api/clients/12/assisted-commerce/',canOperate,fetch,FormData:FakeFormData,csrfToken:'synthetic-only',onOpenProposal:id=>opened.push(id),operationId:()=>{const id='66666666-6666-4666-8666-'+String(uuids.length+1).padStart(12,'0');uuids.push(id);return id;}});
 const response=(commerce,extra={})=>({data:{success:true,commerce,...extra}});
 const flush=async()=>{await new Promise(setImmediate);await new Promise(setImmediate);};
 const load=async data=>{replies.push(response(data));assert.equal(await controller.load(),true);};
 const button=text=>root.all().find(node=>node.tagName==='button'&&node.textContent===text);
 const control=label=>root.all().find(node=>node.attributes['aria-label']===label);
 const change=(node,value)=>{assert.equal(node.disabled,false);node.value=value;node.emit('change');};
 return {root,controller,calls,replies,uuids,opened,response,flush,load,button,control,change,posts:()=>calls.filter(call=>call.method==='POST')};
}

test('passive GET never creates a request; explicit request uses only native source CAS',async()=>{
 const f=fixture();assert.equal(f.calls.length,0);const empty=context({status:'none',request:null,editable_items:[],cas:{}});await f.load(empty);assert.equal(f.posts().length,0);
 f.replies.push(f.response(context()));f.button('Підготувати перевірку поточного кошика').click();await f.flush();
 const payload=JSON.parse(f.posts()[0].body);assert.deepEqual(Object.keys(payload).sort(),['action','expected_full_capture_digest','expected_revision_digest','operation_id','revision_id']);assert.equal(payload.action,'request');assert.equal(payload.revision_id,17);assert.equal(f.controller.canPrepare(),false);assert.doesNotMatch(f.root.textContent,/11111111|full_capture|UUID/);
});

test('Save captures changed terms while Approve is a separate exact-CAS action with no items',async()=>{
 const f=fixture(),initial=context();await f.load(initial);f.control('Назва — позиція 1').input('Узгоджений напис');f.control('Ціна за одиницю — позиція 1').input('450,00');assert.equal(f.button('Затвердити збережені умови').disabled,true);
 let saved;f.replies.push(call=>{const body=JSON.parse(call.body);saved=decision(initial,body.items,'draft',1);return f.response(saved);});f.button('Зберегти чернетку').click();await f.flush();
 const save=JSON.parse(f.posts()[0].body);assert.equal(save.items[0].title,'Узгоджений напис');assert.equal(save.items[0].unit_price,'450.00');assert.equal(save.items[0].quantity,2);assert.equal(save.items[0].checked_quantity,2);assert.equal(initial.request.lines[0].source_choices.color,'Оригінальний чорний');assert.equal(f.button('Затвердити збережені умови').disabled,false);
 f.replies.push(f.response(decision(saved,saved.editable_items,'approved',2)));f.button('Затвердити збережені умови').click();await f.flush();
 const approved=JSON.parse(f.posts()[1].body);assert.equal(approved.action,'approve');assert.equal(approved.expected_decision_version,1);assert.equal(approved.expected_draft_digest,hash('e'));assert.equal(Object.hasOwn(approved,'items'),false);assert.equal(f.controller.canLeave(),true);assert.match(f.root.textContent,/Наявність перевірено:.*14\.10\.2026/);assert.equal(f.button('Підготувати посилання на оформлення').disabled,true);
});

test('catalog facts quantity price and photo remain canonical while custom terms stay editable',async()=>{
 const data=context(),catalog=item({line_id:'catalog-line',original_index:1,item_kind:'catalog_apparel',product_id:5,color_variant_id:6,title:'Каталожна футболка',unit_price:'350.00',quantity:1,checked_quantity:1,supply_basis:'warehouse_checked'});data.editable_items.push(catalog);data.request.lines.push({line_id:catalog.line_id,recipient_id:'self',original_index:1,source_choices:{color:'Чорний',size:'M',quantity:1}});data.item_controls.push({line_id:catalog.line_id,can_edit_price:false,price_is_catalog_hint:true,catalog_image_url:'/media/catalog/verified-shirt.webp'});
 const f=fixture();await f.load(data);const catalogPrice=f.control('Ціна за одиницю — позиція 2');assert.equal(catalogPrice.disabled,true);assert.throws(()=>catalogPrice.input('1.00'),/disabled/);for(const name of ['Назва','Колір','Розмір','Крій · необов’язково','Кількість'])assert.equal(f.control(name+' — позиція 2').disabled,true);assert.equal(f.control('Підстава наявності — позиція 2').disabled,false);assert.equal(f.control('Перевірена кількість — позиція 2').disabled,false);assert.equal(f.control('Завантажити фото — Каталожна футболка'),undefined);assert.equal(f.root.querySelector('.ig-ac-thumbnail').src,'/media/catalog/verified-shirt.webp');assert.equal(await f.controller.upload('catalog-line',{type:'image/png',size:100}),false);assert.equal(f.posts().length,0);f.control('Назва — позиція 1').input('Мій точний напис');f.control('Колір — позиція 1').input('Фіолетовий');f.control('Розмір — позиція 1').input('XL');f.control('Крій · необов’язково — позиція 1').input('oversize');f.control('Кількість — позиція 1').input('3');f.control('Перевірена кількість — позиція 1').input('3');f.control('Ціна за одиницю — позиція 1').input('499,00');
 f.replies.push(call=>f.response(decision(data,JSON.parse(call.body).items,'draft',1)));f.button('Зберегти чернетку').click();await f.flush();const payload=JSON.parse(f.posts()[0].body);assert.deepEqual(payload.items.map(row=>row.unit_price),['499.00','350.00']);assert.deepEqual(payload.items.map(row=>row.line_id),['custom-line','catalog-line']);assert.equal(payload.items[1].product_id,5);assert.equal(payload.items[1].title,catalog.title);assert.equal(payload.items[1].color,catalog.color);assert.equal(payload.items[1].size,catalog.size);assert.equal(payload.items[1].quantity,1);assert.equal(payload.items[1].image_id,null);assert.deepEqual([payload.items[0].title,payload.items[0].color,payload.items[0].size,payload.items[0].fit_code,payload.items[0].quantity],['Мій точний напис','Фіолетовий','XL','oversize',3]);assert.equal(f.root.querySelectorAll('input').some(node=>/знижк|discount/i.test(node.attributes['aria-label']||'')),false);
});

test('WAIT remains in saved full basket and cannot prepare a partial payable link even under an inconsistent gate',async()=>{
 const waiting=item({line_id:'wait-line',original_index:1,title:'Пізніше худі',availability:'wait',unit_price:null,checked_quantity:null,supply_basis:null,preparation:null,dispatch:null});const data=context();data.editable_items.push(waiting);data.request.lines.push({line_id:waiting.line_id,recipient_id:'self',original_index:1,source_choices:{size:'L',quantity:2}});data.item_controls.push({line_id:waiting.line_id,can_edit_price:true});
 const approved=decision(data,data.editable_items,'approved',2);approved.can_prepare_checkout=true;const f=fixture();await f.load(approved);assert.equal(f.button('Підготувати посилання на оформлення').disabled,true);assert.match(f.root.textContent,/посилання на оплату для частини кошика не створюється/);assert.equal(f.controller.local.items.length,2);assert.equal(f.controller.local.items[1].availability,'wait');assert.equal(f.posts().length,0);
});

test('manual prepare needs native-buy authority while bot pause disables only automatic sending',async()=>{
 let data=decision(context(),[item()],'approved',2);const f=fixture();await f.load(data);assert.equal(f.button('Підготувати посилання на оформлення').disabled,true);
 data.can_prepare_checkout=true;data.bot_effects_allowed=false;await f.load(data);assert.equal(f.button('Підготувати посилання на оформлення').disabled,false);assert.match(f.root.textContent,/Бот на паузі: посилання можна підготувати вручну, автоматичне надсилання вимкнено/);assert.equal(f.posts().length,0);
 f.replies.push(f.response(data,{checkout:{ready:true,public_id:PUBLIC,checkout_url:'/ig-checkout/'+PUBLIC+'/?token=synthetic-only'}}));f.button('Підготувати посилання на оформлення').click();await f.flush();const body=JSON.parse(f.posts()[0].body);assert.deepEqual(Object.keys(body).sort(),['action','decision_id','expected_decision_digest','expected_request_digest','operation_id','request_id']);assert.equal(body.action,'prepare');assert.equal(body.expected_decision_digest,data.approved_head.decision_digest);assert.equal(f.root.querySelector('.ig-ac-checkout').querySelector('a'),null);f.button('Відкрити пропозицію').click();assert.deepEqual(f.opened,[PUBLIC]);assert.doesNotMatch(f.root.textContent,/Оплату підтверджено|synthetic-only|33333333/);
});

test('READY questions require verified answers and another Save before exact approval',async()=>{
 const base=context(),question={question_id:hash('f'),line_id:'custom-line',question:'Чи є напис на спині?'};base.request.questions=[question];base.editable_items[0].question_answers=[{question_id:question.question_id,state:'unknown',answer:''}];const f=fixture();await f.load(decision(base,base.editable_items,'draft',1));assert.equal(f.button('Затвердити збережені умови').disabled,true);
 f.control('Відповідь: '+question.question).input('Так, напис перевірено.');f.change(f.control('Стан відповіді на питання'),'confirmed');assert.equal(f.button('Затвердити збережені умови').disabled,true);
 f.replies.push(call=>f.response(decision(base,JSON.parse(call.body).items,'draft',2)));f.button('Зберегти чернетку').click();await f.flush();assert.equal(f.button('Затвердити збережені умови').disabled,false);assert.equal(JSON.parse(f.posts()[0].body).items[0].question_answers[0].question_id,hash('f'));
});

test('UNKNOWN Save pins A GET and replays exact body/UUID even if readback wrongly returns newer B',async()=>{
 const f=fixture(),a=context();await f.load(a);f.control('Назва — позиція 1').input('Збережіть саме A');f.replies.push(new Error('network timeout'));f.button('Зберегти чернетку').click();await f.flush();const original=f.posts()[0].body;assert.equal(f.controller.canLeave(),false);assert.equal(f.controller.destroy(),false);
 const b=context();b.request.id=B;b.cas.expected_request_digest=hash('c');b.request.request_digest=hash('c');b.request.lines[0].source_choices.color='Новий червоний';await f.load(b);assert.equal(new URL(f.calls.at(-1).url,'https://manager.example').searchParams.get('request_id'),A);assert.equal(f.controller.local.requestId,A);assert.equal(f.control('Назва — позиція 1').disabled,true);assert.match(f.root.querySelector('.ig-ac-source').textContent,/Оригінальний чорний/);assert.doesNotMatch(f.root.querySelector('.ig-ac-source').textContent,/Новий червоний/);
 f.replies.push(f.response(decision(a,JSON.parse(original).items,'draft',1)));f.button('Перевірити ту саму операцію').click();await f.flush();assert.equal(f.posts()[1].body,original);assert.equal(f.uuids.length,1);assert.equal(f.controller.canLeave(),true);
});

test('409 same-source head change retains local terms and requires explicit rebasing before a new draft',async()=>{
 const base=decision(context(),[item()],'draft',1),f=fixture();await f.load(base);f.control('Ціна за одиницю — позиція 1').input('799,00');const fresh=decision(base,[item({unit_price:'850.00'})],'draft',2);
 f.replies.push({status:409,data:{success:false,reason:'decision_head_changed'}},f.response(fresh));f.button('Зберегти чернетку').click();await f.flush();assert.equal(f.controller.local.items[0].unit_price,'799,00');assert.equal(f.button('Зберегти чернетку').disabled,true);assert.match(f.root.querySelector('.ig-ac-warning').textContent,/850\.00 UAH/);assert.equal(f.posts().length,1);
 f.button('Звірено · застосувати актуальну версію').click();assert.equal(f.posts().length,1);f.replies.push(call=>f.response(decision(fresh,JSON.parse(call.body).items,'draft',3)));f.button('Зберегти чернетку').click();await f.flush();const body=JSON.parse(f.posts()[1].body);assert.equal(body.expected_decision_version,2);assert.equal(body.items[0].unit_price,'799.00');assert.equal(f.button('Затвердити збережені умови').disabled,false);
});

test('a changed customer source cannot rebase old draft; discard restores the actual new request',async()=>{
 const f=fixture();await f.load(context());f.control('Назва — позиція 1').input('Стара чернетка');const b=context();b.request.id=B;b.request.request_digest=hash('c');b.cas.expected_request_digest=hash('c');b.editable_items[0].title='Нове бажання клієнта';await f.load(b);assert.equal(f.button('Звірено · застосувати актуальну версію'),undefined);assert.equal(f.button('Зберегти чернетку').disabled,true);assert.equal(f.controller.local.items[0].title,'Стара чернетка');f.button('Відкинути чернетку й прочитати актуальну').click();assert.equal(f.controller.local.requestId,B);assert.equal(f.controller.local.items[0].title,'Нове бажання клієнта');assert.equal(f.posts().length,0);
});

test('upload UNKNOWN reuses the exact multipart file and operation; removing photo only edits the draft',async()=>{
 const f=fixture();await f.load(context());const file={name:'shirt.png',type:'image/png',size:42};f.replies.push(new Error('upload timeout'));assert.equal(await f.controller.upload('custom-line',file),false);const original=f.posts()[0].body;assert.equal(original.get('image'),file);assert.equal(original.get('request_id'),A);assert.equal(f.posts()[0].headers['Content-Type'],undefined);assert.equal(f.controller.canLeave(),false);
 f.replies.push(f.response(context(),{image:{id:IMAGE,content_digest:hash('9'),preview_url:'/bot/api/clients/12/assisted-commerce/images/'+IMAGE+'/'}}));f.button('Перевірити ту саму операцію').click();await f.flush();assert.equal(f.posts()[1].body,original);assert.equal(f.uuids.length,1);assert.equal(f.controller.local.items[0].image_id,IMAGE);assert.equal(f.controller.local.dirty,true);assert.equal(f.root.querySelector('.ig-ac-thumbnail').src,'/bot/api/clients/12/assisted-commerce/images/'+IMAGE+'/');
 f.button('Прибрати фото з пропозиції').click();assert.equal(f.controller.local.items[0].image_id,null);assert.equal(f.controller.local.items[0].image_digest,'');assert.equal(f.root.querySelector('.ig-ac-thumbnail'),null);assert.equal(f.posts().length,2);assert.ok(f.root.querySelector('.ig-ac-brand'));assert.equal(f.controller.canLeave(),false);
});

test('viewer controls stay readonly and invalid upload cannot issue any POST',async()=>{
 const readonly=fixture(false);await readonly.load(context());assert.ok(readonly.root.querySelectorAll('input,select,textarea').every(node=>node.disabled));assert.equal(readonly.root.querySelector('input[type="file"]'),null);assert.equal(await readonly.controller.perform({action:'prepare'}),false);assert.equal(readonly.posts().length,0);
 const f=fixture();await f.load(context());assert.equal(await f.controller.upload('custom-line',{type:'image/svg+xml',size:40}),false);assert.equal(await f.controller.upload('custom-line',{type:'image/png',size:6291457}),false);assert.equal(f.posts().length,0);assert.equal(f.uuids.length,0);
});

test('empty custom timing does not become implicit zero days; explicit zero remains valid server-anchored timing',async()=>{
 const f=fixture();await f.load(context());f.change(f.control('Готовність — строк'),'custom');assert.equal(f.button('Зберегти чернетку').disabled,true);f.change(f.control('Готовність — строк'),'0');assert.equal(f.button('Зберегти чернетку').disabled,false);f.replies.push(call=>f.response(decision(context(),JSON.parse(call.body).items,'draft',1)));f.button('Зберегти чернетку').click();await f.flush();assert.deepEqual(JSON.parse(f.posts()[0].body).items[0].preparation,{min_days:0,max_days:0,unit:'calendar'});
});

test('unanswered sparse color/quantity cannot crash or become checked READY defaults',async()=>{
 const data=context();data.editable_items[0]=item({availability:'clarify',color:null,quantity:null,checked_quantity:null,supply_basis:null});const f=fixture();await f.load(data);assert.equal(f.button('Зберегти чернетку').disabled,true);f.change(f.control('Наявність — позиція 1'),'ready');assert.equal(f.button('Зберегти чернетку').disabled,true);assert.equal(f.controller.local.items[0].checked_quantity,null);assert.equal(f.posts().length,0);
});

test('prepared checkout is readonly and reconciliation reuses its actual operation without another UUID',async()=>{
 const data=decision(context(),[item()],'approved',2);data.status='prepared';data.prepared_checkout={ready:true,public_id:PUBLIC,state:'paid',operation_id:'77777777-7777-4777-8777-777777777777',request_id:A,decision_id:data.approved_head.id,expected_request_digest:data.request.request_digest,expected_decision_digest:data.approved_head.decision_digest,can_recover_checkout:true};
 const f=fixture();await f.load(data);assert.ok(f.root.querySelectorAll('input,select,textarea').every(control=>control.disabled));assert.equal(f.button('Підготувати посилання на оформлення').disabled,true);assert.equal(f.root.querySelector('.ig-ac-checkout').querySelector('a'),null);assert.equal(f.posts().length,0);
 f.replies.push(f.response(data,{checkout:{ready:true,public_id:PUBLIC,replayed:true}}));f.button('Звірити підготовлене оформлення').click();await f.flush();const body=JSON.parse(f.posts()[0].body);assert.equal(body.operation_id,data.prepared_checkout.operation_id);assert.equal(body.expected_decision_digest,data.approved_head.decision_digest);assert.equal(f.uuids.length,0);assert.equal(f.controller.canLeave(),true);
});

test('only a fully matched prepared receipt can settle UNKNOWN prepare during a GET without reposting',async()=>{
 const data=decision(context(),[item()],'approved',2);data.can_prepare_checkout=true;const f=fixture();await f.load(data);f.replies.push(new Error('prepare response lost'));f.button('Підготувати посилання на оформлення').click();await f.flush();const body=JSON.parse(f.posts()[0].body);
 const prepared=clone(data);prepared.status='prepared';prepared.prepared_checkout={ready:true,public_id:PUBLIC,operation_id:body.operation_id,request_id:body.request_id,decision_id:body.decision_id,expected_request_digest:body.expected_request_digest,expected_decision_digest:'wrong-decision'};
 await f.load(prepared);assert.equal(f.controller.canLeave(),false);assert.equal(f.root.querySelector('.ig-ac-checkout'),null);assert.ok(f.button('Перевірити ту саму операцію'));
 prepared.prepared_checkout.expected_decision_digest=body.expected_decision_digest;await f.load(prepared);assert.equal(f.controller.pending,null);assert.equal(f.controller.unknown,false);assert.equal(f.controller.canLeave(),true);assert.ok(f.root.querySelector('.ig-ac-checkout'));assert.equal(f.posts().length,1);
});


test('native size and fit limits block oversized draft and preserve exact boundary values',async()=>{
 const data=context(),f=fixture();await f.load(data);const size=f.control('Розмір — позиція 1'),fit=f.control('Крій · необов’язково — позиція 1');assert.equal(size.maxLength,16);assert.equal(fit.maxLength,50);
 size.input('S'.repeat(17));assert.equal(f.button('Зберегти чернетку').disabled,true);assert.equal(f.posts().length,0);
 size.input('S'.repeat(16));fit.input('F'.repeat(51));assert.equal(f.button('Зберегти чернетку').disabled,true);
 fit.input('F'.repeat(50));assert.equal(f.button('Зберегти чернетку').disabled,false);f.replies.push(call=>f.response(decision(data,JSON.parse(call.body).items,'draft',1)));f.button('Зберегти чернетку').click();await f.flush();const sent=JSON.parse(f.posts()[0].body).items[0];assert.equal(sent.size,'S'.repeat(16));assert.equal(sent.fit_code,'F'.repeat(50));
});


function stockContext(){const data=decision(context(),[item()],'approved',2);data.stock_sync={eligible:true,reason:'',request_id:A,request_digest:hash('a'),decision_id:data.approved_head.id,decision_digest:hash('d'),preview_digest:hash('1'),pools:[{pool_id:'warehouse:11',source:'warehouse',before_total:0,protected:1,required:2,minimum_total:3,labels:['Футболка — чорний M'],size_counts:{}},{pool_id:'catalog_variant:12',source:'catalog_variant',before_total:0,protected:1,required:2,minimum_total:3,labels:['Худі з каталогу — білий'],size_counts:{'21':{size:'L',fit:'regular',before_total:0,protected:1,required:2,minimum_total:3}}}]};return data;}
function fillStock(f,total='5',size='4'){f.control('Фізичний залишок — група 1').input(total);f.control('Фізичний залишок — група 2').input(total);f.control('Фізична кількість розміру — група 2 — L · regular').input(size);f.control('Підтверджую весь фактичний залишок і кількості розмірів').click();}
function stockReceipt(data,operation){const next=clone(data);next.stock_sync={...next.stock_sync,eligible:false,reason:'already_synced',status:'stock_ready',sync_id:'77777777-7777-4777-8777-777777777777',operation_id:operation};return next;}

test('stock recount starts empty and sends explicit absolute pool counts separate from basket quantities',async()=>{
 const data=stockContext(),f=fixture();await f.load(data);assert.equal(f.control('Фізичний залишок — група 1').value,'');assert.equal(f.control('Фізична кількість розміру — група 2 — L · regular').value,'');assert.equal(f.button('Підтвердити переоблік').disabled,true);assert.match(f.root.textContent,/новий абсолютний підсумок/);
 fillStock(f,'5','2');assert.equal(f.button('Підтвердити переоблік').disabled,true);fillStock(f);assert.equal(f.button('Підтвердити переоблік').disabled,false);
 f.replies.push(call=>f.response(stockReceipt(data,JSON.parse(call.body).operation_id),{stock_sync:{ready:true,sync_id:'saved'}}));f.button('Підтвердити переоблік').click();await f.flush();const body=JSON.parse(f.posts()[0].body);assert.deepEqual(body.counts_by_pool,{'warehouse:11':{total:5},'catalog_variant:12':{total:5,size_counts:{'21':4}}});assert.deepEqual(Object.keys(body).sort(),['action','counts_by_pool','decision_id','expected_decision_digest','expected_preview_digest','expected_request_digest','operation_id','request_id']);assert.equal(body.expected_preview_digest,hash('1'));assert.equal(f.posts().length,1);assert.equal(f.controller.canLeave(),true);assert.match(f.root.textContent,/Переоблік збережено/);assert.equal(data.editable_items[0].checked_quantity,2);
});

test('stock stale preview preserves physical counts and needs explicit fresh confirmation',async()=>{
 const data=stockContext(),fresh=clone(data);fresh.stock_sync.preview_digest=hash('2');fresh.stock_sync.pools[0].protected=2;fresh.stock_sync.pools[0].minimum_total=4;const f=fixture();await f.load(data);fillStock(f);f.replies.push({status:409,data:{success:false,stock_sync:{ready:false,reason:'preview_changed'}}},f.response(fresh));f.button('Підтвердити переоблік').click();await f.flush();assert.equal(f.control('Фізичний залишок — група 1').value,'5');assert.equal(f.control('Підтверджую весь фактичний залишок і кількості розмірів').checked,false);assert.equal(f.button('Підтвердити переоблік').disabled,true);assert.equal(f.controller.canLeave(),false);
 f.control('Підтверджую весь фактичний залишок і кількості розмірів').click();f.replies.push(call=>f.response(stockReceipt(fresh,JSON.parse(call.body).operation_id),{stock_sync:{ready:true,sync_id:'saved'}}));f.button('Підтвердити переоблік').click();await f.flush();const bodies=f.posts().map(call=>JSON.parse(call.body));assert.equal(bodies[1].expected_preview_digest,hash('2'));assert.deepEqual(bodies[1].counts_by_pool,bodies[0].counts_by_pool);assert.notEqual(bodies[1].operation_id,bodies[0].operation_id);
});

test('UNKNOWN stock recount keeps exact A body and operation despite newer GET B',async()=>{
 const data=stockContext(),other=clone(data);other.request.id=B;other.stock_sync.request_id=B;other.stock_sync.decision_id=PUBLIC;other.approved_head.id=PUBLIC;const f=fixture();await f.load(data);fillStock(f);f.replies.push(new Error('timeout'));f.button('Підтвердити переоблік').click();await f.flush();const original=f.posts()[0].body;assert.equal(f.controller.canLeave(),false);await f.load(other);assert.match(f.calls.at(-1).url,/request_id=11111111/);assert.equal(f.controller.pending.body,original);f.replies.push(f.response(stockReceipt(data,JSON.parse(original).operation_id),{stock_sync:{ready:true,sync_id:'saved'}}));f.button('Перевірити ту саму операцію').click();await f.flush();assert.equal(f.posts()[1].body,original);assert.equal(f.uuids.length,1);assert.equal(f.controller.canLeave(),true);
});

test('stock receipt settles UNKNOWN only for exact original operation and proof identities',async()=>{
 const data=stockContext(),f=fixture();await f.load(data);fillStock(f);f.replies.push(new Error('timeout'));f.button('Підтвердити переоблік').click();await f.flush();const original=JSON.parse(f.posts()[0].body),receipt=stockReceipt(data,original.operation_id);
 const foreign=clone(receipt);foreign.request.id=B;foreign.stock_sync.request_id=B;await f.load(foreign);assert.ok(f.controller.pending);const wrong=clone(receipt);wrong.stock_sync.decision_digest=hash('9');await f.load(wrong);assert.ok(f.controller.pending);await f.load(receipt);assert.equal(f.controller.pending,null);assert.equal(f.controller.canLeave(),true);assert.equal(f.posts().length,1);
});

test('dirty offer and readonly viewer cannot issue stock recount',async()=>{
 const data=stockContext(),f=fixture();await f.load(data);fillStock(f);f.control('Назва — позиція 1').input('Нова назва');assert.equal(f.button('Підтвердити переоблік').disabled,true);const viewer=fixture(false);await viewer.load(data);assert.equal(viewer.button('Підтвердити переоблік'),undefined);assert.equal(viewer.posts().length,0);
});


test('stock no-op stays unobtrusive and nested operation conflict keeps exact recovery identity',async()=>{
 const noStock=stockContext();noStock.stock_sync={eligible:false,requires_sync:false,reason:'catalog_lines_missing',pools:[]};const simple=fixture();await simple.load(noStock);assert.equal(simple.root.querySelector('.ig-ac-stock'),null);
 const data=stockContext(),f=fixture();await f.load(data);fillStock(f);f.replies.push({status:409,data:{success:false,stock_sync:{ready:false,reason:'operation_conflict'}}});f.button('Підтвердити переоблік').click();await f.flush();assert.ok(f.controller.pending);assert.equal(f.controller.unknown,true);const original=f.posts()[0].body;f.replies.push(f.response(stockReceipt(data,JSON.parse(original).operation_id),{stock_sync:{ready:true,sync_id:'saved'}}));f.button('Перевірити ту саму операцію').click();await f.flush();assert.equal(f.posts()[1].body,original);assert.equal(f.uuids.length,1);
});


test('native catalog price drift exposes explicit CAS renewal without copying prior custom terms or photo',async()=>{
 const data=decision(context(),[item({title:'Попередня особлива назва',image_id:IMAGE,image_digest:hash('8')})],'approved',2);data.can_prepare_reason='assisted_catalog_price_changed';data.prepare_reason='Ціна каталогу змінилася.';const f=fixture();await f.load(data);const renew=f.button('Перевірити актуальні умови кошика');assert.equal(renew.disabled,false);assert.equal(f.posts().length,0);assert.match(f.root.textContent,/Нова версія потребує повторної перевірки та затвердження/);
 f.control('Колір — позиція 1').input('Локальна зміна');assert.equal(f.button('Перевірити актуальні умови кошика').disabled,true);f.button('Відкинути незбережені зміни').click();assert.equal(f.button('Перевірити актуальні умови кошика').disabled,false);
 const fresh=context();fresh.request.id=B;fresh.request.version=2;fresh.request.intention_id=data.request.intention_id;fresh.cas.expected_request_version=2;f.replies.push(f.response(fresh));f.button('Перевірити актуальні умови кошика').click();await f.flush();const sent=JSON.parse(f.posts()[0].body);assert.equal(sent.action,'request');assert.equal(sent.intention_id,data.request.intention_id);assert.equal(sent.expected_request_version,data.request.version);assert.equal(sent.expected_request_digest,data.request.request_digest);assert.equal(sent.expected_revision_digest,data.source_cas.expected_revision_digest);assert.equal(sent.expected_full_capture_digest,data.source_cas.expected_full_capture_digest);assert.equal(Object.hasOwn(sent,'items'),false);assert.equal(f.controller.local.items[0].title,fresh.editable_items[0].title);assert.equal(f.controller.local.items[0].image_id,null);assert.equal(f.controller.commerce.approved_head,null);assert.equal(f.controller.canPrepare(),false);
});

test('catalog renewal cannot run over unsaved recount or already prepared execution',async()=>{
 const data=stockContext();data.can_prepare_reason='assisted_catalog_facts_changed';const f=fixture();await f.load(data);assert.equal(f.button('Перевірити актуальні умови кошика').disabled,false);fillStock(f);assert.equal(f.button('Перевірити актуальні умови кошика').disabled,true);f.button('Відкинути введений переоблік').click();assert.equal(f.button('Перевірити актуальні умови кошика').disabled,false);data.status='prepared';data.prepared_checkout={ready:true,public_id:PUBLIC};await f.load(data);assert.equal(f.button('Перевірити актуальні умови кошика'),undefined);assert.equal(f.posts().length,0);
});
