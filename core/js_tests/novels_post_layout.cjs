// novels.js (zaznaczanie rozdziałów i filtr autorów powieści) oraz post-layout.js (panele operacji zbiorczych
// korekty poskładowej: aktywny tylko panel wybranej operacji, pozostałe pola wyłączone i wyczyszczone).
const assert = require('node:assert/strict');
const {Document, runScript, ready} = require('./support/dom.js');

// novels.js – przypisywanie rozdziałów
let doc = new Document();
const chapters = [1, 2, 3].map(i => doc.el('input', {type: 'checkbox', name: 'chapters', value: String(i), checked: i === 2}));
const all = doc.el('input', {type: 'checkbox', dataset: {selectChapters: ''}});
const status = doc.el('span', {dataset: {selectedChapters: ''}});
const authors = doc.el('select', {multiple: true, dataset: {filterOptions: ''}},
    doc.option('Łucja Żak', '1'), doc.option('Anna Nowak', '2', {selected: true}), doc.option('ŁUKASZ Kos', '3'));
doc.body.append(doc.el('form', {id: 'chapter-assign'}, all, status, ...chapters), doc.el('form', {}, authors));
runScript('novels.js', doc);
assert.equal(status.textContent, 'Zaznaczone rozdziały: 1'); assert.equal(all.checked, false); assert.equal(all.indeterminate, true);
chapters[0].checked = true; chapters[0].fire('change'); chapters[2].checked = true; chapters[2].fire('change');
assert.equal(status.textContent, 'Zaznaczone rozdziały: 3'); assert.equal(all.checked, true); assert.equal(all.indeterminate, false);
all.checked = false; all.fire('change');
assert.deepEqual(chapters.map(c => c.checked), [false, false, false]);
assert.equal(status.textContent, 'Zaznaczone rozdziały: 0'); assert.equal(all.indeterminate, false);
all.checked = true; all.fire('change'); assert.deepEqual(chapters.map(c => c.checked), [true, true, true]);
// Filtr autorów: polskie wielkie litery, wybrane osoby zawsze widoczne, pusty filtr pokazuje wszystkich.
const filter = authors.parentNode.children[0];
assert.equal(filter.type, 'search'); assert.equal(filter.getAttribute('aria-label'), 'Filtruj autorów');
filter.value = ' łu '; filter.fire('input');
assert.deepEqual(authors.options.map(o => o.hidden), [false, false, false]);
filter.value = 'łuk'; filter.fire('input');
assert.deepEqual(authors.options.map(o => o.hidden), [true, false, false], 'wybrana Anna widoczna mimo filtra');
filter.value = ''; filter.fire('input'); assert.deepEqual(authors.options.map(o => o.hidden), [false, false, false]);
// Strona bez formularza rozdziałów i bez filtra – bez błędów.
runScript('novels.js', new Document());

// post-layout.js
doc = new Document();
const operation = doc.el('select', {name: 'operation'}, doc.option('Przypisz', 'assign'), doc.option('Status', 'status'));
const assignee = doc.el('select', {name: 'assignee'}, doc.option('Anna', '1'));
const notify = doc.el('input', {type: 'checkbox', name: 'notify', checked: true});
const state = doc.el('select', {name: 'state'}, doc.option('Gotowe', 'done'));
const confirm = doc.el('input', {type: 'checkbox', name: 'confirm', checked: true});
const panels = [doc.el('fieldset', {dataset: {bulkAction: 'assign'}}, assignee, notify), doc.el('fieldset', {dataset: {bulkAction: 'status'}}, state, confirm)];
doc.body.append(doc.el('form', {id: 'post-layout-bulk'}, operation, ...panels));
const windowEvents = {};
runScript('post-layout.js', doc, {addEventListener: (type, fn) => { windowEvents[type] = fn; }}); ready(doc);
const snapshot = () => panels.map(p => [p.hidden, ...p.children.map(f => [f.disabled, f.required, f.type === 'checkbox' ? f.checked : null])]);
assert.deepEqual(snapshot(), [[false, [false, true, null], [false, true, true]], [true, [true, false, null], [true, false, false]]]);
operation.value = 'status'; operation.fire('change');
assert.deepEqual(snapshot(), [[true, [true, false, null], [true, false, false]], [false, [false, true, null], [false, true, false]]]);
// Powrót przyciskiem „wstecz” (pageshow z pamięci podręcznej) – panele zgodne z przywróconym wyborem.
operation.value = 'assign'; windowEvents.pageshow();
assert.equal(panels[0].hidden, false); assert.equal(panels[1].hidden, true); assert.equal(state.disabled, true);
runScript('post-layout.js', new Document(), {addEventListener() {}});
console.log('Novels and post-layout: chapter selection, author filter, bulk operation panels OK');
