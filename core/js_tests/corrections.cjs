// corrections.js: formularz uwag korektorskich – wczytywanie tekstów antologii (blokada wysyłki, przestarzałe
// odpowiedzi, błąd), zapis „zapisz i dodaj kolejną” przez fetch (sukces, błędy pól, brak potwierdzenia, podwójne kliknięcie).
const assert = require('node:assert/strict');
const {Document, fakeFetch, flush, runScript, ready} = require('./support/dom.js');

class FormData {
    constructor(form) { this.entries = form.elements.filter(field => field.name && !field.disabled).map(field => [field.name, field.value]); }
    get(name) { return this.entries.find(([key]) => key === name)?.[1] ?? null; }
}
function fixture({correctionForm = true} = {}) {
    const doc = new Document();
    const anthology = doc.el('select', {name: 'anthology'}, doc.option('Antologia A', '1'), doc.option('Antologia B', '2'));
    const text = doc.el('select', {name: 'text', dataset: {textsUrl: '/korekty/teksty/'}}, doc.option('Inne miejsce', ''));
    const field = name => doc.el('textarea', {name});
    const fields = {fragment: field('fragment'), problem: field('problem'), suggestion: field('suggestion'),
        token: doc.el('input', {type: 'hidden', name: 'submission_token', value: 't1'})};
    const save = doc.el('button', {type: 'submit', name: 'action', value: 'save'});
    const more = doc.el('button', {type: 'submit', name: 'action', value: 'continue'});
    const form = doc.el('form', {attrs: {action: '/korekty/nowa/', ...(correctionForm ? {'data-correction-form': ''} : {})}},
        anthology, text, fields.fragment, fields.problem, fields.suggestion, fields.token, save, more);
    doc.body.append(form);
    const net = fakeFetch(), saved = [];
    doc.addEventListener('cms:form-saved', event => saved.push(event.detail.form));
    runScript('corrections.js', doc, {fetch: net.fetch, FormData});
    ready(doc);
    const message = form.children[0];
    const submit = submitter => form.fire('submit', {submitter});
    const choose = value => { anthology.value = value; anthology.fire('change'); };
    return {doc, form, anthology, text, fields, save, more, net, saved, message, submit, choose};
}

