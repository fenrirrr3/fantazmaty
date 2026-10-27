// Ikona CSV w nagłówku (table-csv.js + table-data.js): cała tabela jako CSV – kolejność wierszy z tabeli
// (sortowanie), wiersze ukryte lokalną paginacją tak, bez odfiltrowanych wierszy i ukrytych kolumn; listy
// stronicowane na serwerze – wszystkie strony; schowek i ręczne kopiowanie, gdy przeglądarka odmawia.
const assert = require('node:assert/strict');
const {Document, flush, runScript, ready} = require('./support/dom.js');
const {csvField, tableCsv} = require('../static/core/table-csv.js');

function buildTable(doc, rows, {server = false} = {}) {
    const el = doc.el.bind(doc);
    const sortButton = label => el('th', {}, el('button', {className: 'table-sort-button', textContent: label},
        el('span', {className: 'sort-indicator', attrs: {'aria-hidden': 'true'}, textContent: '▲'})));
    const head = el('thead', {}, el('tr', {},
        el('th', {}, el('span', {className: 'visually-hidden', textContent: 'Zaznacz'})),
        sortButton('Tytuł'), el('th', {textContent: 'Autor'}), el('th', {textContent: 'Status'}),
        el('th', {className: 'hidden-column', textContent: 'Uwagi'}), el('th', {textContent: 'Akcje'})));
    const body = el('tbody', {}, ...rows);
    const table = el('table', {dataset: server ? {serverPaginated: 'true'} : {}}, head, body);
    table.getClientRects = () => [1];
    table.compareDocumentPosition = node => node === table.nav ? 4 : 2;
    return table;
}
function record(doc, title, author, status, extra = {}) {
    const el = doc.el.bind(doc);
    const select = doc.el('select', {}, doc.option('Nowy', 'new'), doc.option(status, 'x', {selected: true}));
    return el('tr', extra, el('td', {}, el('input', {type: 'checkbox'})), el('td', {}, el('a', {textContent: title})),
        el('td', {textContent: author}), el('td', {}, select), el('td', {className: 'hidden-column', textContent: 'tajne'}),
        el('td', {}, el('button', {textContent: 'Usuń'})));
}

// --- Budowa CSV ---
{
    const doc = new Document();
    const detail = doc.el('tr', {}, doc.el('td', {colSpan: 6, textContent: 'Szczegóły pod wierszem'}));
    const rows = [
        record(doc, 'Zmierzch, czyli "koniec"', 'Anna  Nowak', 'Gotowe'),
        detail,
        record(doc, 'Druga strona', 'Jan\nKowalski', 'W toku', {className: 'cms-page-hidden'}),
        record(doc, 'Odfiltrowany', 'X', 'Y', {dataset: {filteredOut: 'true'}}),
        doc.el('tr', {className: 'empty-form'}, doc.el('td', {textContent: 'szablon'})),
    ];
    const notes = doc.el('details', {}, doc.el('summary', {textContent: 'Skrót'}), doc.el('p', {textContent: 'cała notatka'}));
    rows[0].children[2].append(notes);
    const table = buildTable(doc, rows);
    const csv = tableCsv(table, new Set([4]));
    assert.equal(csv.text, [
        'Tytuł;Autor;Status;Akcje',
        '"Zmierzch, czyli ""koniec""";Anna Nowak Skrót;Gotowe;',
        'Druga strona;Jan Kowalski;W toku;',
    ].join('\r\n'));
    assert.deepEqual([csv.rows, csv.columns], [2, 4]);
    // Kolumna akcji ma nagłówek, więc zostaje (pusta); kolumna pól wyboru i ukryta kolumna znikają.
    for (const [value, expected] of [['zwykły', 'zwykły'], ['a,b', 'a,b'], ['a;b', '"a;b"'], ['cudzysłów "x"', '"cudzysłów ""x"""'], ['', ''],
        ['dwie\nlinie', '"dwie\nlinie"']]) assert.equal(csvField(value), expected);
}

