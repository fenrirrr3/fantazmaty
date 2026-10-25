// author-search.js (z lookup.js): wyszukiwarka autora w formularzu pojedynczego zgłoszenia (strona „Dodaj do recenzji”) –
// debounce, wyniki, klawiatura, wybór wypełnia dane autora, przestarzałe odpowiedzi, błędy sieci i ręczne wpisanie.
const assert = require('node:assert/strict');
const {Document, fakeTimers, fakeFetch, flush, runScript, ready} = require('./support/dom.js');

const FIELDS = ['author_first_name', 'author_last_name', 'author_pseudonym', 'email', 'phone_number'];
function fixture() {
    const doc = new Document();
    const select = doc.el('select', {id: 'id_author', name: 'author', dataset: {authorSearchUrl: '/autorzy/podpowiedzi/'}},
        doc.option('---------', ''));
    const label = doc.el('label', {htmlFor: 'id_author'}); select.labels = [label];
    const fields = Object.fromEntries(FIELDS.map(name => [name, doc.el('input', {name})]));
    const form = doc.el('form', {}, label, doc.el('p', {}, select), ...Object.values(fields));
    doc.body.append(form);
    const timers = fakeTimers(), net = fakeFetch(), events = [];
    select.addEventListener('change', () => events.push(['change', select.value]));
    fields.email.addEventListener('input', () => events.push(['email', fields.email.value]));
    runScript(['lookup.js', 'author-search.js'], doc, {setTimeout: timers.setTimeout, clearTimeout: timers.clearTimeout, fetch: net.fetch});
    ready(doc);
    const input = doc.getElementById('id_author_search'), list = doc.getElementById('id_author_suggestions');
    const status = doc.querySelector('.lookup-status'), manual = doc.querySelector('.author-manual-button');
    const type = value => { input.value = value; input.fire('input', {bubbles: true}); };
    const key = name => input.fire('keydown', {key: name});
    return {doc, select, label, fields, form, timers, net, events, input, list, status, manual, type, key};
}
const ANNA = {id: 5, label: 'Anna <b>Nowak</b>', first_name: 'Anna', last_name: 'Nowak', pseudonym: '', email: 'anna@example.test', phone_number: '+48 600'};
const JAN = {id: 6, label: 'Jan Kowalski', first_name: 'Jan', last_name: 'Kowalski', pseudonym: 'Smok', email: '', phone_number: ''};

