document.addEventListener('DOMContentLoaded', () => {
    const main = document.querySelector('main');
    if (!main) return;
    main.querySelectorAll('input[name="start_date"]').forEach(start => {
        const end = start.form?.elements.namedItem('end_date');
        if (!end || end.type !== 'date') return;
        const todayMinimum = end.min;
        const update = () => { end.min = start.value > todayMinimum ? start.value : todayMinimum; };
        start.addEventListener('change', update); update();
    });
    // The existing ui.js owns the beforeunload guard.
    main.querySelectorAll('form[method="post"]').forEach(form => {
        if (form.querySelector('textarea, input:not([type="hidden"]):not([type="checkbox"]), select'))
            form.setAttribute('data-warn-unsaved', '');
    });
    main.querySelectorAll('table').forEach(table => {
        const container = table.closest('.table-container') || table;
        const footer = document.createElement('div');
        footer.className = 'table-bottom-controls';
        container.after(footer);
        const heads = [...(table.tHead?.rows[0]?.cells || [])];
        const titleIndex = heads.findIndex(th => /tytuł|^tekst$|opowiadanie/i.test(th.textContent.trim()));
        if (titleIndex >= 0) [...table.rows].forEach(row => row.cells[titleIndex]?.classList.add('sticky-title'));
        // A server-paginated list already has a page-size form; auxiliary tables still paginate locally.
        const serverPaged = main.querySelector('.page-size-form') && table === main.querySelector('table');
        const rows = [...(table.tBodies[0]?.rows || [])];
        if (serverPaged || !rows.length) footer.remove();
        if (!serverPaged && rows.length) {
            let page = 0;
            const nav = document.createElement('div'); nav.className = 'pagination-links';
            const prev = document.createElement('button'), next = document.createElement('button'), label = document.createElement('span');
            prev.type = next.type = 'button'; prev.textContent = 'Poprzednia'; next.textContent = 'Następna';
            const draw = () => {
                [...table.tBodies[0].rows].forEach((row, i) => { row.hidden = Math.floor(i / 25) !== page; });
                label.textContent = `Strona ${page + 1} z ${Math.ceil(rows.length / 25)}`;
                prev.disabled = page === 0; next.disabled = (page + 1) * 25 >= rows.length;
            };
            prev.onclick = () => { page--; draw(); }; next.onclick = () => { page++; draw(); };
            nav.append(prev, label, next); footer.append(nav); draw();
            new MutationObserver(draw).observe(table.tBodies[0], {childList:true});
        }
    });
    main.addEventListener('change', event => {
        if (!event.target.matches('[data-select-table]')) return;
        event.target.closest('table').querySelectorAll('tbody input[name="selected"]').forEach(input => {
            if (!input.closest('tr').hidden) input.checked = event.target.checked;
        });
    });
    main.querySelectorAll('form[data-remember-filters]').forEach(form => {
        const params = new URLSearchParams(new FormData(form));
        const chips = document.createElement('div'); chips.className = 'active-filters'; chips.setAttribute('aria-label', 'Aktywne filtry');
        for (const [name, value] of params) {
            if (!value || ['page', 'page_size', 'sort', 'old_reviews', 'filters_applied', 'filters'].includes(name)) continue;
            const fields = [...form.elements].filter(field => field.name === name);
            if (!fields.length) continue;
            const field = fields.find(f => f.value === value) || fields[0];
            if (field.type === 'hidden') continue;
            let description = field instanceof HTMLSelectElement
                ? [...field.options].find(o => o.value === value)?.textContent : null;
            description ||= field.type === 'checkbox' ? field.labels?.[0]?.textContent.trim() : value;
            const link = document.createElement('a'); link.className = 'filter-chip';
            const next = new URLSearchParams(params);
            next.delete(name); params.getAll(name).filter(v => v !== value).forEach(v => next.append(name, v)); next.delete('page');
            next.set('filters_applied', '1');
            link.href = `${location.pathname}?${next}`; link.textContent = `${description} ×`; link.setAttribute('aria-label', `Usuń filtr: ${description}`);
            chips.append(link);
        }
        if (chips.children.length) {
            const clear = document.createElement('a');
            const cleared = new URLSearchParams({filters_applied:'1', hide_ready:'0'});
            if (params.has('old_reviews')) cleared.set('old_reviews', params.get('old_reviews'));
            clear.href = `${location.pathname}?${cleared}`;
            clear.textContent = 'Wyczyść wszystkie'; clear.dataset.clearSavedFilters = form.dataset.rememberFilters;
            chips.append(clear); form.after(chips);
        }
        const empty = main.querySelector('.empty-results-panel');
        if (empty && !form.querySelector('.field-error, .errorlist, [aria-invalid="true"]') && main.querySelector('table tbody tr td[colspan]')) {
            const strong = empty.querySelector('strong');
            if (strong) strong.textContent = chips.children.length ? 'Brak wyników dla wybranych filtrów.' : 'Nie ma jeszcze pozycji na tej liście.';
        }
    });
    const success = main.querySelector('.message.success');
    if (success) {
        let saved; try { saved = JSON.parse(sessionStorage.getItem('fantazmaty:last-form') || 'null'); } catch (_) {}
        if (saved && Date.now() - saved.time < 15000) {
            const target = [...main.querySelectorAll('form')].find(f => f.action === saved.action);
            if (target) { const message = success.cloneNode(true); message.classList.add('inline-save-message'); target.prepend(message); }
        }
        try { sessionStorage.removeItem('fantazmaty:last-form'); } catch (_) {}
    }
    main.addEventListener('submit', event => {
        if (event.target.method === 'post') try { sessionStorage.setItem('fantazmaty:last-form', JSON.stringify({action:event.target.action,time:Date.now()})); } catch (_) {}
    });
});

