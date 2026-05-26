// Pinned-posts carousel enhancement.
//
// Loaded only on post-index pages with 2+ pinned posts (see the
// _pinned_carousel.html partial). Two jobs:
//
//   1. Intercept dot-nav clicks so they scroll the strip
//      horizontally (preserving the document's vertical scroll
//      position) instead of doing the browser's default anchor
//      jump. Fixes #267.
//
//   2. Auto-advance the carousel on a per-site timer
//      (data-autoadvance-seconds on the section). 7 by default,
//      0 disables. Pauses on hover / focus-within / when the
//      section leaves the viewport. Honours
//      prefers-reduced-motion. Implements #266.
//
// No framework, no build step. Reads everything it needs from the
// DOM the partial already renders; the partial also still works
// with JS disabled (manual swipe + default anchor jumps).

(function () {
  "use strict";

  const section = document.querySelector("section.pinned");
  if (!section) return;
  const strip = section.querySelector(".pinned-strip");
  const dots = Array.from(section.querySelectorAll(".pinned-dots a"));
  const cards = Array.from(section.querySelectorAll(".pinned-card"));
  if (!strip || dots.length < 2 || cards.length < 2) return;

  // ---- #267: dot-click intercept --------------------------------
  function scrollToCard(card) {
    if (!card) return;
    // offsetLeft on the card is relative to the strip's offsetParent.
    // Subtracting strip.offsetLeft yields the scrollLeft we want.
    const left = card.offsetLeft - strip.offsetLeft;
    strip.scrollTo({ left: left, behavior: "smooth" });
    // Keep `:target` in sync without triggering the browser's
    // anchor-scroll (which is what caused #267). replaceState
    // updates the URL fragment silently; `:target` matches on
    // whatever the URL fragment is at the moment.
    if (card.id) {
      history.replaceState(null, "", "#" + card.id);
    }
  }

  dots.forEach(function (dot) {
    dot.addEventListener("click", function (event) {
      event.preventDefault();
      const href = dot.getAttribute("href") || "";
      const targetId = href.startsWith("#") ? href.slice(1) : "";
      if (!targetId) return;
      const card = document.getElementById(targetId);
      scrollToCard(card);
    });
  });

  // ---- #266: auto-advance ---------------------------------------
  const intervalSeconds = Number(section.dataset.autoadvanceSeconds || 0);
  const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  if (!intervalSeconds || reducedMotion) return;

  let timer = null;
  let paused = false;
  let offscreen = false;

  function currentIndex() {
    // The card whose offsetLeft is closest to (and not greater
    // than) the strip's scrollLeft is the currently snapped one.
    const sl = strip.scrollLeft;
    let best = 0;
    for (let i = 0; i < cards.length; i++) {
      if (cards[i].offsetLeft - strip.offsetLeft <= sl + 4) {
        best = i;
      }
    }
    return best;
  }

  function advance() {
    const next = (currentIndex() + 1) % cards.length;
    scrollToCard(cards[next]);
  }

  function start() {
    if (timer !== null) return;
    if (paused || offscreen) return;
    timer = window.setInterval(advance, intervalSeconds * 1000);
  }

  function stop() {
    if (timer === null) return;
    window.clearInterval(timer);
    timer = null;
  }

  section.addEventListener("mouseenter", function () { paused = true; stop(); });
  section.addEventListener("mouseleave", function () { paused = false; start(); });
  section.addEventListener("focusin", function () { paused = true; stop(); });
  section.addEventListener("focusout", function () {
    // focusout fires before the new focus target is known; defer
    // so document.activeElement reflects the post-blur state.
    window.setTimeout(function () {
      if (!section.contains(document.activeElement)) {
        paused = false;
        start();
      }
    }, 0);
  });

  if ("IntersectionObserver" in window) {
    const io = new IntersectionObserver(function (entries) {
      for (const entry of entries) {
        offscreen = !entry.isIntersecting;
        if (offscreen) stop(); else start();
      }
    }, { threshold: 0.1 });
    io.observe(section);
  } else {
    // No IntersectionObserver: just start the timer. Old browsers
    // (where this matters) are far from this site's audience.
    start();
  }
})();
