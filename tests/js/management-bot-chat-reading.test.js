const test = require('node:test');
const assert = require('node:assert/strict');
const { capture, restore, merge } = require('../../twocomms/management/static/management/bot_chat_reading.js');

function conversation(ids) {
  const messages = {
    isConnected: true, scrollTop: 150, clientHeight: 200, scrollHeight: 800,
    top: 100, rows: [],
    getBoundingClientRect() { return { top: this.top }; },
    querySelectorAll() { return this.rows; },
    contains(row) { return this.rows.includes(row); },
    get lastElementChild() { return this.rows.at(-1); },
  };
  messages.rows = ids.map((id, index) => ({
    dataset: { mid: String(id) },
    getBoundingClientRect() {
      const top = messages.top + index * 100 - messages.scrollTop;
      return { top, bottom: top + 100 };
    },
  }));
  return messages;
}

test('retains an older reading anchor when refreshed controls change chat position', () => {
  const messages = conversation([1, 2, 3, 4, 5, 6, 7, 8]);
  const state = capture(messages);
  assert.equal(state.atBottom, false);
  assert.equal(state.anchor.dataset.mid, '2');
  // Reattaching a scroll container can reset its scrollTop; controls can move it.
  messages.scrollTop = 0;
  messages.top = 180;
  restore(messages, state);
  assert.equal(messages.scrollTop, 150);
  assert.equal(state.anchor.getBoundingClientRect().top - messages.top, state.offset);
});

test('refresh/poll overlap keeps older nodes and inserts each new message once', () => {
  const messages = conversation([1, 2, 3]);
  const original = messages.rows[0];
  const append = (root, item) => root.rows.push({ dataset: { mid: String(item.id) } });
  merge(messages, [{ id: 2 }, { id: 3 }, { id: 4 }, { id: 4 }], append);
  merge(messages, [{ id: 4 }, { id: 5 }], append);
  assert.equal(messages.rows[0], original);
  assert.deepEqual(messages.rows.map(row => Number(row.dataset.mid)), [1, 2, 3, 4, 5]);
});

test('captures following the tail separately from reading old messages', () => {
  const messages = conversation([1, 2, 3]);
  messages.scrollTop = 580;
  assert.equal(capture(messages).atBottom, true);
  messages.scrollTop = 300;
  assert.equal(capture(messages).atBottom, false);
  assert.equal(capture(null), null);
});

test('refreshes changed media presentation without replacing the reading anchor', () => {
  const messages = conversation([1]);
  const row = messages.rows[0];
  const originalPayload = { id: 1, text: '(зображення)', media: [{ capture_state: 'fetching' }] };
  row.dataset.messageSignature = JSON.stringify(originalPayload);
  let updates = 0;
  const refresh = (existing, item) => {
    updates += 1;
    assert.equal(existing, row);
    existing.dataset.messageSignature = JSON.stringify(item);
  };
  const changed = { ...originalPayload, media: [{ capture_state: 'owned', preview_url: '/bot/private-media/1/preview/' }] };
  merge(messages, [originalPayload], () => assert.fail('must not append'), refresh);
  assert.equal(updates, 0);
  merge(messages, [changed], () => assert.fail('must not append'), refresh);
  merge(messages, [changed], () => assert.fail('must not append'), refresh);
  assert.equal(updates, 1);
  assert.equal(messages.rows[0], row);
});

test('does not change disconnected conversations after a client switch', () => {
  const messages = conversation([1, 2, 3]);
  const state = capture(messages);
  messages.scrollTop = 0;
  messages.isConnected = false;
  restore(messages, state);
  assert.equal(messages.scrollTop, 0);
});

function contextSection(label, { dirty = false, focused = false } = {}) {
  return {
    id: '', dataset: dirty ? { operatorDirty: 'true' } : {}, parentNode: null,
    getAttribute() { return ''; }, querySelector() { return { textContent: label }; },
    contains(active) { return focused && Boolean(active); },
    replaceWith(section) {
      const root = this.parentNode; root.children[root.children.indexOf(this)] = section;
      this.parentNode = null; section.parentNode = root;
    },
  };
}
function contextRoot(sections) {
  const root = {
    children: sections, scrollTop: 160,
    contains(active) { return this.children.some(section => section.contains(active)); },
    appendChild(section) { section.parentNode = this; this.children.push(section); },
  };
  sections.forEach(section => { section.parentNode = root; }); return root;
}

