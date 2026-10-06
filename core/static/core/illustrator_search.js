(() => {
    const input = document.getElementById('illustrator-search');
    const source = document.getElementById('id_illustrators');
    const results = document.getElementById('illustrator-search-results');
    const selected = document.getElementById('illustrator-selected');
    const counter = document.getElementById('illustrator-search-count');
    if (!input || !source || !results || !selected || !counter) return;
    const normalize = value => value.normalize('NFD').replace(/[\u0300-\u036f]/g, '').replace(/ł/g, 'l').replace(/Ł/g, 'L').toLowerCase();
    const people = Array.from(source.querySelectorAll('input[type="checkbox"]')).map(box => ({
        box, name: box.closest('label').textContent.trim(),
    }));
    const change = (person, checked) => {
        person.box.checked = checked;
        person.box.dispatchEvent(new Event('change', {bubbles: true}));
        input.value = '';
        renderSelected();
        renderResults();
        input.focus();
    };
    const renderSelected = () => {
        selected.replaceChildren();
        people.filter(person => person.box.checked).forEach(person => {
            const row = document.createElement('li');
            const name = document.createElement('span');
            name.textContent = person.name;
            const remove = document.createElement('button');
            remove.type = 'button'; remove.className = 'secondary-button';
            remove.textContent = 'Usuń';
            remove.setAttribute('aria-label', `Usuń z wyboru: ${person.name}`);
            remove.addEventListener('click', () => change(person, false));
            row.append(name, remove); selected.append(row);
        });
        if (!selected.children.length) {
            const empty = document.createElement('li');
            empty.className = 'muted-text'; empty.textContent = 'Nie wybrano ilustratora.';
            selected.append(empty);
        }
    };
    const renderResults = () => {
        results.replaceChildren(); results.hidden = true;
        const query = normalize(input.value).trim();
        if (query.length < 2) {
            counter.textContent = 'Wpisz co najmniej 2 znaki, aby wyszukać kolejną osobę.';
            return;
        }
        const tokens = query.split(/\s+/);
        const matching = people.filter(person => !person.box.checked && tokens.every(token => normalize(person.name).includes(token)));
        counter.textContent = matching.length ? `Znaleziono: ${matching.length}. Wybierz „Dodaj” przy właściwej osobie.${matching.length > 10 ? ' Pokazano pierwsze 10 wyników; doprecyzuj wyszukiwanie.' : ''}` : 'Brak pasujących osób. Wybrane osoby są już na liście wybranych.';
        matching.slice(0, 10).forEach(person => {
            const row = document.createElement('li');
            const button = document.createElement('button');
            button.type = 'button'; button.className = 'secondary-button';
            button.textContent = `Dodaj: ${person.name}`;
            button.addEventListener('click', () => change(person, true));
            row.append(button); results.append(row);
        });
        results.hidden = !matching.length;
    };
    input.addEventListener('input', renderResults);
    input.addEventListener('keydown', event => {
        if (event.key === 'Enter' || event.key === 'ArrowDown') {
            event.preventDefault();
            results.querySelector('button')?.focus();
        } else if (event.key === 'Escape') {
            input.value = ''; renderResults();
        }
    });
    source.closest('form')?.addEventListener('reset', () => {
        // Checkbox defaults are restored after the reset event.
        setTimeout(() => { input.value = ''; renderSelected(); renderResults(); }, 0);
    });
    renderSelected(); renderResults();
})();
