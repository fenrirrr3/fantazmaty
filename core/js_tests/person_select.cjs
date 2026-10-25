// person-select.js (z lookup.js): wyszukiwalny select jednej osoby (przypisania, korekta, scalanie haseł) – polskie znaki,
// duplikaty nazw, dokładne wpisanie, klawiatura, limit 30 wyników, filtr według roli i reset formularza.
const assert = require('node:assert/strict');
const {Document, fakeTimers, runScript} = require('./support/dom.js');

function fixture({role = null, people = null, data = {}} = {}) {
    const doc = new Document();
    const options = (people || [['Anna Kowalska', '1'], ['Łukasz Żurawski', '2'], ['Jan Nowak', '3'], ['Jan Nowak', '4']])
        .map(([text, value, roles]) => doc.option(text, value, roles === undefined ? {} : {dataset: {assignmentRoles: roles}}));
    const select = doc.el('select', {id: 'id_person', required: true, dataset: {searchablePerson: 'true', ...data}},
        doc.option('Wybierz', ''), ...options, doc.option('Nieaktywna', '9', {disabled: true}));
    const label = doc.el('label', {htmlFor: 'id_person'}); select.labels = [label];
    const form = doc.el('form', {}, label, select);
    let roleSelect = null;
    if (role !== null) {
        roleSelect = doc.el('select', {id: 'id_role'}, doc.option('---', ''), doc.option('Tłumaczenie', 'translator'), doc.option('Redakcja', 'editor'));
        roleSelect.value = role; select.dataset.assignmentRoleSelect = 'id_role'; form.append(roleSelect);
    }
    doc.body.append(form);
    return {doc, select, label, form, roleSelect, options};
}
function start(f, presetValue) {
    if (presetValue !== undefined) f.select.value = presetValue;
    const timers = fakeTimers(), changes = [];
    f.select.addEventListener('change', () => changes.push(f.select.value));
    runScript(['lookup.js', 'person-select.js'], f.doc, {setTimeout: timers.setTimeout, clearTimeout: timers.clearTimeout});
    const input = f.doc.getElementById('id_person_search');
    const list = f.doc.getElementById('id_person_suggestions'), status = f.doc.querySelector('.lookup-status');
    const type = value => { input.value = value; input.fire('input', {bubbles: true}); };
    const key = name => input.fire('keydown', {key: name});
    return Object.assign(f, {timers, changes, input, list, status, type, key, labels: () => list.children.map(b => b.textContent)});
}

// Panel admina: skrypt z Media ładuje się w <head>, zanim powstanie formularz – widżet powstaje po DOMContentLoaded,
// a ponowne uruchomienie (np. drugi skrypt na stronie) nie dubluje pola.
{
    const g = fixture(); g.doc.readyState = 'loading';
    const timers = fakeTimers();
    const context = runScript(['lookup.js', 'person-select.js'], g.doc, {setTimeout: timers.setTimeout, clearTimeout: timers.clearTimeout});
    assert.equal(g.doc.getElementById('id_person_search'), null);
    g.doc.readyState = 'interactive'; g.doc.dispatchEvent(new context.Event('DOMContentLoaded'));
    assert.ok(g.doc.getElementById('id_person_search'));
    g.doc.dispatchEvent(new context.Event('DOMContentLoaded'));
    assert.equal(g.doc.querySelectorAll('.lookup-status').length, 1);
}

// Stan początkowy: zapisana wartość widoczna w polu, wymaganie przeniesione z selecta na pole, etykieta przepięta.
let f = start(fixture(), '1');
assert.equal(f.input.value, 'Anna Kowalska'); assert.equal(f.input.required, true); assert.equal(f.select.required, false);
assert.equal(f.select.hidden, true); assert.equal(f.label.htmlFor, 'id_person_search');
assert.equal(f.input.placeholder, 'Wpisz nazwisko i wybierz osobę');

// Wyszukiwanie bez polskich znaków i wielkości liter; częściowy tekst nie jest wyborem.
f.type('LUKASZ zur');
assert.deepEqual(f.labels(), ['Łukasz Żurawski']); assert.equal(f.select.value, '');
assert.equal(f.input.validationMessage, 'Wybierz osobę z podpowiedzi albo wyczyść pole.');
assert.equal(f.status.textContent, 'Wybierz osobę z listy.'); assert.equal(f.input.getAttribute('aria-expanded'), 'true');
f.list.children[0].click();
assert.equal(f.select.value, '2'); assert.equal(f.input.value, 'Łukasz Żurawski'); assert.equal(f.input.validationMessage, '');
assert.equal(f.changes.at(-1), '2'); assert.equal(f.status.textContent, 'Wybrano osobę.'); assert.equal(f.list.children.length, 0);

// Duplikaty nazw rozróżnione numerem; wyłączone opcje pominięte; wpisanie pełnej etykiety wybiera osobę.
f.type('jan');
assert.deepEqual(f.labels(), ['Jan Nowak (#3)', 'Jan Nowak (#4)']);
f.type('Jan Nowak (#4)'); assert.equal(f.select.value, '4'); assert.equal(f.input.validationMessage, '');
f.type('Nieaktywna'); assert.deepEqual(f.labels(), []); assert.equal(f.select.value, '');
assert.equal(f.status.textContent, 'Brak pasujących osób.'); assert.equal(f.input.getAttribute('aria-expanded'), 'false');

