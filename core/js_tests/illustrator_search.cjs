// illustrator_search.js: wybór ilustratorów (pola wyboru) przez wyszukiwarkę – min. 2 znaki, wiele słów, polskie
// znaki, e-mail, limit 10, pomijanie wybranych, usuwanie, klawiatura, reset formularza; nazwy zawsze jako tekst.
const assert = require('node:assert/strict');
const {Document, fakeTimers, runScript} = require('./support/dom.js');

const doc = new Document();
const person = (name, email, checked = false) => {
    const box = doc.el('input', {type: 'checkbox', name: 'illustrators', checked, dataset: email ? {email} : {}});
    return doc.el('label', {}, box, ` ${name} `);
};
const people = [person('Łucja Żak', 'lucja@example.test', true), person('Anna <img src=x>', ''), person('Marek Łoś', 'marek@example.test'),
    ...Array.from({length: 11}, (_, i) => person(`Grafik ${i}`, `grafik${i}@example.test`))];
const source = doc.el('div', {id: 'id_illustrators'}, ...people);
const input = doc.el('input', {id: 'illustrator-search'});
const results = doc.el('ul', {id: 'illustrator-search-results'}), selected = doc.el('ul', {id: 'illustrator-selected'});
const counter = doc.el('p', {id: 'illustrator-search-count'});
const form = doc.el('form', {}, source, input, counter, results, selected);
doc.body.append(form);
const changes = [];
source.addEventListener('change', event => changes.push(event.target.closest('label').textContent.trim()));
const timers = fakeTimers();
runScript('illustrator_search.js', doc, {setTimeout: timers.setTimeout, clearTimeout: timers.clearTimeout});
const box = i => people[i].children[0];
const type = value => { input.value = value; input.fire('input'); };
const resultNames = () => results.children.map(row => row.children[0].textContent);
const selectedNames = () => selected.children.map(row => row.textContent);

// Start: lista wybranych z e-mailem, podpowiedź o 2 znakach, wyniki ukryte.
assert.deepEqual(selectedNames(), ['Łucja Żak · lucja@example.testUsuń']);
assert.equal(selected.children[0].children[1].getAttribute('aria-label'), 'Usuń z wyboru: Łucja Żak');
assert.equal(counter.textContent, 'Wpisz co najmniej 2 znaki, aby wyszukać kolejną osobę.'); assert.equal(results.hidden, true);
type('m'); assert.equal(results.children.length, 0); assert.match(counter.textContent, /co najmniej 2 znaki/);

// Bez polskich znaków, wiele słów w dowolnej kolejności, także po e-mailu.
type('los mar'); assert.deepEqual(resultNames(), ['Dodaj: Marek Łoś']); assert.equal(results.hidden, false);
assert.equal(counter.textContent, 'Znaleziono: 1. Wybierz „Dodaj” przy właściwej osobie.');
type('MAREK@EXAMPLE'); assert.deepEqual(resultNames(), ['Dodaj: Marek Łoś']);
// Nazwa z HTML pozostaje tekstem.
type('img'); assert.deepEqual(resultNames(), ['Dodaj: Anna <img src=x>']); assert.equal(results.children[0].children[0].children.length, 0);
// Wybrana osoba nie jest proponowana ponownie.
type('lucja'); assert.equal(results.children.length, 0); assert.equal(results.hidden, true);
assert.equal(counter.textContent, 'Brak pasujących osób. Wybrane osoby są już na liście wybranych.');

// Limit 10 wyników z informacją o doprecyzowaniu.
type('grafik'); assert.equal(results.children.length, 10);
assert.match(counter.textContent, /^Znaleziono: 11\. .*Pokazano pierwsze 10 wyników/);

// Dodanie: zaznacza pole, zgłasza change (bąbelkujące), czyści wyszukiwanie i wraca fokusem do pola.
type('marek'); results.children[0].children[0].click();
assert.equal(box(2).checked, true); assert.deepEqual(changes, ['Marek Łoś']);
assert.equal(input.value, ''); assert.equal(doc.activeElement, input); assert.equal(results.hidden, true);
assert.deepEqual(selectedNames(), ['Łucja Żak · lucja@example.testUsuń', 'Marek Łoś · marek@example.testUsuń']);
// Usunięcie z wyboru odznacza; pusty wybór pokazuje komunikat.
selected.children[0].children[1].click(); selected.children[0].children[1].click();
assert.equal(box(0).checked, false); assert.equal(box(2).checked, false);
assert.deepEqual(selectedNames(), ['Nie wybrano ilustratora.']); assert.equal(selected.children[0].className, 'muted-text');

// Klawiatura: Enter/ArrowDown przenosi fokus na pierwszy wynik (bez wysyłania formularza), Escape czyści.
type('anna');
let event = input.fire('keydown', {key: 'Enter'});
assert.equal(event.defaultPrevented, true); assert.equal(doc.activeElement, results.children[0].children[0]);
input.fire('keydown', {key: 'Escape'});
assert.equal(input.value, ''); assert.equal(results.children.length, 0);
event = input.fire('keydown', {key: 'ArrowDown'}); assert.equal(event.defaultPrevented, true);

// Reset formularza: po przywróceniu domyślnych pól (setTimeout) lista wybranych odświeżona, wyszukiwanie wyczyszczone.
type('grafik'); box(5).checked = true; form.fire('reset');
assert.equal(timers.pending().length, 1); timers.runAll();
assert.equal(input.value, ''); assert.equal(results.children.length, 0);
assert.deepEqual(selectedNames(), ['Grafik 2 · grafik2@example.testUsuń']);

// Bez wymaganych elementów skrypt nic nie robi.
const empty = new Document(); empty.body.append(empty.el('input', {id: 'illustrator-search'}));
runScript('illustrator_search.js', empty, {setTimeout() {}});
console.log('Illustrator search: min length, Polish/multi-word/e-mail search, limit, add/remove, keyboard, reset OK');
