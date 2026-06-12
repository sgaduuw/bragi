/* Hydrates ::: dataset format=chart ::: blocks.
 *
 * The page carries <div class="bragi-dataset-chart"
 * data-vega-spec="..."> with the full Vega-Lite spec (data
 * inlined at save time). vega / vega-lite / vega-embed load from
 * jsDelivr only when a chart is actually present, mirroring the
 * project's htmx-from-CDN precedent. On any failure the noscript
 * table inside the div stays as the fallback content.
 */
(function () {
  "use strict";
  var charts = document.querySelectorAll(".bragi-dataset-chart");
  if (!charts.length) return;
  var SOURCES = [
    "https://cdn.jsdelivr.net/npm/vega@5",
    "https://cdn.jsdelivr.net/npm/vega-lite@5",
    "https://cdn.jsdelivr.net/npm/vega-embed@6",
  ];
  function load(i) {
    if (i >= SOURCES.length) { render(); return; }
    var s = document.createElement("script");
    s.src = SOURCES[i];
    s.onload = function () { load(i + 1); };
    document.head.appendChild(s);
  }
  function render() {
    charts.forEach(function (el) {
      try {
        var spec = JSON.parse(el.getAttribute("data-vega-spec"));
        window.vegaEmbed(el, spec, { actions: false });
      } catch (e) {
        /* leave fallback content in place */
      }
    });
  }
  load(0);
})();
