const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
class Element {
  constructor(tag) { this.tagName = tag; this.children = []; this.attrs = {}; this.events = {}; this.value = ''; this.dataset = {}; }
  append(...items) { this.children.push(...items); }
  replaceChildren(...items) { this.children = items; }
  before(node) { this.beforeNode = node; }
  setAttribute(k, v) { this.attrs[k] = v; }
  removeAttribute(k) { delete this.attrs[k]; }
  addEventListener(k, fn) { (this.events[k] ||= []).push(fn); }
  dispatchEvent(event) { for (const fn of this.events[event.type] || []) fn(event); }
  focus() { this.dispatchEvent({type: 'focus'}); }
  click() { this.dispatchEvent({type: 'click'}); }
  setCustomValidity(message) { this.validityMessage = message; }
  contains(target) { return this === target || this.children.some(c => c.contains?.(target)); }
}
const select = new Element('select'); select.id = 'id_controllers'; select.labels = [{htmlFor: select.id}];
select.options = [
  {value: '1', text: 'Anna Kowalska', selected: true},
  {value: '2', text: 'Łukasz Żurawski', selected: false},
  {value: '3', text: 'Jan Nowak', selected: false},
  {value: '4', text: 'Jan Nowak', selected: false},
];
select.form = new Element('form');
const doc = new Element('document'); doc.readyState = 'complete';
doc.querySelectorAll = () => [select]; doc.createElement = tag => new Element(tag);
vm.runInNewContext(fs.readFileSync(path.join(__dirname, '../static/core/person-multiselect.js'), 'utf8'), {
  document: doc, Event: class { constructor(type) { this.type = type; } }, setTimeout: fn => fn(),
});
const [chips, input, results] = select.beforeNode.children;
assert.equal(select.hidden, true); assert.equal(chips.children.length, 1);
assert.equal(input.attrs['aria-expanded'], 'false');
input.value = 'lukasz zur'; input.dispatchEvent({type: 'input'});
assert.equal(results.children.length, 1); results.children[0].click();
assert.deepEqual(select.options.filter(o => o.selected).map(o => o.value), ['1', '2']);
assert.equal(input.value, ''); assert.equal(chips.children.length, 2);
input.value = 'Jan Nowak'; input.dispatchEvent({type: 'input'});
assert.deepEqual(results.children.map(c => c.textContent), ['Jan Nowak (#3)', 'Jan Nowak (#4)']);
input.dispatchEvent({type: 'keydown', key: 'ArrowDown', preventDefault() {}});
input.dispatchEvent({type: 'keydown', key: 'Enter', preventDefault() {}});
assert.equal(select.options[2].selected, true);
chips.children[0].children[1].click();
assert.deepEqual(select.options.filter(o => o.selected).map(o => o.value), ['2', '3']);
input.value = 'Nieznana osoba'; input.dispatchEvent({type: 'input'});
assert.equal(results.children.length, 0); assert.ok(input.validityMessage);
assert.deepEqual(select.options.filter(o => o.selected).map(o => o.value), ['2', '3']);
input.value = ''; input.dispatchEvent({type: 'input'}); assert.equal(input.validityMessage, '');
console.log('Multi-person search: selections, Polish names, duplicates, removal, keyboard and unknown input OK');
