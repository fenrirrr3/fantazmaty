// Server lists keep their full-result pagination; detail tables paginate independently.
document.addEventListener('DOMContentLoaded', () => {
    document.querySelectorAll('[data-page-size-url]').forEach(select => {
        select.addEventListener('change', () => location.assign(select.value));
    });
    const sizes = [25, 50, 100, 250, 500];
    document.querySelectorAll('main table').forEach((table, index) => {
        if (table.dataset.pagination === 'off' || table.dataset.serverPaginated === 'true' || table.id === 'result_list' || !table.tBodies.length) return;
        if (table.dataset.localPagination === 'ready') return;
        table.dataset.localPagination = 'ready';
        table.id ||= `cms-local-table-${index}`;
        const nav = document.createElement('nav');
        nav.className = 'cms-pagination';
        nav.setAttribute('aria-label', 'Stronicowanie: ' + (table.caption?.textContent.trim() || table.closest('section')?.querySelector('h2,h3')?.textContent.trim() || `tabela ${index + 1}`));
        const status = document.createElement('span');
        status.setAttribute('role', 'status'); status.setAttribute('aria-live', 'polite');
        const label = document.createElement('label'); label.append('Wyników na stronie');
        const select = document.createElement('select');
        select.setAttribute('aria-controls', table.id);
        sizes.forEach(size => { const option = document.createElement('option'); option.value = String(size); option.textContent = String(size); select.append(option); });
        label.append(select);
        const controls = document.createElement('div'); controls.className = 'cms-pagination-controls';
        let page = 1, size = 25, groups = [];
        const button = (text, action) => {
            const node = document.createElement('button'); node.type = 'button'; node.className = 'cms-page-button'; node.textContent = text;
            node.setAttribute('aria-controls', table.id);
            node.addEventListener('click', () => { page = action(); render(); }); controls.append(node); return node;
        };
        const first = button('Pierwsza', () => 1);
        const previous = button('Poprzednia', () => page - 1);
        const next = button('Następna', () => page + 1);
        const last = button('Ostatnia', () => Math.max(1, Math.ceil(groups.length / size)));
        nav.append(status, label, controls);
        (table.closest('.table-container') || table).after(nav);
        function collect() {
            groups = [];
            [...table.tBodies].forEach(body => {
                let group = null;
                [...body.rows].forEach(row => {
                    // Empty placeholders and the template for a new admin inline are not records.
                    if (row.classList.contains('empty-form') || row.classList.contains('add-row')) return;
                    const spanning = row.cells.length === 1 && row.cells[0].colSpan > 1;
                    if (spanning) { if (group) group.push(row); return; }
                    group = [row]; groups.push(group);
                });
            });
        }
        function render() {
            const total = groups.length, pages = Math.max(1, Math.ceil(total / size));
            page = Math.max(1, Math.min(page, pages));
            groups.forEach((group, i) => group.forEach(row => row.classList.toggle('cms-page-hidden', i < (page - 1) * size || i >= page * size)));
            status.textContent = `Strona ${page} z ${pages} · Wyniki ${total ? (page - 1) * size + 1 : 0}–${Math.min(page * size, total)} z ${total}`;
            first.disabled = previous.disabled = page === 1;
            next.disabled = last.disabled = page === pages;
        }
        select.addEventListener('change', () => { size = Number(select.value); page = 1; render(); });
        table.addEventListener('invalid', event => {
            const i = groups.findIndex(group => group.some(row => row.contains(event.target)));
            if (i >= 0) { page = Math.floor(i / size) + 1; render(); }
        }, true);
        // Sorting and adding inline rows changes DOM order; recompute without losing input values.
        const observer = new MutationObserver(() => { collect(); page = 1; render(); });
        [...table.tBodies].forEach(body => observer.observe(body, {childList: true}));
        collect(); render();
    });
});
