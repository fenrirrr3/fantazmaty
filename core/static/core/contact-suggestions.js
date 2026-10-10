/* Coordinator-only endpoint; textContent avoids interpreting contact data as HTML. */
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
