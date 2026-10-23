const test=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const template=fs.readFileSync(path.join(__dirname,'../../twocomms/management/templates/management/bot.html'),'utf8');

function mountFixture(){
  const controllers=[],partialControllers=[],loads=[];
  const context={appendChild(root){root.parentNode=this;}};
  class Commerce{
    constructor(root,options){this.root=root;this.options=options;this.busy=false;this.unknown=false;this.pending=null;this.dirty=false;this.commerce=null;controllers.push(this);}
    canLeave(){return !this.pending&&!this.dirty;}
    destroy(){if(!this.canLeave())return false;this.destroyed=true;return true;}
    load(){loads.push(this);this.commerce=this.nextCommerce||{status:'none'};this.options.onCommerce(this.commerce);return Promise.resolve(true);}
    draw(){}
  }
  class Partial{
    constructor(root,options){this.root=root;this.options=options;this.busy=false;this.unknown=false;this.pending=null;this.dirty=false;partialControllers.push(this);}
    canLeave(){return !this.pending&&!this.dirty;}
    destroy(){if(!this.canLeave())return false;this.destroyed=true;return true;}
    update(value){this.commerce=value;}
    syncActions(){}
  }
  const sandbox={contextEl:context,contextPreservedRoots:new Set(),csrf:'fixture',CheckoutProposals:{select(){}},activate(){},syncAssistedPanelOrder(){},
    node:()=>({setAttribute(){}}),window:{IgAssistedCommerce:{Controller:Commerce},IgAssistedPartial:{Controller:Partial,isRelevant:()=>false}},
    fetch:()=>assert.fail('parent harness must only invoke the existing controller load API')};
  let source=template.slice(template.indexOf('    function renderAssistedCommerce(id){'),template.indexOf('    function renderSplitShipping(id){'));
  source=source.replace('{% url "management_assisted_commerce_api" 0 %}','/bot/api/clients/0/assisted-commerce/').replace(/\{%[^%]*%\}/g,'');
  vm.createContext(sandbox);
  vm.runInContext('let assistedCommerceController=null,assistedCommerceRoot=null,assistedCommerceClientId=null,assistedPartialController=null,assistedPartialRoot=null;'+source,sandbox);
  return {sandbox,controllers,partialControllers,loads,render:id=>sandbox.renderAssistedCommerce(id)};
}

test('accepted same-client detail refresh replaces an empty view with newly available commerce using the existing controller',()=>{
  const f=mountFixture();f.render(12);const c=f.controllers[0],p=f.partialControllers[0];
  assert.equal(f.loads.length,1);assert.equal(c.commerce.status,'none');
  c.nextCommerce={status:'current',request:{id:'new-request'}};f.render(12);
  assert.equal(f.controllers.length,1);assert.equal(f.loads.length,2);
  assert.equal(c.commerce.request.id,'new-request');assert.equal(p.commerce,c.commerce);
});

test('full detail refresh preserves dirty, busy, pending and unknown work in either controller',()=>{
  for(const owner of ['commerce','partial'])for(const field of ['dirty','busy','pending','unknown']){
    const f=mountFixture();f.render(12);const c=owner==='commerce'?f.controllers[0]:f.partialControllers[0];
    c[field]=field==='pending'?{body:'original-operation-body'}:true;f.render(12);
    assert.equal(f.loads.length,1,owner+' '+field);assert.ok(c[field]);
    c[field]=field==='pending'?null:false;f.render(12);assert.equal(f.loads.length,2);
  }
});

test('focused preserved commerce or shipping-terms root survives accepted detail refresh without reload',()=>{
  for(const owner of ['commerce','partial']){
    const f=mountFixture();f.render(12);const controller=owner==='commerce'?f.controllers[0]:f.partialControllers[0];
    f.sandbox.contextPreservedRoots.add(controller.root);f.render(12);
    assert.equal(f.loads.length,1);f.sandbox.contextPreservedRoots.clear();f.render(12);assert.equal(f.loads.length,2);
  }
});

test('client change destroys old clean controllers and loads only the new instance',()=>{
  const f=mountFixture();f.render(12);const old=f.controllers[0];f.render(23);
  assert.equal(old.destroyed,true);assert.equal(f.partialControllers[0].destroyed,true);
  assert.equal(f.controllers.length,2);assert.equal(f.loads.length,2);assert.equal(f.loads[1],f.controllers[1]);
  assert.match(f.controllers[1].options.apiUrl,/clients\/23\//);
});

test('reply entry preserves old text and context pins and explains why the requested reply was not created',()=>{
  const notices=[],draft={text:'original private draft',contextMessageId:11},updates=[],created=[];
  const sandbox={activeId:12,humanDocumentsClientId:12,ClientContextDrawer:{open(){}},humanDocumentsDisclosure:{open:false},
    humanDocumentsController:{hasUnsavedChanges:()=>true,update:options=>{draft.contextMessageId=options.contextMessageId;updates.push(options);}},
    humanDocumentsRoot:{querySelector:selector=>selector==='[data-action="new"]'?{click:()=>{draft.text='';created.push(true);}}:{focus(){}}},
    setGlobalFeedback:(...args)=>notices.push(args)};
  let source=template.slice(template.indexOf('    function openFulfillmentReply('),template.indexOf('    function placeFulfillmentReview(')).replace(/\{%[^%]*%\}/g,'');
  vm.createContext(sandbox);vm.runInContext(source,sandbox);sandbox.openFulfillmentReply(12,22);
  assert.equal(updates.length,0);assert.equal(created.length,0);assert.deepEqual(draft,{text:'original private draft',contextMessageId:11});
  assert.match(notices[0][0],/попередню приватну чернетку.*#22.*Нова відповідь ще не створена/);
  sandbox.activeId=23;sandbox.openFulfillmentReply(12,33);assert.equal(notices.length,1);
});
