// mailbox-import.js: pasek postępu przy pobieraniu zgłoszeń ze skrzynki (liczba wiadomości, blokada przycisków,
// ukrycie po otrzymaniu ZIP-a przez ciasteczko z tokenem). Do tego na tej samej stronie: „zaznacz wszystkie”
// z table-selection.js dla pól „uids” (stan pośredni) i podgląd treści maila z recruitment-samples.js.
const assert = require('node:assert/strict');
const {Document, Event, flush, runScript, ready} = require('./support/dom.js');

function page() {
    const doc = new Document();
    doc.cookie = '';
    const box = doc.el('div', {className: 'mailbox-progress', hidden: true, dataset: {mailboxProgressBox: ''}},
        doc.el('p', {dataset: {mailboxProgressLabel: ''}}), doc.el('progress'));
    const token = doc.el('input', {type: 'hidden', name: 'download_token'});
    const all = doc.el('input', {type: 'checkbox', dataset: {selectTable: 'uids'}});
    const rows = [12, 13, 14].map(uid => doc.el('input', {type: 'checkbox', name: 'uids', value: String(uid), checked: uid === 12}));
    rows.forEach(box => { box.getClientRects = () => [{}]; });
    const table = doc.el('table', {},
        doc.el('thead', {}, doc.el('tr', {}, doc.el('th', {}, all))),
        doc.el('tbody', {}, ...rows.map(box => doc.el('tr', {}, Object.assign(doc.el('td', {}, box), {cellIndex: 0})))));
    const download = doc.el('button', {name: 'action', value: 'download', dataset: {mailboxProgress: ''}});
    const headers = doc.el('button', {name: 'action', value: 'headers'});
    const panel = doc.el('section', {dataset: {mailPreview: ''}},
        ...['state', 'subject', 'sender', 'body', 'truncated'].map(name => doc.el('p', {dataset: {[`preview${name[0].toUpperCase()}${name.slice(1)}`]: ''}})));
    const form = doc.el('form', {dataset: {mailboxImport: '', previewField: 'uids'}}, token, panel, table, headers, download, box);
    doc.body.append(doc.el('main', {}, form));
    return {doc, form, box, token, all, rows, table, download, headers};
}

(async () => {
    // „Zaznacz wszystkie” dla pól uids: stan pośredni po załadowaniu, zaznaczenie i odznaczenie wszystkich.
    const {doc, form, box, token, all, rows, table, download, headers} = page();
    runScript('table-selection.js', doc, {Event});
    doc.querySelectorAll = selector => selector === 'main table' ? [table] : Document.prototype.querySelectorAll.call(doc, selector);
    ready(doc);
    assert.equal(all.checked, false); assert.equal(all.indeterminate, true);
    all.checked = true; table.dispatchEvent(Object.assign(new Event('change'), {target: all}));
    assert.deepEqual(rows.map(box => box.checked), [true, true, true]);
    assert.equal(all.indeterminate, false);
    rows[1].checked = false; table.dispatchEvent(Object.assign(new Event('change'), {target: rows[1]}));
    assert.equal(all.checked, false); assert.equal(all.indeterminate, true);
    all.checked = false; table.dispatchEvent(Object.assign(new Event('change'), {target: all}));
    assert.equal(rows.some(box => box.checked), false); assert.equal(all.indeterminate, false);

    // Pasek postępu: liczba zaznaczonych wiadomości, blokada przycisków dopiero po wysłaniu.
    const later = [], intervals = [], windowEvents = {};
    const context = runScript('mailbox-import.js', doc, {
        setTimeout: fn => later.push(fn), setInterval: fn => { intervals.push(fn); return intervals.length; }, clearInterval: () => {},
        location: {pathname: '/recenzje/import/'}, addEventListener: (type, fn) => { windowEvents[type] = fn; },
    });
    context.Math = Math; context.Date = Date;
    ready(doc);
    rows[0].checked = rows[2].checked = true;
    const ignored = Object.assign(new Event('submit'), {submitter: headers});
    form.dispatchEvent(ignored);
    assert.equal(box.hidden, true); // Pobranie nagłówków nie pokazuje paska.
    form.dispatchEvent(Object.assign(new Event('submit'), {submitter: download}));
    assert.equal(box.hidden, false);
    assert.match(box.textContent, /^Trwa pobieranie 2 wiadomości/);
    assert.equal(download.disabled, false);
    later.forEach(fn => fn());
    assert.equal(download.disabled, true); assert.equal(headers.disabled, true);
    assert.equal(token.value, ''); // Podgląd odpowiada stroną HTML – bez tokenu pobrania.

    // Zatwierdzenie: token w formularzu, ciasteczko z serwera chowa pasek i odblokowuje przyciski.
    download.value = 'confirm';
    form.dispatchEvent(Object.assign(new Event('submit'), {submitter: download}));
    assert.match(token.value, /^[a-z0-9]{8,40}$/);
    intervals.at(-1)();
    assert.equal(box.querySelector('progress').hidden, false);
    doc.cookie = `mailbox_import_done=${token.value}`;
    intervals.at(-1)();
    assert.equal(box.textContent, 'Pobrano paczkę ZIP. Zgłoszenia zapisano.');
    assert.equal(download.disabled, false);
    assert.match(doc.cookie, /max-age=0; path=\/recenzje\/import\//);
    // Powrót „Wstecz” chowa pasek.
    windowEvents.pageshow();
    assert.equal(box.hidden, true);

    // Podgląd maila: ten sam skrypt co w rekrutacji, pole wyboru „uids” usuwane z zapytania podglądu.
    const preview = page(), sent = [];
    runScript('recruitment-samples.js', preview.doc, {
        FormData: class extends Map { constructor() { super([['uids', '12'], ['selection', 'x']]); } },
        AbortController, location: {href: 'https://cms.example/recenzje/import/'}, setTimeout: fn => fn(), clearTimeout: () => {},
        DOMParser: class { parseFromString() { return {querySelector: () => ({querySelector: selector => (
            selector.includes('truncated') ? {hidden: true} : {textContent: selector.includes('body') ? '<b>treść</b>' : 'x'})})}; } },
        fetch: (url, options) => { sent.push(options.body); return Promise.resolve({ok: true, redirected: false, text: async () => ''}); },
    });
    ready(preview.doc);
    const subject = preview.doc.el('button', {dataset: {previewUid: '13'}});
    preview.table.append(subject);
    subject.click();
    await flush();
    assert.equal(sent.length, 1);
    assert.equal(sent[0].get('action'), 'preview'); assert.equal(sent[0].get('uid'), '13');
    assert.equal(sent[0].has('uids'), false);
    assert.equal(preview.form.querySelector('[data-preview-body]').textContent, '<b>treść</b>');
    console.log('Mailbox import: select all (uids), progress bar with count and ZIP cookie, mail preview reuse OK');
})().catch(error => { console.error(error); process.exitCode = 1; });
