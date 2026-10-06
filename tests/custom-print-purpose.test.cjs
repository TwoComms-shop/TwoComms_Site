const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const timers = new Map();
let nextTimer = 0, reduced = false, motionChange, pagehide;
const context = { document: { body: { appendChild(el) { el.portaled = true; } } },
 setTimeout(fn, ms) { const id = ++nextTimer; timers.set(id, {fn, ms}); return id; },
 clearTimeout(id) { timers.delete(id); },
 matchMedia() { return { matches: reduced, addEventListener(_, fn) { motionChange = fn; } }; },
 addEventListener(_, fn) { pagehide = fn; },
};
vm.createContext(context);
vm.runInContext(fs.readFileSync(path.join(__dirname, '../twocomms/twocomms_django_theme/static/js/custom-print-purpose.js'), 'utf8'), context);
(async () => {
 const tools=context.CustomPrintPurpose;
 assert.equal(tools.fromChoice('gift'),'gift');
 assert.equal(tools.fromChoice('brand'),'organization');
 assert.equal(tools.normalize('gift','personal'),'gift');
 assert.equal(tools.normalize('gift','brand'),'organization');
 const classes=new Set();
 const el={hidden:true,offsetWidth:200,classList:{add:x=>classes.add(x),remove:x=>classes.delete(x)}};
 const reveal=tools.createReveal(el);
 assert.ok(el.portaled,'reveal must escape clipped studio');
 const first=reveal.play();
 assert.equal(el.hidden,false);
 assert.equal([...timers.values()][0].ms,1080);
 reveal.cancel();
 assert.equal(await first,false,'exit cancels transition');
 assert.equal(el.hidden,true);
 assert.equal(timers.size,0);
 const repeat=reveal.play();
 const newest=reveal.play();
 assert.equal(await repeat,false,'only latest reveal may advance');
 [...timers.values()][0].fn();
 assert.equal(await newest,true);
 assert.equal(el.hidden,true);
 reduced=true;
 assert.equal(await reveal.play(),true);
 assert.equal(timers.size,0,'reduced motion never waits');
 assert.equal(el.hidden,true);
 reduced=false;
 const preference=reveal.play();
 motionChange({matches:true});
 assert.equal(await preference,true,'new reduced-motion preference advances immediately');
 const leaving=reveal.play(); pagehide();
 assert.equal(await leaving,false);
 assert.equal(await tools.createReveal(null).play(),true,'missing decoration must not block gift flow');
 console.log('custom print purpose lifecycle: ok');
})().catch(error=>{console.error(error);process.exit(1)});
