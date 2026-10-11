"use strict";
(() => {
    // „Zaznacz/Odznacz wszystkie” działa na wszystkich polach siatki reguł w tej samej
    // grupie – także na „Usuń miękkie entery i spacje nieprzenoszące”.
    document.querySelectorAll(".rule-actions").forEach((actions) => {
        const group = actions.closest("fieldset");
        if (!group) return;
        actions.hidden = false;
        actions.addEventListener("click", (event) => {
            const button = event.target.closest("button[data-rules]");
            if (!button) return;
            group.querySelectorAll('.odkurzacz-rules input[type="checkbox"]').forEach((input) => {
                input.checked = button.dataset.rules === "all";
            });
        });
    });
    // Opcje zależne (format strony przy przebudowie, reguły przy „Użyj Odkurzacza”)
    // są ukrywane, ale nadal wysyłane – dzięki temu zapamiętane ustawienia nie giną.
    document.querySelectorAll(".program-suboption[data-requires]").forEach((option) => {
        const form = option.closest("form");
        const control = form && form.querySelector(`input[name="${option.dataset.requires}"]`);
        if (!control) return;
        const update = () => { option.hidden = !control.checked; };
        control.addEventListener("change", update);
        update();
    });
})();
