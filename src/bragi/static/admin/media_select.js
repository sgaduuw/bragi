/* Optional select-all; individual form controls work without JavaScript. */
document.addEventListener("change", function (event) {
  const all = document.getElementById("media-select-all");
  if (!all) return;
  const boxes = Array.from(document.querySelectorAll('input[name="ids"][form="media-delete-selection"]'));
  if (event.target === all) boxes.forEach(function (box) { box.checked = all.checked; });
  else if (!boxes.includes(event.target)) return;
  const selected = boxes.filter(function (box) { return box.checked; }).length;
  all.checked = boxes.length > 0 && selected === boxes.length;
  all.indeterminate = selected > 0 && selected < boxes.length;
});
