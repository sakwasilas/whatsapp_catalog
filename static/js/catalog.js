/* Catalog: progressive enhancement only.
 *
 * Search / sort / in-stock / category are all handled server-side via the
 * <form class="tools"> GET submission and pagination links (see catalog.html).
 * This script only makes the *current* page feel instant: it hides or
 * reorders the ~12 cards already on the page while the network request is
 * in flight. Without JS, everything still works via the form.
 *
 * Auto-submit timings:
 *   - Search:  900 ms debounce after last keystroke
 *   - Sort:    immediate
 *   - Stock:   immediate
 */
(function () {
  "use strict";

  var grid = document.getElementById("productGrid");
  if (!grid) return;

  var form = document.querySelector("form.tools");
  var cols = Array.prototype.slice.call(grid.querySelectorAll(".product-col"));
  var search = document.getElementById("productSearch");
  var sort = document.getElementById("productSort");
  var stock = document.getElementById("inStockOnly");
  var none = document.getElementById("noResults");
  var count = document.getElementById("resultCount");

  var original = cols.slice();      // snapshot in the order the server sent
  var submitTimer = null;

  // ---------------------------------------------------------------- helpers

  function submitForm() {
    if (form) form.submit();
  }

  function scheduleSubmit(delay) {
    if (submitTimer) clearTimeout(submitTimer);
    submitTimer = setTimeout(submitForm, delay);
  }

  function applyLocalFilter() {
    // Mirror what the server would do, for the current page only.
    var q = (search && search.value || "").trim().toLowerCase();
    var onlyInStock = !!(stock && stock.checked);
    var shown = 0;

    cols.forEach(function (c) {
      var name = c.dataset.name || "";
      var desc = c.dataset.desc || "";
      var cat = c.dataset.category || "";
      var available = c.dataset.available === "1";

      var matchSearch = !q || name.indexOf(q) > -1 || desc.indexOf(q) > -1 || cat.indexOf(q) > -1;
      var matchStock = !onlyInStock || available;

      var ok = matchSearch && matchStock;
      c.classList.toggle("d-none", !ok);
      if (ok) shown++;
    });

    if (none) none.classList.toggle("d-none", shown > 0);
    if (count) count.textContent = shown + (shown === 1 ? " product" : " products");
  }

  function applyLocalSort() {
    // Only reorder what's on the page; the server does the real work.
    var mode = sort ? sort.value : "featured";
    var list = original.slice();

    if (mode === "price_asc") {
      list.sort(function (a, b) { return parseFloat(a.dataset.price) - parseFloat(b.dataset.price); });
    } else if (mode === "price_desc") {
      list.sort(function (a, b) { return parseFloat(b.dataset.price) - parseFloat(a.dataset.price); });
    } else if (mode === "name_asc") {
      list.sort(function (a, b) { return (a.dataset.name || "").localeCompare(b.dataset.name || ""); });
    } else if (mode === "newest") {
      // Server already returns newest-first on non-featured; keep the DOM order.
      list = original.slice();
    } else {
      // featured (default) — keep DOM order as returned by server.
      list = original.slice();
    }

    list.forEach(function (c) { grid.appendChild(c); });
    cols = list;
  }

  // ---------------------------------------------------------------- wiring

  if (search) {
    search.addEventListener("input", function () {
      applyLocalFilter();
      scheduleSubmit(900);       // server-side refresh if user pauses
    });
  }

  if (sort) {
    sort.addEventListener("change", function () {
      applyLocalSort();
      applyLocalFilter();
      submitForm();              // submit immediately; server does the real sort
    });
  }

  if (stock) {
    stock.addEventListener("change", function () {
      applyLocalFilter();
      submitForm();
    });
  }

  // ---------------------------------------------------------------- init
  // Nothing to pre-apply: the server has already rendered the correct page.
  // If the URL had ?q=..., the input already shows it (form prefill).
})();