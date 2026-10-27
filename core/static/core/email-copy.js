// Koperta w nagłówku: adresy e-mail z widocznej tabeli (wszystkie strony, bieżące filtry). Wymaga table-data.js.
(() => {
    'use strict';
    const data = typeof module !== 'undefined' && module.exports ? require('./table-data.js') : globalThis.CmsTableData;
    const {excluded, visibleText} = data;
    const addressPattern = /[\p{L}\p{N}.!#$%&'*+\-/=?^_`{|}~]+@[\p{L}\p{N}](?:[\p{L}\p{N}.-]*[\p{L}\p{N}])?\.[\p{L}]{2,}/gu;
    function addresses(value) { return String(value || '').match(addressPattern) || []; }
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
    async function collectServerEmails(options) {
        return uniqueEmails(await data.collectServerPages({label: 'Zbieram adresy', ...options}));
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
            data.closeOtherPanels(panel);
            tables = [...document.querySelectorAll('main table')].filter(table => table.getClientRects().length &&
                (tableEmails(table).length || /e-?mail|nadawca/i.test(table.tHead?.textContent || '')));
            select.replaceChildren();
            tables.forEach((table, index) => {
                const option = document.createElement('option'); option.value = String(index);
                option.textContent = data.tableLabel(table, index, tables.length);
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
                    const result = await response.json();
                    if (!Array.isArray(result.emails)) throw Error('Nieprawidłowa odpowiedź serwera.');
                    emails = uniqueEmails(result.emails);
                } else if (table.dataset.serverPaginated === 'true') {
                    const hidden = data.hiddenColumns(table);
                    emails = uniqueEmails(await data.serverPages(table, {marker: '[data-email-copy]', progress, label: 'Zbieram adresy',
                        extract: nextTable => tableEmails(nextTable, hidden)}));
                } else emails = uniqueEmails(tableEmails(table));
                if (!emails.length) { progress('Brak adresów pasujących do bieżących filtrów.'); return; }
                await data.copyText(emails.join('; '), output, progress, `Skopiowano adresy: ${emails.length}.`, `Adresy: ${emails.length}.`);
            } catch (error) { progress(error.message || 'Nie udało się skopiować adresów.'); }
            finally { busy = false; run.disabled = select.disabled = false; }
        });
    });
})();
