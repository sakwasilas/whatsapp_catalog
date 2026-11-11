/* Shared behaviour. No inline scripts anywhere (strict CSP), so everything is wired by data-attributes. */
(function () {
  "use strict";
  var $ = function (s, r) { return (r || document).querySelector(s); };
  var $$ = function (s, r) { return Array.prototype.slice.call((r || document).querySelectorAll(s)); };

  // Confirm dialogs: <form data-confirm="Are you sure?">
  document.addEventListener("submit", function (e) {
    var msg = e.target.getAttribute && e.target.getAttribute("data-confirm");
    if (msg && !window.confirm(msg)) e.preventDefault();
  });

  // Prevent double submits
  $$("form").forEach(function (f) {
    f.addEventListener("submit", function (e) {
      if (e.defaultPrevented) return;
      var b = f.querySelector("button[type=submit]:not([data-no-lock])");
      if (b) setTimeout(function () { b.disabled = true; var t = b.getAttribute("data-busy"); if (t) b.textContent = t; }, 0);
    });
  });
  window.addEventListener("pageshow", function () { $$("button[type=submit]").forEach(function (b) { b.disabled = false; }); });

  // Auto-submit selects: <select data-autosubmit>
  $$("select[data-autosubmit]").forEach(function (sel) { sel.addEventListener("change", function () { sel.form.submit(); }); });

  // Copy to clipboard: data-copy="text" or data-copy-from="#inputId"
  $$("[data-copy],[data-copy-from]").forEach(function (btn) {
    btn.addEventListener("click", function () {
      var text = btn.getAttribute("data-copy");
      if (!text) { var el = $(btn.getAttribute("data-copy-from")); text = el ? (el.value || el.textContent) : ""; }
      var done = function () {
        var old = btn.innerHTML; btn.innerHTML = '<i class="bi bi-check2"></i> Copied';
        setTimeout(function () { btn.innerHTML = old; }, 1600);
      };
      if (navigator.clipboard) navigator.clipboard.writeText(text.trim()).then(done);
      else { var t = document.createElement("textarea"); t.value = text; document.body.appendChild(t); t.select(); document.execCommand("copy"); t.remove(); done(); }
    });
  });

  // Native share sheet when available: data-share-url / data-share-title
  $$("[data-share-url]").forEach(function (btn) {
    btn.addEventListener("click", function () {
      var data = { title: btn.getAttribute("data-share-title") || document.title, url: btn.getAttribute("data-share-url") };
      if (navigator.share) navigator.share(data).catch(function () {});
      else if (navigator.clipboard) navigator.clipboard.writeText(data.url).then(function () {
        var old = btn.innerHTML; btn.innerHTML = '<i class="bi bi-check2"></i> Link copied'; setTimeout(function () { btn.innerHTML = old; }, 1600);
      });
    });
  });

  // Show / hide password
  $$("[data-toggle-password]").forEach(function (btn) {
    btn.addEventListener("click", function () {
      var input = btn.parentElement.querySelector("input");
      var show = input.type === "password"; input.type = show ? "text" : "password";
      btn.innerHTML = '<i class="bi bi-eye' + (show ? "-slash" : "") + '"></i>';
    });
  });

  // Image preview: <input type=file data-preview="#imgId">
  $$("input[type=file][data-preview]").forEach(function (inp) {
    inp.addEventListener("change", function () {
      var img = $(inp.getAttribute("data-preview")); var f = inp.files && inp.files[0];
      if (f && f.size > 4 * 1024 * 1024) { alert("That image is larger than 4 MB. Please choose a smaller one."); inp.value = ""; return; }
      if (f && img) { img.src = URL.createObjectURL(f); img.classList.remove("d-none"); var ph = $(inp.getAttribute("data-placeholder")); if (ph) ph.classList.add("d-none"); }
    });
  });

  // Character counters: <textarea data-counter="#id">
  $$("[data-counter]").forEach(function (el) {
    var out = $(el.getAttribute("data-counter")); if (!out) return;
    var upd = function () { out.textContent = el.value.length + " / " + el.maxLength; };
    el.addEventListener("input", upd); upd();
  });

  // Reject-payment modal: buttons carry data-action and data-label
  $$("[data-reject-action]").forEach(function (btn) {
    btn.addEventListener("click", function () {
      var form = $("#rejectForm"); if (!form) return;
      form.action = btn.getAttribute("data-reject-action");
      $("#rejectLabel").textContent = btn.getAttribute("data-label") || "";
    });
  });

  // Reject modal preset reasons fill the text box
  var preset = $("#rejectPreset"), note = $("#rejectNote");
  if (preset && note) { preset.addEventListener("change", function () { note.value = preset.value; }); note.value = preset.value; }

  // Order form: price/total helper
  var prod = $("#orderProduct"), qty = $("#orderQty"), price = $("#orderPrice"), total = $("#orderTotal"), name = $("#orderName");
  if (prod && qty && price && total) {
    var fmt = function (n) { return "KSh " + (Math.round(n * 100) / 100).toLocaleString("en-KE"); };
    var calc = function () { var p = parseFloat((price.value || "0").replace(/,/g, "")) || 0, q = parseInt(qty.value, 10) || 1; total.textContent = fmt(p * q); };
    prod.addEventListener("change", function () {
      var o = prod.options[prod.selectedIndex];
      if (o && o.value) { price.value = o.getAttribute("data-price"); if (name && !name.value) name.value = o.getAttribute("data-name"); }
      calc();
    });
    [qty, price].forEach(function (el) { el.addEventListener("input", calc); }); calc();
  }

  // Auto-dismiss success alerts
  setTimeout(function () { $$(".alert-success.alert-dismissible").forEach(function (a) { if (window.bootstrap) bootstrap.Alert.getOrCreateInstance(a).close(); }); }, 6000);
})();
