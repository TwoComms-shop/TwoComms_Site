/* Presentation only: coordinates never create client facts or transitions. */
(function () {
  'use strict';
  const rows = [
    ['inbound',0,0,'message','Звернення'],
    ['ad_resolved_product',1,0,'shirt','Відомий товар'],
    ['catalog_discovery',2,0,'shirt','Підбір'],
    ['photo_reference',1,2,'image','Фото'],
    ['availability_question',2,2,'question','Доступність'],
    ['custom_print',1,1,'image','Кастом'],
    ['dtf_only',1,3,'image','DTF-плівка'],
    ['custom_brief',2,1,'brief','Бриф'],
    ['mockup_current_acceptance',3,1,'image','Макет'],
    ['prize_candidate',1,4,'gift','Приз'],
    ['prize_decision',2,4,'person','Рішення про приз'],
    ['information_question',1,-8,'info','Питання'],
    ['information_resolved',3,-8,'message','Відповідь'],
    ['collaboration',1,-3,'handshake','Співпраця'],
    ['collaboration_designer',2,-6,'image','Дизайнер'],
    ['collaboration_partnership',2,-5,'handshake','Партнерство'],
    ['collaboration_dropship',2,-4,'package','Dropship'],
    ['collaboration_wholesale_store',2,-3,'package','Опт / магазин'],
    ['collaboration_creator',2,-2,'image','Creator'],
    ['collaboration_other',2,-1,'handshake','Інше'],
    ['business_decision',3,-3,'person','Рішення команди'],
    ['employment',1,-7,'work','Робота'],
    ['employment_response',3,-7,'message','Відповідь'],
    ['spam_confirmed',1,9,'cross','Спам'],
    ['configured_line',3,0,'shirt','Склад'],
    ['quoted_offer',4,0,'tag','Пропозиція'],
    ['awaiting_payment',5,0,'money','Очікування оплати'],
    ['payment_help',5,2,'question','Допомога'],
    ['settlement',6,0,'money','Розрахунок'],
    ['fulfillment',7,0,'package','Виконання'],
    ['objection_case',4,4,'question','Заперечення'],
    ['channel_consent',7,6,'bell','Згода на канал'],
    ['channel_grant_checked',8,6,'person','Дозвіл каналу'],
    ['post_purchase_contact_offer',9,7,'message','Після покупки'],
    ['post_sale_request',1,5,'return','Сервісний запит'],
    ['post_sale_case',8,5,'return','Сервіс'],
    ['ugc_assessment',10,7,'image','Перевірка UGC'],
    ['reward_entitlement',11,7,'gift','Право на нагороду'],
    ['reward_delivery',12,7,'gift','Видача нагороди'],
    ['reward_use',13,7,'tag','Використання'],
    ['repeat_interest',8,8,'repeat','Новий інтерес'],
    ['new_purchase_interest',14,8,'repeat','Наступна покупка']
  ];
  const visuals = new Map(rows.map(([key,rank,lane,icon,short_label]) =>
    [key,Object.freeze({rank,lane,icon,short_label})]));
  const intentKeys = {catalog:'catalog_discovery',custom_print:'custom_print',dtf:'dtf_only',
    information:'information_question',employment:'employment',collaboration:'collaboration',
    support:'post_sale_request',community:'prize_candidate'};
  const returns = new Set(['configuration_correction','offer_correction','settlement_correction',
    'new_selection','amended_offer','new_attempt']);
  function visualFor(node = {}) {
    const guideAliases={'guide:selection':'catalog_discovery','guide:offer':'mockup_current_acceptance','guide:terms':'quoted_offer','guide:payment':'settlement','guide:fulfillment':'fulfillment','guide:inquiry':'inbound'};
    const exact = visuals.get(node.semantic_key) || visuals.get(guideAliases[node.id]);
    if (exact) return {...exact};
    if (node.semantic_key === 'conversation_intent') {
      const subtype = node.route_kind === 'collaboration' && node.route_subtype
        ? visuals.get('collaboration_' + node.route_subtype) : null;
      const near = subtype || visuals.get(intentKeys[node.route_kind]);
      // A topic has its own position and identity; it is not a visited stage.
      if (near) return {...near,lane:near.lane + .35,
        short_label:node.short_label || node.label || near.short_label};
    }
    return {rank:Number.isFinite(node.layout?.rank) ? node.layout.rank : 0,
      lane:Number.isFinite(node.layout?.lane) ? node.layout.lane : 0,
      icon:'info',short_label:node.short_label || node.label || 'Подія'};
  }
  function isReturn(edge, a, b) {
    return b.col < a.col || edge.relation === 'return' || returns.has(edge.outcome);
  }
  function layout({nodes = [],edges = [],width = 560,full = false} = {}) {
    const available = Math.max(80,Number.isFinite(width) ? width : 560);
    const unique = [...new Map(nodes.filter(n => n && typeof n.id === 'string').map(n => [n.id,n])).values()];
    const ordered = unique.map(node => ({node,...visualFor(node)}))
      .sort((a,b) => a.rank-b.rank || a.lane-b.lane || a.node.id.localeCompare(b.node.id));
    const ranks = [...new Set(ordered.map(n => n.rank))];
    const lanes = [...new Set(ordered.map(n => n.lane))].sort((a,b) => a-b);
    const positions = new Map(),occupied = new Set();
    let maxRow = 0;
    for (const item of ordered) {
      const col = ranks.indexOf(item.rank);
      let row = lanes.indexOf(item.lane);
      // Distinct versions/scopes remain distinct; never move a node to a later phase.
      while (occupied.has(col + ':' + row)) row++;
      occupied.add(col + ':' + row);maxRow = Math.max(maxRow,row);
      positions.set(item.node.id,{x:0,y:0,col,row});
    }
    const backwards = edges.filter(e => positions.has(e.from_node_id) && positions.has(e.to_node_id)
      && isReturn(e,positions.get(e.from_node_id),positions.get(e.to_node_id))).length;
    const top = full ? 32 + backwards * 8 : 4 + Math.min(backwards,2) * 6;
    const step = full ? 112 : (available - 32) / Math.max(1,ranks.length);
    for (const p of positions.values()) {
      p.x = (full ? 32 : 16) + step * (p.col + .5);
      p.y = top + 22 + p.row * (full ? 64 : 50);
    }
    return {positions,width:full ? Math.max(available,64 + ranks.length * 112) : available,
      height:positions.size ? top + (full ? 70 : 60) + maxRow * (full ? 64 : 50) : 64};
  }
  function tidy(points) {
    const result = [];
    for (const p of points) {
      const last = result[result.length-1];
      if (last && last.x === p.x && last.y === p.y) continue;
      while (result.length > 1) {
        const a = result[result.length-2],b = result[result.length-1];
        if (!((a.x === b.x && b.x === p.x) || (a.y === b.y && b.y === p.y))) break;
        result.pop();
      }
      result.push(p);
    }
    return result;
  }
  function blocked(a,b,boxes) {
    return boxes.some(r => a.x === b.x
      ? a.x > r.left && a.x < r.right && Math.max(a.y,b.y) > r.top && Math.min(a.y,b.y) < r.bottom
      : a.y > r.top && a.y < r.bottom && Math.max(a.x,b.x) > r.left && Math.min(a.x,b.x) < r.right);
  }
  class Heap {
    constructor(){this.items=[];}
    push(value){const a=this.items;let i=a.length;a.push(value);while(i){const p=(i-1)>>1;if(a[p].f<=value.f)break;a[i]=a[p];i=p;}a[i]=value;}
    pop(){const a=this.items,first=a[0],last=a.pop();if(a.length){let i=0;while(i*2+1<a.length){let child=i*2+1;if(child+1<a.length&&a[child+1].f<a[child].f)child++;if(a[child].f>=last.f)break;a[i]=a[child];i=child;}a[i]=last;}return first;}
  }
  // Orthogonal visibility grid. No fallback ever draws through an obstacle.
  function findPath(start,end,boxes,width,height) {
    const xs = [...new Set([4,width-4,start.x,end.x,...boxes.flatMap(r=>[r.left-2,r.right+2])])].filter(x=>x>=0&&x<=width).sort((a,b)=>a-b);
    const ys = [...new Set([4,height-4,start.y,end.y,...boxes.flatMap(r=>[r.top-2,r.bottom+2])])].filter(y=>y>=0&&y<=height).sort((a,b)=>a-b);
    const nx=xs.length,sx=xs.indexOf(start.x),sy=ys.indexOf(start.y),ex=xs.indexOf(end.x),ey=ys.indexOf(end.y);
    if(sx<0||sy<0||ex<0||ey<0)return null;
    const startKey=sy*nx+sx,endKey=ey*nx+ex;
    const distance=new Map([[startKey,0]]),previous=new Map(),heap=new Heap();
    const point=k=>({x:xs[k%nx],y:ys[Math.floor(k/nx)]});
    heap.push({key:startKey,g:0,f:0});
    while(heap.items.length){
      const item=heap.pop(),key=item.key;if(item.g!==distance.get(key))continue;
      if(key===endKey){const path=[];let cursor=key;while(cursor!==undefined){path.push(point(cursor));cursor=previous.get(cursor);}return tidy(path.reverse());}
      const x=key%nx,y=Math.floor(key/nx),a=point(key);
      for(const [cx,cy] of [[x-1,y],[x+1,y],[x,y-1],[x,y+1]]){
        if(cx<0||cy<0||cx>=nx||cy>=ys.length)continue;
        const next=cy*nx+cx,b=point(next);if(blocked(a,b,boxes))continue;
        const cost=item.g+Math.abs(a.x-b.x)+Math.abs(a.y-b.y);
        if(cost >= (distance.get(next) ?? Infinity))continue;
        distance.set(next,cost);previous.set(next,key);
        heap.push({key:next,g:cost,f:cost+Math.abs(b.x-end.x)+Math.abs(b.y-end.y)});
      }
    }
    return null;
  }
  function pathData(points) {
    if(!points.length)return '';
    const xy=p=>Number(p.x.toFixed(2))+' '+Number(p.y.toFixed(2));
    let d='M'+xy(points[0]);
    for(let i=1;i<points.length-1;i++){
      const a=points[i-1],b=points[i],c=points[i+1],ab=Math.hypot(b.x-a.x,b.y-a.y),bc=Math.hypot(c.x-b.x,c.y-b.y),r=Math.min(6,ab/2,bc/2);
      if(!r){d+=' L'+xy(b);continue;}
      const before={x:b.x+(a.x-b.x)*r/ab,y:b.y+(a.y-b.y)*r/ab},after={x:b.x+(c.x-b.x)*r/bc,y:b.y+(c.y-b.y)*r/bc};
      d+=' L'+xy(before)+' Q'+xy(b)+' '+xy(after);
    }
    return d+' L'+xy(points[points.length-1]);
  }
  function routeEdges({nodes = [],edges = [],positions = new Map(),width = 560,height = 200} = {}) {
    const result=new Map(),byId=new Map(nodes.map(n=>[n.id,n]));
    const distinctX=[...new Set([...positions.values()].map(p=>p.x))].sort((a,b)=>a-b);
    const gap=distinctX.length>1?Math.min(...distinctX.slice(1).map((x,i)=>x-distinctX[i])):112;
    const halfLabel=Math.max(22,Math.min(50,(gap-12)/2));
    const boxes=[];
    for(const [id,p] of positions){
      if(!byId.has(id))continue;
      boxes.push({left:p.x-23,right:p.x+23,top:p.y-23,bottom:p.y+23});
      boxes.push({left:p.x-halfLabel-1,right:p.x+halfLabel+1,top:p.y+21,bottom:p.y+39});
    }
    let returnTrack=0;
    const minY=Math.min(...[...positions.values()].map(p=>p.y));
    for(const edge of [...edges].sort((a,b)=>String(a.id).localeCompare(String(b.id)))){
      const a=positions.get(edge.from_node_id),b=positions.get(edge.to_node_id);
      if(!a||!b||!byId.has(edge.from_node_id)||!byId.has(edge.to_node_id))continue;
      let start,end,path;
      if(isReturn(edge,a,b)){
        start={x:a.x,y:a.y-25};end={x:b.x,y:b.y-25};
        const corridor=Math.max(4,minY-32-8*returnTrack++);
        const left={x:a.x,y:corridor},right={x:b.x,y:corridor};
        const one=findPath(start,left,boxes,width,height),two=findPath(right,end,boxes,width,height);
        if(one&&two&&!blocked(left,right,boxes))path=tidy([...one,right,...two]);
      }else if(a.col===b.col){
        const down=b.y>a.y;
        start={x:a.x,y:a.y+(down?41:-25)};end={x:b.x,y:b.y+(down?-25:41)};
        path=findPath(start,end,boxes,width,height);
      }else{
        start={x:a.x+25,y:a.y};end={x:b.x-25,y:b.y};
        const middle={x:(start.x+end.x)/2,y:start.y},join={x:middle.x,y:end.y};
        const direct=[start,middle,join,end];
        path=direct.slice(1).every((p,i)=>!blocked(direct[i],p,boxes))?tidy(direct):findPath(start,end,boxes,width,height);
      }
      if(!path||path.length<2)continue;
      // The longest clear horizontal segment is a stable reason-marker anchor.
      let marker=null,length=-1;
      for(let i=1;i<path.length;i++){
        const p=path[i-1],q=path[i],span=Math.abs(p.x-q.x)+Math.abs(p.y-q.y);
        if(p.y===q.y&&span>length){length=span;marker={x:(p.x+q.x)/2,y:p.y};}
      }
      if(!marker){const p=path[0],q=path[path.length-1];marker={x:(p.x+q.x)/2,y:(p.y+q.y)/2};}
      result.set(edge.id,{d:pathData(path),markerX:marker.x,markerY:marker.y});
    }
    return result;
  }
  window.TwcJourneyGeometry=Object.freeze({visualFor,layout,routeEdges});
})();
