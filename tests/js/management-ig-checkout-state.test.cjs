const test=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const template=fs.readFileSync(path.join(__dirname,'../../twocomms/management/templates/management/bot.html'),'utf8');
const source=template.slice(template.indexOf('    function appendCapturedCheckoutOrder('),template.indexOf('    function renderActualRequests('));
class Node{
 constructor(tag,cls='',text=''){this.tagName=tag;this.className=cls;this._text=String(text);this.children=[];this.style={};this.dataset={};this.listeners={};this.open=false;}
 append(...nodes){this.children.push(...nodes);}appendChild(node){this.append(node);return node;}
 replaceChildren(...nodes){this.children=nodes;this._text='';}
 addEventListener(type,handler){this.listeners[type]=handler;}
 get textContent(){return this._text+this.children.map(node=>node.textContent).join('');}
 set textContent(value){this._text=String(value);this.children=[];}
 all(){return [this,...this.children.flatMap(node=>node.all())];}
 visible(){return this._text+(this.tagName==='details'&&!this.open?this.children.filter(node=>node.tagName==='summary'):this.children).map(node=>node.visible()).join(' ');}
}
function order(){return {schema:'current-checkout-order.v1',scope:{client_id:12,episode_id:7,order_id:81},order_number:'TC-0081',observed_at:'2026-10-14T09:45:00Z',items:[{line_id:'private-current-line',title:'Футболка після правки',quantity:1,size:'L',fit:'classic',color:'black',unit_price:'1000.00',line_total:'1000.00'}],money:{gross:'1000.00',discount:'100.00',merchandise_payable:'900.00',payable:'900.00',confirmed_paid:'900.00',balance:'0.00',currency:'UAH'},shipping_authorities:[],checkout_changes:[{amendment_id:'private-amendment',source_hmac:'private-hmac',operation:'quantity',line_id:'private-current-line',changed_at:'2026-10-14T09:00:00Z',before:[{line_id:'private-current-line',title:'Футболка після правки',quantity:3}],after:[{line_id:'private-current-line',title:'Футболка після правки',quantity:1}]},{operation:'remove',line_id:'private-removed-line',changed_at:'2026-10-14T09:15:00Z',before:[{line_id:'private-removed-line',title:'Видалене худі',quantity:1}],after:[]}],source_ref:{proposal_id:11,attempt_id:21,paid_at:'2026-10-14T09:40:00Z'}};}
function slot(value=order(),extra={}){return {value,status:'confirmed',authority:'payment_ledger',confirmation_semantics:'verified_native_checkout_order_snapshot',fully_paid:true,source_refs:[{kind:'order',id:81}],...extra};}
async function render(slots){const root=new Node('div'),calls=[],node=(tag,cls,text)=>new Node(tag,cls,text);const section=(root,title)=>{const block=node('section','',title);root.appendChild(block);return block;};const context={node,section,Date,Intl,sizeCorrectionController:null,sizeCorrectionClientId:null,fetch:async(url,options)=>{calls.push({url,options});return {ok:true,json:async()=>({success:true,status:'captured',as_of:'2026-10-14T10:00:00Z',state:{slots,omissions:[]}})};}};const api=vm.runInNewContext(source+'\n({renderCapturedState});',context);api.renderCapturedState(root,12);const details=root.all().find(node=>node.className==='bot-analysis-disclosure');assert.equal(calls.length,0);details.open=true;await details.listeners.toggle();return {root,calls,visible:()=>root.visible()};}

