// vocabulary.js: podpowiedzi tagów i gatunków ze słownika – debounce, zapytanie o ostatni element listy,
// wstawienie podpowiedzi, przestarzałe odpowiedzi, błędy (bez pozostawiania nieaktualnych podpowiedzi).
const assert = require('node:assert/strict');
const {Document, fakeTimers, fakeFetch, flush, runScript} = require('./support/dom.js');

const doc = new Document();
const tags = doc.el('textarea', {name: 'tags', dataset: {vocabulary: 'tag'}});
const genre = doc.el('input', {name: 'genre', dataset: {vocabulary: 'genre'}});
doc.body.append(doc.el('span', {hidden: true, dataset: {vocabularyEndpoint: '/slownik/podpowiedzi/'}}), doc.el('form', {}, tags, genre));
const timers = fakeTimers(), net = fakeFetch(), changes = [];
tags.parentNode.addEventListener('change', () => changes.push(tags.value)); // change bąbelkuje do formularza
runScript('vocabulary.js', doc, {setTimeout: timers.setTimeout, clearTimeout: timers.clearTimeout, fetch: net.fetch});
const box = tags.parentNode.children[tags.parentNode.children.indexOf(tags) + 1];
const type = (field, value) => { field.value = value; field.fire('input'); };
const names = () => box.children.map(b => b.textContent);

(async () => {
    assert.equal(box.className, 'vocabulary-suggestions'); assert.equal(box.getAttribute('aria-label'), 'Podpowiedzi słownika');
    // Debounce 200 ms; pytamy o ostatni element listy (po przecinku lub nowej linii), z rodzajem słownika.
    type(tags, 'magia, smo'); type(tags, 'magia,\n smok');
    assert.equal(timers.pending().length, 1); assert.equal(timers.pending()[0].delay, 200);
    timers.runAll();
    assert.equal(net.requests.length, 1);
    const request = net.last();
    assert.equal(request.url.pathname, '/slownik/podpowiedzi/'); assert.equal(request.url.searchParams.get('kind'), 'tag');
    assert.equal(request.url.searchParams.get('q'), 'smok'); assert.equal(request.options.credentials, 'same-origin');
    net.answer(request, {results: ['smoki', '<b>smocze</b>']}); await flush();
    assert.deepEqual(names(), ['smoki', '<b>smocze</b>']); assert.equal(box.children[1].children.length, 0);
    assert.equal(box.children[0].type, 'button');

    // Wybór podpowiedzi zastępuje ostatni element, normalizuje separatory, zgłasza change i wraca fokusem do pola.
    box.children[0].click();
    assert.equal(tags.value, 'magia, smoki'); assert.deepEqual(changes, ['magia, smoki']);
    assert.equal(box.children.length, 0); assert.equal(doc.activeElement, tags);

    // Pusty ostatni element: brak zapytania, podpowiedzi czyszczone.
    type(tags, 'magia, '); timers.runAll(); assert.equal(net.requests.length, 1); assert.equal(box.children.length, 0);

    // Spóźniona odpowiedź na wcześniejsze wpisanie jest ignorowana.
    type(tags, 'ma'); timers.runAll(); const stale = net.last();
    type(tags, 'mag'); timers.runAll(); const fresh = net.last();
    net.answer(fresh, {results: ['magia']}); await flush();
    net.answer(stale, {results: ['mapa']}); await flush();
    assert.deepEqual(names(), ['magia']);
    // Spóźniony błąd poprzedniego zapytania nie kasuje aktualnych podpowiedzi.
    type(tags, 'ma'); timers.runAll(); const failing = net.last();
    type(tags, 'mag'); timers.runAll(); net.answer(net.last(), {results: ['magia']}); await flush();
    failing.reject(new TypeError('offline')); await flush();
    assert.deepEqual(names(), ['magia']);
    // Odpowiedź po wybraniu podpowiedzi nie otwiera listy ponownie.
    type(tags, 'magi'); timers.runAll(); const afterChoice = net.last();
    box.children[0].click(); assert.equal(tags.value, 'magia');
    net.answer(afterChoice, {results: ['magia']}); await flush();
    assert.equal(box.children.length, 0);

    // Błąd HTTP i sieci: nieaktualne podpowiedzi znikają.
    type(tags, 'smo'); timers.runAll(); net.answer(net.last(), {results: ['smoki']}); await flush();
    assert.deepEqual(names(), ['smoki']);
    type(tags, 'smok'); timers.runAll(); net.answer(net.last(), {}, {ok: false, status: 500}); await flush();
    assert.equal(box.children.length, 0, 'po błędzie nie zostają podpowiedzi dla innego zapytania');
    type(tags, 'smo'); timers.runAll(); net.answer(net.last(), {results: ['smoki']}); await flush();
    type(tags, 'smok'); timers.runAll(); net.last().reject(new TypeError('offline')); await flush();
    assert.equal(box.children.length, 0);

    // Drugie pole używa własnego rodzaju słownika.
    type(genre, 'fan'); timers.runAll(); assert.equal(net.last().url.searchParams.get('kind'), 'genre');

    // Bez adresu słownika (niezalogowany) skrypt nic nie dodaje.
    const plain = new Document(); const field = plain.el('input', {dataset: {vocabulary: 'tag'}}); plain.body.append(field);
    runScript('vocabulary.js', plain, {setTimeout() { throw new Error('nie powinno'); }});
    assert.equal(plain.body.children.length, 1); field.fire('input');
    console.log('Vocabulary: debounce, last item query, insertion, stale responses and errors OK');
})().catch(error => { console.error(error); process.exitCode = 1; });
