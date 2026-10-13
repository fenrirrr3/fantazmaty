"use strict";
// Scalanie DOCX: pod polem wyboru kilku plików lista nazw w kolejności, w jakiej
// zostaną scalone (kolejność wyboru w oknie systemowym). Bez JavaScriptu kolejność
// pokazuje podsumowanie zadania po scaleniu.
(() => {
    document.querySelectorAll("ol[data-file-list]").forEach((list) => {
        const input = document.getElementById(list.dataset.fileList);
        if (!input) return;
        const update = () => {
            const files = Array.from(input.files || []);
            list.replaceChildren(...files.map((file) => {
                const item = document.createElement("li");
                item.textContent = file.name;
                return item;
            }));
            list.hidden = files.length === 0;
        };
        input.addEventListener("change", update);
        update();
    });
})();
