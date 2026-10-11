// Dymki „?” na stronie Programy i „Zaznacz/Odznacz wszystkie” w regułach Odkurzacza.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const read = name => fs.readFileSync(path.resolve(__dirname, '../static/core', name), 'utf8');

class Node {
    constructor(props = {}) { Object.assign(this, props); this.events = {}; this.children = []; }
    addEventListener(name, fn) { this.events[name] = fn; }
    contains(node) { return node === this || this.children.some(child => child.contains(node)); }
}

// --- program-help.js ---
function tip(right) {
    const classes = new Set();
    const body = new Node({classList: {add: c => classes.add(c), remove: c => classes.delete(c)},
                           getBoundingClientRect: () => ({right}), classes});
    const summary = new Node({focus() { summary.focused = true; }});
    const element = new Node({open: false,
        querySelector: selector => (selector === 'summary' ? summary : body)});
    element.children.push(summary, body);
    return {element, summary, body};
}
const tips = [tip(300), tip(1300)];
const doc = new Node({querySelectorAll: () => tips.map(t => t.element)});
vm.runInNewContext(read('program-help.js'), {document: doc, window: {innerWidth: 1280}});
const [a, b] = tips;
a.element.open = true; a.element.events.toggle();
assert.equal(a.body.classes.has('is-end'), false);
b.element.open = true; b.element.events.toggle();
assert.equal(a.element.open, false, 'Otwarcie dymka zamyka inne');
assert.equal(b.body.classes.has('is-end'), true, 'Dymek przy prawej krawędzi dosuwa się w lewo');
doc.events.click({target: b.body});
assert.equal(b.element.open, true, 'Kliknięcie w dymek go nie zamyka');
doc.events.click({target: new Node()});
assert.equal(b.element.open, false, 'Kliknięcie poza dymkiem zamyka go');
a.element.open = true;
doc.events.keydown({key: 'Enter'});
assert.equal(a.element.open, true);
doc.events.keydown({key: 'Escape'});
assert.equal(a.element.open, false, 'Escape zamyka dymek');
assert.equal(a.summary.focused, true, 'Fokus wraca na „?”');

// --- odkurzacz.js: „Odznacz wszystkie” obejmuje też miękkie entery ---
const boxes = [{name: 'remove_soft_whitespace', checked: true}, {name: 'rules', checked: true},
               {name: 'rules', checked: false}];
const group = {querySelectorAll: selector => {
    assert.equal(selector, '.odkurzacz-rules input[type="checkbox"]');
    return boxes;
}};
const actions = new Node({hidden: true, closest: () => group});
vm.runInNewContext(read('odkurzacz.js'), {document: {querySelectorAll: selector => (selector === '.rule-actions' ? [actions] : [])}});
assert.equal(actions.hidden, false);
const button = value => ({target: {closest: () => ({dataset: {rules: value}})}});
actions.events.click(button('none'));
assert.deepEqual(boxes.map(box => box.checked), [false, false, false]);
actions.events.click(button('all'));
assert.deepEqual(boxes.map(box => box.checked), [true, true, true]);
console.log('Program help tips and rule toggles OK');
