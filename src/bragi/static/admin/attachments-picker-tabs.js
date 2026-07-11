// Attachments picker tab switching. This markup is swapped into the image
// dialog via htmx, and htmx executes external <script> tags in swapped
// content, so wiring here runs each time the picker opens. Idempotent via a
// `data-tabs-wired` flag on the picker root.
(function () {
  var picker = document.getElementById("attachment-picker");
  if (!picker || picker.dataset.tabsWired === "1") return;
  picker.dataset.tabsWired = "1";

  var tablist = picker.querySelector('[role="tablist"]');
  if (!tablist) return;

  tablist.addEventListener("click", function (e) {
    var btn = e.target.closest('[role="tab"]');
    if (!btn) return;

    var targetId = btn.getAttribute("aria-controls");
    if (!targetId) return;

    // Deselect all tabs and hide all panes. CSS reacts to
    // [aria-selected="true"] for the active-tab styling.
    tablist.querySelectorAll('[role="tab"]').forEach(function (t) {
      t.setAttribute("aria-selected", "false");
    });
    picker.querySelectorAll('[role="tabpanel"]').forEach(function (p) {
      p.hidden = true;
    });

    // Activate the clicked tab and its pane.
    btn.setAttribute("aria-selected", "true");
    var pane = document.getElementById(targetId);
    if (pane) pane.hidden = false;
  });
})();
