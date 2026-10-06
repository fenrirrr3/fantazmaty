(() => {
    "use strict";
    document.querySelectorAll("[data-header-search]").forEach(panel => {
        const toggle = panel.querySelector("summary");
        panel.addEventListener("toggle", () => {
            if (panel.open) panel.querySelector('input[name="query"]').focus();
        });
        panel.addEventListener("keydown", event => {
            if (event.key === "Escape" && panel.open) {
                panel.open = false;
                toggle.focus();
                event.preventDefault();
            }
        });
        document.addEventListener("click", event => {
            if (panel.open && !panel.contains(event.target)) panel.open = false;
        });
    });
})();
