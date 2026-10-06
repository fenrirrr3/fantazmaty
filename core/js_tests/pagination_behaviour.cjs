const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assets = path.resolve(__dirname, '../static/core');
class Element {
    constructor(tag = '') {
        this.tag = tag; this.children = []; this.dataset = {}; this.attrs = {}; this.events = {}; this.value = '';
        this.classes = new Set();
        this.classList = {contains: name => this.classes.has(name), toggle: (name, on) => on ? this.classes.add(name) : this.classes.delete(name)};
    }
    append(...items) { this.children.push(...items); }
    addEventListener(name, fn) { this.events[name] = fn; }
    setAttribute(name, value) { this.attrs[name] = value; }
    closest() { return null; }
    after(node) { this.nav = node; }
}
function serverFixture(param = 'archive_page', target = '?q=smoki&roles=1&roles=2&sort=-title&page_size=50&archive_page=1#archive') {
    const input = new Element('input'); input.value = '2'; input.max = '8';
    input.dataset = {pageParam: param, pageUrl: target};
    const select = new Element('select'); select.value = '?page_size=500&archive_page=1#archive';
    const callbacks = [], assigned = [];
    vm.runInNewContext(fs.readFileSync(path.join(assets, 'pagination-controls.js'), 'utf8'), {
        document: {addEventListener: (_, fn) => callbacks.push(fn), querySelectorAll: s => s === '[data-server-page]' ? [input] : [select]},
        URL, location: {href: 'https://cms.invalid/list', assign: url => assigned.push(url)},
    }); callbacks.forEach(fn => fn());
    return {input, select, assigned};
}
let f = serverFixture(); let prevented = false;
f.input.value = '5'; f.input.events.keydown({key: 'Enter', preventDefault() { prevented = true; }});
assert.ok(prevented); let url = new URL(f.assigned[0]);
assert.equal(url.searchParams.get('archive_page'), '5'); assert.deepEqual(url.searchParams.getAll('roles'), ['1', '2']);
assert.equal(url.searchParams.get('sort'), '-title'); assert.equal(url.searchParams.get('page_size'), '50'); assert.equal(url.hash, '#archive');
f.input.events.change(); assert.equal(f.assigned.length, 1); // Enter and blur must not navigate twice.
for (const [value, expected] of [['999', '8'], ['0', '1']]) {
    f = serverFixture(); f.input.value = value; f.input.events.change();
    assert.equal(new URL(f.assigned[0]).searchParams.get('archive_page'), expected);
}
for (const value of ['', '-2', '2.5', 'abc', '1e2', '999999999999999999999']) {
    f = serverFixture(); f.input.value = value; f.input.events.change(); assert.equal(f.assigned.length, 0); assert.equal(f.input.value, '2');
}
f = serverFixture('p', '?q=smoki&p=1'); f.input.value = '3'; f.input.events.change(); assert.equal(new URL(f.assigned[0]).searchParams.get('p'), '3');
f = serverFixture(); f.select.events.change(); assert.equal(f.assigned[0], f.select.value);

const storage = new Map();
function localFixture(pathname = '/detail/1/', id = 'credits', storageBlocked = false) {
    const table = new Element('table'); table.id = id;
    const rows = Array.from({length: 115}, () => Object.assign(new Element('tr'), {cells: [{colSpan: 1}, {colSpan: 1}]}));
    table.tBodies = [{rows}];
    const callbacks = [];
    vm.runInNewContext(fs.readFileSync(path.join(assets, 'pagination.js'), 'utf8'), {
        document: {addEventListener: (_, fn) => callbacks.push(fn), querySelectorAll: () => [table], createElement: tag => new Element(tag)},
        location: {pathname}, localStorage: {
            getItem(key) { if (storageBlocked) throw Error('blocked'); return storage.get(key); },
            setItem(key, value) { if (storageBlocked) throw Error('blocked'); storage.set(key, value); },
        },
        MutationObserver: class { observe() {} },
    }); callbacks.forEach(fn => fn());
    const [status, label, controls] = table.nav.children;
    const [first, previous, jump, next, last] = controls.children;
    const input = jump.children[1], select = label.children[1];
    return {table, status, select, first, previous, input, next, last, visible: () => rows.filter(r => !r.classes.has('cms-page-hidden')).length};
}
f = localFixture(); assert.equal(f.visible(), 25); assert.equal(f.input.value, '1'); assert.equal(f.input.max, '5');
f.select.value = '50'; f.select.events.change(); assert.equal(f.visible(), 50); assert.equal(f.input.max, '3');
f.input.value = '2'; prevented = false; f.input.events.keydown({key: 'Enter', preventDefault() { prevented = true; }});
assert.ok(prevented); assert.equal(f.input.value, '2'); assert.equal(f.visible(), 50);
f.next.events.click(); assert.equal(f.input.value, '3'); assert.equal(f.visible(), 15); assert.ok(f.next.disabled);
f.input.value = '999'; f.input.events.change(); assert.equal(f.input.value, '3');
f.input.value = ''; f.input.events.change(); assert.equal(f.input.value, '3');
f.first.events.click(); assert.equal(f.input.value, '1'); assert.ok(f.previous.disabled);
assert.equal(localFixture('/other/').select.value, '25');
assert.equal(localFixture('/detail/1/', 'another-table').select.value, '25');
f = localFixture(); assert.equal(f.select.value, '50'); assert.equal(f.visible(), 50); assert.equal(f.input.value, '1');
f.select.value = '500'; f.select.events.change(); assert.equal(f.visible(), 115); assert.equal(f.input.max, '1'); assert.ok(f.next.disabled);
assert.equal(localFixture().select.value, '500');
f = localFixture('/private/', 'table', true); f.select.value = '100'; f.select.events.change(); assert.equal(f.visible(), 100);
console.log('PASS: server and local page jumps, input validation, no form submit, query/anchor preservation, remembered sizes, independent tables and unavailable storage.');