test('real lazy state renderer displays final paid goods and dated quantity/removal changes without exposing technical proof',async()=>{
 const f=await render({'order.completed':slot()});assert.equal(f.calls.length,1);assert.equal(f.calls[0].options.cache,undefined);
 const visible=f.visible();assert.match(visible,/Фактичне замовлення.*TC-0081/);assert.match(visible,/До оплати.*900,00 грн/);assert.match(visible,/Підтверджено оплачено.*900,00 грн/);assert.match(visible,/Повна оплата підтверджена/);
 assert.match(visible,/Знімок замовлення: 14\.10\.2026, 12:45 \(Київ\)/);assert.match(visible,/Оплату зафіксовано: 14\.10\.2026, 12:40 \(Київ\)/);
 assert.match(visible,/14\.10\.2026, 12:00 \(Київ\).*кількість 3 → 1/);assert.match(visible,/14\.10\.2026, 12:15 \(Київ\).*Видалено: Видалене худі/);
 assert.doesNotMatch(visible,/private-|proposal_id|source_hmac|order\.completed|джерело №81/);
 const goods=f.root.all().find(node=>node.tagName==='ul');assert.equal(goods.children.length,1);assert.match(goods.textContent,/1 шт/);assert.doesNotMatch(goods.textContent,/3 шт|Видалене худі/);
 const proof=f.root.all().find(node=>node.tagName==='pre');assert.match(proof.textContent,/private-amendment/);assert.equal(f.root.all().filter(node=>node.tagName==='button'||node.tagName==='input').length,0);
});

test('deposit and unknown balance render their actual totals without claiming full payment or substituting zero',async()=>{
 const deposit=order();deposit.money.confirmed_paid='200.00';deposit.money.balance='700.00';
 const f=await render({'order.completed':slot(deposit,{fully_paid:false})});assert.match(f.visible(),/Підтверджено часткову оплату.*700,00 грн/);assert.doesNotMatch(f.visible(),/Повна оплата підтверджена/);
 const unknown=order();unknown.money.confirmed_paid=null;unknown.money.balance=null;const u=await render({'order.completed':slot(unknown,{fully_paid:false})});assert.match(u.visible(),/Оплату або залишок ще не підтверджено/);assert.doesNotMatch(u.visible(),/Повна оплата підтверджена/);
});

test('explicit prior order stays visibly historical and cannot lend payment to the current purchase',async()=>{
 const f=await render({'order.completed':{value:null,status:'unknown',authority:'none'},'order.completed_history':slot([order()],{historical:true,confirmation_semantics:'verified_historical_checkout_orders',current_purchase_authority:false})});
 assert.match(f.visible(),/Попередні оформлення/);assert.match(f.visible(),/Їхня оплата не підтверджує оплату поточного кошика/);assert.match(f.visible(),/TC-0081/);assert.doesNotMatch(f.visible(),/Повна оплата підтверджена/);
});

test('unverified or wrong-authority order snapshot does not render actual charged facts',async()=>{
 for(const extra of [{status:'unknown'},{authority:'derived'},{confirmation_semantics:'raw_order_label'}]){const f=await render({'order.completed':slot(order(),extra)});assert.doesNotMatch(f.visible(),/Футболка після правки|900,00 грн|Повна оплата підтверджена/);}
});

test('literal item titles remain escaped text in the actual renderer with no generated markup or outbound mutations',async()=>{
 const value=order();value.items[0].title='<img src=x onerror=alert(1)>';const f=await render({'order.completed':slot(value)});
 assert.match(f.visible(),/<img src=x onerror=alert\(1\)>/);assert.equal(f.root.all().some(node=>node.tagName==='img'),false);assert.equal(f.calls.length,1);assert.equal(f.calls[0].options.method,undefined);
});

test('verified delivery modes distinguish merchant funding from customer included and carrier-paid fees',async()=>{
 for(const [shipping,expected,absent]of [
  [{mode:'merchant_free',payer_type:'Sender'},/Доставку оплачує TwoComms/,/клієнтом перевізнику/],
  [{mode:'carrier_recipient',payer_type:'Recipient',customer_maximum:'120.00'},/клієнтом перевізнику окремо.*120,00 грн/,/Доставку оплачує TwoComms/],
  [{mode:'customer_paid_included',payer_type:'Sender',customer_charge:'80.00'},/доставки включена в оформлення.*80,00 грн/,/Доставку оплачує TwoComms/],
  [{mode:'customer_prepaid',payer_type:'Sender',delivery_prepaid:false},/її підтвердження звірте окремо/,/Доставку передплачено окремо/]
 ]){const value=order();value.shipping_authorities=[shipping];const f=await render({'order.completed':slot(value)});assert.match(f.visible(),expected);assert.doesNotMatch(f.visible(),absent);}
});