// Native selects remain usable without JS; with JS use checkbox dropdowns.
document.addEventListener('DOMContentLoaded', () => {
    document.querySelectorAll('form[method="get"] select[multiple]').forEach(select => {
        const dropdown = document.createElement('details');
        dropdown.className = 'checkbox-dropdown'; dropdown.dataset.checkboxDropdown = '';
        dropdown.dataset.emptyLabel = 'Wszystkie statusy';
        const summary = document.createElement('summary');
        const summaryText = document.createElement('span'); summaryText.dataset.checkboxSummary = '';
        summaryText.textContent = 'Wszystkie statusy'; summary.append(summaryText);
        const options = document.createElement('div'); options.className = 'checkbox-dropdown-options';
        options.setAttribute('role', 'group'); options.setAttribute('aria-label', select.labels?.[0]?.textContent.trim() || 'Statusy');
        const clear = document.createElement('button'); clear.type = 'button'; clear.textContent = 'Wyczyść wybór'; clear.dataset.checkboxClear = '';
        options.append(clear);
        [...select.options].filter(option => option.value).forEach(option => {
            const label = document.createElement('label'); label.className = 'multi-filter-option';
            const input = document.createElement('input'); input.type = 'checkbox'; input.name = select.name;
            input.value = option.value; input.checked = option.selected;
            const text = document.createElement('span'); text.textContent = option.textContent.trim();
            label.append(input, text); options.append(label);
        });
        dropdown.append(summary, options); select.replaceWith(dropdown);
    });
    document.querySelectorAll('form[method="get"].filters-form, form[method="get"][data-remember-filters]').forEach(form => {
        let timer;
        const key = `fantazmaty:filter-focus:${location.pathname}`;
        const submit = field => {
            clearTimeout(timer);
            if (!form.checkValidity()) return;
            const page = form.elements.namedItem('page'); if (page) page.value = '1';
            if (!form.elements.namedItem('filters_applied')) {
                const applied = document.createElement('input'); applied.type = 'hidden'; applied.name = 'filters_applied'; applied.value = '1'; form.append(applied);
            }
            try { sessionStorage.setItem(key, JSON.stringify({name:field?.name, value:field?.value,
                position:field?.selectionStart, open:!!field?.closest('details[open]'), time:Date.now()})); } catch (_) {}
            form.requestSubmit();
        };
        form.addEventListener('change', event => {
            if (event.target.matches('[data-auto-submit]')) return;
            clearTimeout(timer); timer = setTimeout(() => submit(event.target), 200);
        });
        form.addEventListener('input', event => {
            if (!event.target.matches('input[type="search"], input[type="text"], input:not([type])')) return;
            clearTimeout(timer); timer = setTimeout(() => submit(event.target), 450);
        });
        form.addEventListener('click', event => {
            if (event.target.closest('[data-checkbox-clear]')) {
                clearTimeout(timer); timer = setTimeout(() => submit(event.target.closest('details').querySelector('input')), 200);
            }
        });
        try {
            const saved = JSON.parse(sessionStorage.getItem(key) || 'null');
            if (saved && Date.now() - saved.time < 15000) {
                const field = [...form.elements].find(f => f.name === saved.name && (f.type !== 'checkbox' || f.value === saved.value));
                if (field) {
                    if (saved.open) field.closest('details').open = true;
                    field.focus({preventScroll:true});
                    if (typeof saved.position === 'number' && field.setSelectionRange) field.setSelectionRange(saved.position, saved.position);
                }
                sessionStorage.removeItem(key);
            }
        } catch (_) {}
    });
});