// --- Panel: jedna tabela kopiuje się od razu, panele nagłówka zamykają się nawzajem ---
function page({server = false, clipboardFails = false, pages = {}, tables = null, hiddenHeaders = [], response = {ok: true}} = {}) {
    const doc = new Document();
    const toggle = doc.el('button', {attrs: {'aria-controls': 'table-csv-panel', 'aria-expanded': 'false'}, dataset: {tableCsvToggle: ''}});
    const panel = doc.el('div', {id: 'table-csv-panel', hidden: true, dataset: {headerPanel: ''}},
        doc.el('select', {dataset: {tableCsvTable: ''}}), doc.el('button', {dataset: {tableCsvRun: ''}}),
        doc.el('p', {dataset: {tableCsvStatus: ''}}), doc.el('textarea', {hidden: true, select() { this.selected = true; }, dataset: {tableCsvResult: ''}}));
    const emailToggle = doc.el('button', {attrs: {'aria-controls': 'email-copy-panel', 'aria-expanded': 'true'}});
    const emailPanel = doc.el('div', {id: 'email-copy-panel', hidden: false, dataset: {headerPanel: ''}});
    const main = doc.el('main', {}, doc.el('h1', {textContent: 'Teksty'}));
    if (tables) main.append(...tables(doc));
    else {
        const table = buildTable(doc, [record(doc, 'B tytuł', 'Ola', 'Gotowe'), record(doc, 'A tytuł', 'Ewa', 'Nowy')], {server});
        const nav = doc.el('nav', {dataset: {sortConfig: '', pageParam: 'page', sizeParam: 'page_size', resultCount: '3'}});
        table.nav = nav;
        main.append(table, nav);
    }
    doc.body.append(doc.el('header', {}, doc.el('div', {dataset: {tableCsv: ''}}, toggle, panel), emailToggle, emailPanel), main);
    const copied = [], requests = [];
    const context = runScript(['table-data.js', 'table-csv.js'], doc, {
        navigator: {clipboard: {writeText: async text => { if (clipboardFails) throw Error('denied'); copied.push(text); }}},
        // Kolumna ukryta przez użytkownika (przełącznik kolumn) – widać to tylko w stylach bieżącego widoku.
        getComputedStyle: cell => ({display: hiddenHeaders.includes(cell.textContent.replace('▲', '').trim()) ? 'none' : 'table-cell'}),
        location: {href: 'https://cms.example/teksty/?q=smok&sort=-title&page=2&page_size=25'},
        fetch: async (url, options) => { requests.push({url: new URL(url), options}); return {redirected: false, text: async () => url, ...response}; },
        DOMParser: function DOMParser() { this.parseFromString = url => pages[new URL(url).searchParams.get('page')]; },
    });
    ready(doc);
    return {doc, toggle, panel, emailPanel, emailToggle, select: panel.children[0], run: panel.children[1],
        status: panel.children[2], output: panel.children[3], copied, requests, context};
}

