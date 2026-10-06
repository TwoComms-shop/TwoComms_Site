const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const timers = new Map();
let nextTimer = 0, reduced = false, motionChange, pagehide;
const context = {
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
 const el={hidden:false,offsetWidth:200,classList:{add:x=>classes.add(x),remove:x=>classes.delete(x)}};
 const reveal=tools.createReveal(el);
 assert.equal(reveal.duration,1250,'the complete gift response lasts 1.25 seconds');
 assert.equal(el.portaled,undefined,'gift must stay on its button');
 const first=reveal.play();
 assert.ok(classes.has('is-gift-opening'));
 assert.equal([...timers.values()][0].ms,1250);
 reveal.cancel();
 assert.equal(await first,false,'exit cancels transition');
 assert.equal(el.hidden,false,'the purpose card must remain visible');
 assert.equal(classes.has('is-gift-opening'),false);
 assert.equal(timers.size,0,'cancelled animation removes its timer');
 const repeat=reveal.play();
 const newest=reveal.play();
 assert.equal(await repeat,false,'only latest reveal may advance');
 [...timers.values()][0].fn();
 assert.equal(await newest,true);
 assert.equal(el.hidden,false,'the purpose card must remain visible');
 assert.equal(classes.has('is-gift-opening'),false);
 assert.equal(timers.size,0,'completed animation removes its timer');
 reduced=true;
 assert.equal(await reveal.play(),true);
 assert.equal(timers.size,0,'reduced motion never waits');
 assert.equal(el.hidden,false,'the purpose card must remain visible');
 assert.equal(classes.has('is-gift-opening'),false);
 reduced=false;
 const preference=reveal.play();
 motionChange({matches:true});
 assert.equal(await preference,true,'new reduced-motion preference advances immediately');
 assert.equal(timers.size,0,'preference change clears the timer');
 const leaving=reveal.play(); pagehide();
 assert.equal(await leaving,false);
 assert.equal(timers.size,0,'page exit clears the timer');
 assert.equal(classes.has('is-gift-opening'),false,'page exit cleans the card');
 assert.equal(await tools.createReveal(null).play(),true,'missing decoration must not block gift flow');
 console.log('custom print purpose lifecycle: ok');
})().catch(error=>{console.error(error);process.exit(1)});
