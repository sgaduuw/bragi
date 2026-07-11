/* Hydrates ::: dataset format=chart ::: blocks.
 *
 * The page carries <div class="bragi-dataset-chart"
 * data-vega-spec="..."> with the full Vega-Lite spec (data
 * inlined at save time). vega / vega-lite / vega-embed are
 * self-hosted (vendored beside this file) and load, in dependency
 * order, only when a chart is actually present -- so a chart page
 * doesn't leak the visitor's IP to a CDN, and works offline. On any
 * failure the noscript table inside the div stays as fallback.
 * Update the three vendored *.min.js together; vega@5 / vega-lite@5
 * / vega-embed@6.
 */
(function () {
  "use strict";
  var charts = document.querySelectorAll(".bragi-dataset-chart");
  if (!charts.length) return;
  var SOURCES = [
    "/static/datasets/vega.min.js",
    "/static/datasets/vega-lite.min.js",
    "/static/datasets/vega-embed.min.js",
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
