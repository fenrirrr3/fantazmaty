document.querySelectorAll('[data-vocabulary]').forEach(input => {
    const endpoint = document.querySelector('[data-vocabulary-endpoint]')?.dataset.vocabularyEndpoint;
    if (!endpoint) return;
    const box = document.createElement('div');
    box.className = 'vocabulary-suggestions';
    box.setAttribute('aria-label', 'Podpowiedzi słownika');
    input.after(box);
    let timer, serial = 0;
    input.addEventListener('input', () => {
        clearTimeout(timer);
        const version = ++serial;
        timer = setTimeout(async () => {
            const parts = input.value.split(',');
            const query = parts.at(-1).trim();
            if (!query) { box.replaceChildren(); return; }
            const url = new URL(endpoint, window.location.origin);
            url.searchParams.set('kind', input.dataset.vocabulary);
            url.searchParams.set('q', query);
            try {
                const response = await fetch(url, {credentials: 'same-origin'});
                if (!response.ok) return;
                const data = await response.json();
                if (version !== serial) return;
                box.replaceChildren();
                data.results.forEach(name => {
                    const button = document.createElement('button');
                    button.type = 'button'; button.className = 'secondary-button small-button'; button.textContent = name;
                    button.addEventListener('click', () => {
                        const values = input.value.split(','); values[values.length - 1] = name;
                        input.value = values.map(value => value.trim()).filter(Boolean).join(', ');
                        ++serial; box.replaceChildren(); input.dispatchEvent(new Event('change', {bubbles: true})); input.focus();
                    });
                    box.append(button);
                });
            } catch (_) { box.replaceChildren(); }
        }, 200);
    });
});
