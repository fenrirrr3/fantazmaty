const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const pending = [], timers = new Map(); let timerId = 0;
function node() { return {textContent: '', hidden: false, attrs: {}, events: {},
    setAttribute(k,v) { this.attrs[k] = v; }, addEventListener(k,v) { this.events[k] = v; }}; }
const state = node(), subject = node(), sender = node(), body = node(), truncated = node(), panel = node();
Object.defineProperty(body, 'innerHTML', {set() { throw Error('Untrusted mail must never use innerHTML'); }});
const parts = {'[data-preview-state]': state, '[data-preview-subject]': subject, '[data-preview-sender]': sender,
    '[data-preview-body]': body, '[data-preview-truncated]': truncated};
panel.querySelector = s => parts[s];
const roles = node(); roles.options = [{value:'all',selected:true}, {value:'editors',selected:false}, {value:'reviewers',selected:false}];
Object.defineProperty(roles,'selectedOptions',{get() { return this.options.filter(o=>o.selected); }});
const form = node();
// A named action control shadows HTMLFormElement.action in a real browser.
form.action = {toString: () => '[object RadioNodeList]'};
form.getAttribute = () => null;
const checked = {value: '12', checked: true, matches: selector => selector !== '[data-select-table]'};
form.querySelector = s => s === '[data-mail-preview]' ? panel : s === 'select[name="roles"]' ? roles : checked;
const callbacks = [];
vm.runInNewContext(fs.readFileSync(path.resolve(__dirname,'../static/core/recruitment-samples.js'),'utf8'), {
    document: {addEventListener: (_, fn) => callbacks.push(fn), querySelector: () => form},
    DOMParser: class { parseFromString(value) {
        return {querySelector() { return value && {querySelector(selector) {
            const key=selector.match(/data-preview-(.*)\]/)[1];
            return key==='truncated' ? {hidden:!value.truncated} : {textContent:value[key] || ''};
        }}; }};
    } },
    FormData: class extends Map { constructor() { super(); } }, AbortController,
    location: {href:'https://cms.invalid/rekrutacja/skrzynka/'},
    setTimeout(fn) { const id=++timerId; timers.set(id,fn); return id; }, clearTimeout(id) { timers.delete(id); },
    fetch(url, options) { return new Promise(resolve => pending.push({url, options, resolve})); },
});
callbacks.forEach(fn=>fn());
const tick = () => new Promise(resolve => setImmediate(resolve));
const reply = (request, value) => request.resolve({ok:true, redirected:false, text:async()=>value});
function click(uid) {
    let prevented=false;
    form.events.click({target:{closest:()=>({dataset:{previewUid:uid}})}, preventDefault(){prevented=true;}});
    assert.ok(prevented);
}
(async()=>{
    form.events.change({target:{matches:()=>true}});
    assert.equal(timers.size,0); assert.equal(pending.length,0);
    for(let i=0;i<50;i++) form.events.change({target:checked});
    assert.equal(timers.size,1); // A Shift range triggers one preview; the header checkbox triggers none.
    [...timers.values()][0](); timers.clear(); assert.equal(pending.length,1);
    assert.equal(pending[0].url,'https://cms.invalid/rekrutacja/skrzynka/');
    assert.equal(pending[0].options.body.get('action'),'preview');
    assert.equal(pending[0].options.body.get('uid'),'12');
    click('13'); assert.equal(pending[0].options.signal.aborted,true);
    reply(pending[1],{subject:'Drugi',sender:'second@example.test',body:'<img onerror="alert(1)">',truncated:false});
    await tick(); assert.equal(subject.textContent,'Drugi'); assert.equal(body.textContent,'<img onerror="alert(1)">');
    reply(pending[0],{subject:'Stary',sender:'first@example.test',body:'Stara treść'});
    await tick(); assert.equal(subject.textContent,'Drugi'); // Late response cannot replace the latest selection.
    roles.options[1].selected=true; roles.events.change();
    assert.equal(roles.options[0].selected,false); assert.equal(roles.options[1].selected,true);
    assert.equal(body.textContent,'');
    roles.options[0].selected=true; roles.events.change();
    assert.equal(roles.options[1].selected,false); assert.equal(roles.options[0].selected,true);
    click('14'); pending[2].resolve({ok:false, redirected:false, text:async()=>({state:'Wybór wygasł'})});
    await tick(); assert.equal(state.textContent,'Wybór wygasł'); assert.equal(panel.attrs['aria-busy'],'false');
    click('15'); pending[3].resolve({ok:false, redirected:false, text:async()=>null});
    await tick(); assert.match(state.textContent,/Odśwież stronę/);
    console.log('Recruitment preview: selection, batching, late responses, safe text, All and errors OK');
})().catch(error=>{console.error(error);process.exitCode=1;});
