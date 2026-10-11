// translation-search.js: wybór tłumaczy/redaktorów w szczegółach tekstu – debounce, pomijanie już wybranych,
// chipy z usuwaniem, klawiatura, przestarzałe odpowiedzi, błędy, zamykanie przy opuszczeniu pola.
const assert = require('node:assert/strict');
const {Document, fakeTimers, fakeFetch, flush, runScript, ready} = require('./support/dom.js');

const doc = new Document();
const select = doc.el('select', {id: 'id_translators', multiple: true, dataset: {translationSearchUrl: '/tlumaczenia/osoby/translator/'},
    attrs: {'aria-describedby': 'id_translators_help'}}, doc.option('Ewa <i>Lis</i>', '3', {selected: true}));
const label = doc.el('label', {htmlFor: 'id_translators'}); select.labels = [label];
const outside = doc.el('button', {type: 'button'});
doc.body.append(doc.el('form', {}, label, select), outside);
const timers = fakeTimers(), net = fakeFetch(), changes = [];
select.addEventListener('change', () => changes.push(select.selectedOptions.map(o => o.value)));
runScript('translation-search.js', doc, {setTimeout: timers.setTimeout, clearTimeout: timers.clearTimeout, fetch: net.fetch});
ready(doc);
const input = doc.getElementById('id_translators_search'), list = doc.getElementById('id_translators_suggestions');
const status = doc.getElementById('id_translators_status'), chips = doc.querySelector('.selected-authors');
const wrapper = input.parentNode;
const type = value => { input.value = value; input.fire('input', {bubbles: true}); };
const key = name => input.fire('keydown', {key: name});
const chipNames = () => chips.children.map(chip => chip.children[0].textContent);
const selectedIds = () => select.selectedOptions.map(o => o.value);

(async () => {
    // Budowa: select ukryty, etykieta wskazuje pole, opis łączy istniejący opis i status, chip dla zapisanych osób (jako tekst).
    assert.equal(select.hidden, true); assert.equal(label.htmlFor, 'id_translators_search');
    assert.equal(input.getAttribute('aria-describedby'), 'id_translators_help id_translators_status');
    assert.deepEqual(chipNames(), ['Ewa <i>Lis</i>']); assert.equal(chips.children[0].children[0].children.length, 0);
    assert.equal(chips.children[0].children[1].getAttribute('aria-label'), 'Usuń: Ewa <i>Lis</i>');

    // Debounce 350 ms, niepełny wybór blokuje wysłanie formularza.
    type('Ma'); type('Marek ');
    assert.equal(timers.pending().length, 1); assert.equal(timers.pending()[0].delay, 350);
    assert.match(input.validationMessage, /Wybierz osobę z wyników/);
    timers.runAll();
    const request = net.last();
    assert.equal(request.url.pathname, '/tlumaczenia/osoby/translator/'); assert.equal(request.url.searchParams.get('q'), 'Marek');
    assert.equal(request.options.cache, 'no-store'); assert.equal(status.textContent, 'Szukam…');
    // Już wybrana osoba (id 3) nie pojawia się w wynikach.
    net.answer(request, {results: [{id: 3, label: 'Ewa Lis'}, {id: 7, label: 'Marek <script>'}, {id: 8, label: 'Marta Nowak'}]}); await flush();
    assert.deepEqual(list.children.map(b => b.textContent), ['Marek <script>', 'Marta Nowak']);
    assert.equal(input.getAttribute('aria-expanded'), 'true'); assert.equal(status.textContent, 'Wybierz osobę z wyników.');

    // Klawiatura: ArrowDown dwa razy, ArrowUp wraca; Enter wybiera.
    key('ArrowDown'); key('ArrowDown'); key('ArrowUp');
    assert.equal(input.getAttribute('aria-activedescendant'), 'id_translators_suggestions_0');
    assert.equal(key('Enter').defaultPrevented, true);
    assert.deepEqual(selectedIds(), ['3', '7']); assert.deepEqual(changes.at(-1), ['3', '7']);
    assert.deepEqual(chipNames(), ['Ewa <i>Lis</i>', 'Marek <script>']);
    assert.equal(input.value, ''); assert.equal(input.validationMessage, ''); assert.equal(doc.activeElement, input);
    assert.equal(list.children.length, 0); assert.match(status.textContent, /Dodano osobę/);

    // Usunięcie chipu odznacza osobę w selekcie.
    chips.children[0].children[1].click();
    assert.deepEqual(selectedIds(), ['7']); assert.deepEqual(chipNames(), ['Marek <script>']);

    // Wszystkie wyniki już wybrane / pusty wynik.
    type('Marek'); timers.runAll(); net.answer(net.last(), {results: [{id: 7, label: 'Marek <script>'}]}); await flush();
    assert.equal(list.children.length, 0); assert.equal(status.textContent, 'Brak nowych pasujących osób.');
    assert.equal(input.getAttribute('aria-expanded'), 'false');

    // Przestarzała odpowiedź po nowym wpisaniu jest ignorowana, poprzednie zapytanie przerwane.
    type('Ann'); timers.runAll(); const stale = net.last();
    type('Bea'); assert.equal(stale.aborted, true); timers.runAll(); const fresh = net.last();
    net.answer(stale, {results: [{id: 20, label: 'Anna'}]}); net.answer(fresh, {results: [{id: 21, label: 'Beata'}]}); await flush();
    assert.deepEqual(list.children.map(b => b.textContent), ['Beata']);
    // Kliknięcie w wynik dodaje osobę spoza listy opcji selecta.
    list.children[0].click(); assert.deepEqual(selectedIds(), ['7', '21']);

    // Błędy: HTTP 500 i brak sieci – komunikat; Escape przerywa zapytanie bez komunikatu błędu.
    type('X1'); timers.runAll(); net.answer(net.last(), {}, {ok: false, status: 500}); await flush();
    assert.equal(status.textContent, 'Nie udało się pobrać osób. Spróbuj ponownie.');
    type('X2'); timers.runAll(); net.last().reject(new TypeError('offline')); await flush();
    assert.equal(status.textContent, 'Nie udało się pobrać osób. Spróbuj ponownie.');
    type('X3'); timers.runAll(); const escaped = net.last(); key('Escape'); await flush();
    assert.equal(escaped.aborted, true); assert.equal(status.textContent, '', 'po przerwaniu nie zostaje „Szukam…”');

    // Puste pole: bez zapytania, status wyczyszczony.
    const count = net.requests.length; type('  '); timers.runAll();
    assert.equal(net.requests.length, count); assert.equal(status.textContent, ''); assert.equal(input.validationMessage, '');

    // Opuszczenie pola (focusout poza wrapper) i kliknięcie obok zamykają listę i przerywają zapytanie.
    type('Ola'); key('Enter'); net.answer(net.last(), {results: [{id: 30, label: 'Ola'}]}); await flush();
    assert.equal(list.children.length, 1);
    wrapper.fire('focusout', {relatedTarget: list.children[0]}); assert.equal(list.children.length, 1, 'fokus na wyniku nie zamyka listy');
    wrapper.fire('focusout', {relatedTarget: outside}); assert.equal(list.children.length, 0);
    type('Ola'); key('Enter'); const clicked = net.last(); outside.click();
    assert.equal(clicked.aborted, true);
    console.log('Translation search: debounce, skipping selected people, chips, keyboard, stale responses, errors, closing OK');
})().catch(error => { console.error(error); process.exitCode = 1; });
