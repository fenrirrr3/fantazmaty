const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
class Node {
    constructor() { this.children = []; this.events = {}; this.dataset = {}; this.value = ''; }
    setAttribute() {}
    addEventListener(name, fn) { this.events[name] = fn; }
    append(node) { this.children.push(node); }
    replaceChildren() { this.children = []; }
    after(node) { boxes.push(node); this.box = node; }
    closest() { return form; }
    dispatchEvent(event) { this.events[event.type]?.(); }
    set innerHTML(_) { throw new Error('Contact data must not use innerHTML'); }
}
const boxes = [], pending = [], timers = new Map(); let timerId = 0;
const fields = Object.fromEntries(['name','email','id'].map(key => {
    const node = new Node(); node.dataset = {contactKind: 'audio', contactGroup: 'narrator', contactField: key}; return [key, node];
}));
const form = {dataset: {contactEndpoint: '/kontakty/podpowiedzi/'},
    querySelector(selector) { return fields[selector.match(/data-contact-field="(\w+)"/)[1]]; },
    querySelectorAll() { return boxes; }};
vm.runInNewContext(fs.readFileSync(path.resolve(__dirname,'../static/core/contact-suggestions.js'),'utf8'), {
    document: {querySelectorAll: () => [fields.name, fields.email], createElement: () => new Node()},
    URL, location: {origin: 'https://cms.example'}, Event: class { constructor(type) { this.type = type; } },
    setTimeout: fn => { timers.set(++timerId, fn); return timerId; }, clearTimeout: id => timers.delete(id),
    fetch: url => new Promise(resolve => pending.push({url, resolve})),
});
const flush = () => new Promise(resolve => setImmediate(resolve));
const answer = (request, results) => request.resolve({ok: true, json: async () => ({results})});
const person = {id: 7, name: 'Jan Lektor', email: 'jan@example.test'};
(async () => {
    fields.name.value = 'Jan'; fields.name.events.input();
    [...timers.values()].at(-1)();
    answer(pending.shift(), [person]); await flush();
    fields.name.box.children[0].events.click();
    assert.equal(fields.email.value, person.email); assert.equal(fields.id.value, 7);
    fields.email.value = 'anna@example.test'; fields.email.events.blur();
    answer(pending.shift(), [{id: 8, name: 'Anna', email: fields.email.value}]); await flush();
    assert.equal(fields.name.value, 'Anna'); assert.equal(fields.id.value, 8);
    fields.email.value = 'shared@example.test'; fields.email.events.blur();
    answer(pending.shift(), [{id: 1, name: 'A', email: fields.email.value},{id: 2,name:'B',email:fields.email.value}]); await flush();
    assert.equal(fields.name.value, 'Anna'); assert.equal(fields.email.box.children.length, 2);
    fields.name.value = 'Old'; fields.name.events.blur(); const stale = pending.shift();
    fields.email.value = 'new@example.test'; fields.email.events.blur();
    answer(pending.shift(), [{id: 9, name: '<img onerror=alert(1)>', email: fields.email.value}]); await flush();
    answer(stale, [{id: 10, name: 'Old', email: 'old@example.test'}]); await flush();
    assert.equal(fields.name.value, '<img onerror=alert(1)>'); assert.equal(fields.email.value, 'new@example.test');
    console.log('Contact suggestions: selection, reverse lookup, ambiguity and stale responses passed.');
})().catch(error => { console.error(error); process.exitCode = 1; });
