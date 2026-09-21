document.addEventListener('DOMContentLoaded', () => {
    document.querySelectorAll('select[data-author-search-url]').forEach(select => {
        const input = document.createElement('input');
        input.type = 'search'; input.autocomplete = 'off';
        input.id = `${select.id}_search`; input.placeholder = 'Wpisz imię, nazwisko, pseudonim lub e-mail';
        input.setAttribute('role', 'combobox'); input.setAttribute('aria-autocomplete', 'list');
        input.setAttribute('aria-expanded', 'false');
        const list = document.createElement('div');
        list.id = `${select.id}_suggestions`; list.className = 'author-suggestions';
        list.setAttribute('role', 'listbox'); input.setAttribute('aria-controls', list.id);
        const status = document.createElement('span'); status.setAttribute('role', 'status');
        [...select.labels].forEach(label => { label.htmlFor = input.id; });
        const multiple = select.multiple;
        let selectedIdentity = !multiple && !!select.value;
        const manual = document.createElement('button'); manual.type='button'; manual.textContent='Wpisz nowego autora ręcznie';
        if (!multiple) select.before(manual);
        const resetIdentity = () => {
            if (selectedIdentity) ['author_first_name','author_last_name','email','phone_number'].forEach(name => { const field=select.form.elements.namedItem(name); if(field) {field.value='';field.dispatchEvent(new Event('input',{bubbles:true}));} });
            selectedIdentity=false; select.value='';
        };
        manual.addEventListener('click', () => { ++sequence; clearTimeout(timer); controller?.abort(); resetIdentity(); input.value=''; input.setCustomValidity(''); close(); select.form.elements.namedItem('author_first_name')?.focus(); });
        input.value = !multiple && select.value ? select.selectedOptions[0].textContent : '';
        const selected = document.createElement('div'); selected.className = 'selected-authors';
        const wasRequired = select.required;
        if (multiple) { select.required = false; select.before(selected); }
        const renderSelected = () => {
            if (!multiple) return;
            selected.replaceChildren();
            [...select.selectedOptions].forEach(option => {
                const chip = document.createElement('span'); chip.className = 'selected-author';
                const label = document.createElement('span'); label.textContent = option.textContent;
                const remove = document.createElement('button'); remove.type = 'button'; remove.textContent = 'Usuń';
                remove.setAttribute('aria-label', `Usuń autora: ${option.textContent}`);
                remove.addEventListener('click', () => { option.selected = false; select.dispatchEvent(new Event('change', {bubbles:true})); renderSelected(); });
                chip.append(label, remove); selected.append(chip);
            });
            input.setCustomValidity(wasRequired && !select.selectedOptions.length ? 'Wybierz przynajmniej jednego autora.' : '');
        };
        renderSelected();
        select.hidden = true; select.after(input, list, status);
        let timer, controller, sequence = 0, active = -1;
        const close = () => { list.replaceChildren(); input.setAttribute('aria-expanded', 'false'); input.removeAttribute('aria-activedescendant'); active = -1; };
        const choose = item => {
            ++sequence; clearTimeout(timer); controller?.abort();
            let option = [...select.options].find(o => o.value === String(item.id));
            if (!option) { option = new Option(item.label, item.id); select.add(option); }
            if (multiple) {
                option.selected = true; input.value = ''; renderSelected();
                select.dispatchEvent(new Event('change', {bubbles:true})); close(); status.textContent = 'Dodano autora. Możesz wyszukać kolejną osobę.'; input.focus(); return;
            }
            select.value = String(item.id); input.value = item.label; selectedIdentity=true; input.setCustomValidity('');
            for (const [field, value] of Object.entries({author_first_name:item.first_name, author_last_name:item.last_name, email:item.email, phone_number:item.phone_number})) {
                const target = select.form.elements.namedItem(field);
                if (target) { target.value = value || ''; target.dispatchEvent(new Event('input', {bubbles:true})); }
            }
            select.dispatchEvent(new Event('change', {bubbles:true})); close(); status.textContent = 'Wybrano autora.';
        };
        const search = async () => {
            const number = ++sequence; controller?.abort(); controller = new AbortController();
            const q = input.value.trim(); close(); if (!q) { status.textContent = ''; return; }
            status.textContent = 'Szukam…';
            try {
                const url = new URL(select.dataset.authorSearchUrl, location.origin); url.searchParams.set('q', q);
                const response = await fetch(url, {credentials:'same-origin', signal:controller.signal, cache:'no-store'});
                if (!response.ok) throw new Error();
                const data = await response.json(); if (number !== sequence) return;
                data.results.forEach((item, i) => {
                    const button = document.createElement('button'); button.type = 'button';
                    button.id = `${list.id}_${i}`; button.setAttribute('role', 'option'); button.setAttribute('aria-selected','false');
                    button.textContent = `${item.label}${item.email ? ' · ' + item.email : ''}`;
                    button.addEventListener('click', () => choose(item)); list.append(button);
                });
                input.setAttribute('aria-expanded', String(!!data.results.length));
                status.textContent = data.results.length ? 'Wybierz autora z listy.' : 'Brak pasujących autorów.';
            } catch (error) {
                if (error.name !== 'AbortError' && number === sequence) status.textContent = 'Nie udało się pobrać autorów. Wpisz ponownie lub uzupełnij dane ręcznie.';
            }
        };
        input.addEventListener('input', () => {
            if (!multiple) { resetIdentity(); input.setCustomValidity(input.value.trim() ? 'Wybierz autora z sugestii albo przejdź do ręcznego wpisania danych.' : ''); select.dispatchEvent(new Event('change', {bubbles:true})); }
            clearTimeout(timer); ++sequence; controller?.abort(); close(); timer = setTimeout(search, 500);
        });
        input.addEventListener('keydown', event => {
            if (event.key === 'Escape') { ++sequence; controller?.abort(); clearTimeout(timer); close(); return; }
            if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
                event.preventDefault(); const choices = [...list.children]; if (!choices.length) return;
                active = (active + (event.key === 'ArrowDown' ? 1 : -1) + choices.length) % choices.length;
                choices.forEach((button, i) => button.setAttribute('aria-selected', String(i === active)));
                input.setAttribute('aria-activedescendant', choices[active].id); choices[active].scrollIntoView({block:'nearest'});
            }
            if (event.key === 'Enter') { event.preventDefault(); if (active >= 0) list.children[active].click(); else { clearTimeout(timer); search(); } }
        });
        document.addEventListener('click', event => { if (event.target !== input && !list.contains(event.target)) close(); });
    });
});
