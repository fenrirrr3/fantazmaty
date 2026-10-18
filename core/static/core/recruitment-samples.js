document.addEventListener('DOMContentLoaded', () => {
    // Ten sam podgląd treści maila w rekrutacji i przy pobieraniu zgłoszeń do recenzji.
    const form = document.querySelector('[data-recruitment-samples], [data-mailbox-import][data-preview-field]');
    if (!form) return;
    const field = (form.dataset && form.dataset.previewField) || 'selected';
    const boxSelector = `input[name="${field}"]`;
    const panel = form.querySelector('[data-mail-preview]');
    const state = panel.querySelector('[data-preview-state]');
    const subject = panel.querySelector('[data-preview-subject]');
    const sender = panel.querySelector('[data-preview-sender]');
    const body = panel.querySelector('[data-preview-body]');
    const truncated = panel.querySelector('[data-preview-truncated]');
    let controller = null, serial = 0, previewTimer = null;
    function clear(message) {
        state.textContent = message; subject.textContent = sender.textContent = body.textContent = '';
        truncated.hidden = true;
    }
    async function preview(uid) {
        clearTimeout(previewTimer);
        controller?.abort(); controller = new AbortController();
        const revision = ++serial;
        clear('Pobieram treść wiadomości…'); panel.setAttribute('aria-busy', 'true');
        const data = new FormData(form); data.set('action', 'preview'); data.set('uid', uid);
        data.delete(field); data.delete('cursor'); data.delete('preview_uid');
        try {
            const response = await fetch(form.getAttribute('action') || location.href, {method: 'POST', body: data,
                credentials: 'same-origin', signal: controller.signal});
            if (response.redirected) throw Error('Sesja wygasła. Odśwież stronę.');
            const html = await response.text();
            const result = new DOMParser().parseFromString(html, 'text/html').querySelector('[data-preview-result]');
            if (!result) throw Error('Nie udało się pobrać wiadomości. Odśwież stronę i spróbuj ponownie.');
            const text = name => result.querySelector(`[data-preview-${name}]`).textContent;
            if (!response.ok) throw Error(text('state') || 'Nie udało się pobrać wiadomości.');
            if (revision !== serial) return;
            // Only text is copied; mail markup and remote resources are never inserted.
            subject.textContent = text('subject'); sender.textContent = text('sender');
            body.textContent = text('body') || 'Wiadomość nie zawiera czytelnej treści tekstowej.';
            truncated.hidden = result.querySelector('[data-preview-truncated]').hidden; state.textContent = '';
        } catch (error) {
            if (revision === serial && error.name !== 'AbortError') clear(error.message || 'Nie udało się pobrać treści.');
        } finally { if (revision === serial) panel.setAttribute('aria-busy', 'false'); }
    }
    form.addEventListener('click', event => {
        const button = event.target.closest('[data-preview-uid]');
        if (button) { event.preventDefault(); preview(button.dataset.previewUid); }
    });
    form.addEventListener('change', event => {
        if (!event.target.matches(`${boxSelector}, [data-select-table]`)) return;
        clearTimeout(previewTimer);
        if (event.target.matches('[data-select-table]')) {
            controller?.abort(); ++serial;
            clear('Kliknij temat wiadomości.'); panel.setAttribute('aria-busy', 'false');
            return;
        }
        previewTimer = setTimeout(() => {
            const checked = event.target.matches(boxSelector) && event.target.checked ? event.target : form.querySelector(`${boxSelector}:checked`);
            if (checked) preview(checked.value);
            else { controller?.abort(); ++serial; clear('Kliknij temat wiadomości.'); panel.setAttribute('aria-busy', 'false'); }
        }, 80);
    });
    const roles = form.querySelector('[data-recruitment-roles]');
    if (!roles) return;
    const boxes = () => [...roles.querySelectorAll('input[name="roles"]')];
    roles.addEventListener('change', event => {
        // „Wszystkie” wyklucza pojedyncze role; pusty wybór wraca do „Wszystkie”.
        const target = event.target;
        if (target.checked) boxes().forEach(box => { if ((box.value === 'all') !== (target.value === 'all')) box.checked = false; });
        if (!boxes().some(box => box.checked)) boxes().forEach(box => { box.checked = box.value === 'all'; });
        clearTimeout(previewTimer);
        controller?.abort(); ++serial;
        clear('Zmieniono role. Pobierz nagłówki, aby odświeżyć listę.'); panel.setAttribute('aria-busy', 'false');
    });
});
