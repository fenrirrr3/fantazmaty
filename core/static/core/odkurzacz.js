"use strict";
(() => {
    const actions = document.getElementById("rule-actions");
    if (!actions) return;
    actions.hidden = false;
    actions.addEventListener("click", (event) => {
        const button = event.target.closest("button[data-rules]");
        if (!button) return;
        document.querySelectorAll('.odkurzacz input[name="rules"]').forEach((input) => {
            input.checked = button.dataset.rules === "all";
        });
    });
})();
