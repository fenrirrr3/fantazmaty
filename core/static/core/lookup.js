// Wspólne części pól wyszukiwania z listą podpowiedzi (autor, tłumacze, osoba, ilustratorzy): porównanie bez
// polskich znaków, pole combobox z listą opcji i komunikatem, obsługa strzałek i Enter oraz zapytania do serwera.
// Ładowany przed skryptem widżetu; udostępnia window.Lookup.
window.Lookup = (() => {
    "use strict";
    // Klucz porównania: bez wielkości liter i znaków diakrytycznych („Łukasz Żak” = „lukasz zak”).
    const fold = value => value.toLocaleLowerCase('pl').normalize('NFD').replace(/[̀-ͯ]/g, '').replace(/ł/g, 'l');

    // Pole combobox z listą opcji i komunikatem (role=status) zamiast selecta; etykiety selecta wskazują pole.
    // Wstawienie do strony (przed albo za selectem) należy do widżetu.
    const combobox = (select, {placeholder, listTag = 'div'}) => {
        const input = document.createElement('input');
        input.type = 'search'; input.autocomplete = 'off';
        input.id = `${select.id}_search`; input.placeholder = placeholder;
        input.setAttribute('role', 'combobox'); input.setAttribute('aria-autocomplete', 'list');
        input.setAttribute('aria-expanded', 'false');
        const list = document.createElement(listTag);
        list.id = `${select.id}_suggestions`; list.className = 'author-suggestions';
        list.setAttribute('role', 'listbox'); input.setAttribute('aria-controls', list.id);
        const status = document.createElement('span'); status.className = 'lookup-status'; status.setAttribute('role', 'status');
        const wrapper = document.createElement('span'); wrapper.className = 'lookup-field';
        wrapper.append(input, list);
        [...select.labels].forEach(label => { label.htmlFor = input.id; });
        let active = -1;
        const close = () => {
            list.replaceChildren(); active = -1;
            input.setAttribute('aria-expanded', 'false'); input.removeAttribute('aria-activedescendant');
        };
        // Opcje jako przyciski z tekstem (nigdy HTML); kliknięcie albo Enter wywołuje choose(item).
        const show = (items, label, choose) => {
            items.forEach((item, index) => {
                const button = document.createElement('button'); button.type = 'button';
                button.id = `${list.id}_${index}`; button.setAttribute('role', 'option');
                button.setAttribute('aria-selected', 'false'); button.textContent = label(item);
                button.addEventListener('click', () => choose(item)); list.append(button);
            });
            input.setAttribute('aria-expanded', String(!!list.children.length));
        };
        // Strzałka w górę lub w dół przesuwa aktywną opcję (z zawijaniem); false – lista jest pusta.
        const move = key => {
            const choices = [...list.children]; if (!choices.length) return false;
            active = (active + (key === 'ArrowDown' ? 1 : -1) + choices.length) % choices.length;
            choices.forEach((button, index) => button.setAttribute('aria-selected', String(index === active)));
            input.setAttribute('aria-activedescendant', choices[active].id); choices[active].scrollIntoView({block: 'nearest'});
            return true;
        };
        // Enter: wybór aktywnej opcji; false – żadna nie jest aktywna.
        const pick = () => {
            if (active < 0) return false;
            list.children[active].click(); return true;
        };
        // Kliknięcie poza polem i listą.
        const onOutside = callback => document.addEventListener('click', event => { if (!wrapper.contains(event.target)) callback(); });
        return {input, list, status, wrapper, close, show, move, pick, onOutside};
    };

    // Podpowiedzi z serwera (adres url, parametr q) dla pola z combobox(): zapytanie po przerwie w pisaniu, przerwanie
    // poprzedniego, pominięcie spóźnionych odpowiedzi; Escape i kliknięcie obok przerywają też trwające zapytanie.
    const remote = (field, url, {render, failure}) => {
        const {input, status, close} = field;
        let timer, controller, sequence = 0;
        const invalidate = () => { ++sequence; clearTimeout(timer); controller?.abort(); };
        const search = async () => {
            invalidate(); const number = sequence;
            const q = input.value.trim(); close();
            if (!q) { status.textContent = ''; return; }
            controller = new AbortController(); status.textContent = 'Szukam…';
            try {
                const address = new URL(url, location.origin); address.searchParams.set('q', q);
                const response = await fetch(address, {credentials: 'same-origin', cache: 'no-store', signal: controller.signal});
                if (!response.ok) throw new Error();
                const data = await response.json(); if (number !== sequence) return;
                render(data.results);
            } catch (error) {
                if (error.name !== 'AbortError' && number === sequence) status.textContent = failure;
            }
        };
        // Przerwane wyszukiwanie nie może zostawić „Szukam…” w komunikacie.
        const cancel = () => { invalidate(); close(); if (status.textContent === 'Szukam…') status.textContent = ''; };
        const later = delay => { invalidate(); timer = setTimeout(search, delay); };
        input.addEventListener('keydown', event => {
            if (event.key === 'Escape') { cancel(); return; }
            if (event.key === 'ArrowDown' || event.key === 'ArrowUp') { event.preventDefault(); field.move(event.key); }
            if (event.key === 'Enter') { event.preventDefault(); if (!field.pick()) search(); }
        });
        field.onOutside(cancel);
        return {search, cancel, invalidate, later};
    };

    return {fold, combobox, remote};
})();
