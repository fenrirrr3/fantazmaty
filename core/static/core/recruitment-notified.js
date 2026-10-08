document.addEventListener('change', async event => {
    const field = event.target.closest?.('[data-recruitment-notified]');
    if (!field || field.disabled) return;
    const state = field.closest('.recruitment-notified').querySelector('[role="status"]');
    const previous = field.dataset.value;
    const value = field.value;
    const mailbox = field.closest('[data-recruitment-samples]');
    let data;
    let url = field.dataset.url;
    if (url) {
        data = new FormData();
        data.set('csrfmiddlewaretoken', document.querySelector('[name="csrfmiddlewaretoken"]').value);
        data.set('version', field.dataset.version);
    } else {
        data = new FormData(mailbox);
        url = mailbox.getAttribute('action') || location.href;
        data.set('action', 'set_notified');
        data.delete('selected'); data.append('selected', field.dataset.mailUid);
        data.delete('decision'); data.delete('cursor'); data.delete('preview_uid');
    }
    data.set('notified', value);
    field.disabled = true; state.textContent = 'Zapisywanie…';
    try {
        const response = await fetch(url, {method: 'POST', body: data, credentials: 'same-origin', headers: {'Accept': 'application/json'}});
        if (response.redirected || !response.headers.get('content-type')?.includes('application/json')) throw Error('Nie zapisano. Odśwież stronę i sprawdź sesję.');
        const result = await response.json();
        if (!response.ok) throw Error(result.error || 'Nie zapisano zmiany. Odśwież stronę.');
        field.value = field.dataset.value = result.notified ? 'yes' : 'no';
        field.dataset.url = result.url; field.dataset.version = result.version;
        const cell = field.closest('td');
        cell.dataset.copyValue = cell.dataset.sortValue = result.notified ? 'Tak' : 'Nie';
        const row = field.closest('tr');
        const linkCell = row.querySelector('[data-recruitment-record]');
        if (linkCell) {
            const link = document.createElement('a'); link.href = result.record_url;
            link.textContent = 'Podgląd'; link.className = 'secondary-button';
            linkCell.replaceChildren(link);
            row.querySelector('[data-recruitment-stored]').textContent = 'W bazie';
        }
        const decisionCell = row.querySelector('.recruitment-decision-cell');
        if (decisionCell) {
            const label = document.createElement('span'); label.textContent = result.decision;
            label.className = ['accepted','rejected'].includes(result.status) ? 'recruitment-decision ' + result.status : 'muted-text';
            decisionCell.replaceChildren(label);
        }
        state.textContent = 'Zapisano';
    } catch (error) {
        field.value = previous;
        state.textContent = error.message || 'Nie zapisano zmiany. Spróbuj ponownie.';
    } finally { field.disabled = false; }
});