test('background card refresh preserves actual dirty editor nodes and focused field', () => {
  const { captureContext, restoreContext } = require('../../twocomms/management/static/management/bot_chat_reading.js');
  let focusCalls = 0;
  const active = { value: 'unsaved manager note', focus(options) { assert.equal(options.preventScroll, true); focusCalls++; } };
  const draft = contextSection('UGC-нагороди', { dirty: true, focused: true });
  const root = contextRoot([contextSection('Стан клієнта'), draft]);
  const state = captureContext(root, active);
  const freshStatus = contextSection('Стан клієнта'), replacementDraft = contextSection('UGC-нагороди');
  root.children.forEach(section => { section.parentNode = null; });
  root.children = [freshStatus, replacementDraft]; root.children.forEach(section => { section.parentNode = root; });
  root.scrollTop = 0; restoreContext(root, state);
  assert.equal(root.children[0], freshStatus); assert.equal(root.children[1], draft);
  assert.equal(active.value, 'unsaved manager note'); assert.equal(focusCalls, 1); assert.equal(root.scrollTop, 160);
});

test('mobile document reader stays at the visible anchor through a revision refresh', () => {
  const view={scrollY:1300,innerHeight:600,document:{documentElement:{scrollHeight:3000}},
    scrollTo({top}){this.scrollY=top;},scrollBy({top}){this.scrollY+=top;}};
  let shift=0;
  const rows=[1000,1400,1800].map((y,index)=>({dataset:{mid:String(index+1)},getBoundingClientRect(){const top=y+shift-view.scrollY;return {top,bottom:top+120};}}));
  const messages={isConnected:true,scrollTop:0,clientHeight:1600,scrollHeight:1600,
    getBoundingClientRect(){return {top:1000-view.scrollY};},querySelectorAll(){return rows;},contains(row){return rows.includes(row);}};
  const reading=capture(messages,{documentScrolled:true,scrollWindow:view});
  assert.equal(reading.atBottom,false);assert.equal(reading.anchor,rows[1]);assert.equal(reading.offset,100);
  shift=140;restore(messages,reading);
  assert.equal(view.scrollY,1440);assert.equal(rows[1].getBoundingClientRect().top,100);
  view.scrollY=2500;assert.equal(capture(messages,{documentScrolled:true,scrollWindow:view}).atBottom,true);
});

function timeline() {
  const root={rows:[],isConnected:true,scrollTop:50,clientHeight:100,scrollHeight:600,
    getBoundingClientRect(){return {top:0};},querySelectorAll(){return this.rows;},contains(row){return this.rows.includes(row);},
    get lastElementChild(){return this.rows.at(-1);},
    insertBefore(row,before){this.rows.splice(this.rows.indexOf(row),1);const index=before?this.rows.indexOf(before):this.rows.length;this.rows.splice(index,0,row);},
  };
  const append=(messages,item)=>messages.rows.push({dataset:{mid:String(item.id),messageTime:item.time,messageSignature:JSON.stringify(item)},
    getBoundingClientRect(){const top=root.rows.indexOf(this)*100-root.scrollTop;return {top,bottom:top+100};}});
  return {root,append};
}

test('delayed old import with a new ID is placed between loaded events without replacing them',()=>{
  const {root,append}=timeline();
  const at=hour=>'2026-10-22T'+hour+':00:00+00:00';
  merge(root,[{id:1,time:at('10')},{id:2,time:at('12')}],append);
  const live=root.rows[1],anchor=capture(root);
  merge(root,[{id:9,time:at('11')},{id:9,time:at('11')}],append);
  restore(root,anchor);
  assert.deepEqual(root.rows.map(row=>Number(row.dataset.mid)),[1,9,2]);assert.equal(root.rows[2],live);
  assert.equal(Math.max(...root.rows.map(row=>Number(row.dataset.mid))),9);
  // The same ID updates in place, including a corrected provider event time.
  merge(root,[{id:9,time:at('13'),text:'updated'}],append,(row,item)=>{row.dataset.messageSignature=JSON.stringify(item);});
  assert.deepEqual(root.rows.map(row=>Number(row.dataset.mid)),[1,2,9]);assert.equal(root.rows.length,3);
});

test('normal older ID pages merge chronologically while preserving existing history nodes',()=>{
  const {root,append}=timeline();const at=day=>'2026-10-'+day+'T12:00:00+00:00';
  merge(root,[{id:3,time:at('21')},{id:4,time:at('22')}],append);const existing=root.rows[0];
  merge(root,[{id:1,time:at('19')},{id:2,time:at('20')},{id:3,time:at('21')}],append);
  assert.deepEqual(root.rows.map(row=>Number(row.dataset.mid)),[1,2,3,4]);assert.equal(root.rows[2],existing);
});
