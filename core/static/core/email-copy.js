(() => {
    'use strict';
    const addressPattern = /[\p{L}\p{N}.!#$%&'*+\-/=?^_`{|}~]+@[\p{L}\p{N}](?:[\p{L}\p{N}.-]*[\p{L}\p{N}])?\.[\p{L}]{2,}/gu;
    function addresses(value) { return String(value || '').match(addressPattern) || []; }
    function excluded(node) {
        // Local pagination hides otherwise eligible records with cms-page-hidden.
        // Explicit filtering/hidden content must still be excluded.
        return Boolean(node.closest('[hidden], .hidden-column, .visually-hidden, [aria-hidden="true"], [data-filtered-out="true"]')) || node.style?.display === 'none';
    }
    function visibleText(node) {
        if (node.nodeType === 3) return node.textContent;
        if (excluded(node) || ['SCRIPT', 'STYLE', 'TEXTAREA', 'INPUT', 'SELECT'].includes(node.tagName)) return '';
        if (node.tagName === 'DETAILS' && !node.open) return node.querySelector('summary')?.textContent || '';
        return [...node.childNodes].map(visibleText).join(' ');
    }
    function tableEmails(table, hiddenColumns = new Set()) {
        const found = [];
        for (const body of table.tBodies) for (const row of body.rows) {
            if (excluded(row)) continue;
            for (const [index, cell] of [...row.cells].entries()) {
                if (hiddenColumns.has(index) || excluded(cell)) continue;
                found.push(...addresses(visibleText(cell)));
            }
        }
        return found;
    }
    function uniqueEmails(values) {
        const unique = new Map();
        values.forEach(value => { const key = value.toLocaleLowerCase(); if (!unique.has(key)) unique.set(key, value); });
        return [...unique.values()];
    }
    function paginationFor(table, root) {
        return [...root.querySelectorAll('[data-sort-config]')].find(nav =>
            Boolean(table.compareDocumentPosition(nav) & 4));
    }
    async function collectServerEmails({url, pageParam, sizeParam, loadPage, progress}) {
        const target = new URL(url); target.hash = '';
        target.searchParams.set(sizeParam, '500');
        const collected = []; let pages = 1, count = null;
        for (let page = 1; page <= pages; page++) {
            target.searchParams.set(pageParam, String(page));
            progress(`Zbieram adresy: strona ${page}${page > 1 ? ' z ' + pages : ''}…`);
            const result = await loadPage(target.toString());
            if (result.page !== page || !Number.isSafeInteger(result.pages) || result.pages < 1 ||
                !Number.isSafeInteger(result.count) || result.count < 0 || (count !== null && (count !== result.count || pages !== result.pages))) {
                throw Error('Lista zmieniła się podczas pobierania. Odśwież tabelę i spróbuj ponownie.');
            }
            pages = result.pages; count = result.count;
            collected.push(...result.emails);
        }
        return uniqueEmails(collected);
    }
    if (typeof module !== 'undefined' && module.exports) module.exports = {addresses, tableEmails, uniqueEmails, collectServerEmails};
    if (typeof document === 'undefined') return;
    document.addEventListener('DOMContentLoaded', () => {
        const box = document.querySelector('[data-email-copy]');
        if (!box) return;
        const toggle = box.querySelector('[data-email-copy-toggle]'), panel = box.querySelector('.email-copy-panel');
        const select = box.querySelector('[data-email-copy-table]'), run = box.querySelector('[data-email-copy-run]');
        const status = box.querySelector('[data-email-copy-status]'), output = box.querySelector('[data-email-copy-result]');
        let tables = [], busy = false;
        const progress = value => { status.textContent = value; };
        toggle.addEventListener('click', () => {
            panel.hidden = !panel.hidden; toggle.setAttribute('aria-expanded', String(!panel.hidden));
            if (panel.hidden || busy) return;
            const search = document.querySelector('[data-header-search]');
            if (search) search.open = false;
            tables = [...document.querySelectorAll('main table')].filter(table => table.getClientRects().length &&
                (tableEmails(table).length || /e-?mail|nadawca/i.test(table.tHead?.textContent || '')));
            select.replaceChildren();
            tables.forEach((table, index) => {
                const option = document.createElement('option'); option.value = String(index);
                option.textContent = table.caption?.textContent.trim() || table.closest('section')?.querySelector('h2,h3')?.textContent.trim() ||
                    `${document.querySelector('h1')?.textContent.trim() || 'Tabela'}${tables.length > 1 ? ' – ' + (index + 1) : ''}`;
                select.append(option);
            });
            run.disabled = !tables.length;
            progress(tables.length ? '' : 'Na tej stronie nie ma widocznej tabeli z adresami e-mail.');
            output.hidden = true; output.value = '';
            if (tables.length === 1) run.click();
        });
        document.addEventListener('keydown', event => { if (event.key === 'Escape') { panel.hidden = true; toggle.setAttribute('aria-expanded', 'false'); } });
        run.addEventListener('click', async () => {
            if (busy || !tables[Number(select.value)]) return;
            busy = true; run.disabled = select.disabled = true; output.hidden = true; output.value = '';
            const table = tables[Number(select.value)];
            try {
                let emails;
                if (table.dataset.emailMailbox) {
                    const senderColumn = [...(table.tHead?.rows[0]?.cells || [])].find(cell => /nadawca/i.test(cell.textContent));
                    if (senderColumn && (excluded(senderColumn) || getComputedStyle(senderColumn).display === 'none')) {
                        progress('Kolumna nadawców jest ukryta. Odkryj ją, aby skopiować adresy.');
                        return;
                    }
                    const form = table.closest('form'), payload = new FormData(form);
                    payload.set('action', 'copy_emails'); payload.delete('uids'); payload.delete('selected'); payload.delete('cursor');
                    progress('Zbieram adresy z wszystkich pasujących wiadomości…');
                    const response = await fetch(location.href, {method: 'POST', body: payload, credentials: 'same-origin'});
                    if (!response.ok || response.redirected) throw Error('Nie udało się pobrać całej listy. Odśwież nagłówki i spróbuj ponownie.');
                    const data = await response.json();
                    if (!Array.isArray(data.emails)) throw Error('Nieprawidłowa odpowiedź serwera.');
                    emails = uniqueEmails(data.emails);
                } else if (table.dataset.serverPaginated === 'true') {
                    const nav = paginationFor(table, document);
                    if (!nav?.dataset.sizeParam) throw Error('Brakuje informacji o paginacji. Odśwież stronę.');
                    const index = [...document.querySelectorAll('main table')].indexOf(table);
                    const hiddenColumns = new Set([...(table.tHead?.rows[0]?.cells || [])]
                        .flatMap((cell, i) => excluded(cell) || getComputedStyle(cell).display === 'none' ? [i] : []));
                    emails = await collectServerEmails({url: location.href, pageParam: nav.dataset.pageParam,
                        sizeParam: nav.dataset.sizeParam, progress, loadPage: async url => {
                            const response = await fetch(url, {credentials: 'same-origin', cache: 'no-store', headers: {'X-CMS-Email-Copy': '1'}});
                            if (!response.ok || response.redirected) throw Error('Nie udało się pobrać całej listy. Odśwież stronę i spróbuj ponownie.');
                            const doc = new DOMParser().parseFromString(await response.text(), 'text/html');
                            if (!doc.querySelector('[data-email-copy]')) throw Error('Sesja wygasła lub zmieniły się uprawnienia. Zaloguj się ponownie.');
                            const nextTable = doc.querySelectorAll('main table')[index];
                            if (!nextTable) throw Error('Nie znaleziono tabeli na kolejnej stronie.');
                            const nextNav = paginationFor(nextTable, doc), input = nextNav?.querySelector('[data-server-page]');
                            return {emails: tableEmails(nextTable, hiddenColumns), page: Number(input?.value), pages: Number(input?.max), count: Number(nextNav?.dataset.resultCount)};
                        }});
                } else emails = uniqueEmails(tableEmails(table));
                if (!emails.length) { progress('Brak adresów pasujących do bieżących filtrów.'); return; }
                const text = emails.join('; ');
                try { await navigator.clipboard.writeText(text); progress(`Skopiowano adresy: ${emails.length}.`); }
                catch (_) {
                    // Some browsers require a new gesture after asynchronous retrieval.
                    output.value = text; output.hidden = false; output.focus(); output.select();
                    progress(`Adresy: ${emails.length}. Naciśnij Ctrl+C (na Macu ⌘C), aby skopiować zaznaczoną listę.`);
                }
            } catch (error) { progress(error.message || 'Nie udało się skopiować adresów.'); }
            finally { busy = false; run.disabled = select.disabled = false; }
        });
    });
})();
