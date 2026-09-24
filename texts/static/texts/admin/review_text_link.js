(() => {
  "use strict";
  document.addEventListener("click", async (event) => {
    const link = event.target.closest("#add_id_copied_text");
    if (!link) return;
    event.preventDefault();
    event.stopImmediatePropagation();
    const field = document.getElementById("id_copied_text");
    const endpoint = field.dataset.prepareTextUrl;
    if (!field.dataset.sourceReviewId) { alert("Najpierw zapisz recenzję, a następnie użyj +."); return; }
    const popup = window.open("", "review_text_" + Date.now() + "_" + Math.random().toString(36).slice(2), "height=650,width=1000,resizable=yes,scrollbars=yes");
    if (!popup) { alert("Zezwól na otwieranie okien formularza."); return; }
    // Django resolves the target input by window.name when dismissing the popup.
    // _blank-like unique target prevents reusing a window belonging to another tab.
    const parentIndex = document.querySelector('[name="_popup"]') ? Number(window.name.match(/__(\d+)$/)?.[1] || 0) : 0;
    popup.name = 'id_copied_text__' + (parentIndex + 1);
    window.relatedWindows?.push(popup);
    const data = new FormData();
    data.set('review_id', field.dataset.sourceReviewId);
    for (const name of ["title", "length", "content_warnings", "anthology"]) {
      data.set(name, document.getElementById("id_" + name)?.value || "");
    }
    for (const name of ["first_name", "last_name", "email"]) {
      data.set("source_author_" + name, document.getElementById("id_" + (name === "email" ? name : "author_" + name))?.value || "");
    }
    const author = document.getElementById("id_author");
    if (author?.value) data.append("authors", author.value);
    for (const option of document.getElementById("id_coauthors")?.selectedOptions || []) data.append("authors", option.value);
    try {
      if (!endpoint) throw new Error();
      const response = await fetch(endpoint, {method: "POST", body: data, credentials: "same-origin",
        headers: {"X-CSRFToken": document.querySelector('[name=csrfmiddlewaretoken]').value}});
      const result = await response.json();
      if (!response.ok) throw new Error(result.error || "Nie udało się przygotować formularza.");
      popup.location.href = result.url;
    } catch (error) {
      popup.close();
      alert(error.message || "Nie udało się przygotować formularza tekstu. Spróbuj ponownie.");
    }
  }, true);
})();