// Klawiatura: strzałka przy pustej liście uruchamia wyszukiwanie, Enter wybiera aktywną pozycję, Escape zamyka.
f.input.value = 'Jan'; f.list.replaceChildren();
f.key('ArrowDown'); assert.equal(f.list.children.length, 2);
f.key('ArrowDown');
assert.equal(f.input.getAttribute('aria-activedescendant'), 'id_person_suggestions_0');
assert.deepEqual(f.list.children.map(b => b.getAttribute('aria-selected')), ['true', 'false']);
f.key('ArrowUp');
assert.equal(f.input.getAttribute('aria-activedescendant'), 'id_person_suggestions_1', 'strzałka w górę zawija na koniec');
assert.deepEqual(f.list.children.map(b => b.getAttribute('aria-selected')), ['false', 'true']);
assert.equal(f.key('Enter').defaultPrevented, true); assert.equal(f.select.value, '4');
f.type('Jan'); f.key('Escape'); assert.equal(f.list.children.length, 0); assert.equal(f.input.getAttribute('aria-expanded'), 'false');
// Enter bez listy nie blokuje wysłania formularza.
assert.equal(f.key('Enter').defaultPrevented, false);
// Fokus pokazuje podpowiedzi, kliknięcie poza polem je zamyka; puste pole czyści wybór.
f.input.focus(); assert.equal(f.list.children.length, 2);
f.doc.body.click(); assert.equal(f.list.children.length, 0);
f.type(''); assert.equal(f.select.value, ''); assert.equal(f.input.validationMessage, ''); assert.equal(f.status.textContent, '');

// Reset formularza przywraca etykietę wartości selecta (po zdarzeniu reset, w setTimeout).
f.type('xyz'); f.select.value = '1'; f.form.fire('reset');
f.timers.runAll();
assert.equal(f.input.value, 'Anna Kowalska'); assert.equal(f.input.validationMessage, ''); assert.equal(f.status.textContent, '');

// Limit 30 wyników i własne nazwy pozycji (scalanie haseł słownika).
const many = Array.from({length: 35}, (_, i) => [`Hasło ${i}`, String(100 + i)]);
f = start(fixture({people: many, data: {searchItem: 'hasło', searchItems: 'haseł', searchPlaceholder: 'Wpisz nazwę hasła'}}));
assert.equal(f.input.placeholder, 'Wpisz nazwę hasła'); assert.equal(f.input.value, '');
f.type('hasło');
assert.equal(f.list.children.length, 30); assert.equal(f.status.textContent, 'Zawęź zapytanie – pokazano pierwszych 30 haseł.');
f.type('Hasło 7'); assert.equal(f.select.value, '107'); assert.equal(f.input.validationMessage, '');
f.list.children[0].click(); assert.equal(f.status.textContent, 'Wybrano hasło.');

// Filtr roli: bez roli pole zablokowane; po wyborze roli tylko uprawnione osoby; niedozwolony wybór czyszczony.
const roles = [['Anna Kowalska', '1', 'translator'], ['Łukasz Żurawski', '2', 'editor'], ['Jan Nowak', '3', 'translator editor'], ['Ola Bez Roli', '4', '']];
f = start(fixture({role: '', people: roles}), '2');
assert.equal(f.input.disabled, true); assert.equal(f.select.value, ''); assert.equal(f.input.value, '');
assert.equal(f.status.textContent, 'Najpierw wybierz rolę.');
f.roleSelect.value = 'translator'; f.roleSelect.fire('change');
assert.equal(f.input.disabled, false); assert.equal(f.status.textContent, 'Wybierz osobę uprawnioną do tej roli.');
assert.deepEqual(f.options.map(o => o.disabled), [false, true, false, true]);
f.type('a'); assert.deepEqual(f.labels(), ['Anna Kowalska', 'Jan Nowak']);
f.type('Łukasz Żurawski'); assert.equal(f.select.value, '', 'osoba bez roli nie może zostać wybrana');
assert.match(f.input.validationMessage, /Wybierz osobę/);
f.type('Jan Nowak'); assert.equal(f.select.value, '3');
f.roleSelect.value = 'editor'; f.roleSelect.fire('change');
assert.equal(f.select.value, '3', 'osoba z obiema rolami zostaje'); assert.equal(f.input.value, 'Jan Nowak');
f.type('Anna Kowalska'); assert.equal(f.select.value, '');
f.type('Jan Nowak'); f.roleSelect.value = 'translator'; f.roleSelect.fire('change'); assert.equal(f.select.value, '3');
f.select.value = '1'; f.roleSelect.value = 'editor'; f.roleSelect.fire('change');
assert.equal(f.select.value, ''); assert.equal(f.input.value, '');
// Reset formularza przelicza dostępność według przywróconej roli.
f.roleSelect.value = ''; f.form.fire('reset'); f.timers.runAll();
assert.equal(f.input.disabled, true); assert.equal(f.status.textContent, 'Najpierw wybierz rolę.');
console.log('Person select: Polish search, duplicates, exact entry, keyboard, 30-result limit, role filter and reset OK');
