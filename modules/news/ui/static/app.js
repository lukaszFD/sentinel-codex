// Sentinel Codex — News Radar config page. Loaded as an external file (CSP: script-src 'self').
(function () {
  "use strict";

  // Confirmation dialogs: <form data-confirm="..."> or <button data-confirm="...">.
  document.addEventListener("submit", function (e) {
    var msg = (e.submitter && e.submitter.dataset.confirm) || e.target.dataset.confirm;
    if (msg && !window.confirm(msg)) {
      e.preventDefault();
    }
  });

  // Line / character counter for prompt textareas.
  document.querySelectorAll("textarea[data-counter]").forEach(function (ta) {
    var out = ta.parentElement.parentElement.querySelector(".counter");
    if (!out) return;
    var update = function () {
      out.textContent = ta.value.split("\n").length + " lines · " + ta.value.length + " characters";
    };
    ta.addEventListener("input", update);
    update();
  });

  // Warn before leaving a page with unsaved edits (prompt editor, settings).
  var dirty = false;
  document.querySelectorAll("form[method=post] textarea:not([readonly]), form[method=post] input.num, form[data-track] select")
    .forEach(function (el) { el.addEventListener("input", function () { dirty = true; }); });
  document.addEventListener("submit", function () { dirty = false; });
  window.addEventListener("beforeunload", function (e) {
    if (dirty) { e.preventDefault(); e.returnValue = ""; }
  });

  // Source form: the target field is a search query or a feed URL, depending on the type.
  document.querySelectorAll("select[name=source_type]").forEach(function (sel) {
    var form = sel.form;
    var label = form.querySelector("[data-target-label]");
    var input = form.querySelector("input[name=target]");
    var update = function () {
      var isQuery = sel.value === "google_news" || sel.value === "hn_algolia";
      if (label) label.textContent = isQuery ? "Search query" : "Feed URL (https://)";
      if (input) input.placeholder = isQuery ? 'e.g. "zero-day" exploited' : "https://example.com/feed/";
    };
    sel.addEventListener("change", update);
    update();
  });
})();
