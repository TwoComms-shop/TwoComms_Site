const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const crypto = require('node:crypto');

const source = fs.readFileSync(path.join(__dirname,
  '../../twocomms/management/static/management/js/ig_fulfillment_review.js'), 'utf8');
const REQUEST_A = '11111111-1111-4111-8111-111111111111';

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

const DIGEST='a'.repeat(64);
function review({reviewed=false,multi=false,final=false}={}){
  const lines=[{line_id:'selected',label:'Selected tee XXL',target:{size:'XXL',quantity:1}}];
  if(multi)lines.push({line_id:'unrelated',label:'Other tee M',target:{size:'M',quantity:1}});
  return {version:1,status:reviewed?'reviewed':'pending',request_digest:'signed-request',current_request:{id:REQUEST_A,scope:'cart',
    customer_requirements:[{deadline_hard:true,deadline_kind:'departure',deadline_at:'2026-10-22T10:00:00+03:00',date_status:'resolved',deadline_raw:'tomorrow',source_id:7,requirement_digest:DIGEST}],
    lines,sources:[]},decision:reviewed?{id:'sealed-decision',version:1,request_digest:'signed-request',lines:lines.map(line=>({line_id:line.line_id,availability:'wait',checked_quantity:null,supply_basis:null,source_location:'',alternative:'',clarification:'',
      preparation:{min_days:1,max_days:3,unit:'calendar',anchor:'after_payment_confirmation'},dispatch:null,configuration_digest:'configuration-proof',inventory_digest:'inventory-proof',accounting_recount_required:false})),
      deadline_review:{requirement_digest:DIGEST,outcome:final?'impossible':'review_pending',line_ids:['selected'],options:['later_domestic_contact']},split_offer:null}:null};
}
async function flush(){await new Promise(setImmediate);await new Promise(setImmediate);}
function fixture({canOperate=true}={}){
  const document={createElement(tag){return new Node(tag,this);}};
  const root=new Node('div',document),calls=[],replies=[];
  const fetch=async(url,options={})=>{calls.push({url:String(url),method:options.method||'GET',options});assert.ok(replies.length);const value=replies.shift();if(value instanceof Error)throw value;return value.http||{ok:true,status:200,json:async()=>({success:true,review:value})};};
  const window={location:{origin:'https://management.example'},crypto};vm.runInNewContext(source,{window,document,fetch,URL,AbortController,Date,Intl,Uint8Array});
  const controller=window.IgFulfillmentReview.create(root,{canOperate,csrfToken:'synthetic-csrf',url:id=>'/bot/api/clients/'+id+'/fulfillment-review/'});
  const label=text=>root.querySelectorAll('select,input,textarea').find(node=>node.attributes['aria-label']===text);
  return {root,calls,replies,controller,label,async load(value){replies.push(value);controller.render(12);await flush();},posts(){return calls.filter(call=>call.method==='POST');}};
}
function choose(node,value){assert.ok(node);assert.equal(node.disabled,false);node.value=value;node.emit('change');}
function submit(f){f.root.querySelector('button[type="submit"]').click();}
function pendingFields(f){for(const node of f.root.querySelectorAll('select'))if((node.attributes['aria-label']||'').startsWith('Наявність — '))choose(node,'wait');}
function http(status,body){return {http:{ok:status>=200&&status<300,status,json:async()=>body}};}

test('initial hard cutoff requires explicit outcome and serializes exact source scope and payment anchor',async()=>{
  const f=fixture();await f.load(review());pendingFields(f);submit(f);await flush();assert.equal(f.posts().length,0);
  assert.match(f.root.textContent,/Оберіть висновок/);assert.match(f.root.textContent,/10:00/);assert.match(f.root.textContent,/22\.10\.2026/);
  choose(f.label('Висновок щодо обов’язкового строку'),'impossible');
  choose(f.label('Готовність товару — термін'),'1-3');choose(f.label('Готовність товару — початок відліку'),'after_payment_confirmation');
  f.label('Отримання пізніше хлопцем або іншою контактною особою в Україні').click();
  f.label('Міжнародна доставка: окремо перевірити можливість і вартість').click();
  f.replies.push(http(200,{success:true,review:review({reviewed:true,final:true})}));submit(f);await flush();
  const call=f.posts()[0],body=JSON.parse(call.options.body);
  assert.equal(call.url,'/bot/api/clients/12/fulfillment-review/');assert.equal(call.options.headers['X-CSRFToken'],'synthetic-csrf');
  assert.equal(body.request_id,REQUEST_A);assert.equal(body.expected_request_digest,'signed-request');assert.match(body.operation_id,/^[a-f0-9-]{36}$/);
  assert.deepEqual(body.decisions_by_line.deadline_review,{requirement_digest:DIGEST,outcome:'impossible',line_ids:['selected'],options:['later_domestic_contact','international_review_required']});
  assert.deepEqual(body.decisions_by_line.lines[0].preparation,{min_days:1,max_days:3,unit:'calendar',anchor:'after_payment_confirmation'});
  assert.equal(body.decisions_by_line.lines[0].dispatch,null);assert.doesNotMatch(JSON.stringify(body),/anchor_at|arrival_guaranteed/);
});

