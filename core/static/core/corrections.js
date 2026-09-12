document.addEventListener('DOMContentLoaded', () => {
    const text = document.querySelector('select[data-texts-url]');
    if (!text) return;
    const form = text.form;
    const anthology = form.elements.namedItem('anthology');
    let loadNumber = 0, busy = false;
    const message = document.createElement('p'); message.setAttribute('role', 'status'); form.prepend(message);
    const load = async () => {
        const number = ++loadNumber;
        text.replaceChildren(new Option('Wczytywanie…', '')); text.disabled = true;
        try {
            const url = new URL(text.dataset.textsUrl, location.origin); url.searchParams.set('anthology', anthology.value);
            const response = await fetch(url, {credentials:'same-origin'});
            if (!response.ok) throw new Error();
            const data = await response.json();
            if (number !== loadNumber) return;
            text.replaceChildren(new Option('Inne miejsce', ''));
            data.texts.forEach(item => text.add(new Option(item.title, item.id)));
            message.textContent = '';
        } catch (_) {
            if (number === loadNumber) { text.replaceChildren(new Option('Nie udało się wczytać tekstów', '')); message.textContent = 'Wybierz antologię ponownie, aby spróbować jeszcze raz.'; }
        } finally { if (number === loadNumber) text.disabled = false; }
    };
    anthology.addEventListener('change', load);
    if (!form.hasAttribute('data-correction-form')) return;
    form.addEventListener('submit', async event => {
        if (event.submitter?.value !== 'continue') return;
        event.preventDefault();
        if (busy) return;
        busy = true;
        const data = new FormData(form);
        const enabled = [...form.elements].filter(field => !field.disabled);
        enabled.forEach(field => { field.disabled = true; });
        message.textContent = 'Zapisywanie…';
        try {
            const response = await fetch(form.getAttribute('action') || window.location.pathname, {method:'POST', body:data, credentials:'same-origin', headers:{'X-Requested-With':'XMLHttpRequest'}});
            const result = await response.json();
            if (!response.ok || !result.ok) {
                message.textContent = Object.entries(result.errors || {}).map(([field, errors]) => {
                    const labels = {text:'Opowiadanie', anthology:'Antologia', fragment:'Fragment', problem:'Co jest źle', suggestion:'Propozycja poprawki', __all__:'Formularz'};
                    return `${labels[field] || field}: ${errors.join(' ')}`;
                }).join(' ') || 'Nie udało się zapisać uwagi.';
                return;
            }
            ['fragment', 'problem', 'suggestion'].forEach(name => { form.elements.namedItem(name).value = ''; });
            form.elements.namedItem('submission_token').value = result.next_token;
            message.textContent = `${result.message} Zapisano uwagę nr ${result.id}. `;
            const link = document.createElement('a'); link.href = result.list_url; link.textContent = 'Odśwież listę zapisanych uwag'; message.append(link);
            enabled.forEach(field => { field.disabled = false; });
            document.dispatchEvent(new CustomEvent('cms:form-saved', {detail:{form}}));
            form.elements.namedItem('fragment').focus();
        } catch (_) { message.textContent = 'Nie otrzymano potwierdzenia zapisu. Dane pozostają w formularzu; możesz ponowić zapis.'; }
        finally { busy = false; enabled.forEach(field => { field.disabled = false; }); }
    });
});
