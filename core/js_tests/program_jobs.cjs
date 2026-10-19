const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const script = fs.readFileSync(path.resolve(__dirname, '../static/core/program-jobs.js'), 'utf8');
class Element {
    constructor(tag) { this.tag = tag; this.children = []; this.events = {}; this.attrs = {}; this.hidden = false; }
    append(...children) { this.children.push(...children); }
    replaceChildren(...children) { this.children = [...children]; }
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
    const [label,images,bar,stop,accept,download,mail,notes,summary] = submit.box.children;
    async function respond(value) { assert.ok(tasks.length); tasks.shift()(value); await new Promise(resolve => setImmediate(resolve)); }
    async function tick() { const entry = timers.entries().next().value; assert.ok(entry); timers.delete(entry[0]); entry[1](); }
    return {form,submit,label,images,bar,stop,accept,download,mail,notes,summary,requests,respond,tick,details,storage: () => storage};
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
    f = fixture(); f.form.events.submit({preventDefault(){},submitter:f.submit});
    await f.respond({url:'/programy/zadania/summary/'});
    await f.respond({state:'done',warnings:[],summary:[{rule:'spaces',label:'Spacje',count:3}]});
    assert.equal(f.summary.hidden,false); assert.equal(f.notes.hidden,true);
    const table = f.summary.children[1];
    assert.deepEqual(table.children[1].children.map(cell => cell.textContent),['Spacje','3']);
    f = fixture(); f.form.events.submit({preventDefault(){},submitter:f.submit});
    await f.respond({url:'/programy/zadania/empty/'});
    await f.respond({state:'done',warnings:[],summary:[]});
    assert.equal(f.summary.children[1].textContent,'Reguły korekty nie wprowadziły zmian.');
    f = fixture(); f.form.events.submit({preventDefault(){},submitter:f.submit});
    await f.respond({url:'/programy/zadania/plain/'}); await f.respond({state:'done',warnings:[]});
    assert.equal(f.summary.hidden,true); assert.equal(f.images.hidden,true);
    // Usunięte obrazy: wyraźny komunikat z odmianą liczebnika; brak klucza lub 0 – nic.
    for (const [count, word] of [[1,'obraz'],[3,'obrazy'],[5,'obrazów'],[13,'obrazów'],[22,'obrazy'],[112,'obrazów']]) {
        f = fixture(); f.form.events.submit({preventDefault(){},submitter:f.submit});
        await f.respond({url:'/programy/zadania/images/'}); await f.respond({state:'done',warnings:[],removed_images:count});
        assert.equal(f.images.hidden,false);
        assert.equal(f.images.textContent,`Usunięto ${count} ${word} z dokumentu – wynik ich nie zawiera.`);
    }
    f = fixture(); f.form.events.submit({preventDefault(){},submitter:f.submit});
    await f.respond({url:'/programy/zadania/noimages/'}); await f.respond({state:'done',warnings:[],removed_images:0});
    assert.equal(f.images.hidden,true);
    // Lista zamian słownikowych pod tabelą reguł; uszkodzone wpisy są pomijane.
    f = fixture(); f.form.events.submit({preventDefault(){},submitter:f.submit});
    await f.respond({url:'/programy/zadania/replacements/'});
    await f.respond({state:'done',warnings:[],summary:[{rule:'user_word_corrections',label:'Słownik',count:3}],
        replacements:[{source:'pickupem',target:'pikapem',count:2},{source:'<b>',target:'x',count:1},null,{source:'a'}]});
    const list = f.summary.children.at(-1);
    assert.equal(list.className,'program-replacements');
    assert.deepEqual(list.children.map(item => item.textContent),['pickupem → pikapem (2)','<b> → x (1)']);
    f = fixture(); f.form.events.submit({preventDefault(){},submitter:f.submit});
    await f.respond({url:'/programy/zadania/badreplacements/'});
    await f.respond({state:'done',warnings:[],summary:[],replacements:'zepsute'});
    assert.equal(f.summary.children.length,2);
    // Podział DOCX: przycisk „Wyślij autorom” tylko z adresem serwera; archiwum zostaje po pobraniu.
    for (const [mailUrl, shown] of [['/programy/zadania/split/wyslij-autorom/',true],[undefined,false],['https://evil.example/',false],['//evil.example/',false]]) {
        f = fixture(); f.form.events.submit({preventDefault(){},submitter:f.submit});
        await f.respond({url:'/programy/zadania/split/'}); await f.respond({state:'done',warnings:[],mail_url:mailUrl});
        assert.equal(f.mail.hidden,!shown);
        if (shown) assert.equal(f.mail.href,mailUrl);
        f.download.events.click(); await f.tick();
        assert.equal(f.download.hidden,!shown);
        assert.match(f.label.textContent,shown ? /możesz wysłać pliki autorom/ : /Kopia na serwerze została usunięta/);
    }
    console.log('Program jobs JS: progress, cancellation, early cancellation, reload, confirmation, download, change summary, removed images, replacements, split mailing OK');
})().catch(error => { console.error(error); process.exitCode = 1; });
