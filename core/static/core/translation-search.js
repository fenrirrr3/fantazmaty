// Wybór tłumaczy i autorów zagranicznych w szczegółach tekstu: podpowiedzi z serwera (bez osób już wybranych),
// wybrane osoby jako chipy z przyciskiem „Usuń”. Wymaga lookup.js.
document.addEventListener('DOMContentLoaded', () => {
    document.querySelectorAll('select[data-translation-search-url]').forEach(select => {
        const field = Lookup.combobox(select, {placeholder: 'Wpisz imię, nazwisko lub pseudonim', listTag: 'span'});
        const {input, status, wrapper, close} = field;
        const selected = document.createElement('span'); selected.className = 'selected-authors';
        status.id = `${select.id}_status`;
        input.setAttribute('aria-describedby', [select.getAttribute('aria-describedby'), status.id].filter(Boolean).join(' '));
        const renderSelected = () => {
            selected.replaceChildren();
            [...select.selectedOptions].forEach(option => {
                const chip = document.createElement('span'); chip.className = 'selected-author';
                const label = document.createElement('span'); label.textContent = option.textContent;
                const remove = document.createElement('button'); remove.type = 'button'; remove.textContent = 'Usuń';
                remove.setAttribute('aria-label', `Usuń: ${option.textContent}`);
                remove.addEventListener('click', () => {
                    search.invalidate(); close(); option.selected = false;
                    select.dispatchEvent(new Event('change', {bubbles: true})); renderSelected(); input.focus();
                });
                chip.append(label, remove); selected.append(chip);
            });
        };
        const choose = item => {
            search.invalidate();
            let option = [...select.options].find(value => value.value === String(item.id));
            if (!option) { option = new Option(item.label, String(item.id)); select.add(option); }
            option.selected = true; input.value = ''; input.setCustomValidity('');
            renderSelected(); close();
            select.dispatchEvent(new Event('change', {bubbles: true}));
            status.textContent = 'Dodano osobę. Możesz wyszukać kolejną.'; input.focus();
        };
        const search = Lookup.remote(field, select.dataset.translationSearchUrl, {
            failure: 'Nie udało się pobrać osób. Spróbuj ponownie.',
            render: found => {
                const selectedIds = new Set([...select.selectedOptions].map(option => option.value));
                const results = found.filter(item => !selectedIds.has(String(item.id)));
                field.show(results, item => item.label, choose);
                status.textContent = results.length ? 'Wybierz osobę z wyników.' : 'Brak nowych pasujących osób.';
            },
        });
        input.addEventListener('input', () => {
            close(); status.textContent = '';
            input.setCustomValidity(input.value.trim() ? 'Wybierz osobę z wyników lub wyczyść wyszukiwanie.' : '');
            search.later(350);
        });
        wrapper.addEventListener('focusout', event => { if (!wrapper.contains(event.relatedTarget)) search.cancel(); });
        select.hidden = true; select.after(selected, wrapper, status); renderSelected();
    });
});
