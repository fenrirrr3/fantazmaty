// Shared controls for server-paginated lists, including admin changelists.
// No new forms: paginators may be inside an existing bulk-action form.
document.addEventListener('DOMContentLoaded', () => {
    document.querySelectorAll('[data-page-size-url]').forEach(select => {
        select.addEventListener('change', () => location.assign(select.value));
    });
    document.querySelectorAll('[data-server-page]').forEach(input => {
        const current = input.value;
        let navigating = false;
        function jump() {
            const raw = input.value.trim();
            if (!/^[0-9]+$/.test(raw) || !Number.isSafeInteger(Number(raw))) {
                input.value = current;
                return;
            }
            const page = Math.max(1, Math.min(Number(raw), Number(input.max)));
            input.value = String(page);
            if (navigating || page === Number(current)) return;
            const url = new URL(input.dataset.pageUrl, location.href);
            url.searchParams.set(input.dataset.pageParam, String(page));
            navigating = true;
            location.assign(url.toString());
        }
        input.addEventListener('change', jump);
        input.addEventListener('keydown', event => {
            if (event.key === 'Enter') { event.preventDefault(); jump(); }
        });
    });
});
