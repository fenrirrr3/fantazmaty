document.addEventListener('DOMContentLoaded', () => {
    document.querySelectorAll('select[data-translation-search-url]').forEach(select => {
        const wrapper = document.createElement('span');
        wrapper.className = 'lookup-field';
        const input = document.createElement('input');
        input.type = 'search'; input.autocomplete = 'off';
        input.id = `${select.id}_search`;
        input.placeholder = 'Wpisz imię, nazwisko lub pseudonim';
        input.setAttribute('role', 'combobox');
        input.setAttribute('aria-autocomplete', 'list');
        input.setAttribute('aria-expanded', 'false');
        const list = document.createElement('span');
        list.id = `${select.id}_suggestions`; list.className = 'author-suggestions';
        list.setAttribute('role', 'listbox'); input.setAttribute('aria-controls', list.id);
        const selected = document.createElement('span'); selected.className = 'selected-authors';
        const status = document.createElement('span'); status.className = 'lookup-status';
        status.id = `${select.id}_status`; status.setAttribute('role', 'status');
        input.setAttribute('aria-describedby', [select.getAttribute('aria-describedby'), status.id].filter(Boolean).join(' '));
        [...select.labels].forEach(label => { label.htmlFor = input.id; });
        let timer, controller, sequence = 0, active = -1;
        const invalidate = () => { ++sequence; clearTimeout(timer); controller?.abort(); };
        const close = () => {
            list.replaceChildren(); active = -1;
            input.setAttribute('aria-expanded', 'false'); input.removeAttribute('aria-activedescendant');
        };
        const renderSelected = () => {
            selected.replaceChildren();
            [...select.selectedOptions].forEach(option => {
                const chip = document.createElement('span'); chip.className = 'selected-author';
                const label = document.createElement('span'); label.textContent = option.textContent;
                const remove = document.createElement('button'); remove.type = 'button'; remove.textContent = 'Usuń';
                remove.setAttribute('aria-label', `Usuń: ${option.textContent}`);
                remove.addEventListener('click', () => {
                    invalidate(); close(); option.selected = false;
                    select.dispatchEvent(new Event('change', {bubbles: true})); renderSelected(); input.focus();
                });
                chip.append(label, remove); selected.append(chip);
            });
        };
        const choose = item => {
            invalidate();
            let option = [...select.options].find(value => value.value === String(item.id));
            if (!option) { option = new Option(item.label, String(item.id)); select.add(option); }
            option.selected = true; input.value = ''; input.setCustomValidity('');
            renderSelected(); close();
            select.dispatchEvent(new Event('change', {bubbles: true}));
            status.textContent = 'Dodano osobę. Możesz wyszukać kolejną.'; input.focus();
        };
        const search = async () => {
            invalidate(); const number = sequence;
            const q = input.value.trim(); close();
            if (!q) { status.textContent = ''; return; }
            controller = new AbortController(); status.textContent = 'Szukam…';
            try {
                const url = new URL(select.dataset.translationSearchUrl, location.origin);
                url.searchParams.set('q', q);
                const response = await fetch(url, {credentials: 'same-origin', cache: 'no-store', signal: controller.signal});
                if (!response.ok) throw new Error();
                const data = await response.json(); if (number !== sequence) return;
                const selectedIds = new Set([...select.selectedOptions].map(option => option.value));
                const results = data.results.filter(item => !selectedIds.has(String(item.id)));
                results.forEach((item, i) => {
                    const button = document.createElement('button'); button.type = 'button';
                    button.id = `${list.id}_${i}`; button.setAttribute('role', 'option');
                    button.setAttribute('aria-selected', 'false'); button.textContent = item.label;
                    button.addEventListener('click', () => choose(item)); list.append(button);
                });
                input.setAttribute('aria-expanded', String(!!results.length));
                status.textContent = results.length ? 'Wybierz osobę z wyników.' : 'Brak nowych pasujących osób.';
            } catch (error) {
                if (error.name !== 'AbortError' && number === sequence) status.textContent = 'Nie udało się pobrać osób. Spróbuj ponownie.';
            }
        };
        input.addEventListener('input', () => {
            invalidate(); close(); status.textContent = '';
            input.setCustomValidity(input.value.trim() ? 'Wybierz osobę z wyników lub wyczyść wyszukiwanie.' : '');
            timer = setTimeout(search, 350);
        });
        input.addEventListener('keydown', event => {
            if (event.key === 'Escape') { invalidate(); close(); return; }
            if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
                event.preventDefault(); const choices = [...list.children]; if (!choices.length) return;
                active = (active + (event.key === 'ArrowDown' ? 1 : -1) + choices.length) % choices.length;
                choices.forEach((button, i) => button.setAttribute('aria-selected', String(i === active)));
                input.setAttribute('aria-activedescendant', choices[active].id); choices[active].scrollIntoView({block: 'nearest'});
            }
            if (event.key === 'Enter') { event.preventDefault(); if (active >= 0) list.children[active].click(); else search(); }
        });
        document.addEventListener('click', event => { if (!wrapper.contains(event.target)) { invalidate(); close(); } });
        wrapper.addEventListener('focusout', event => { if (!wrapper.contains(event.relatedTarget)) { invalidate(); close(); } });
        select.hidden = true; select.after(selected, wrapper, status); wrapper.append(input, list); renderSelected();
    });
});
