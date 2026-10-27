// Vanilla JS: feed filters (domain, period, impact, region, country, entity, segment, industry,
// families, event types, full-text search) kept in the URL hash, live counts, the world map of the
// Géopolitique tab, and the "open a profile" box.
(function () {
  "use strict";

  const root = document.body.dataset.root || "";

  // Same normalisation as src/filter.py: lowercase, no accents.
  const normalize = (text) =>
    text.normalize("NFKD").replace(/[̀-ͯ]/g, "").toLowerCase().trim();

  // ------------------------------------------------------------ profile jump box
  const jump = document.getElementById("jump");
  const indexEl = document.getElementById("entity-index");
  if (jump && indexEl) {
    const index = JSON.parse(indexEl.textContent);
    const go = () => {
      const wanted = normalize(jump.value);
      const hit = index.find((e) => normalize(e.name) === wanted)
        || index.find((e) => normalize(e.name).startsWith(wanted) && wanted.length > 1);
      if (hit) window.location.href = `${root}entite/${hit.id}.html`;
    };
    jump.addEventListener("change", go);
    jump.addEventListener("keydown", (e) => { if (e.key === "Enter") go(); });
  }

  // Keep sticky elements below the header.
  const header = document.querySelector(".top");
  const setHeaderHeight = () =>
    document.documentElement.style.setProperty("--header-h", header.offsetHeight + "px");
  setHeaderHeight();
  window.addEventListener("resize", setHeaderHeight);

  // ------------------------------------------------------------ feed filters
  const bar = document.querySelector(".filters");
  const feed = document.querySelector("[data-feed]");
  if (!bar || !feed) return;

  const SET_FIELDS = ["tags", "entities", "countries", "regions", "events", "segments", "industries", "themes"];
  const SELECTS = ["regions", "countries", "entities", "segments", "industries"];
  const CHIP_GROUPS = { events: "event", themes: "theme" };   // multi-select chips: field -> data attribute
  const defaults = {
    domain: bar.dataset.defaultDomain || "all",
    days: Number(bar.dataset.defaultDays) || 14,
  };
  const EMPTY = { q: "", impact: 0, regions: "", countries: "", entities: "", segments: "", industries: "" };
  const state = { ...defaults, ...EMPTY, events: new Set(), themes: new Set() };

  const items = Array.from(feed.querySelectorAll(".item")).map((el) => {
    const it = { el, age: Number(el.dataset.age), impact: Number(el.dataset.impact || 0), search: el.dataset.search };
    SET_FIELDS.forEach((f) => { it[f] = new Set((el.dataset[f] || "").split(" ").filter(Boolean)); });
    return it;
  });
  const days = Array.from(feed.querySelectorAll(".day"));
  const domainButtons = bar.querySelectorAll("[data-domain]");
  const impactButtons = bar.querySelectorAll("[data-impact]");
  const selects = bar.querySelectorAll("select[data-filter]");
  const searchInput = bar.querySelector('input[data-filter="q"]');
  const chips = Object.fromEntries(Object.entries(CHIP_GROUPS).map(([f, attr]) =>
    [f, bar.querySelectorAll(`[data-${attr}]`)]));
  const resetButton = bar.querySelector("[data-reset]");

  function matches(it, except) {
    if (state.domain !== "all" && !it.tags.has(state.domain)) return false;
    if (it.age >= state.days) return false;
    if (state.impact && it.impact < state.impact) return false;
    for (const f of SELECTS) {
      if (f !== except && state[f] && !it[f].has(state[f])) return false;
    }
    for (const f of Object.keys(CHIP_GROUPS)) {
      if (f === except || !state[f].size) continue;
      let any = false;
      for (const v of state[f]) if (it[f].has(v)) { any = true; break; }
      if (!any) return false;
    }
    if (state.q) {
      const terms = normalize(state.q).split(/\s+/).filter(Boolean);
      if (!terms.every((t) => it.search.includes(t))) return false;
    }
    return true;
  }

  function countBy(field, except) {
    const counts = new Map();
    items.forEach((it) => {
      if (!matches(it, except)) return;
      it[field].forEach((v) => counts.set(v, (counts.get(v) || 0) + 1));
    });
    return counts;
  }

  function isDefault() {
    return state.domain === defaults.domain && state.days === defaults.days && !state.q && !state.impact
      && SELECTS.every((f) => !state[f]) && Object.keys(CHIP_GROUPS).every((f) => !state[f].size);
  }

  function writeHash() {
    const p = new URLSearchParams();
    if (state.domain !== defaults.domain) p.set("domain", state.domain);
    if (state.impact) p.set("impact", state.impact);
    if (state.days !== defaults.days) p.set("days", state.days);
    SELECTS.forEach((f) => { if (state[f]) p.set(f, state[f]); });
    Object.keys(CHIP_GROUPS).forEach((f) => { if (state[f].size) p.set(f, Array.from(state[f]).join(",")); });
    if (state.q) p.set("q", state.q);
    const hash = p.toString();
    history.replaceState(null, "", hash ? `#${hash}` : window.location.pathname + window.location.search);
  }

  function readHash() {
    const p = new URLSearchParams(window.location.hash.slice(1));
    if (p.get("domain")) state.domain = p.get("domain");
    state.impact = Number(p.get("impact")) || 0;
    if (p.get("days")) state.days = Number(p.get("days")) || defaults.days;
    SELECTS.forEach((f) => { state[f] = p.get(f) || ""; });
    Object.keys(CHIP_GROUPS).forEach((f) => { state[f] = new Set((p.get(f) || "").split(",").filter(Boolean)); });
    state.q = p.get("q") || "";
  }

  function syncControls() {
    domainButtons.forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.domain === state.domain)));
    impactButtons.forEach((b) => b.setAttribute("aria-pressed", String(Number(b.dataset.impact) === state.impact)));
    selects.forEach((s) => {
      const f = s.dataset.filter;
      const value = f === "days" ? String(state.days) : state[f];
      if (value && !Array.from(s.options).some((o) => o.value === value)) {
        // A value from the URL or the map that the static list does not offer.
        s.add(new Option(f === "days" ? `${value} derniers jours` : value, value));
      }
      s.value = value;
    });
    if (searchInput) searchInput.value = state.q;
    Object.entries(CHIP_GROUPS).forEach(([f, attr]) => chips[f].forEach((c) =>
      c.setAttribute("aria-pressed", String(state[f].has(c.dataset[attr])))));
  }

  function apply() {
    let visible = 0;
    items.forEach((it) => {
      const show = matches(it);
      it.el.hidden = !show;
      if (show) visible++;
    });
    days.forEach((d) => { d.hidden = !d.querySelector(".item:not([hidden])"); });
    feed.querySelector(".empty").hidden = visible > 0;
    document.querySelectorAll("[data-count]").forEach((el) => { el.textContent = visible; });

    // Live counts: each list ignores its own filter (a country list ignores the country filter…).
    const cache = {};
    const counts = (field) => (cache[field] = cache[field] || countBy(field, field));
    document.querySelectorAll("[data-live-count]").forEach((el) => {
      const { field, key } = el.dataset;
      const n = counts(field).get(key) || 0;
      el.textContent = n;
      const li = el.closest("li");
      li.classList.toggle("zero", n === 0);
      li.classList.toggle("active", state[field] === key);
    });
    Object.entries(CHIP_GROUPS).forEach(([f, attr]) => {
      document.querySelectorAll(`[data-${attr}-count]`).forEach((el) => {
        const n = counts(f).get(el.dataset[`${attr}Count`]) || 0;
        el.textContent = n;
        el.closest("button").classList.toggle("zero", n === 0);
      });
    });
    paintMap(counts("countries"));
    if (resetButton) resetButton.hidden = isDefault();
    writeHash();
  }

  // ------------------------------------------------------------ world map (Géopolitique)
  const map = document.querySelector("[data-map]");
  const shapes = map ? Array.from(map.querySelectorAll("path[data-country]")) : [];
  const mapTip = map ? map.querySelector(".map-tip") : null;
  let mapCounts = new Map();

  function paintMap(countryCounts) {
    if (!map) return;
    mapCounts = countryCounts;
    const max = Math.max(1, ...countryCounts.values());
    shapes.forEach((p) => {
      const n = countryCounts.get(p.dataset.country) || 0;
      const bin = n ? 1 + Math.min(4, Math.floor((5 * (n - 1)) / max)) : 0;
      p.setAttribute("class", `land m${bin}${state.countries === p.dataset.country ? " selected" : ""}`);
    });
    const maxEl = map.querySelector("[data-map-max]");
    if (maxEl) maxEl.textContent = `Plus (max ${max})`;
  }

  if (map) {
    shapes.forEach((p) => {
      p.addEventListener("click", () => {
        const code = p.dataset.country;
        state.countries = state.countries === code ? "" : code;
        syncControls(); apply();
      });
      p.addEventListener("dblclick", () => {
        if (mapCounts.get(p.dataset.country)) window.location.href = `${root}pays/${p.dataset.country}.html`;
      });
      p.addEventListener("pointermove", (e) => {
        const n = mapCounts.get(p.dataset.country) || 0;
        mapTip.textContent = `${p.dataset.name} · ${n} sujet${n > 1 ? "s" : ""}`;
        mapTip.hidden = false;
        const box = map.getBoundingClientRect();
        const x = Math.min(e.clientX - box.left + 12, box.width - mapTip.offsetWidth - 8);
        mapTip.style.left = `${Math.max(8, x)}px`;
        mapTip.style.top = `${e.clientY - box.top + 14}px`;
      });
      p.addEventListener("pointerleave", () => { mapTip.hidden = true; });
    });
  }

  // ------------------------------------------------------------ events
  impactButtons.forEach((b) => b.addEventListener("click", () => {
    state.impact = Number(b.dataset.impact); syncControls(); apply();
  }));
  domainButtons.forEach((b) => b.addEventListener("click", () => {
    state.domain = b.dataset.domain; syncControls(); apply();
  }));
  selects.forEach((s) => s.addEventListener("change", () => {
    const f = s.dataset.filter;
    state[f] = f === "days" ? Number(s.value) : s.value;
    apply();
  }));
  Object.entries(CHIP_GROUPS).forEach(([f, attr]) => chips[f].forEach((c) => c.addEventListener("click", () => {
    const k = c.dataset[attr];
    if (state[f].has(k)) state[f].delete(k); else state[f].add(k);
    syncControls(); apply();
  })));
  let timer;
  if (searchInput) searchInput.addEventListener("input", () => {
    clearTimeout(timer);
    timer = setTimeout(() => { state.q = searchInput.value.trim(); apply(); }, 150);
  });
  if (resetButton) resetButton.addEventListener("click", () => {
    Object.assign(state, defaults, EMPTY);
    Object.keys(CHIP_GROUPS).forEach((f) => { state[f] = new Set(); });
    syncControls(); apply();
  });
  window.addEventListener("hashchange", () => { readHash(); syncControls(); apply(); });

  // "/" focuses the search box, Escape clears it.
  document.addEventListener("keydown", (e) => {
    if (!searchInput) return;
    const typing = ["INPUT", "SELECT", "TEXTAREA"].includes(document.activeElement.tagName);
    if (e.key === "/" && !typing) {
      e.preventDefault();
      searchInput.focus();
    } else if (e.key === "Escape" && document.activeElement === searchInput) {
      searchInput.value = ""; state.q = ""; apply();
    }
  });

  readHash();
  syncControls();
  apply();
})();
