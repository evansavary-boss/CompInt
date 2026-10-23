// Vanilla JS: feed filters (domain, period, region, country, entity, segment, event types,
// full-text search) kept in the URL hash, live counts, and the "open a profile" box.
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

  const SET_FIELDS = ["tags", "entities", "countries", "regions", "events", "segments"];
  const SELECTS = ["regions", "countries", "entities", "segments"];
  const defaults = {
    domain: bar.dataset.defaultDomain || "all",
    days: Number(bar.dataset.defaultDays) || 14,
  };
  const state = { ...defaults, q: "", regions: "", countries: "", entities: "", segments: "", events: new Set() };

  const items = Array.from(feed.querySelectorAll(".item")).map((el) => {
    const it = { el, age: Number(el.dataset.age), search: el.dataset.search };
    SET_FIELDS.forEach((f) => { it[f] = new Set((el.dataset[f] || "").split(" ").filter(Boolean)); });
    return it;
  });
  const days = Array.from(feed.querySelectorAll(".day"));
  const domainButtons = bar.querySelectorAll("[data-domain]");
  const selects = bar.querySelectorAll("select[data-filter]");
  const searchInput = bar.querySelector('input[data-filter="q"]');
  const chips = bar.querySelectorAll("[data-event]");
  const resetButton = bar.querySelector("[data-reset]");

  function matches(it, except) {
    if (state.domain !== "all" && !it.tags.has(state.domain)) return false;
    if (it.age >= state.days) return false;
    for (const f of SELECTS) {
      if (f !== except && state[f] && !it[f].has(state[f])) return false;
    }
    if (except !== "events" && state.events.size) {
      let any = false;
      for (const e of state.events) if (it.events.has(e)) { any = true; break; }
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
    return state.domain === defaults.domain && state.days === defaults.days && !state.q
      && SELECTS.every((f) => !state[f]) && !state.events.size;
  }

  function writeHash() {
    const p = new URLSearchParams();
    if (state.domain !== defaults.domain) p.set("domain", state.domain);
    if (state.days !== defaults.days) p.set("days", state.days);
    SELECTS.forEach((f) => { if (state[f]) p.set(f, state[f]); });
    if (state.events.size) p.set("events", Array.from(state.events).join(","));
    if (state.q) p.set("q", state.q);
    const hash = p.toString();
    history.replaceState(null, "", hash ? `#${hash}` : window.location.pathname + window.location.search);
  }

  function readHash() {
    const p = new URLSearchParams(window.location.hash.slice(1));
    if (p.get("domain")) state.domain = p.get("domain");
    if (p.get("days")) state.days = Number(p.get("days")) || defaults.days;
    SELECTS.forEach((f) => { state[f] = p.get(f) || ""; });
    state.events = new Set((p.get("events") || "").split(",").filter(Boolean));
    state.q = p.get("q") || "";
  }

  function syncControls() {
    domainButtons.forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.domain === state.domain)));
    selects.forEach((s) => {
      const f = s.dataset.filter;
      const value = f === "days" ? String(state.days) : state[f];
      if (f === "days" && !Array.from(s.options).some((o) => o.value === value)) {
        s.add(new Option(`${value} derniers jours`, value));
      }
      s.value = value;
    });
    if (searchInput) searchInput.value = state.q;
    chips.forEach((c) => c.setAttribute("aria-pressed", String(state.events.has(c.dataset.event))));
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

    // Live counts: entity list ignores the entity filter, chips ignore the event filter.
    const entityCounts = countBy("entities", "entities");
    document.querySelectorAll("[data-entity-count]").forEach((el) => {
      const id = el.dataset.entityCount;
      const n = entityCounts.get(id) || 0;
      el.textContent = n;
      const li = el.closest("li");
      li.classList.toggle("zero", n === 0);
      li.classList.toggle("active", state.entities === id);
    });
    const eventCounts = countBy("events", "events");
    document.querySelectorAll("[data-event-count]").forEach((el) => {
      const n = eventCounts.get(el.dataset.eventCount) || 0;
      el.textContent = n;
      el.closest("button").classList.toggle("zero", n === 0);
    });
    if (resetButton) resetButton.hidden = isDefault();
    writeHash();
  }

  // ------------------------------------------------------------ events
  domainButtons.forEach((b) => b.addEventListener("click", () => {
    state.domain = b.dataset.domain; syncControls(); apply();
  }));
  selects.forEach((s) => s.addEventListener("change", () => {
    const f = s.dataset.filter;
    state[f] = f === "days" ? Number(s.value) : s.value;
    apply();
  }));
  chips.forEach((c) => c.addEventListener("click", () => {
    const k = c.dataset.event;
    if (state.events.has(k)) state.events.delete(k); else state.events.add(k);
    syncControls(); apply();
  }));
  let timer;
  if (searchInput) searchInput.addEventListener("input", () => {
    clearTimeout(timer);
    timer = setTimeout(() => { state.q = searchInput.value.trim(); apply(); }, 150);
  });
  if (resetButton) resetButton.addEventListener("click", () => {
    Object.assign(state, defaults, { q: "", regions: "", countries: "", entities: "", segments: "" });
    state.events = new Set();
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
