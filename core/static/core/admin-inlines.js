document.addEventListener('DOMContentLoaded', () => {
    const update = () => document.querySelectorAll('.inline-group').forEach(group => {
        const stage = group.id.includes('workflow_stages');
        const role = group.id.includes('workflow_role_assignments');
        const note = /notatk(?:a|ę|i) o autorze/i.test(group.textContent);
        if (stage || role || note) group.querySelectorAll('.add-row a').forEach(link => {
            const label = stage ? 'Dodaj kolejny etap pracy' : role ? 'Dodaj kolejne przypisanie roli' : 'Dodaj kolejną notatkę o autorze';
            if (link.textContent !== label) link.textContent = label;
        });
    });
    update();
    new MutationObserver(update).observe(document.body, {childList: true, subtree: true});
});
