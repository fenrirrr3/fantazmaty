(() => {
  "use strict";
  document.addEventListener("click", (event) => {
    const link = event.target.closest("#add_id_copied_text");
    if (!link) return;
    const url = new URL(link.href, window.location.href);
    const source = window.location.pathname.match(/\/review\/(\d+)\/change\//);
    if (source) url.searchParams.set("source_review", source[1]);
    for (const name of ["title", "length", "content_warnings", "anthology"]) {
      const field = document.getElementById("id_" + name);
      if (field) url.searchParams.set(name, field.value);
    }
    for (const [target, sourceField] of [["source_author_first_name", "author_first_name"], ["source_author_last_name", "author_last_name"], ["source_author_email", "email"]]) {
      const field = document.getElementById("id_" + sourceField);
      if (field) url.searchParams.set(target, field.value);
    }
    url.searchParams.delete("authors");
    const author = document.getElementById("id_author");
    if (author && author.value) url.searchParams.append("authors", author.value);
    const coauthors = document.getElementById("id_coauthors");
    if (coauthors) for (const option of coauthors.selectedOptions) url.searchParams.append("authors", option.value);
    link.href = url.toString();
  }, true);
})();
