// Skrypty z base.html: layout.js (tabele jako karty na telefonie – etykiety komórek i pasek sortowania)
// oraz header-search.js (panel wyszukiwania w nagłówku: fokus, Escape, kliknięcie obok).
const assert = require('node:assert/strict');
const {Document, runScript, ready} = require('./support/dom.js');

const doc = new Document();
const sortButton = (text, cls = 'table-sort-button') => doc.el('button', {className: cls}, text);
const th = (...content) => doc.el('th', {}, ...content);
const row = (...cells) => doc.el('tr', {}, ...cells.map(c => typeof c === 'string' ? doc.el('td', {}, c) : c));
const table = (head, body) => doc.el('table', {}, doc.el('thead', {}, doc.el('tr', {}, ...head)), doc.el('tbody', {}, ...body));
const card = (tbl, {shell = false, extra = []} = {}) => {
    const container = doc.el('div', {className: 'table-container'}, tbl);
    return doc.el('section', {}, ...extra, shell ? doc.el('div', {className: 'table-scroll-shell'}, container) : container);
};
// 1. Płaska tabela: etykiety z nagłówków (bez strzałek sortowania), pole wyboru → „Wybór”, pusty → „Informacja”.
const titleSort = sortButton('Tytuł ▲'), dateSort = sortButton('Data', 'sort-button');
const titleTh = th(titleSort); titleTh.setAttribute('aria-sort', 'ascending');
const preset = doc.el('td', {dataset: {label: 'Własna'}}, 'x');
const detail = doc.el('td', {colSpan: 4}, 'Szczegóły');
const flat = table([th(doc.el('input', {type: 'checkbox'})), titleTh, th(dateSort), th('')],
    [row('', 'Smok', '2025-01-01', preset), row(detail), row('', 'Wiedźma', '2024-12-01', 'y')]);
const flatCard = card(flat, {shell: true});
// 2. Złożone nagłówki (colspan) i 3. komórki z rowspan – bez zmian.
const spanning = table([doc.el('th', {colSpan: 2}, 'A')], [row('1', '2')]);
const rowspanCell = doc.el('td', {rowSpan: 2}, 'r');
const rowspan = table([th('A'), th('B')], [row(rowspanCell, '1'), row('2')]);
// 4. Karta z serwerowym sortowaniem – etykiety tak, pasek nie; 5. tabela bez sortowania – bez paska.
const serverSorted = table([th(sortButton('Nazwa'))], [row('a')]);
const noSort = table([th('Nazwa')], [row('a')]);
// 6. Tabela poza <main> – pominięta.
const outside = table([th('A')], [row('a')]);
const main = doc.el('main', {}, flatCard, card(spanning), card(rowspan),
    card(serverSorted, {extra: [doc.el('div', {className: 'mobile-table-sort'})]}), card(noSort));
doc.body.append(main, doc.el('aside', {}, doc.el('div', {className: 'table-container'}, outside)));

runScript('layout.js', doc); ready(doc);
const labels = tbl => tbl.tBodies[0].rows.map(r => r.cells.map(c => c.dataset.label));
assert.equal(flat.className, 'mobile-review-cards');
assert.deepEqual(labels(flat), [['Wybór', 'Tytuł', 'Data', 'Własna'], [undefined], ['Wybór', 'Tytuł', 'Data', 'Informacja']]);
for (const [name, tbl] of [['colspan', spanning], ['rowspan', rowspan], ['poza main', outside]]) {
    assert.equal(tbl.className, '', name); assert.ok(tbl.querySelectorAll('td').every(c => c.dataset.label === undefined), name);
}
assert.equal(serverSorted.className, 'mobile-review-cards'); assert.deepEqual(labels(serverSorted), [['Nazwa']]);
assert.equal(doc.querySelectorAll('.mobile-table-sort').length, 2, 'pasek tylko dla płaskiej tabeli z przyciskami sortowania');
assert.equal(noSort.className, 'mobile-review-cards');
// Pasek sortowania przed powłoką przewijania; wybrana opcja odpowiada aktualnemu sortowaniu.
const toolbar = flatCard.children[0];
assert.equal(toolbar.className, 'mobile-table-sort'); assert.equal(flatCard.children[1].className, 'table-scroll-shell');
const [label, button] = toolbar.children;
const select = label.querySelector('select'); assert.match(label.textContent, /^Sortowanie/);
assert.deepEqual(select.children.map(o => [o.value, o.textContent, o.selected]), [['1', 'Tytuł', true], ['2', 'Data', false]]);
// „Sortuj” klika przycisk sortowania wybranej kolumny.
const clicks = [];
titleSort.addEventListener('click', () => clicks.push('tytuł')); dateSort.addEventListener('click', () => clicks.push('data'));
select.value = '2'; button.click(); select.value = '1'; button.click();
assert.deepEqual(clicks, ['data', 'tytuł']);

// header-search.js
const hdoc = new Document();
const summary = hdoc.el('summary', {}, 'Szukaj');
const query = hdoc.el('input', {name: 'query'});
const panel = hdoc.el('details', {dataset: {headerSearch: ''}}, summary, hdoc.el('form', {}, query));
const elsewhere = hdoc.el('a', {}, 'Strona');
hdoc.body.append(panel, elsewhere);
runScript('header-search.js', hdoc);
panel.open = true; panel.fire('toggle'); assert.equal(hdoc.activeElement, query);
query.click(); assert.equal(panel.open, true, 'klik w panelu go nie zamyka');
let event = query.fire('keydown', {key: 'Escape', bubbles: true});
assert.equal(panel.open, false); assert.equal(hdoc.activeElement, summary); assert.equal(event.defaultPrevented, true);
event = query.fire('keydown', {key: 'Escape', bubbles: true}); assert.equal(event.defaultPrevented, false, 'zamknięty panel nie przechwytuje Escape');
panel.open = false; panel.fire('toggle'); assert.equal(hdoc.activeElement, summary, 'zamknięcie nie przenosi fokusu do pola');
panel.open = true; elsewhere.click(); assert.equal(panel.open, false);
console.log('Page layout: mobile table cards, sort toolbar, skipped complex tables; header search focus, Escape, outside click OK');