(async () => {
    let f = fixture();
    assert.equal(f.message.tagName, 'P'); assert.equal(f.message.getAttribute('role'), 'status');

    // Wczytywanie tekstów: blokada przycisków i listy, adres z antologią, tytuły jako tekst.
    f.choose('2');
    assert.equal(f.text.disabled, true); assert.deepEqual(f.text.options.map(o => o.text), ['Wczytywanie…']);
    assert.equal(f.save.disabled, true); assert.equal(f.more.disabled, true);
    const request = f.net.last();
    assert.equal(request.url.pathname, '/korekty/teksty/'); assert.equal(request.url.searchParams.get('anthology'), '2');
    // Wysłanie w trakcie wczytywania jest wstrzymane.
    let event = f.submit(f.save);
    assert.equal(event.defaultPrevented, true); assert.match(f.message.textContent, /Najpierw wczytaj listę tekstów/);
    f.net.answer(request, {texts: [{id: 4, title: '<b>Smok</b>'}, {id: 5, title: 'Wiedźma'}]}); await flush();
    assert.deepEqual(f.text.options.map(o => [o.text, o.value]), [['Inne miejsce', ''], ['<b>Smok</b>', '4'], ['Wiedźma', '5']]);
    assert.equal(f.text.disabled, false); assert.equal(f.save.disabled, false); assert.equal(f.more.disabled, false);
    assert.equal(f.message.textContent, '');
    // Zwykły zapis (nie „continue”) idzie standardową drogą formularza.
    event = f.submit(f.save); assert.equal(event.defaultPrevented, false); assert.equal(f.net.requests.length, 1);

    // Szybka zmiana antologii: liczy się tylko ostatnia odpowiedź.
    f.choose('1'); const first = f.net.last(); f.choose('2'); const second = f.net.last();
    f.net.answer(second, {texts: [{id: 7, title: 'Nowy'}]}); await flush();
    f.net.answer(first, {texts: [{id: 1, title: 'Stary'}]}); await flush();
    assert.deepEqual(f.text.options.map(o => o.text), ['Inne miejsce', 'Nowy']); assert.equal(f.save.disabled, false);

    // Błąd wczytania: komunikat, przyciski pozostają zablokowane, wysłanie wstrzymane do ponownego wyboru antologii.
    f.choose('1'); f.net.answer(f.net.last(), {}, {ok: false, status: 500}); await flush();
    assert.deepEqual(f.text.options.map(o => o.text), ['Nie udało się wczytać tekstów']);
    assert.equal(f.text.disabled, false); assert.equal(f.save.disabled, true);
    assert.equal(f.message.textContent, 'Wybierz antologię ponownie, aby spróbować jeszcze raz.');
    assert.equal(f.submit(f.more).defaultPrevented, true); assert.equal(f.net.requests.length, 4);
    f.choose('1'); f.net.last().reject(new TypeError('offline')); await flush();
    assert.equal(f.save.disabled, true);
    f.choose('1'); f.net.answer(f.net.last(), {texts: []}); await flush();
    assert.equal(f.save.disabled, false); assert.equal(f.message.textContent, '');

    // „Zapisz i dodaj kolejną”: wysyłka fetch, pola zablokowane na czas zapisu, drugie kliknięcie ignorowane.
    f.fields.fragment.value = 'Smok zionoł'; f.fields.problem.value = 'Literówka'; f.fields.suggestion.value = 'zionął';
    event = f.submit(f.more);
    assert.equal(event.defaultPrevented, true);
    const post = f.net.last();
    assert.equal(post.url.pathname, '/korekty/nowa/'); assert.equal(post.options.method, 'POST');
    assert.equal(post.options.headers['X-Requested-With'], 'XMLHttpRequest'); assert.equal(post.options.body.get('fragment'), 'Smok zionoł');
    assert.equal(f.fields.fragment.disabled, true); assert.equal(f.more.disabled, true); assert.equal(f.message.textContent, 'Zapisywanie…');
    f.submit(f.more); assert.equal(f.net.last(), post, 'podwójne kliknięcie nie wysyła drugiej uwagi');
    f.net.answer(post, {ok: true, next_token: 't2', message: 'Zapisano uwagę.', id: 12, list_url: '/korekty/lista/'}); await flush();
    assert.deepEqual([f.fields.fragment.value, f.fields.problem.value, f.fields.suggestion.value], ['', '', '']);
    assert.equal(f.fields.token.value, 't2'); assert.equal(f.anthology.value, '1', 'antologia zostaje do kolejnej uwagi');
    assert.equal(f.message.textContent, 'Zapisano uwagę. Zapisano uwagę nr 12. Odśwież listę zapisanych uwag');
    assert.equal(f.message.children.at(-1).href, '/korekty/lista/');
    assert.deepEqual(f.saved, [f.form]); assert.equal(f.doc.activeElement, f.fields.fragment);
    assert.equal(f.fields.fragment.disabled, false); assert.equal(f.more.disabled, false);

    // Błędy walidacji: komunikat z polskimi nazwami pól, dane zostają, pola odblokowane.
    f.fields.fragment.value = 'Fragment';
    f.submit(f.more);
    f.net.answer(f.net.last(), {ok: false, errors: {problem: ['To pole jest wymagane.'], __all__: ['Uwaga już istnieje.'], extra: ['X']}}, {ok: false, status: 400});
    await flush();
    assert.equal(f.message.textContent, 'Co jest źle: To pole jest wymagane. Formularz: Uwaga już istnieje. extra: X');
    assert.equal(f.fields.fragment.value, 'Fragment'); assert.equal(f.fields.token.value, 't2'); assert.equal(f.fields.fragment.disabled, false);
    for (const [body, http] of [[{ok: false}, {ok: false, status: 400}], [{ok: false, errors: {}}, {ok: true}]]) {
        f.submit(f.more); f.net.answer(f.net.last(), body, http); await flush();
        assert.equal(f.message.textContent, 'Nie udało się zapisać uwagi.'); assert.equal(f.fields.token.value, 't2');
    }
    // Brak potwierdzenia (sieć / odpowiedź nie-JSON): dane zostają, można ponowić.
    f.submit(f.more); f.net.last().reject(new TypeError('offline')); await flush();
    assert.match(f.message.textContent, /Nie otrzymano potwierdzenia zapisu/);
    f.submit(f.more); f.net.last().resolve({ok: true, json: async () => { throw new SyntaxError('HTML'); }}); await flush();
    assert.match(f.message.textContent, /Nie otrzymano potwierdzenia zapisu/);
    assert.equal(f.fields.fragment.value, 'Fragment'); assert.equal(f.fields.fragment.disabled, false);
    assert.equal(f.saved.length, 1);

    // Formularz edycji uwagi (bez data-correction-form): tylko wczytywanie tekstów, wysyłka standardowa.
    f = fixture({correctionForm: false});
    assert.equal(f.submit(f.more).defaultPrevented, false);
    f.choose('2'); assert.equal(f.net.requests.length, 1);
    console.log('Corrections JS: text loading (lock, stale, failure), AJAX save and continue, field errors, missing confirmation, double click OK');
})().catch(error => { console.error(error); process.exitCode = 1; });
