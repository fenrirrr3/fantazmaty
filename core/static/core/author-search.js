// Wybór autora w formularzu zgłoszenia („Dodaj do recenzji”): podpowiedzi z serwera, wybór wypełnia dane autora,
// edycja tekstu albo „Wpisz nowego autora ręcznie” zrywa powiązanie. Wymaga lookup.js.
document.addEventListener('DOMContentLoaded', () => {
    document.querySelectorAll('select[data-author-search-url]').forEach(select => {
        const field = Lookup.combobox(select, {placeholder: 'Wpisz imię, nazwisko, pseudonim lub e-mail'});
        const {input, status, close} = field;
        let selectedIdentity = !!select.value;
        const manual = document.createElement('button'); manual.type = 'button';
        manual.className = 'secondary-button author-manual-button'; manual.textContent = 'Wpisz nowego autora ręcznie';
        select.before(manual);
        const resetIdentity = () => {
            if (selectedIdentity) ['author_first_name','author_last_name','author_pseudonym','email','phone_number'].forEach(name => { const target=select.form.elements.namedItem(name); if(target) {target.value='';target.dispatchEvent(new Event('input',{bubbles:true}));} });
            selectedIdentity=false; select.value='';
        };
        input.value = select.value ? select.selectedOptions[0].textContent : '';
        select.hidden = true; select.after(field.wrapper, status);
        const choose = item => {
            search.invalidate();
            if (![...select.options].some(o => o.value === String(item.id))) select.add(new Option(item.label, item.id));
            select.value = String(item.id); input.value = item.label; selectedIdentity=true; input.setCustomValidity('');
            for (const [name, value] of Object.entries({author_first_name:item.first_name, author_last_name:item.last_name, author_pseudonym:item.pseudonym || '', email:item.email, phone_number:item.phone_number})) {
                const target = select.form.elements.namedItem(name);
                if (target) { target.value = value || ''; target.dispatchEvent(new Event('input', {bubbles:true})); }
            }
            select.dispatchEvent(new Event('change', {bubbles:true})); close(); status.textContent = 'Wybrano autora.';
        };
        const search = Lookup.remote(field, select.dataset.authorSearchUrl, {
            failure: 'Nie udało się pobrać autorów. Wpisz ponownie lub uzupełnij dane ręcznie.',
            render: results => {
                field.show(results, item => `${item.label}${item.email ? ' · ' + item.email : ''}`, choose);
                status.textContent = results.length ? 'Wybierz autora z listy.' : 'Brak pasujących autorów.';
            },
        });
        manual.addEventListener('click', () => { search.invalidate(); resetIdentity(); input.value=''; input.setCustomValidity(''); close(); select.form.elements.namedItem('author_first_name')?.focus(); });
        input.addEventListener('input', () => {
            resetIdentity(); input.setCustomValidity(input.value.trim() ? 'Wybierz autora z sugestii albo przejdź do ręcznego wpisania danych.' : ''); select.dispatchEvent(new Event('change', {bubbles:true}));
            close(); search.later(500);
        });
    });
});
