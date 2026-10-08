const assert = require('node:assert/strict');
const fs = require('node:fs'), path = require('node:path'), vm = require('node:vm');
const events = {};
let opened = 0, closed = 0, prevented = 0;
const dialog = {showModal() { opened++; }, close() { closed++; }, removeAttribute(name) { assert.equal(name, 'open'); }};
const document = {addEventListener(name, cb) {events[name] = cb;}, getElementById: id => id === 'example' ? dialog : null,
    querySelectorAll: () => [dialog]};
vm.runInNewContext(fs.readFileSync(path.resolve(__dirname, '../static/core/dialogs.js'), 'utf8'), {document});
events.DOMContentLoaded(); assert.equal(opened, 1);
events.click({target:{closest: key => key === '[data-dialog-open]' ? {dataset:{dialogOpen:'example'}} : null}, preventDefault() {prevented++;}});
assert.equal(opened, 2); assert.equal(prevented, 1);
events.click({target:{closest: key => key === '[data-dialog-close]' ? {closest:()=>dialog} : null}, preventDefault() {prevented++;}});
assert.equal(closed, 1); assert.equal(prevented, 2);
// The ordinary fallback link remains usable when its dialog is absent.
events.click({target:{closest: key => key === '[data-dialog-open]' ? {dataset:{dialogOpen:'absent'}} : null}, preventDefault() {prevented++;}});
assert.equal(prevented, 2);
console.log('Dialog open, cancel and fallback: OK');
