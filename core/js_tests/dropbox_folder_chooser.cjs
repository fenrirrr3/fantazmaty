// dropbox-folder-chooser.js: wybór folderu przez Dropbox Chooser – tylko pojedynczy folder z bezpiecznym linkiem
// https do dropbox.com trafia do pola; pliki, złe linki, anulowanie i brak biblioteki zostawiają dotychczasowy link.
const assert = require('node:assert/strict');
const {Document, runScript} = require('./support/dom.js');

function fixture(Dropbox) {
    const doc = new Document();
    const input = doc.el('input', {id: 'id_file_url', name: 'file_url', value: 'https://www.dropbox.com/old', maxLength: 80});
    const button = doc.el('button', {id: 'dropbox-folder-choose', dataset: {inputId: 'id_file_url'}});
    const status = doc.el('p', {id: 'dropbox-folder-status'});
    const form = doc.el('form', {}, input, button, status);
    doc.body.append(form);
    const events = [];
    form.addEventListener('input', () => events.push('input')); form.addEventListener('change', () => events.push('change'));
    runScript('dropbox-folder-chooser.js', doc, {Dropbox});
    return {input, button, status, events};
}
const KEEP = 'https://www.dropbox.com/old';

// Brak biblioteki Dropboxa (zablokowana / bez klucza aplikacji).
let f = fixture(undefined); f.button.click();
assert.match(f.status.textContent, /Nie udało się załadować okna Dropboxa/); assert.equal(f.input.value, KEEP);
f = fixture({}); f.button.click(); assert.match(f.status.textContent, /Nie udało się załadować okna Dropboxa/);
// Okno nie otwiera się (np. blokada wyskakujących okien).
f = fixture({choose() { throw new Error('popup'); }}); f.button.click();
assert.match(f.status.textContent, /Nie udało się otworzyć Dropboxa/);

// Właściwe opcje wyboru i obsługa odpowiedzi.
let options;
f = fixture({choose(value) { options = value; }}); f.button.click();
assert.equal(options.linkType, 'preview'); assert.equal(options.multiselect, false); assert.equal(options.folderselect, true);
const rejected = [
    ['plik zamiast folderu', [{isDir: false, link: 'https://www.dropbox.com/s/plik.docx'}], /Wybierz folder, a nie pojedynczy plik/],
    ['kilka elementów', [{isDir: true, link: 'https://www.dropbox.com/a'}, {isDir: true, link: 'https://www.dropbox.com/b'}], /Wybierz folder/],
    ['pusta lista', [], /Wybierz folder/],
    ['nie lista', null, /Wybierz folder/],
    ['link nie tekstem', [{isDir: true, link: 42}], /Wybierz folder/],
    ['niepoprawny URL', [{isDir: true, link: 'nie link'}], /nieprawidłowy link/],
    ['http', [{isDir: true, link: 'http://www.dropbox.com/sh/abc'}], /nieprawidłowy link/],
    ['obca domena', [{isDir: true, link: 'https://dropbox.com.evil.test/sh/abc'}], /nieprawidłowy link/],
    ['subdomena', [{isDir: true, link: 'https://dl.dropbox.com/sh/abc'}], /nieprawidłowy link/],
    ['dane logowania', [{isDir: true, link: 'https://user:pass@www.dropbox.com/sh/abc'}], /nieprawidłowy link/],
    ['za długi', [{isDir: true, link: 'https://www.dropbox.com/sh/' + 'a'.repeat(80)}], /nieprawidłowy link/],
];
for (const [name, items, message] of rejected) {
    f.status.textContent = '';
    options.success(items);
    assert.match(f.status.textContent, message, name);
    assert.match(f.status.textContent, /Dotychczasowy link pozostał bez zmian/, name);
    assert.equal(f.input.value, KEEP, name); assert.deepEqual(f.events, [], name);
}
options.cancel();
assert.equal(f.status.textContent, 'Anulowano wybór. Dotychczasowy link pozostał bez zmian.'); assert.equal(f.input.value, KEEP);

// Poprawny folder: link w polu, zdarzenia input i change (bąbelkujące – działa ostrzeżenie o niezapisanych zmianach).
options.success([{isDir: true, link: 'https://www.dropbox.com/sh/abc?dl=0', name: 'Opowiadanie <1>'}]);
assert.equal(f.input.value, 'https://www.dropbox.com/sh/abc?dl=0'); assert.deepEqual(f.events, ['input', 'change']);
assert.equal(f.status.textContent, 'Wybrano folder: Opowiadanie <1>. Kliknij „Zapisz link”, aby zachować zmianę.');
options.success([{isDir: true, link: 'https://dropbox.com/sh/xyz'}]);
assert.equal(f.input.value, 'https://dropbox.com/sh/xyz'); assert.match(f.status.textContent, /^Wybrano folder: Dropbox\./);

// Brak przycisku lub pola docelowego – skrypt nic nie robi i nie zgłasza błędu.
const doc = new Document(); doc.body.append(doc.el('button', {id: 'dropbox-folder-choose', dataset: {inputId: 'brak'}}), doc.el('p', {id: 'dropbox-folder-status'}));
runScript('dropbox-folder-chooser.js', doc, {});
runScript('dropbox-folder-chooser.js', new Document(), {});
console.log('Dropbox folder chooser: missing library, popup failure, file/invalid/foreign links rejected, cancel, valid folder OK');
