const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const script = fs.readFileSync(path.resolve(__dirname, '../static/core/program-jobs.js'), 'utf8');
class Element {
    constructor(tag) { this.tag = tag; this.children = []; this.events = {}; this.attrs = {}; this.hidden = false; }
    append(...children) { this.children.push(...children); }
    setAttribute(k, v) { this.attrs[k] = v; }
    removeAttribute(k) { delete this.attrs[k]; }
    addEventListener(k, fn) { this.events[k] = fn; }
    before(box) { this.box = box; }
    click() { this.clicks = (this.clicks || 0) + 1; }
}
function fixture(saved) {
    const submit = new Element('button'); submit.name = 'program_action'; submit.value = 'clean';
    const form = new Element('form'); form.action = '/programy/';
    const details = {};
    form.querySelector = selector => selector.startsWith('button') ? submit : {value: 'csrf'};
    form.querySelectorAll = () => [submit]; form.closest = () => details;
    let storage = saved, tasks = [], responses = [], requests = [];
    const timers = new Map(); let id = 0;
    vm.runInNewContext(script, {
        document: {querySelectorAll: () => [form], createElement: tag => new Element(tag)},
        location: {pathname: '/programy/'},
        sessionStorage: {getItem: () => storage, setItem: (_,v) => { storage = v; }, removeItem: () => { storage = null; }},
        setTimeout: fn => { timers.set(++id,fn); return id; }, clearTimeout: id => timers.delete(id),
        URLSearchParams, FormData: class { set() {} },
        fetch: async (url, options) => {
            requests.push({url,options});
            return new Promise(resolve => {
                tasks.push(value => resolve({ok: true, headers: {get: () => 'application/json'}, json: async () => value}));
            });
        },
    });
    const [label,bar,stop,accept,download] = submit.box.children;
    async function respond(value) { assert.ok(tasks.length); tasks.shift()(value); await new Promise(resolve => setImmediate(resolve)); }
    async function tick() { const entry = timers.entries().next().value; assert.ok(entry); timers.delete(entry[0]); entry[1](); }
    return {form,submit,label,bar,stop,accept,download,requests,respond,tick,details,storage: () => storage};
}
(async () => {
    let f = fixture();
    f.form.events.submit({preventDefault(){},submitter:f.submit});
    assert.equal(f.submit.disabled,true); assert.equal(f.requests[0].options.headers['X-Program-Job'],'1');
    await f.respond({url:'/programy/zadania/token/'});
    await f.respond({state:'running',stage:'Skład PDF',completed:20,total:100});
    assert.equal(f.bar.value,20); assert.match(f.label.textContent,/20%/);
    f.stop.events.click(); assert.equal(f.requests.at(-1).options.body.get('action'),'cancel');
    await f.respond({state:'cancelling'}); await f.respond({state:'cancelled',message:'Zatrzymano'});
    assert.equal(f.submit.disabled,false); assert.equal(f.stop.hidden,true); assert.equal(f.storage(),null);

    f = fixture('/programy/zadania/retained/');
    assert.equal(f.details.open,true);
    await f.respond({state:'confirmation',message:'Nagłówki zostaną pominięte.'});
    assert.equal(f.accept.hidden,false); assert.equal(f.submit.disabled,true);
    f.accept.events.click(); assert.equal(f.requests.at(-1).options.body.get('action'),'confirm');
    await f.respond({state:'queued',url:'/programy/zadania/renewed/'}); await f.respond({state:'done',warnings:0});
    assert.equal(f.download.hidden,false); assert.equal(f.download.clicks,1);
    assert.equal(f.download.href,'/programy/zadania/renewed/?download=1');
    assert.equal(f.submit.disabled,false);

    f = fixture(); f.form.events.submit({preventDefault(){},submitter:f.submit});
    f.stop.events.click(); // Stop before the upload receives its job URL.
    await f.respond({url:'/programy/zadania/early/'});
    assert.equal(f.requests.at(-1).options.body.get('action'),'cancel');
    await f.respond({state:'cancelling'}); await f.respond({state:'cancelled',message:'Zatrzymano'});
    assert.equal(f.submit.disabled,false);
    console.log('Program jobs JS: progress, cancellation, early cancellation, reload, confirmation, download OK');
})().catch(error => { console.error(error); process.exitCode = 1; });
