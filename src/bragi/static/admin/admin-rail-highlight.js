// Admin rail active-state: re-apply `.is-current` to the rail link
// matching the current path on htmx boosted navigation (pushState) and
// on history restore. The server renders `is-current` on the initial load;
// this keeps it correct across boosted section swaps.
function syncRailHighlight() {
  var here = window.location.pathname;
  document.querySelectorAll(".rail-link").forEach(function (a) {
    a.classList.toggle("is-current", a.getAttribute("href") === here);
  });
}
document.body.addEventListener("htmx:pushedIntoHistory", syncRailHighlight);
document.body.addEventListener("htmx:historyRestore", syncRailHighlight);
