// contact-suggestions.js: podpowiedzi dla nowej osoby bez konta (tryb „hint”) – jedno pole statusu na grupę,
// min. 2 znaki, zapytanie łączy pola, rodzaj z selecta zadania, przestarzałe odpowiedzi, błąd sieci; nazwy jako tekst.
const assert = require('node:assert/strict');
const {Document, fakeTimers, fakeFetch, flush, runScript} = require('./support/dom.js');

const doc = new Document();
const hint = name => doc.el('input', {name, dataset: {contactMode: 'hint', contactGroup: 'new-person'}});
const first = hint('first_name'), last = hint('last_name');
const select = doc.el('select', {dataset: {contactKindSelect: 'true'}}, doc.option('Wybierz', ''), doc.option('Okładka', 'cover'));
const form = doc.el('form', {dataset: {contactEndpoint: '/kontakty/podpowiedzi/'}},
    doc.el('div', {className: 'form-field'}, first), doc.el('div', {className: 'form-field'}, last), doc.el('div', {}, select));
doc.body.append(form);
const timers = fakeTimers(), net = fakeFetch();
runScript('contact-suggestions.js', doc, {setTimeout: timers.setTimeout, clearTimeout: timers.clearTimeout, fetch: net.fetch});
const boxes = doc.querySelectorAll('.contact-suggestions');
const type = (field, value) => { field.value = value; field.fire('input'); };

(async () => {
    // Jeden komunikat na grupę, wstawiony po ostatnim polu grupy, z aria-live.
    assert.equal(boxes.length, 1); const box = boxes[0];
    assert.equal(form.children.indexOf(box), 2); assert.equal(box.getAttribute('aria-live'), 'polite');

    // Poniżej 2 znaków nie ma zapytania; wpisywanie kasuje poprzedni zegar (debounce 250 ms).
    type(first, 'J'); timers.runAll(); assert.equal(net.requests.length, 0); assert.equal(box.textContent, '');
    type(first, 'Ja'); type(first, 'Jan'); assert.equal(timers.pending().length, 1); assert.equal(timers.pending()[0].delay, 250);
    timers.runAll(); assert.equal(net.requests.length, 1);
    assert.equal(net.last().url.pathname, '/kontakty/podpowiedzi/');
    assert.equal(net.last().url.searchParams.get('kind'), 'person'); assert.equal(net.last().url.searchParams.get('q'), 'Jan');
    net.answer(net.last(), {results: [{name: 'Jan <b>Kowalski</b>', note: 'nieaktywny'}, {name: 'Jan Nowak', note: ''}]}); await flush();
    assert.equal(box.textContent, 'Podobne wpisy w bazie: Jan <b>Kowalski</b> (nieaktywny), Jan Nowak. '
        + 'Jeśli to ta sama osoba, wybierz ją w sekcji zadania zamiast dodawać nową.');
    assert.equal(box.children.length, 0);

    // Zapytanie łączy imię i nazwisko; wybór okładki zmienia rodzaj podpowiedzi.
    type(last, ' Nowak '); timers.runAll(); assert.equal(net.last().url.searchParams.get('q'), 'Jan Nowak');
    net.answer(net.last(), {results: []}); await flush(); assert.equal(box.textContent, '');
    select.value = 'cover'; select.fire('change'); timers.runAll();
    assert.equal(net.last().url.searchParams.get('kind'), 'cover');

    // Przestarzała odpowiedź jest ignorowana, odpowiedź z błędem nie zmienia komunikatu.
    const stale = net.last(); type(last, 'Nowicki'); timers.runAll(); const fresh = net.last();
    net.answer(fresh, {results: [{name: 'Jan Nowicki', note: ''}]}); await flush();
    net.answer(stale, {results: [{name: 'Stary wpis', note: ''}]}); await flush();
    assert.match(box.textContent, /^Podobne wpisy w bazie: Jan Nowicki\./);
    type(last, 'Nowy'); timers.runAll(); net.answer(net.last(), {results: []}, {ok: false, status: 500}); await flush();
    assert.match(box.textContent, /Jan Nowicki/);

    // Wyczyszczenie pól poniżej 2 znaków usuwa komunikat.
    type(first, ''); type(last, ''); timers.runAll(); assert.equal(box.textContent, '');
    console.log('Contact suggestions: hint mode (status box, debounce, kind, stale and failed responses) passed.');
})().catch(error => { console.error(error); process.exitCode = 1; });
