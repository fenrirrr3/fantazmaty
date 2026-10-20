"use strict";
(() => {
    document.querySelectorAll('.program-tool form[data-program-job]').forEach((form, index) => {
        const box = document.createElement('div');
        box.className = 'program-progress'; box.hidden = true;
        const label = document.createElement('p'); label.setAttribute('role', 'status');
        label.setAttribute('aria-live', 'polite');
        const bar = document.createElement('progress'); bar.max = 100;
        bar.setAttribute('aria-label', 'Postęp aktualnego etapu');
        const stop = document.createElement('button'); stop.type = 'button';
        stop.className = 'secondary-button'; stop.textContent = 'Zatrzymaj';
        const accept = document.createElement('button'); accept.type = 'button';
        accept.className = 'primary-button'; accept.textContent = 'Akceptuję i kontynuuję'; accept.hidden = true;
        const download = document.createElement('a'); download.className = 'primary-button';
        download.textContent = 'Pobierz wynik'; download.hidden = true;
        // Po podziale DOCX: przejście do wysyłki plików autorom (adres podaje serwer).
        const mail = document.createElement('a'); mail.className = 'secondary-button';
        mail.textContent = 'Wyślij autorom'; mail.hidden = true;
        const notes = document.createElement('div'); notes.className = 'form-warning'; notes.hidden = true;
        const summary = document.createElement('div'); summary.className = 'program-summary'; summary.hidden = true;
        // Wyraźny komunikat o obrazach usuniętych z dokumentu przed przetwarzaniem.
        const images = document.createElement('p'); images.className = 'message warning program-images';
        images.setAttribute('role', 'status'); images.hidden = true;
        box.append(label, images, bar, stop, accept, download, mail, notes, summary);
        const submit = form.querySelector('button[type="submit"]');
        if (!submit) return;
        // Pasek postępu stoi pod plikiem i przyciskiem (miejsce oznaczone w szablonie).
        const slot = form.querySelector('[data-program-progress]');
        if (slot && slot.append) slot.append(box); else submit.before(box);
        const csrf = form.querySelector('[name="csrfmiddlewaretoken"]').value;
        const storageKey = `program-job:${location.pathname}:${index}`;
        let url = null, busy = false, timer = null, stopRequested = false, downloaded = false, keepsResult = false;
        function remember(value) {
            try { if (value) sessionStorage.setItem(storageKey, value); else sessionStorage.removeItem(storageKey); } catch (_) { /* Storage is optional. */ }
        }
        function lock(active) {
            busy = active;
            form.querySelectorAll('button[type="submit"]').forEach(button => { button.disabled = active; });
        }
        function finish(message) {
            clearTimeout(timer); lock(false); remember(null);
            notes.hidden = true; summary.hidden = true; images.hidden = true;
            bar.hidden = true; stop.hidden = true; accept.hidden = true;
            label.textContent = message;
        }
        async function request(target, options = {}) {
            const response = await fetch(target, {credentials: 'same-origin', cache: 'no-store', ...options});
            if (!(response.headers.get('content-type') || '').includes('application/json')) {
                throw new Error('Nie można odczytać odpowiedzi. Sprawdź połączenie i czy nadal jesteś zalogowany.');
            }
            const data = await response.json();
            if (!response.ok) {
                const error = new Error(data.message || Object.values(data.errors || {}).flat().join(' ') || 'Nie udało się uruchomić programu.');
                error.status = response.status; throw error;
            }
            return data;
        }
        async function action(name) {
            return request(url, {method: 'POST', headers: {'X-CSRFToken': csrf}, body: new URLSearchParams({action: name})});
        }
        function imagesWord(count) {
            const last = count % 10, lastTwo = count % 100;
            if (count === 1) return 'obraz';
            return last >= 2 && last <= 4 && (lastTwo < 12 || lastTwo > 14) ? 'obrazy' : 'obrazów';
        }
        function showImages(count) {
            if (!Number.isInteger(count) || count <= 0) return;
            images.textContent = `Usunięto ${count} ${imagesWord(count)} z dokumentu – wynik ich nie zawiera.`;
            images.hidden = false;
        }
        function replacementItems(value) {
            // Zamiany słownikowe {source, target, count}; uszkodzone wpisy są pomijane.
            if (!Array.isArray(value)) return [];
            return value.filter(item => item && typeof item.source === 'string' && item.source
                && typeof item.target === 'string' && Number.isFinite(item.count) && item.count > 0);
        }
        function showSummary(rows, replacements) {
            // Podsumowanie Odkurzacza: reguła i liczba zmian (tylko reguły, które coś zmieniły)
            // oraz lista zamian słownikowych „źródło → wynik (liczba)”.
            summary.replaceChildren();
            const title = document.createElement('strong'); title.textContent = 'Podsumowanie zmian';
            summary.append(title);
            if (rows === null) {
                // Brak tabeli reguł – tylko lista zamian.
            } else if (!rows.length) {
                const empty = document.createElement('p'); empty.textContent = 'Reguły korekty nie wprowadziły zmian.';
                summary.append(empty);
            } else {
                const table = document.createElement('table'); table.className = 'data-table';
                const head = document.createElement('tr');
                ['Reguła', 'Zmiany'].forEach(text => { const cell = document.createElement('th'); cell.textContent = text; head.append(cell); });
                table.append(head);
                rows.forEach(row => {
                    const line = document.createElement('tr');
                    [row.label, String(row.count)].forEach(text => { const cell = document.createElement('td'); cell.textContent = text; line.append(cell); });
                    table.append(line);
                });
                summary.append(table);
            }
            if (replacements.length) {
                const heading = document.createElement('p'); heading.className = 'program-replacements-title';
                heading.textContent = 'Zamiany słownikowe:';
                const list = document.createElement('ul'); list.className = 'program-replacements';
                replacements.forEach(item => {
                    const entry = document.createElement('li');
                    entry.textContent = `${item.source} → ${item.target} (${item.count})`;
                    list.append(entry);
                });
                summary.append(heading, list);
            }
            summary.hidden = false;
        }
        async function poll() {
            try {
                const data = await request(url);
                if (data.state === 'done') {
                    const warnings = Array.isArray(data.warnings) ? data.warnings : [];
                    finish(warnings.length ? 'Gotowe. Program zgłosił uwagi – są wypisane poniżej.' : 'Gotowe. Plik jest pobierany.');
                    bar.hidden = false; bar.value = 100;
                    if (warnings.length) {
                        notes.replaceChildren();
                        const title = document.createElement('strong'); title.textContent = 'Uwagi programu';
                        const list = document.createElement('ul');
                        warnings.forEach(text => { const item = document.createElement('li'); item.textContent = text; list.append(item); });
                        notes.append(title, list); notes.hidden = false;
                    }
                    showImages(data.removed_images);
                    const replacements = replacementItems(data.replacements);
                    if (Array.isArray(data.summary) || replacements.length) {
                        showSummary(Array.isArray(data.summary) ? data.summary : null, replacements);
                    }
                    download.href = `${url}?download=1`; download.hidden = false;
                    keepsResult = typeof data.mail_url === 'string' && /^\/(?!\/)/.test(data.mail_url);
                    if (keepsResult) { mail.href = data.mail_url; mail.hidden = false; }
                    if (!downloaded) { downloaded = true; download.click(); }
                    return;
                }
                if (data.state === 'error' || data.state === 'cancelled') { finish(data.message); return; }
                if (data.state === 'confirmation') {
                    bar.hidden = true; accept.hidden = false; accept.disabled = false;
                    stop.hidden = false; stop.disabled = false;
                    label.textContent = data.message + ' Dokument jest zachowany przez 30 minut; nie przesyłaj go ponownie.';
                    return;
                }
                bar.hidden = false;
                let message = data.stage || 'Przetwarzanie';
                if (Number.isFinite(data.completed) && data.total > 0) {
                    bar.value = Math.floor(100 * data.completed / data.total);
                    message += `: ${bar.value}%`;
                } else { bar.removeAttribute('value'); }
                label.textContent = stopRequested ? 'Zatrzymywanie pracy…' : message;
                timer = setTimeout(poll, 800);
            } catch (error) {
                if (error.status === 404) { finish(error.message); return; }
                label.textContent = `${error.message} Ponawiam sprawdzanie zadania…`;
                timer = setTimeout(poll, 3000);
            }
        }
        download.addEventListener('click', () => {
            // Serwer usuwa wynik po pobraniu, więc drugi raz nie da się go pobrać.
            setTimeout(() => {
                if (keepsResult) {
                    label.textContent = 'Plik pobrany. Archiwum zostaje na serwerze do 30 minut – możesz wysłać pliki autorom.';
                    return;
                }
                download.hidden = true;
                label.textContent = 'Plik pobrany. Kopia na serwerze została usunięta.';
            }, 1500);
        });
        stop.addEventListener('click', async () => {
            stopRequested = true; stop.disabled = true; accept.hidden = true;
            label.textContent = 'Zatrzymywanie pracy…';
            if (!url) return; // Send cancellation as soon as the upload is acknowledged.
            try { await action('cancel'); clearTimeout(timer); poll(); }
            catch (error) { label.textContent = error.message; stop.disabled = false; stopRequested = false; }
        });
        accept.addEventListener('click', async () => {
            accept.disabled = true;
            try {
                const data = await action('confirm');
                if (data.url) { url = data.url; remember(url); }
                accept.hidden = true; bar.hidden = false; poll();
            }
            catch (error) { label.textContent = error.message; accept.disabled = false; }
        });
        form.addEventListener('submit', async event => {
            event.preventDefault();
            if (busy) return;
            const body = new FormData(form);
            if (event.submitter?.name) body.set(event.submitter.name, event.submitter.value);
            lock(true); url = null; stopRequested = false; downloaded = false; keepsResult = false;
            box.hidden = false; bar.hidden = false; bar.removeAttribute('value');
            stop.hidden = false; stop.disabled = false; download.hidden = true; mail.hidden = true; accept.hidden = true;
            notes.hidden = true; summary.hidden = true; images.hidden = true;
            label.textContent = 'Przesyłanie pliku i uruchamianie programu…';
            try {
                const data = await request(form.action, {method: 'POST', headers: {'X-Program-Job': '1', 'X-CSRFToken': csrf}, body});
                url = data.url; remember(url);
                if (stopRequested) await action('cancel');
                poll();
            } catch (error) { finish(error.message); }
        });
        try { url = sessionStorage.getItem(storageKey); } catch (_) { /* Storage is optional. */ }
        if (url && url.startsWith(location.pathname + 'zadania/')) {
            lock(true); box.hidden = false; (form.closest('details.program-tool') || form.closest('details')).open = true; poll();
        } else { url = null; }
    });
})();
