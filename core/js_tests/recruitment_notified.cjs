const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
let change;
const pending = [];
const status = {textContent: ''}, cell = {dataset: {}};
const row = {querySelector: () => null};
const mailbox = {getAttribute: () => null};
const field = {value: 'yes', disabled: false, dataset: {value: 'no', version: 'v1', url: '/save/'},
    closest(selector) {
        return {'[data-recruitment-notified]': this, '.recruitment-notified': {querySelector: () => status},
            '[data-recruitment-samples]': mailbox, td: cell, tr: row}[selector];
    }};
class FormDataMock extends Map {
    constructor(form) { super(form ? [['selected', 'other'], ['cursor', 'old'], ['decision', 'accepted'], ['csrfmiddlewaretoken', 'csrf']] : []); }
    append(key, value) { assert.ok(!this.has(key)); this.set(key, value); }
}
vm.runInNewContext(fs.readFileSync(path.resolve(__dirname, '../static/core/recruitment-notified.js'), 'utf8'), {
    document: {addEventListener: (_, cb) => change = cb, querySelector: () => ({value: 'csrf'})},
    FormData: FormDataMock, location: {href: '/mailbox/'},
    fetch(url, options) { return new Promise(resolve => pending.push({url, options, resolve})); },
});
function respond(request, value, ok=true) {
    request.resolve({ok, redirected:false, headers:{get:()=> 'application/json'}, json:async()=>value});
}
(async () => {
    const saving = change({target: field});
    assert.equal(field.disabled, true);
    await change({target: field}); assert.equal(pending.length, 1);
    assert.equal(pending[0].url, '/save/');
    assert.equal(pending[0].options.body.get('csrfmiddlewaretoken'), 'csrf');
    assert.equal(pending[0].options.body.get('version'), 'v1');
    respond(pending[0], {notified:true, version:'v2', url:'/save/'}); await saving;
    assert.equal(field.dataset.value, 'yes'); assert.equal(cell.dataset.sortValue, 'Tak');
    assert.equal(field.disabled, false); assert.equal(field.dataset.version, 'v2');
    field.value = 'no';
    const failed = change({target: field});
    respond(pending[1], {error:'Odśwież stronę'}, false); await failed;
    assert.equal(field.value, 'yes'); assert.equal(field.dataset.version, 'v2');
    assert.equal(status.textContent, 'Odśwież stronę'); assert.equal(field.disabled, false);
    delete field.dataset.url; field.dataset.mailUid='42'; field.dataset.value='no'; field.value='yes';
    const importing = change({target: field});
    const request = pending[2]; assert.equal(request.url, '/mailbox/');
    assert.equal(request.options.body.get('selected'), '42');
    assert.equal(request.options.body.get('action'), 'set_notified');
    assert.equal(request.options.body.has('decision'), false); assert.equal(request.options.body.has('cursor'), false);
    respond(request, {notified:true, version:'v3', url:'/save/42/'}); await importing;
    assert.equal(field.dataset.url, '/save/42/');
    console.log('Recruitment notification autosave: OK');
})().catch(error => {console.error(error); process.exitCode=1;});
