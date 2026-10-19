// Admin skrzynek: przy przeznaczeniu „Wysyłka plików autorom” widać tylko sekcję SMTP, przy pozostałych – tylko IMAP.
document.addEventListener('DOMContentLoaded', () => {
    const purpose = document.querySelector('select[name="purpose"]');
    const imap = document.querySelector('fieldset.mailbox-imap');
    const smtp = document.querySelector('fieldset.mailbox-smtp');
    if (!purpose || !imap || !smtp) return;
    const update = () => {
        const sender = purpose.value === 'split_mailing';
        imap.hidden = sender;
        smtp.hidden = !sender;
    };
    purpose.addEventListener('change', update);
    update();
});
