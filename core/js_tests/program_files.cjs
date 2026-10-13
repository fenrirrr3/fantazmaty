// Scalanie DOCX: lista wybranych plików w kolejności scalania (program-files.js).
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const script = fs.readFileSync(path.resolve(__dirname, '../static/core', 'program-files.js'), 'utf8');

class Element {
    constructor(tag) { this.tag = tag; this.children = []; this.events = {}; this.dataset = {}; this.hidden = false; }
    addEventListener(name, fn) { this.events[name] = fn; }
    replaceChildren(...children) { this.children = children; }
}
const list = new Element('ol');
list.dataset.fileList = 'id_merge-documents';
list.hidden = true;
const input = new Element('input');
input.files = [];
vm.runInNewContext(script, {
    document: {
        querySelectorAll: selector => (selector === 'ol[data-file-list]' ? [list] : []),
        getElementById: id => (id === 'id_merge-documents' ? input : null),
        createElement: tag => new Element(tag),
    },
});
assert.equal(list.hidden, true, 'Bez wybranych plików lista jest ukryta');
input.files = [{name: 'b.docx'}, {name: 'a.docx'}, {name: 'c.docx'}];
input.events.change();
assert.equal(list.hidden, false);
assert.deepEqual(list.children.map(item => item.textContent), ['b.docx', 'a.docx', 'c.docx'],
                 'Kolejność listy = kolejność wyboru (nie alfabetyczna)');
input.files = [];
input.events.change();
assert.equal(list.hidden, true, 'Wyczyszczenie wyboru ukrywa listę');
assert.deepEqual(list.children, []);
console.log('Program file list OK');
