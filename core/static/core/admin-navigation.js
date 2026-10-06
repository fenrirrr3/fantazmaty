(() => {
  "use strict";
  document.addEventListener("DOMContentLoaded", () => {
    const scope = document.querySelector("[data-cms-admin-user]")?.dataset.cmsAdminUser;
    if (!scope) return;
    const prefix = "fantazmaty:admin:" + scope + ":";
    const read = key => { try { return localStorage.getItem(prefix + key); } catch { return null; } };
    const write = (key, value) => { try { localStorage.setItem(prefix + key, value); } catch { /* Navigation works without storage. */ } };
    const groups = [...document.querySelectorAll("details[data-admin-group]")];
    for (const group of groups) {
      const saved = read("group:" + group.dataset.adminGroup);
      if (saved !== null) group.open = saved === "1";
      group.addEventListener("toggle", () => {
        if (document.getElementById("nav-filter")?.value.trim()) return;
        write("group:" + group.dataset.adminGroup, group.open ? "1" : "0");
        for (const other of groups) {
          if (other !== group && other.dataset.adminGroup === group.dataset.adminGroup && other.open !== group.open) other.open = group.open;
        }
      });
    }
    const sidebar = document.getElementById("nav-sidebar");
    if (sidebar) {
      sidebar.scrollTop = Number(read("sidebar-scroll")) || 0;
      sidebar.addEventListener("scroll", () => write("sidebar-scroll", String(sidebar.scrollTop)), {passive: true});
      sidebar.addEventListener("click", event => { if (event.target.closest("a")) write("sidebar-scroll", String(sidebar.scrollTop)); });
      const filter = document.getElementById("nav-filter");
      filter?.addEventListener("input", () => {
        sidebar.querySelectorAll("details[data-admin-group]").forEach(group => {
          if (filter.value.trim()) group.open = true;
          else { const saved = read("group:" + group.dataset.adminGroup); if (saved !== null) group.open = saved === "1"; }
        });
      });
    }
  });
})();