(async () => {
    {
        const view = page();
        view.toggle.click(); await flush();
        assert.equal(view.panel.hidden, false);
        assert.equal(view.emailPanel.hidden, true);
        assert.equal(view.emailToggle.getAttribute('aria-expanded'), 'false');
        // Kolejność jak w tabeli (po sortowaniu), nie alfabetyczna.
        assert.deepEqual(view.copied, ['Tytuł;Autor;Status;Akcje\r\nB tytuł;Ola;Gotowe;\r\nA tytuł;Ewa;Nowy;']);
        assert.equal(view.status.textContent, 'Skopiowano CSV: wiersze 2, kolumny 4.');
    }
    {
        // Lista stronicowana na serwerze: wszystkie strony po 500, z filtrami i sortowaniem z adresu.
        const pages = {};
        for (const [number, titles] of [['1', ['Pierwszy', 'Drugi']], ['2', ['Trzeci']]]) {
            const doc = new Document();
            const table = buildTable(doc, titles.map(title => record(doc, title, 'Autor', 'Gotowe')), {server: true});
            const nav = doc.el('nav', {dataset: {sortConfig: '', pageParam: 'page', sizeParam: 'page_size', resultCount: '3'}},
                doc.el('input', {value: number, max: '2', dataset: {serverPage: ''}}));
            table.nav = nav;
            doc.body.append(doc.el('div', {dataset: {tableCsv: ''}}), doc.el('main', {}, table, nav));
            pages[number] = doc;
        }
        const view = page({server: true, pages});
        view.toggle.click(); await flush(); await flush(); await flush();
        assert.deepEqual(view.copied, ['Tytuł;Autor;Status;Akcje\r\nPierwszy;Autor;Gotowe;\r\nDrugi;Autor;Gotowe;\r\nTrzeci;Autor;Gotowe;']);
        assert.equal(view.requests.length, 2);
        for (const {url, options} of view.requests) {
            assert.deepEqual([url.searchParams.get('q'), url.searchParams.get('sort'), url.searchParams.get('page_size')], ['smok', '-title', '500']);
            assert.equal(options.headers['X-CMS-Table-Copy'], '1');
        }
        // Kolumna ukryta przez użytkownika nie trafia do CSV także z pobranych stron (te nie mają stylów).
        const narrowed = page({server: true, pages, hiddenHeaders: ['Autor']});
        narrowed.toggle.click(); await flush(); await flush(); await flush();
        assert.deepEqual(narrowed.copied, ['Tytuł;Status;Akcje\r\nPierwszy;Gotowe;\r\nDrugi;Gotowe;\r\nTrzeci;Gotowe;']);
        // Błąd serwera przy pobieraniu strony: komunikat, schowek bez zmian.
        const failed = page({server: true, pages, response: {ok: false}});
        failed.toggle.click(); await flush(); await flush();
        assert.deepEqual(failed.copied, []);
        assert.match(failed.status.textContent, /Nie udało się pobrać całej listy/);
        // Strona bez ikony CSV (wygasła sesja) przerywa kopiowanie – bez części listy w schowku.
        delete pages['2'].body.children[0].dataset.tableCsv;
        const expired = page({server: true, pages});
        expired.toggle.click(); await flush(); await flush(); await flush();
        assert.deepEqual(expired.copied, []);
        assert.match(expired.status.textContent, /Sesja wygasła/);
    }
    {
        // Przeglądarka odmawia zapisu do schowka: CSV zaznaczony w polu do ręcznego skopiowania.
        const view = page({clipboardFails: true});
        view.toggle.click(); await flush();
        assert.equal(view.output.hidden, false);
        assert.equal(view.output.selected, true);
        assert.match(view.output.value, /^Tytuł;Autor/);
        assert.match(view.status.textContent, /Ctrl\+C/);
    }
    {
        // Kilka tabel: panel nie kopiuje sam, lista wyboru ma podpisy (podpis tabeli, nagłówek sekcji, tytuł strony).
        const view = page({tables: doc => {
            const first = buildTable(doc, [record(doc, 'Pierwsza', 'Ola', 'Gotowe')]);
            first.prepend(doc.el('caption', {textContent: ' Teksty w toku '}));
            first.caption = first.children[0];
            const second = buildTable(doc, [record(doc, 'Druga', 'Ewa', 'Nowy')]);
            const third = buildTable(doc, [record(doc, 'Trzecia', 'Jan', 'Nowy')]);
            // Tabela w zwiniętej sekcji (<details> bez open) nie jest widoczna – nie ma jej na liście.
            const collapsed = buildTable(doc, [record(doc, 'Zwinięta', 'Ala', 'Nowy')]);
            return [first, doc.el('section', {}, doc.el('h2', {textContent: 'Archiwum'}), second),
                doc.el('details', {open: false}, doc.el('summary', {textContent: 'Inne'}), collapsed), third];
        }});
        view.toggle.click(); await flush();
        assert.deepEqual(view.copied, []);
        assert.deepEqual(view.select.children.map(option => option.textContent), ['Teksty w toku', 'Archiwum', 'Teksty – 3']);
        view.select.value = '1'; view.run.click(); await flush();
        assert.deepEqual(view.copied, ['Tytuł;Autor;Status;Akcje\r\nDruga;Ewa;Nowy;']);
        // Escape zamyka panel.
        view.doc.fire('keydown', {key: 'Escape'});
        assert.equal(view.panel.hidden, true);
        assert.equal(view.toggle.getAttribute('aria-expanded'), 'false');
    }
    {
        // Sortowanie i paginacja w przeglądarce: sortowanie przestawia wiersze w tabeli, CSV idzie za tą kolejnością
        // i obejmuje wiersze z dalszych stron (cms-page-hidden). Komórka z colspan w środku wiersza zajmuje kolumny
        // po swojej prawej (pusta wartość), a wiersz „Brak wyników” na całą szerokość jest pomijany.
        let body;
        const view = page({tables: doc => {
            const rows = ['A', 'B', 'C'].map(title => record(doc, title, 'Autor ' + title, 'Gotowe'));
            rows[2].className = 'cms-page-hidden';
            const merged = doc.el('tr', {}, doc.el('td', {}), doc.el('td', {colSpan: 2, textContent: 'Tytuł i autor razem'}),
                doc.el('td', {textContent: 'Nowy'}), doc.el('td', {className: 'hidden-column'}), doc.el('td', {}));
            const table = buildTable(doc, [...rows, merged, doc.el('tr', {}, doc.el('td', {colSpan: 6, textContent: 'Brak wyników'}))]);
            body = table.tBodies[0];
            return [table];
        }});
        [...body.rows].slice(0, 3).reverse().forEach(row => body.append(row));
        view.toggle.click(); await flush();
        assert.deepEqual(view.copied, [['Tytuł;Autor;Status;Akcje', 'Tytuł i autor razem;;Nowy;', 'C;Autor C;Gotowe;',
            'B;Autor B;Gotowe;', 'A;Autor A;Gotowe;'].join('\r\n')]);
    }
    {
        // Tabela bez wierszy danych: komunikat zamiast pustego CSV.
        const view = page({tables: doc => [buildTable(doc, [doc.el('tr', {}, doc.el('td', {colSpan: 6, textContent: 'Brak wyników'}))])]});
        view.toggle.click(); await flush();
        assert.deepEqual(view.copied, []);
        assert.equal(view.status.textContent, 'Tabela nie ma wierszy pasujących do bieżących filtrów.');
        // Strona bez widocznej tabeli.
        const empty = page({tables: () => []});
        empty.toggle.click(); await flush();
        assert.equal(empty.status.textContent, 'Na tej stronie nie ma widocznej tabeli.');
        assert.equal(empty.run.disabled, true);
    }
    console.log('table_csv: OK');
})().catch(error => { console.error(error); process.exitCode = 1; });
