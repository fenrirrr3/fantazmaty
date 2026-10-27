// Ikona CSV w nagłówku (superuser): cała widoczna tabela do schowka jako CSV – wszystkie strony, bieżące
// filtry i sortowanie, bez ukrytych kolumn. Wymaga table-data.js.
(() => {
    'use strict';
    const data = typeof module !== 'undefined' && module.exports ? require('./table-data.js') : globalThis.CmsTableData;
    const {excluded} = data;
    const SKIPPED = new Set(['SCRIPT', 'STYLE', 'TEMPLATE']);
    // Średnik: polski Excel rozdziela nim kolumny (przecinek to separator dziesiętny).
    const SEPARATOR = ';';
    // Tekst komórki: pola formularza dają wybraną wartość, przyciski w treści tabeli (akcje) – nic.
    function cellText(node, inBody) {
        if (node.nodeType === 3) return node.textContent;
        if (excluded(node) || SKIPPED.has(node.tagName) || (inBody && node.tagName === 'BUTTON')) return '';
        if (node.tagName === 'BR') return ' ';
        if (node.tagName === 'SELECT') return [...node.selectedOptions].map(option => option.textContent.trim()).join(', ');
        if (node.tagName === 'TEXTAREA') return node.value;
        if (node.tagName === 'INPUT') return ['checkbox', 'radio', 'hidden', 'button', 'submit', 'reset', 'file', 'image', 'password']
            .includes(String(node.type || 'text').toLowerCase()) ? '' : node.value;
        if (node.tagName === 'DETAILS' && !node.open) return node.querySelector('summary')?.textContent || '';
        return [...node.childNodes].map(child => cellText(child, inBody)).join(' ');
    }
    const clean = value => value.replace(/\s+/g, ' ').trim();
    // Komórki wiersza rozłożone na kolumny (colspan zajmuje kilka kolumn, tekst trafia do pierwszej).
    function slots(row, inBody) {
        return [...row.cells].flatMap(cell => {
            const text = excluded(cell) ? null : clean(cellText(cell, inBody));
            return [text, ...Array(Math.max(1, cell.colSpan || 1) - 1).fill(text === null ? null : '')];
        });
    }
    // Wiersze danych: bez odfiltrowanych, bez pustych formularzy, bez wierszy na całą szerokość
    // (szczegóły pod wierszem, „Brak wyników”). Wiersze ukryte lokalną paginacją zostają – kolejność jak w tabeli.
    function bodyRows(table, hidden) {
        const width = Math.max(0, ...[...(table.tHead?.rows || [])].map(row => slots(row, false).length));
        const rows = [];
        for (const body of table.tBodies) for (const row of body.rows) {
            if (excluded(row) || row.classList?.contains('empty-form') || row.classList?.contains('add-row')) continue;
            if (row.cells.length === 1 && (row.cells[0].colSpan || 1) > 1 && (row.cells[0].colSpan >= width)) continue;
            rows.push(slots(row, true).map((value, index) => hidden.has(index) ? null : value));
        }
        return rows;
    }
    function headerRow(table, hidden) {
        const rows = [...(table.tHead?.rows || [])];
        return rows.length ? slots(rows[rows.length - 1], false).map((value, index) => hidden.has(index) ? null : value) : null;
    }
    // Kolumny do CSV: widoczne (o ukryciu decyduje nagłówek), z nagłówkiem albo z jakąkolwiek wartością
    // (pomija np. kolumnę pól wyboru).
    function columns(header, rows) {
        const width = Math.max(header?.length || 0, ...rows.map(row => row.length));
        const hidden = index => header ? header[index] === null : rows.every(row => row[index] == null);
        return [...Array(width).keys()].filter(index => !hidden(index) && (header?.[index] || rows.some(row => row[index])));
    }
    function csvField(value) {
        const text = value ?? '';
        return /[";\r\n]/.test(text) ? `"${text.replaceAll('"', '""')}"` : text;
    }
    function toCsv(header, rows) {
        const kept = columns(header, rows);
        const lines = (header ? [header, ...rows] : rows).map(row => kept.map(index => csvField(row[index])).join(SEPARATOR));
        return {text: lines.join('\r\n'), rows: rows.length, columns: kept.length};
    }
    function tableCsv(table, hidden = data.hiddenColumns(table), extraRows = null) {
        return toCsv(headerRow(table, hidden), extraRows ?? bodyRows(table, hidden));
    }
    if (typeof module !== 'undefined' && module.exports) module.exports = {cellText, bodyRows, headerRow, toCsv, tableCsv, csvField};
    if (typeof document === 'undefined') return;
    document.addEventListener('DOMContentLoaded', () => {
        const box = document.querySelector('[data-table-csv]');
        if (!box) return;
        const toggle = box.querySelector('[data-table-csv-toggle]'), panel = box.querySelector('[data-header-panel]');
        const select = box.querySelector('[data-table-csv-table]'), run = box.querySelector('[data-table-csv-run]');
        const status = box.querySelector('[data-table-csv-status]'), output = box.querySelector('[data-table-csv-result]');
        let tables = [], busy = false;
        const progress = value => { status.textContent = value; };
        toggle.addEventListener('click', () => {
            panel.hidden = !panel.hidden; toggle.setAttribute('aria-expanded', String(!panel.hidden));
            if (panel.hidden || busy) return;
            data.closeOtherPanels(panel);
            tables = [...document.querySelectorAll('main table')].filter(table => table.getClientRects().length);
            select.replaceChildren();
            tables.forEach((table, index) => {
                const option = document.createElement('option'); option.value = String(index);
                option.textContent = data.tableLabel(table, index, tables.length);
                select.append(option);
            });
            run.disabled = !tables.length;
            progress(tables.length ? '' : 'Na tej stronie nie ma widocznej tabeli.');
            output.hidden = true; output.value = '';
            if (tables.length === 1) run.click();
        });
        document.addEventListener('keydown', event => { if (event.key === 'Escape') { panel.hidden = true; toggle.setAttribute('aria-expanded', 'false'); } });
        run.addEventListener('click', async () => {
            if (busy || !tables[Number(select.value)]) return;
            busy = true; run.disabled = select.disabled = true; output.hidden = true; output.value = '';
            const table = tables[Number(select.value)];
            try {
                const hidden = data.hiddenColumns(table);
                const rows = table.dataset.serverPaginated === 'true'
                    ? await data.serverPages(table, {marker: '[data-table-csv]', progress, label: 'Zbieram wiersze',
                        extract: nextTable => bodyRows(nextTable, hidden)})
                    : null;
                const csv = tableCsv(table, hidden, rows);
                if (!csv.rows) { progress('Tabela nie ma wierszy pasujących do bieżących filtrów.'); return; }
                await data.copyText(csv.text, output, progress, `Skopiowano CSV: wiersze ${csv.rows}, kolumny ${csv.columns}.`,
                    `CSV: wiersze ${csv.rows}, kolumny ${csv.columns}.`);
            } catch (error) { progress(error.message || 'Nie udało się skopiować tabeli.'); }
            finally { busy = false; run.disabled = select.disabled = false; }
        });
    });
})();
