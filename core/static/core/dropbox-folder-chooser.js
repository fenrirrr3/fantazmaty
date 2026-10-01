"use strict";
(() => {
    const button = document.getElementById("dropbox-folder-choose");
    const status = document.getElementById("dropbox-folder-status");
    if (!button || !status) return;
    const input = document.getElementById(button.dataset.inputId);
    if (!input) return;
    button.addEventListener("click", () => {
        if (!window.Dropbox || typeof window.Dropbox.choose !== "function") {
            status.textContent = "Nie udało się załadować okna Dropboxa. Odśwież stronę lub wklej link ręcznie.";
            return;
        }
        try {
            window.Dropbox.choose({
                linkType: "preview",
                multiselect: false,
                folderselect: true,
                success: (items) => {
                    const folder = Array.isArray(items) && items.length === 1 ? items[0] : null;
                    if (!folder || folder.isDir !== true || typeof folder.link !== "string") {
                        status.textContent = "Wybierz folder, a nie pojedynczy plik. Dotychczasowy link pozostał bez zmian.";
                        return;
                    }
                    let url;
                    try { url = new URL(folder.link); } catch (_) { /* Validate before changing the input. */ }
                    if (!url || url.protocol !== "https:" || !["dropbox.com", "www.dropbox.com"].includes(url.hostname)
                        || url.username || url.password || (input.maxLength > 0 && folder.link.length > input.maxLength)) {
                        status.textContent = "Dropbox zwrócił nieprawidłowy link. Dotychczasowy link pozostał bez zmian.";
                        return;
                    }
                    input.value = folder.link;
                    input.dispatchEvent(new Event("input", { bubbles: true }));
                    input.dispatchEvent(new Event("change", { bubbles: true }));
                    status.textContent = "Wybrano folder: " + (folder.name || "Dropbox") + ". Kliknij „Zapisz link”, aby zachować zmianę.";
                },
                cancel: () => { status.textContent = "Anulowano wybór. Dotychczasowy link pozostał bez zmian."; },
            });
        } catch (_) {
            status.textContent = "Nie udało się otworzyć Dropboxa. Sprawdź blokowanie wyskakujących okien lub wklej link ręcznie.";
        }
    });
})();
