(function(global){
  'use strict';
  const labels={pending:'Очікується',active:'Приватний файл',expired:'Строк зберігання минув',deleting:'Видаляється',delete_failed:'Видалення не завершене',deleted:'Видалено',missing:'Файл недоступний',unverified:'Доступність невідома'};
  const details={capture_pending:'Очікується отримання вкладення.',deletion_requested:'Видалення очікується; завершення ще не підтверджене.',privacy_erasure:'Видалення приватних даних очікується.',owned_capture:'Файл буде перевірено під час приватного перегляду.',retention_elapsed:'Видалення ще не підтверджене.',deletion_in_progress:'Завершення видалення ще не підтверджене.',deletion_retry:'Очікується повторна спроба видалення.',deletion_confirmed:'Видалення підтверджене.',capture_missing:'Вкладення недоступне.',expiry_unknown:'Строк зберігання невідомий; приватний перегляд недоступний.',preview_unsupported:'Цей тип вкладення недоступний для приватного перегляду.'};
  function dateMs(value){return typeof value==='string'&&value.length<=64&&/T.*(?:Z|[+-]\d{2}:\d{2})$/.test(value)&&Number.isFinite(Date.parse(value))?Date.parse(value):null;}
  function normalize(presentation,nowMs){
    const raw=presentation&&presentation.lifecycle||{};
    const state=Object.prototype.hasOwnProperty.call(labels,raw.state)?raw.state:'unverified';
    const due=dateMs(raw.deletion_due),now=nowMs===undefined?Date.now():Number.isFinite(nowMs)?nowMs:NaN;
    const expired=state==='active'&&due!==null&&due<=now;
    const effectiveState=expired?'expired':state;
    const eligible=raw.schema==='private-media-lifecycle.v1'&&effectiveState==='active'&&raw.readable===true&&raw.readability==='preview_eligible'&&raw.expiry_known===true&&due!==null&&due>now;
    return {raw,state:effectiveState,reason:expired?'retention_elapsed':raw.reason,due,eligible};
  }
  function previewPath(value,origin){
    if(typeof value!=='string'||value.length>512)return '';
    try{
      const base=origin||(global.location&&global.location.origin);
      if(!base)return '';
      const parsed=new URL(value,base);
      if(!['http:','https:'].includes(parsed.protocol)||parsed.origin!==new URL(base).origin||parsed.search||parsed.hash||!/^\/bot\/private-media\/\d+\/mp1_[a-f0-9]{32}\/preview\/$/.test(parsed.pathname))return '';
      return parsed.pathname;
    }catch(_error){return '';}
  }
  function applyEligibility(media,presentation,options){
    const safe={...(media||{})};
    // Raw provider/storage fields never become a rendering fallback.
    ['url','local_url','storage_name','private_storage','telegram_file_id','file_id'].forEach(key=>delete safe[key]);
    const value=normalize(presentation,options&&options.nowMs);
    const preview=value.eligible?previewPath(safe.preview_url,options&&options.origin):'';
    safe.preview_url=preview;
    safe.public_url=preview;
    return safe;
  }
  function dateLabel(value){
    const ms=dateMs(value);
    if(ms===null)return '';
    try{return new Intl.DateTimeFormat('uk-UA',{day:'2-digit',month:'2-digit',year:'numeric',hour:'2-digit',minute:'2-digit'}).format(new Date(ms));}catch(_error){return '';}
  }
  function policyLabel(policy){
    if(!policy||policy.state!=='verified'||policy.version!=='ig-private-media-retention-v1'||!Number.isInteger(policy.retention_seconds)||policy.retention_seconds<3600||policy.retention_seconds>5184000)return 'Політика зберігання: невідома';
    const seconds=policy.retention_seconds;
    for(const [unit,label] of [[86400,'дн.'],[3600,'год.'],[60,'хв.']])if(seconds%unit===0)return 'Зберігання: до '+seconds/unit+' '+label;
    return 'Зберігання: до '+seconds+' с';
  }
  function render(host,presentation,options){
    if(!host||!host.ownerDocument)return null;
    const previous=host.querySelector&&host.querySelector('.ig-media-lifecycle');
    if(previous)previous.remove();
    const value=normalize(presentation,options&&options.nowMs),doc=host.ownerDocument;
    const root=doc.createElement('div');root.className='ig-media-lifecycle ig-media-lifecycle--'+value.state;
    function text(parent,className,value){const node=doc.createElement('span');node.className=className;node.textContent=value;parent.appendChild(node);return node;}
    const badge=text(root,'ig-media-lifecycle__state',labels[value.state]);badge.setAttribute('role','status');
    const disclosure=doc.createElement('details');disclosure.className='ig-media-lifecycle__disclosure';
    const summary=doc.createElement('summary');summary.className='ig-media-lifecycle__summary';summary.textContent='Деталі';
    disclosure.appendChild(summary);
    const body=doc.createElement('div');body.className='ig-media-lifecycle__body';
    text(body,'ig-media-lifecycle__detail',Object.prototype.hasOwnProperty.call(details,value.reason)?details[value.reason]:'Підтверджених даних недостатньо; перегляд недоступний.');
    if(value.state!=='deleted')text(body,'ig-media-lifecycle__due',value.due!==null?'Видалення після '+dateLabel(value.raw.deletion_due):'Строк зберігання: невідомий');
    if(value.state==='delete_failed'){const retry=dateLabel(value.raw.retry_due);text(body,'ig-media-lifecycle__retry',retry?'Повторна спроба після '+retry:'Час повторної спроби невідомий');}
    text(body,'ig-media-lifecycle__policy',policyLabel(value.raw.policy));
    disclosure.appendChild(body);root.appendChild(disclosure);
    host.appendChild(root);return root;
  }
  const api={render,applyEligibility};
  global.TwcMediaLifecycle=api;
  if(typeof module!=='undefined'&&module.exports)module.exports=api;
})(typeof window!=='undefined'?window:globalThis);
