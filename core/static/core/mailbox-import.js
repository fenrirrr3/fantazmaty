"use strict";
// Pobieranie zgłoszeń ze skrzynki: pasek postępu na czas pobierania i pakowania załączników (do 90 s).
// Import działa w jednym żądaniu, więc pasek jest nieokreślony. Przy zapisie z paczką ZIP strona
// się nie przeładowuje – serwer odsyła ciasteczko z tokenem formularza i wtedy chowamy pasek.
document.addEventListener('DOMContentLoaded', () => {
    const COOKIE = 'mailbox_import_done';
    document.querySelectorAll('form[data-mailbox-import]').forEach(form => {
        const box = form.querySelector('[data-mailbox-progress-box]');
        if (!box) return;
        const label = box.querySelector('[data-mailbox-progress-label]');
        const bar = box.querySelector('progress');
        const tokenField = form.querySelector('input[name="download_token"]');
        let timer = null;
        function count() {
            // Formularz podglądu przekazuje wybrane wiadomości w polach ukrytych.
            const fields = [...form.querySelectorAll('input[name="uids"]')];
            const hidden = fields.filter(field => field.type === 'hidden').length;
            return hidden || fields.filter(field => field.checked).length;
        }
        function buttons(disabled) {
            form.querySelectorAll('button').forEach(button => { button.disabled = disabled; });
        }
        function finish(message) {
            clearInterval(timer); timer = null;
            buttons(false);
            bar.hidden = true;
            label.textContent = message;
            box.hidden = !message;
        }
        form.addEventListener('submit', event => {
            const submitter = event.submitter;
            if (event.defaultPrevented || !submitter || !submitter.matches('[data-mailbox-progress]')) return;
            const total = count();
            // „wiadomości” w dopełniaczu pasuje do każdej liczby.
            label.textContent = `Trwa pobieranie ${total} wiadomości i przygotowanie plików… To może potrwać do 90 sekund.`;
            bar.hidden = false; box.hidden = false;
            // Po wysłaniu: zablokowany przycisk nie trafiłby do danych formularza.
            setTimeout(() => buttons(true), 0);
            if (submitter.value === 'confirm' && tokenField) {
                const token = Math.random().toString(36).slice(2, 12) + Date.now().toString(36);
                tokenField.value = token;
                clearInterval(timer);
                timer = setInterval(() => {
                    if (!document.cookie.split('; ').includes(`${COOKIE}=${token}`)) return;
                    document.cookie = `${COOKIE}=; max-age=0; path=${location.pathname}`;
                    finish('Pobrano paczkę ZIP. Zgłoszenia zapisano.');
                }, 500);
            }
        });
        // Powrót przyciskiem „Wstecz” (bfcache) nie może zostawić zablokowanej strony.
        window.addEventListener('pageshow', () => finish(''));
    });
});
