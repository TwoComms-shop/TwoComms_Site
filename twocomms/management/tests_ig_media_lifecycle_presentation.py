"""Pure labels and actual static renderer privacy/expiry boundaries."""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import shutil
import subprocess
import unittest

from management.services.ig_media_lifecycle_presentation import project_media_lifecycle_presentation


NOW = datetime(2026, 10, 6, 12, tzinfo=timezone.utc)


def presentation(**changes):
    part = {"source_part_id": "mp1_" + "a" * 32, "content_hash": "b" * 64,
        "status": "owned", "capture_state": "owned", "private_storage": True,
        "storage_name": "never-export/private.ogg", "mime": "audio/ogg",
        "url": "https://signed.example/private?secret=never-export",
        "delete_after": (NOW + timedelta(days=1)).isoformat()}
    values = {"message_state": "active", "owner_verified": True, "now": NOW}
    values.update(changes)
    return project_media_lifecycle_presentation(part, **values)


class MediaLifecyclePresentationPureTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node is needed for the actual transcript renderer")
    def test_actual_transcript_renderer_never_converts_denied_preview_into_homepage_link(self):
        root = Path(__file__).parent
        script = r"""
const assert=require('node:assert/strict'),fs=require('node:fs');
const template=fs.readFileSync(process.argv[1],'utf8');
const safe=template.match(/  function safeHttpUrl\(value\)\{[\s\S]*?\n  \}/)[0];
const append=template.match(/    function appendMessage\(root,m\)\{[\s\S]*?\n    \}/)[0];
function walk(element){return [element,...element.children.flatMap(walk)];}
class Element {
 constructor(tag){this.tagName=tag.toUpperCase();this.children=[];this.dataset={};this.style={};this._text='';this.open=false;this.ownerDocument=document;}
 appendChild(child){child.parent=this;this.children.push(child);return child;}
 append(...children){children.forEach(child=>this.appendChild(child));}
 setAttribute(key,value){this[key]=value;}
 querySelector(selector){return walk(this).slice(1).find(row=>String(row.className||'').split(' ').includes(selector.slice(1)))||null;}
 remove(){this.parent.children=this.parent.children.filter(row=>row!==this);}
 get textContent(){return this._text+this.children.map(row=>row.textContent).join(' ');}
 set textContent(value){this._text=String(value);this.children=[];}
}
const document={createElement:tag=>new Element(tag),createTextNode:text=>{const row=new Element('text');row.textContent=text;return row;}};
const node=(tag,cls,text)=>{const row=document.createElement(tag);row.className=cls||'';if(text!==undefined)row.textContent=text;return row;};
global.document=document;global.location={origin:'https://management.twocomms.shop'};
const api=require(process.argv[2]);const browserWindow={location:global.location,TwcMediaLifecycle:api};
const render=Function('window','document','node','fmtDateTime',safe+'\n'+append+'\nreturn appendMessage;')(browserWindow,document,node,value=>String(value));
const due=new Date(Date.now()+3600000).toISOString();
const base={schema:'private-media-lifecycle.v1',state:'active',reason:'owned_capture',readable:true,readability:'preview_eligible',expiry_known:true,deletion_due:due,retry_due:null,policy:{state:'unknown',version:null,retention_seconds:null}};
const preview='/bot/private-media/7/mp1_'+('a'.repeat(32))+'/preview/';
for(const change of [{state:'expired',readable:false},{deletion_due:'2020-01-01T00:00:00Z'},{state:'delete_failed'},{expiry_known:false,deletion_due:null}]){
 const host=node('main');render(host,{role:'user',id:7,media:[{media_kind:'image',public_url:'https://signed.example/raw?secret=never-export',preview_url:preview,media_lifecycle:{lifecycle:{...base,...change}}}]});
 const rows=walk(host);assert.equal(rows.filter(row=>row.tagName==='IMG'||row.tagName==='A').length,0);
 assert(!host.textContent.includes('signed.example'));assert(rows.some(row=>String(row.className||'').includes('ig-media-lifecycle')));
}
const active=node('main');render(active,{role:'user',id:7,media:[{media_kind:'image',preview_url:preview,public_url:'https://signed.example/raw',media_lifecycle:{lifecycle:base}}]});
assert.deepEqual(walk(active).filter(row=>row.tagName==='A').map(row=>row.href),[global.location.origin+preview]);
const catalog=node('main');render(catalog,{role:'model',id:8,media:[{media_kind:'image',public_url:'https://twocomms.shop/media/catalog/item.jpg'}]});
assert.equal(walk(catalog).find(row=>row.tagName==='IMG').src,'https://twocomms.shop/media/catalog/item.jpg');
const clean=node('main');render(clean,{role:'user',id:9,media:[{media_kind:'image',public_url:'   ',preview_url:''}]});
assert.equal(walk(clean).filter(row=>row.tagName==='IMG'||row.tagName==='A').length,0);
"""
        outcome = subprocess.run([shutil.which("node"), "-e", script,
            str(root / "templates" / "management" / "bot.html"),
            str(root / "static" / "management" / "ig_media_lifecycle.js")], capture_output=True, text=True, timeout=10)
        self.assertEqual(outcome.returncode, 0, outcome.stderr)

    def test_active_label_is_preview_eligibility_with_no_file_proof(self):
        dto = presentation()
        self.assertTrue(dto["lifecycle"]["readable"])
        self.assertEqual(dto["display"]["label"], "Приватний файл")
        self.assertIn("перевірено під час", dto["display"]["preview_hint"])
        self.assertNotIn("існує", json.dumps(dto, ensure_ascii=False))

    def test_expired_and_deleted_labels_distinguish_due_from_actual_completion(self):
        expired = presentation(now=NOW + timedelta(days=2))
        self.assertEqual(expired["lifecycle"]["state"], "expired")
        self.assertIn("ще не підтверджене", expired["display"]["detail"])
        self.assertNotEqual(expired["display"]["label"], "Видалено")

    def test_failed_deletion_has_retry_time_and_unknown_policy_stays_unknown(self):
        dto = presentation(message_state="delete_failed", message_delete_after=NOW + timedelta(seconds=60))
        self.assertEqual(dto["display"]["label"], "Видалення не завершене")
        self.assertIn("Повторна спроба після", dto["display"]["retry_label"])
        self.assertIn("невідома", dto["display"]["policy_label"])
        self.assertFalse(dto["lifecycle"]["readable"])

    def test_presentation_has_no_source_paths_urls_hashes_or_privacy_unsafe_reads(self):
        dto = presentation(owner_verified=False)
        serialized = json.dumps(dto)
        for private in ("never-export", "signed.example", "a" * 32, "b" * 64):
            self.assertNotIn(private, serialized)
        self.assertFalse(dto["lifecycle"]["readable"])

    @unittest.skipUnless(shutil.which("node"), "Node is needed for the actual static asset regression")
    def test_actual_asset_denies_expiry_unknown_raw_url_fallback_and_renders_retry_safely(self):
        asset = Path(__file__).parent / "static" / "management" / "ig_media_lifecycle.js"
        script = r"""
const assert=require('assert');
const api=require(process.argv[1]);
const now=Date.parse('2026-10-06T12:00:00Z');
const base={schema:'private-media-lifecycle.v1',state:'active',reason:'owned_capture',readable:true,readability:'preview_eligible',expiry_known:true,deletion_due:'2026-10-07T12:00:00Z',retry_due:null,policy:{state:'unknown',version:null,retention_seconds:null}};
const origin='https://management.twocomms.shop';
const preview='/bot/private-media/7/mp1_'+('a'.repeat(32))+'/preview/';
const input={preview_url:preview,public_url:'https://signed.example/fallback',url:'https://signed.example/raw?secret=x',storage_name:'private/name',local_url:'/media/private/name'};
assert.equal(api.applyEligibility(input,{lifecycle:base},{origin,nowMs:now}).public_url,preview);
assert.equal(input.url,'https://signed.example/raw?secret=x');
for(const change of [{deletion_due:null,expiry_known:false},{deletion_due:'2026-10-05T12:00:00Z'},{state:'delete_failed'},{schema:'unknown'},{readable:'true'}]){
  const safe=api.applyEligibility(input,{lifecycle:{...base,...change}},{origin,nowMs:now});
  assert.equal(safe.public_url,'');assert.equal(safe.preview_url,'');assert(!('url' in safe));assert(!('storage_name' in safe));assert(!('local_url' in safe));
}
for(const invalidClock of [NaN,Infinity,'today',null])assert.equal(api.applyEligibility(input,{lifecycle:base},{origin,nowMs:invalidClock}).public_url,'');
const originalNow=Date.now;Date.now=()=>now+2*86400000;
assert.equal(api.applyEligibility(input,{lifecycle:base},{origin}).public_url,'');Date.now=originalNow;
for(const bad of ['https://signed.example/private','/media/raw.jpg',preview+'?signed=secret','https://foreign.example'+preview])assert.equal(api.applyEligibility({...input,preview_url:bad},{lifecycle:base},{origin,nowMs:now}).public_url,'');
function node(tag='span'){return {tagName:tag.toUpperCase(),open:false,_text:'',children:[],attributes:{},ownerDocument:doc,setAttribute(k,v){this.attributes[k]=v;},appendChild(child){child.parent=this;this.children.push(child);},remove(){this.parent.children=this.parent.children.filter(c=>c!==this);},querySelector(){return this.children.find(c=>String(c.className).split(' ').includes('ig-media-lifecycle'))||null;},get textContent(){return this._text+this.children.map(child=>child.textContent).join(' ');},set textContent(value){this._text=String(value);this.children=[];},set innerHTML(value){throw Error('raw HTML must never be used');}};}
const doc={createElement(tag){return node(tag);}};
function visibleText(item){if(item.tagName==='DETAILS'&&!item.open)return item.children.filter(child=>child.tagName==='SUMMARY').map(visibleText).join(' ');return item._text+' '+item.children.map(visibleText).join(' ');}
const host=node();
api.render(host,{lifecycle:{...base,state:'delete_failed',reason:'deletion_retry',retry_due:'2026-10-06T12:01:00Z'}},{nowMs:now});
let texts=host.children[0].children.map(c=>c.textContent).join(' ');
assert(texts.includes('Видалення не завершене'));assert(texts.includes('Повторна спроба після'));assert(!texts.includes('Видалено'));
let disclosure=host.children[0].children.find(child=>child.tagName==='DETAILS');
assert(disclosure);assert.equal(disclosure.open,false);assert.equal(disclosure.children[0].tagName,'SUMMARY');assert.equal(disclosure.children[0].textContent,'Деталі');
assert(visibleText(host).includes('Видалення не завершене'));assert(!visibleText(host).includes('Повторна спроба після'));assert(!visibleText(host).includes('Політика зберігання'));
disclosure.open=true;assert(visibleText(host).includes('Повторна спроба після'));assert(visibleText(host).includes('Політика зберігання'));
api.render(host,{lifecycle:base},{nowMs:now+2*86400000});
assert.equal(host.children.length,1);texts=host.children[0].children.map(c=>c.textContent).join(' ');
assert(texts.includes('Строк зберігання минув'));assert(texts.includes('ще не підтверджене'));assert(!texts.includes('Видалено'));
api.render(host,{lifecycle:{...base,state:'unverified',reason:'__proto__',policy:{state:'verified',version:'malicious',retention_seconds:7776000}},display:{label:'<script>attack</script>'}},{nowMs:now});
texts=host.children[0].children.map(c=>c.textContent).join(' ');assert(!texts.includes('<script>'));assert(texts.includes('Політика зберігання: невідома'));assert(!texts.includes('[object Object]'));
"""
        outcome = subprocess.run([shutil.which("node"), "-e", script, str(asset)], capture_output=True, text=True, timeout=10)
        self.assertEqual(outcome.returncode, 0, outcome.stderr)
