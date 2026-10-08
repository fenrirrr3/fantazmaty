document.addEventListener('DOMContentLoaded', () => {
    document.addEventListener('click', event => {
        const opener = event.target.closest('[data-dialog-open]');
        const dialog = opener && document.getElementById(opener.dataset.dialogOpen);
        if (dialog?.showModal) { event.preventDefault(); dialog.showModal(); return; }
        const close = event.target.closest('[data-dialog-close]');
        const parent = close?.closest('dialog');
        if (parent) { event.preventDefault(); parent.close(); }
    });
    document.querySelectorAll('dialog[data-dialog-auto-open]').forEach(dialog => {
        if (dialog.showModal) { dialog.removeAttribute('open'); dialog.showModal(); }
    });
});