test('multiple items require explicit affected lines and never default the whole cart',async()=>{
  const f=fixture();await f.load(review({multi:true}));pendingFields(f);choose(f.label('Висновок щодо обов’язкового строку'),'review_pending');
  submit(f);await flush();assert.equal(f.posts().length,0);assert.match(f.root.textContent,/Оберіть позиції/);
  f.label('Строк стосується — Selected tee XXL').click();f.replies.push(http(500,{success:false}));submit(f);await flush();
  assert.deepEqual(JSON.parse(f.posts()[0].options.body).decisions_by_line.deadline_review.line_ids,['selected']);
  assert.equal(f.label('Строк стосується — Other tee M').checked,false);
});

test('review_pending appends impossible with readonly line facts and original timing domain',async()=>{
  const f=fixture();await f.load(review({reviewed:true}));
  assert.equal(f.label('Наявність — Selected tee XXL'),undefined);assert.match(f.root.textContent,/після підтвердження оплати/);
  assert.equal(f.label('Строк стосується — Selected tee XXL').disabled,true);
  assert.equal(f.label('Висновок щодо обов’язкового строку').querySelectorAll('option').some(node=>node.value==='review_pending'),false);
  choose(f.label('Висновок щодо обов’язкового строку'),'impossible');f.replies.push(http(200,{success:true,review:review({reviewed:true,final:true})}));submit(f);await flush();
  const body=JSON.parse(f.posts()[0].options.body);assert.equal(body.decisions_by_line.deadline_review.requirement_digest,DIGEST);
  assert.equal(body.decisions_by_line.lines[0].availability,'wait');assert.equal(body.decisions_by_line.lines[0].preparation.anchor,'after_payment_confirmation');
  assert.doesNotMatch(JSON.stringify(body),/configuration_digest|inventory_digest|accounting_recount_required/);
  assert.equal(f.root.querySelector('button[type="submit"]'),null);
});

test('viewer and final impossible outcome remain readonly and cannot create new authority',async()=>{
  for(const value of [review(),review({reviewed:true}),review({reviewed:true,final:true})]){
    const f=fixture({canOperate:false});await f.load(value);assert.equal(f.root.querySelectorAll('input,select,textarea').length,0);assert.equal(f.root.querySelector('button[type="submit"]'),null);assert.equal(f.posts().length,0);
  }
  const f=fixture();const value=review({reviewed:true});value.decision.deadline_review.requirement_digest='b'.repeat(64);await f.load(value);
  assert.equal(f.root.querySelector('button[type="submit"]'),null);
});

test('unknown POST retry keeps the same operation UUID, request digest and typed decision bytes',async()=>{
  const f=fixture();await f.load(review());pendingFields(f);choose(f.label('Висновок щодо обов’язкового строку'),'impossible');
  f.replies.push(new Error('network outcome unknown'));submit(f);await flush();
  f.replies.push(http(500,{success:false}));submit(f);await flush();assert.equal(f.posts().length,2);
  assert.equal(f.posts()[0].options.body,f.posts()[1].options.body);
});

test('signed incomplete history is visible and server active requirement governs old hints',async()=>{
  const f=fixture();const value=review();
  value.current_request.deadline_history={status:'incomplete',needs_customer_clarification:true};
  value.current_request.active_hard_requirement_digest=DIGEST;
  value.current_request.customer_requirements.push({supersession_hint:'changed',raw_quote:'Тепер змініть розмір',date_status:'none'});
  await f.load(value);
  assert.match(f.root.querySelector('.ig-fr-history-clarification').textContent,/Потрібно уточнити поточний критичний строк/);
  assert.ok(f.label('Висновок щодо обов’язкового строку'));
  assert.match(f.root.querySelector('.ig-fr-requirements').textContent,/10:00/);
  assert.equal(f.posts().length,0);
  const withdrawn=review();withdrawn.current_request.active_hard_requirement_digest='';
  await f.load(withdrawn);assert.equal(f.label('Висновок щодо обов’язкового строку'),undefined);
});
