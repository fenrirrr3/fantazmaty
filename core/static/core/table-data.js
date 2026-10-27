// Wspólne narzędzia kopiowania danych z tabel strony (koperta – email-copy.js, CSV – table-csv.js):
// widoczność wierszy i kolumn, tekst komórek, pobieranie wszystkich stron listy stronicowanej na serwerze.
(() => {
    'use strict';
    // Wiersze ukryte przez lokalną paginację (klasa cms-page-hidden) należą do danych; filtr i ukryte kolumny – nie.
    function excluded(node) {
        return Boolean(node.closest?.('[hidden], .hidden-column, .visually-hidden, [aria-hidden="true"], [data-filtered-out="true"]'))
            || node.style?.display === 'none';
    }
    function visibleText(node) {
        if (node.nodeType === 3) return node.textContent;
        if (excluded(node) || ['SCRIPT', 'STYLE', 'TEXTAREA', 'INPUT', 'SELECT'].includes(node.tagName)) return '';
        if (node.tagName === 'DETAILS' && !node.open) return node.querySelector('summary')?.textContent || '';
        return [...node.childNodes].map(visibleText).join(' ');
    }
    // Numery kolumn ukrytych w bieżącym widoku (pobrane strony nie mają stylów – liczy się widok użytkownika).
    function hiddenColumns(table, style = globalThis.getComputedStyle) {
        return new Set([...(table.tHead?.rows[0]?.cells || [])]
            .flatMap((cell, i) => excluded(cell) || style?.(cell)?.display === 'none' ? [i] : []));
    }
    function paginationFor(table, root) {
        return [...root.querySelectorAll('[data-sort-config]')].find(nav =>
            Boolean(table.compareDocumentPosition(nav) & 4));
    }
    // Wszystkie strony listy po 500 pozycji, z bieżącymi filtrami i sortowaniem z adresu strony.
    // Zmiana liczby wyników albo stron w trakcie przerywa pobieranie – nigdy nie zwracamy części listy.
    async function collectServerPages({url, pageParam, sizeParam, loadPage, progress, label = 'Zbieram dane'}) {
        const target = new URL(url); target.hash = '';
        target.searchParams.set(sizeParam, '500');
        const collected = []; let pages = 1, count = null;
        for (let page = 1; page <= pages; page++) {
            target.searchParams.set(pageParam, String(page));
            progress(`${label}: strona ${page}${page > 1 ? ' z ' + pages : ''}…`);
            const result = await loadPage(target.toString());
            if (result.page !== page || !Number.isSafeInteger(result.pages) || result.pages < 1 ||
                !Number.isSafeInteger(result.count) || result.count < 0 || (count !== null && (count !== result.count || pages !== result.pages))) {
                throw Error('Lista zmieniła się podczas pobierania. Odśwież tabelę i spróbuj ponownie.');
            }
            pages = result.pages; count = result.count;
            collected.push(...result.items);
        }
        return collected;
    }
    // Kolejne strony tabeli stronicowanej na serwerze: extract(tabela) daje pozycje jednej strony.
    // Nagłówek X-CMS-Table-Copy: pobieranie nie zmienia zapamiętanego rozmiaru strony (core/pagination.py).
    function serverPages(table, {marker, extract, progress, label}) {
        const nav = paginationFor(table, document);
        if (!nav?.dataset.sizeParam) throw Error('Brakuje informacji o paginacji. Odśwież stronę.');
        const index = [...document.querySelectorAll('main table')].indexOf(table);
        return collectServerPages({url: location.href, pageParam: nav.dataset.pageParam, sizeParam: nav.dataset.sizeParam,
            progress, label, loadPage: async url => {
                const response = await fetch(url, {credentials: 'same-origin', cache: 'no-store', headers: {'X-CMS-Table-Copy': '1'}});
                if (!response.ok || response.redirected) throw Error('Nie udało się pobrać całej listy. Odśwież stronę i spróbuj ponownie.');
                const doc = new DOMParser().parseFromString(await response.text(), 'text/html');
                if (!doc.querySelector(marker)) throw Error('Sesja wygasła lub zmieniły się uprawnienia. Zaloguj się ponownie.');
                const nextTable = doc.querySelectorAll('main table')[index];
                if (!nextTable) throw Error('Nie znaleziono tabeli na kolejnej stronie.');
                const nextNav = paginationFor(nextTable, doc), input = nextNav?.querySelector('[data-server-page]');
                return {items: extract(nextTable), page: Number(input?.value), pages: Number(input?.max), count: Number(nextNav?.dataset.resultCount)};
            }});
    }
    // Podpis tabeli w liście wyboru panelu.
    function tableLabel(table, index, count) {
        return table.caption?.textContent.trim() || table.closest('section')?.querySelector('h2,h3')?.textContent.trim() ||
            `${document.querySelector('h1')?.textContent.trim() || 'Tabela'}${count > 1 ? ' – ' + (index + 1) : ''}`;
    }
    // Schowek; gdy przeglądarka odmawia (po pobieraniu w tle potrzebny bywa nowy gest), tekst zaznaczony w polu.
    async function copyText(text, output, progress, done, manual) {
        try { await navigator.clipboard.writeText(text); progress(done); }
        catch (_) {
            output.value = text; output.hidden = false; output.focus(); output.select();
            progress(`${manual} Naciśnij Ctrl+C (na Macu ⌘C), aby skopiować zaznaczony tekst.`);
        }
    }
    // Otwarcie jednego panelu nagłówka zamyka pozostałe (wyszukiwarka, koperta, CSV).
    function closeOtherPanels(current) {
        const search = document.querySelector('[data-header-search]');
        if (search) search.open = false;
        document.querySelectorAll('[data-header-panel]').forEach(panel => {
            if (panel === current || panel.hidden) return;
            panel.hidden = true;
            document.querySelector(`[aria-controls="${panel.id}"]`)?.setAttribute('aria-expanded', 'false');
        });
    }
    const api = {excluded, visibleText, hiddenColumns, paginationFor, collectServerPages, serverPages, tableLabel, copyText, closeOtherPanels};
    if (typeof module !== 'undefined' && module.exports) module.exports = api;
    else globalThis.CmsTableData = api;
})();