(async () => {
    // Budowa kontrolki: select ukryty, pole combobox z etykietą, przycisk ręcznego wpisania przed selectem.
    const f = fixture();
    assert.equal(f.select.hidden, true); assert.equal(f.label.htmlFor, 'id_author_search');
    assert.equal(f.input.getAttribute('role'), 'combobox'); assert.equal(f.input.getAttribute('aria-controls'), 'id_author_suggestions');
    assert.equal(f.select.parentNode.children.indexOf(f.manual), f.select.parentNode.children.indexOf(f.select) - 1);

    // Debounce 500 ms: kolejne znaki kasują poprzedni zegar, zapytanie idzie raz, z przyciętym tekstem.
    f.type('Ann'); f.type(' Anna  ');
    assert.equal(f.timers.pending().length, 1); assert.equal(f.timers.pending()[0].delay, 500);
    assert.match(f.input.validationMessage, /Wybierz autora z sugestii/);
    f.timers.runAll();
    assert.equal(f.net.requests.length, 1);
    const request = f.net.last();
    assert.equal(request.url.pathname, '/autorzy/podpowiedzi/'); assert.equal(request.url.searchParams.get('q'), 'Anna');
    assert.equal(request.options.credentials, 'same-origin'); assert.equal(request.options.cache, 'no-store');
    assert.equal(f.status.textContent, 'Szukam…');
    f.net.answer(request, {results: [ANNA, JAN]}); await flush();
    // Wyniki jako textContent (bez HTML), e-mail tylko gdy jest.
    assert.deepEqual(f.list.children.map(b => b.textContent), ['Anna <b>Nowak</b> · anna@example.test', 'Jan Kowalski']);
    assert.equal(f.list.children[0].children.length, 0);
    assert.equal(f.input.getAttribute('aria-expanded'), 'true'); assert.equal(f.status.textContent, 'Wybierz autora z listy.');

    // Klawiatura: strzałki zawijają się, Enter wybiera aktywną pozycję.
    f.key('ArrowDown'); f.key('ArrowDown'); f.key('ArrowDown');
    assert.equal(f.input.getAttribute('aria-activedescendant'), 'id_author_suggestions_0');
    f.key('ArrowUp');
    assert.equal(f.input.getAttribute('aria-activedescendant'), 'id_author_suggestions_1');
    f.key('ArrowDown');
    assert.deepEqual(f.list.children.map(b => b.getAttribute('aria-selected')), ['true', 'false']);
    const enter = f.key('Enter');
    assert.equal(enter.defaultPrevented, true, 'Enter nie może wysłać formularza');
    assert.equal(f.select.value, '5'); assert.equal(f.select.options.at(-1).textContent, ANNA.label);
    assert.equal(f.input.value, ANNA.label); assert.equal(f.input.validationMessage, '');
    assert.deepEqual(FIELDS.map(n => f.fields[n].value), ['Anna', 'Nowak', '', 'anna@example.test', '+48 600']);
    assert.deepEqual(f.events.at(-1), ['change', '5']); assert.ok(f.events.some(e => e[0] === 'email' && e[1] === 'anna@example.test'));
    assert.equal(f.list.children.length, 0); assert.equal(f.input.getAttribute('aria-expanded'), 'false');
    assert.equal(f.status.textContent, 'Wybrano autora.');

    // Edycja tekstu po wyborze zrywa powiązanie i czyści dane wybranego autora.
    f.type('Anna N');
    assert.equal(f.select.value, ''); assert.deepEqual(FIELDS.map(n => f.fields[n].value), ['', '', '', '', '']);
    // Kolejna edycja (bez wybranego autora) nie czyści ręcznie wpisanych danych.
    f.fields.author_first_name.value = 'Ręcznie'; f.type('Anna No');
    assert.equal(f.fields.author_first_name.value, 'Ręcznie');

    // Przestarzała odpowiedź: nowe wpisanie przerywa poprzednie zapytanie, a spóźniona odpowiedź nic nie zmienia.
    f.timers.runAll(); const stale = f.net.last();
    f.type('Jan'); assert.equal(stale.aborted, true);
    f.timers.runAll(); const fresh = f.net.last();
    f.net.answer(stale, {results: [ANNA]}); f.net.answer(fresh, {results: [JAN]}); await flush();
    assert.deepEqual(f.list.children.map(b => b.textContent), ['Jan Kowalski']);
    // Kliknięcie poza listą zamyka podpowiedzi; kliknięcie w pozycję wybiera autora (pseudonim też trafia do formularza).
    f.doc.body.click(); assert.equal(f.list.children.length, 0);
    f.key('Enter'); f.net.answer(f.net.last(), {results: [JAN]}); await flush();
    f.list.children[0].click();
    assert.equal(f.select.value, '6'); assert.equal(f.fields.author_pseudonym.value, 'Smok');
    assert.equal(f.select.options.filter(o => o.value === '6').length, 1, 'opcja autora dodana tylko raz');

    // Pusty wynik, błąd HTTP, błąd sieci, puste zapytanie.
    f.type('Zzz'); f.timers.runAll(); f.net.answer(f.net.last(), {results: []}); await flush();
    assert.equal(f.status.textContent, 'Brak pasujących autorów.'); assert.equal(f.input.getAttribute('aria-expanded'), 'false');
    f.type('Zzz2'); f.timers.runAll(); f.net.answer(f.net.last(), {}, {ok: false, status: 500}); await flush();
    assert.match(f.status.textContent, /Nie udało się pobrać autorów/);
    f.type('Zzz3'); f.timers.runAll(); f.net.last().reject(new TypeError('offline')); await flush();
    assert.match(f.status.textContent, /Nie udało się pobrać autorów/);
    f.type('   '); f.timers.runAll();
    const count = f.net.requests.length;
    assert.equal(f.status.textContent, ''); assert.equal(f.input.validationMessage, ''); assert.equal(f.net.requests.length, count);

    // Kliknięcie obok w trakcie wyszukiwania: spóźniona odpowiedź nie otwiera już listy.
    f.type('Anna'); f.timers.runAll(); const away = f.net.last();
    f.doc.body.click(); f.net.answer(away, {results: [ANNA]}); await flush();
    assert.equal(away.aborted, true); assert.equal(f.list.children.length, 0); assert.equal(f.status.textContent, '');
    // Escape przerywa trwające zapytanie i zamyka listę.
    f.type('Anna'); f.timers.runAll(); const escaped = f.net.last();
    f.key('Escape'); await flush();
    assert.equal(escaped.aborted, true); assert.equal(f.status.textContent, '', 'po przerwaniu nie zostaje „Szukam…”');

    // „Wpisz nowego autora ręcznie”: czyści wybór i dane wybranego autora, przenosi fokus do imienia.
    f.type('Anna'); f.key('Enter'); f.net.answer(f.net.last(), {results: [ANNA]}); await flush();
    f.list.children[0].click(); assert.equal(f.fields.email.value, 'anna@example.test');
    f.manual.click();
    assert.equal(f.select.value, ''); assert.equal(f.input.value, ''); assert.deepEqual(FIELDS.map(n => f.fields[n].value), ['', '', '', '', '']);
    assert.equal(f.input.validationMessage, ''); assert.equal(f.doc.activeElement, f.fields.author_first_name);
    f.type('x'); f.timers.runAll(); const pending = f.net.last();
    f.manual.click(); assert.equal(pending.aborted, true);

    // Formularz ponownie wyświetlony z wybranym autorem: pole pokazuje jego nazwę.
    const doc2 = new Document();
    const preset = doc2.el('select', {id: 'id_author', dataset: {authorSearchUrl: '/a/'}}, doc2.option('---------', ''), doc2.option('Ewa Lis', '9'));
    preset.value = '9'; preset.labels = [];
    doc2.body.append(doc2.el('form', {}, preset));
    runScript(['lookup.js', 'author-search.js'], doc2, {setTimeout: () => 0, clearTimeout() {}, fetch() {}}); ready(doc2);
    assert.equal(doc2.getElementById('id_author_search').value, 'Ewa Lis');

    console.log('Author search: debounce, results as text, keyboard, selection fills author data, stale/aborted requests, errors, manual entry OK');
})().catch(error => { console.error(error); process.exitCode = 1; });
