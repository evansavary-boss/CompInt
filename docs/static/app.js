// Client-side filtering: domain buttons, period toggle and full-text search.
(function () {
  "use strict";

  const state = {
    domain: "geospatial",
    days: Number(document.body.dataset.displayDays) || 14,
    query: "",
  };

  const days = Array.from(document.querySelectorAll(".day"));
  const columns = Array.from(document.querySelectorAll(".col:not(.col-market)"));
  const searchInput = document.getElementById("q");

  // Same normalisation as src/filter.py: lowercase, no accents.
  function normalize(text) {
    return text.normalize("NFKD").replace(/[̀-ͯ]/g, "").toLowerCase().trim();
  }

  function apply() {
    const terms = normalize(state.query).split(/\s+/).filter(Boolean);

    days.forEach((day) => {
      const inPeriod = Number(day.dataset.age) < state.days;
      let visible = 0;
      day.querySelectorAll(".item").forEach((item) => {
        const tags = item.dataset.tags.split(" ");
        const okDomain = state.domain === "all" || tags.includes(state.domain);
        const okSearch = terms.every((t) => item.dataset.search.includes(t));
        const show = inPeriod && okDomain && okSearch;
        item.hidden = !show;
        if (show) visible++;
      });
      day.hidden = visible === 0;
    });

    columns.forEach((col) => {
      const n = col.querySelectorAll(".item:not([hidden])").length;
      col.querySelector("[data-count]").textContent = n;
      col.querySelector(".empty").hidden = n > 0;
    });
  }

  function bindSegmented(selector, key, parse) {
    const buttons = document.querySelectorAll(selector);
    buttons.forEach((btn) => {
      btn.addEventListener("click", () => {
        buttons.forEach((b) => b.setAttribute("aria-pressed", String(b === btn)));
        state[key] = parse(btn);
        apply();
      });
    });
  }

  bindSegmented("[data-domain]", "domain", (b) => b.dataset.domain);
  bindSegmented("[data-days]", "days", (b) => Number(b.dataset.days));

  let timer;
  searchInput.addEventListener("input", () => {
    clearTimeout(timer);
    timer = setTimeout(() => {
      state.query = searchInput.value;
      apply();
    }, 120);
  });

  // "/" focuses the search box, Escape clears it.
  document.addEventListener("keydown", (e) => {
    if (e.key === "/" && document.activeElement !== searchInput) {
      e.preventDefault();
      searchInput.focus();
    } else if (e.key === "Escape" && document.activeElement === searchInput) {
      searchInput.value = "";
      state.query = "";
      apply();
    }
  });

  // Keep sticky columns below the (variable-height) header.
  const header = document.querySelector(".top");
  const setHeaderHeight = () =>
    document.documentElement.style.setProperty("--header-h", header.offsetHeight + "px");
  setHeaderHeight();
  window.addEventListener("resize", setHeaderHeight);

  apply();
})();
