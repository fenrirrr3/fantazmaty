document.addEventListener('DOMContentLoaded', () => {
    "use strict";
    // Enhance flat data tables only. Complex headers/row spans retain the table layout.
    document.querySelectorAll('main .table-container > table').forEach(table => {
        const headings = Array.from(table.tHead?.rows[0]?.cells || []);
        if (!headings.length || table.tHead.rows.length !== 1 || headings.some(cell => cell.colSpan !== 1 || cell.rowSpan !== 1)) return;
        if (Array.from(table.tBodies).some(body => Array.from(body.rows).some(row => Array.from(row.cells).some(cell => cell.rowSpan !== 1)))) return;
        const labels = headings.map(header => header.textContent.replace(/[↕▲▼]/g, '').trim() || (header.querySelector('input[type=checkbox]') ? 'Wybór' : 'Informacja'));
        table.classList.add('mobile-review-cards');
        Array.from(table.tBodies).forEach(body => Array.from(body.rows).forEach(row => {
            if (row.cells.length !== headings.length || Array.from(row.cells).some(cell => cell.colSpan !== 1)) return;
            Array.from(row.cells).forEach((cell, index) => { cell.dataset.label ||= labels[index]; });
        }));
        // Existing server-generated sorting forms already preserve the filters.
        const container = table.closest('.table-container');
        const card = container.closest('section, article');
        if (card?.querySelector('.mobile-table-sort')) return;
        const sortable = headings.map((header, index) => ({header, index, button: header.querySelector('button.table-sort-button, button.sort-button')})).filter(item => item.button);
        if (!sortable.length) return;
        const toolbar = document.createElement('div'); toolbar.className = 'mobile-table-sort';
        const label = document.createElement('label'); label.textContent = 'Sortowanie';
        const select = document.createElement('select');
        sortable.forEach(item => {
            const option = document.createElement('option'); option.value = item.index; option.textContent = labels[item.index];
            option.selected = item.header.hasAttribute('aria-sort'); select.append(option);
        });
        const button = document.createElement('button'); button.type = 'button'; button.className = 'secondary-button'; button.textContent = 'Sortuj';
        button.title = 'Kliknij ponownie, aby odwrócić kolejność';
        button.addEventListener('click', () => sortable.find(item => item.index === Number(select.value))?.button.click());
        label.append(select); toolbar.append(label, button);
        const shell = container.closest('.table-scroll-shell') || container; shell.before(toolbar);
    });
});
