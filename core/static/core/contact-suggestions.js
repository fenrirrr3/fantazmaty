/* Coordinator-only endpoint; textContent avoids interpreting contact data as HTML. */
/* Hint mode (people without an account): lists existing entries to prevent duplicates and typos. */
document.querySelectorAll('[data-contact-mode="hint"]').forEach(input => {
    const form = input.closest('form');
    const group = input.dataset.contactGroup;
    const fields = [...form.querySelectorAll(`[data-contact-group="${group}"][data-contact-mode="hint"]`)];
    if (fields[0] !== input) return;
    const box = document.createElement('p'); box.className = 'contact-suggestions muted-text';
    box.setAttribute('aria-live', 'polite'); fields[fields.length - 1].closest('.form-field, div').after(box);
    let timer, version = 0;
    const kind = () => {
        const select = form.querySelector('[data-contact-kind-select]');
        return select && select.value === 'cover' ? 'cover' : 'person';
    };
    async function hint() {
        const current = ++version;
        const query = fields.map(field => field.value.trim()).filter(Boolean).join(' ');
        if (query.length < 2) { box.replaceChildren(); return; }
        const url = new URL(form.dataset.contactEndpoint, location.origin);
        url.searchParams.set('kind', kind()); url.searchParams.set('q', query);
        try {
            const response = await fetch(url, {credentials: 'same-origin'});
            if (!response.ok || current !== version) return;
            const data = await response.json();
            if (current !== version) return;
            box.textContent = data.results.length
                ? 'Podobne wpisy w bazie: ' + data.results.map(item => item.name + (item.note ? ` (${item.note})` : '')).join(', ')
                  + '. Jeśli to ta sama osoba, wybierz ją w sekcji zadania zamiast dodawać nową.'
                : '';
        } catch { box.replaceChildren(); }
    }
    const schedule = () => { clearTimeout(timer); timer = setTimeout(hint, 250); };
    fields.forEach(field => field.addEventListener('input', schedule));
    const select = form.querySelector('[data-contact-kind-select]');
    if (select) select.addEventListener('change', schedule);
});
