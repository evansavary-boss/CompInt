// Concurrents tab: period switch of the movement matrix; a click on a cell sets the feed filters
// through the URL hash (handled by app.js) and scrolls down to the feed.
(function () {
  "use strict";

  const matrix = document.querySelector("[data-comp-matrix]");
  if (!matrix) return;

  const buttons = matrix.querySelectorAll("[data-comp-period]");
  buttons.forEach((b) => b.addEventListener("click", () => {
    buttons.forEach((o) => o.setAttribute("aria-pressed", String(o === b)));
    matrix.querySelectorAll("[data-comp-panel]").forEach((p) => {
      p.hidden = p.dataset.compPanel !== b.dataset.compPeriod;
    });
  }));

  matrix.querySelectorAll("a[data-comp-feed]").forEach((a) => a.addEventListener("click", () => {
    // Let the hash change first (app.js filters the feed), then bring the feed into view.
    setTimeout(() => document.getElementById("fil").scrollIntoView({ behavior: "smooth", block: "start" }), 0);
  }));
})();
