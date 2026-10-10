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

document.querySelectorAll('[data-contact-kind]').forEach(input => {
    const form = input.closest('form');
    const group = input.dataset.contactGroup;
    const field = input.dataset.contactField;
    const partner = key => form.querySelector(`[data-contact-group="${group}"][data-contact-field="${key}"]`);
    const box = document.createElement('div'); box.className = 'contact-suggestions';
    box.setAttribute('aria-live', 'polite'); input.after(box);
    let timer;
    form.contactVersions ||= {};
    const nextVersion = () => (form.contactVersions[group] = (form.contactVersions[group] || 0) + 1);
    const apply = item => {
        nextVersion();
        partner('name').value = item.name; partner('email').value = item.email;
        const id = partner('id'); if (id) id.value = item.id || '';
        form.querySelectorAll('.contact-suggestions').forEach(node => node.replaceChildren());
        input.dispatchEvent(new Event('change', {bubbles: true}));
    };
    async function suggest(auto = false) {
        const version = nextVersion(), query = input.value.trim();
        if (query.length < 2) { box.replaceChildren(); return; }
        const url = new URL(form.dataset.contactEndpoint, location.origin);
        url.searchParams.set('kind', input.dataset.contactKind); url.searchParams.set('q', query);
        try {
            const response = await fetch(url, {credentials: 'same-origin'});
            if (!response.ok) return;
            const data = await response.json();
            if (version !== form.contactVersions[group] || input.value.trim() !== query) return;
            const exact = data.results.filter(item => item[field].toLocaleLowerCase('pl') === query.toLocaleLowerCase('pl'));
            if (auto && exact.length === 1) { apply(exact[0]); return; }
            box.replaceChildren();
            data.results.forEach(item => {
                const button = document.createElement('button'); button.type = 'button'; button.className = 'secondary-button small-button';
                button.textContent = `${item.name}${item.email ? ' · ' + item.email : ''}${item.id ? ' (#' + item.id + ')' : ''}`;
                button.addEventListener('click', () => apply(item)); box.append(button);
            });
        } catch { box.replaceChildren(); }
    }
    input.addEventListener('input', () => {
        nextVersion(); clearTimeout(timer);
        const id = partner('id'); if (id) id.value = '';
        timer = setTimeout(() => suggest(), 200);
    });
    input.addEventListener('blur', () => { clearTimeout(timer); suggest(true); });
});
