(() => {
  "use strict";
  document.addEventListener("click", async (event) => {
    const link = event.target.closest("#add_id_copied_text");
    if (!link) return;
    event.preventDefault();
    event.stopImmediatePropagation();
    const endpoint = document.getElementById("id_copied_text").dataset.prepareTextUrl;
    const popup = window.open("", "id_copied_text", "height=650,width=1000,resizable=yes,scrollbars=yes");
    if (!popup) { alert("Zezwól na otwieranie okien formularza."); return; }
    const data = new FormData();
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
      if (!response.ok) throw new Error();
      const result = await response.json();
      popup.location.href = result.url;
    } catch (error) {
      popup.close();
      alert("Nie udało się przygotować formularza tekstu. Spróbuj ponownie.");
    }
  }, true);
})();
