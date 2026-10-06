/* Offline regression for the actual read-only payment composition. */
'use strict';
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const source=fs.readFileSync(path.join(__dirname,'static/management/ig_journey.js'),'utf8')
  .replace('window.TwcJourney={create:options=>new Journey(options)};','window.TwcJourney={Journey,paymentView};');
const sandbox={window:{}};
vm.runInNewContext(source,sandbox);
const {Journey,paymentView}=sandbox.window.TwcJourney;
const journey=Object.create(Journey.prototype);
journey.serverTime=Date.parse('2026-10-06T12:00:00Z');
const reviewRef={kind:'payment_review',id:32};
const messageRef={kind:'message',id:3285};
function pending(overrides={}){
  return {id:'guide:payment',semantic_key:'settlement',state:'partial',current:true,
    episode_id:195,facts:[{source:'payment_review.current',state:'open',evidence_refs:[reviewRef]}],
    waiting:{kind:'manager_review',evidence_refs:[reviewRef]},evidence_refs:[reviewRef],...overrides};
}
function composed(node,other=[]){
  return journey.mergePayment({nodes:[node,...other],edges:[]}).nodes.find(n=>n.payment_progress);
}
const discussion={id:'trace:payment',semantic_key:'awaiting_payment',episode_id:195,
  presentation_kind:'interpretation',facts:[],evidence_refs:[]};
const noReceipt=composed(pending(),[discussion]);
assert.equal(noReceipt.payment_progress.label,'Очікує перевірки менеджером');
assert.equal(noReceipt.payment_progress.paid,false);
assert.equal(noReceipt.payment_progress.counts_known,false);
assert.equal(noReceipt.payment_progress.items.filter(i=>i.state==='done').length,0);
assert.match(noReceipt.summary,/зарахування оплати ще не підтверджено/);
assert.equal(noReceipt.waiting.kind,'manager_review');
const receipt=composed(pending({waiting:{kind:'manager_review',evidence_refs:[reviewRef],
  receipt_received:true,receipt_evidence_refs:[messageRef]}}),[discussion]);
assert.equal(receipt.payment_progress.label,'Очікує перевірки менеджером');
assert.equal(receipt.payment_progress.counts_known,true);
assert.equal(receipt.payment_progress.items[0].label,'Квитанція');
assert.equal(receipt.payment_progress.items[0].state,'done');
assert.equal(receipt.payment_progress.items[0].evidence_refs[0].id,3285);
assert.equal(receipt.payment_progress.items[1].state,'next');
assert.equal(receipt.payment_progress.items[2].state,'todo');
assert.equal(receipt.payment_progress.paid,false);
for(const refs of [[],[{kind:'payment_review',id:32}],[{kind:'message',id:0}],[{kind:'message',id:'3285'}],{}]){
  const unbound=composed(pending({waiting:{kind:'manager_review',evidence_refs:[reviewRef],receipt_received:true,receipt_evidence_refs:refs}}));
  assert.equal(unbound.payment_progress.receipt_received,false);
  assert.equal(unbound.payment_progress.counts_known,false);
}
const question=composed({id:'question',semantic_key:'awaiting_payment',presentation_kind:'interpretation',
  waiting:{kind:'manager_review',evidence_refs:[reviewRef]},facts:[],evidence_refs:[messageRef]});
assert.equal(question.payment_progress.manager_review_pending,false);
assert.equal(question.payment_progress.label,'Обговорюємо оплату');
const foreign=composed(pending({facts:[{source:'payment_review.current',state:'open',
  evidence_refs:[{kind:'payment_review',id:99}]}]}));
assert.equal(foreign.payment_progress.manager_review_pending,false);
const differentEpisodes={nodes:[pending(),{...discussion,episode_id:99}],edges:[]};
assert.equal(journey.mergePayment(differentEpisodes),differentEpisodes);
const paid=composed(pending(),[{id:'paid',semantic_key:'settlement',episode_id:195,state:'complete',
  evidence_refs:[{kind:'payment_projection',id:7}],facts:[]}]);
assert.equal(paid.payment_progress.paid,true);
assert.equal(paid.payment_progress.label,'Оплачено');
assert.equal(paid.payment_progress.items[2].state,'done');
const expired={kind:'invoice_expiry',status:'expired',started_at:'2026-10-06T10:00:00Z',
  due_at:'2026-10-06T11:00:00Z',evidence_refs:[{kind:'invoice',id:1}]};
assert.equal(paymentView({},[expired],journey.serverTime).label,'Строк минув · оплату не підтверджено');
assert.equal(paymentView({manager_review_pending:true},[expired],journey.serverTime).label,'Очікує перевірки менеджером');
assert.equal(paymentView({paid:true,manager_review_pending:true},[expired],journey.serverTime).label,'Оплачено');
console.log('Pending review, bound receipt, paid priority, expiry, and foreign-source regressions passed.');
const template=fs.readFileSync(path.join(__dirname,'templates/management/bot.html'),'utf8');
const stageSource=template.slice(template.indexOf('function conversationStageLabel(payload)'),template.indexOf('function linkedOrderContextBadge('));
vm.runInNewContext(stageSource+'\nthis.stageLabel=conversationStageLabel;',sandbox);
const stagePayload={client:{stage:'new',stage_label:'Новий'},journey:{current_episode_id:195,viewed_episode_id:195,is_history:false,graph:{nodes:[pending()]}}};
assert.equal(sandbox.stageLabel(stagePayload),'Оплата потребує перевірки');
assert.equal(sandbox.stageLabel({...stagePayload,client:{stage:'paid',stage_label:'Оплачено'}}),'Оплачено');
assert.equal(sandbox.stageLabel({...stagePayload,journey:{...stagePayload.journey,is_history:true}}),'Новий');
assert.equal(sandbox.stageLabel({...stagePayload,journey:{...stagePayload.journey,viewed_episode_id:99}}),'Новий');
assert.equal(sandbox.stageLabel({...stagePayload,payment:{scope:'current_episode',episode_id:195,needs_reconciliation:true}}),'Новий');
assert.equal(sandbox.stageLabel({...stagePayload,payment:{scope:'current_episode',episode_id:195,review_id:99}}),'Новий');
console.log('Conversation header source guard and paid/history/reconciliation priority passed.');
