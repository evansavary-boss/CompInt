// Marché tab: aggregates the embedded records in the browser so that the domain,
// period and region filters re-scope every tile, chart, heatmap and list at once.
// Labels come from news titles: always inserted with textContent, never innerHTML.
(function () {
  "use strict";

  const dataEl = document.getElementById("market-data");
  if (!dataEl) return;
  const D = JSON.parse(dataEl.textContent);
  const root = document.body.dataset.root || "";
  const SVG = "http://www.w3.org/2000/svg";
  const fmt = (n) => n.toLocaleString("fr-FR");

  const state = { domain: "all", days: Number(document.querySelector('[data-mfilter="days"]').value), region: "" };

  // ------------------------------------------------------------ DOM helpers
  function el(tag, attrs, ...children) {
    const node = document.createElement(tag);
    Object.entries(attrs || {}).forEach(([k, v]) => {
      if (v === null || v === undefined || v === false) return;
      if (k === "class") node.className = v;
      else if (k === "style") node.style.cssText = v;
      else node.setAttribute(k, v);
    });
    children.flat().forEach((c) => node.append(c instanceof Node ? c : document.createTextNode(String(c))));
    return node;
  }
  function svg(tag, attrs) {
    const node = document.createElementNS(SVG, tag);
    Object.entries(attrs || {}).forEach(([k, v]) => node.setAttribute(k, v));
    return node;
  }
  const seriesKeys = D.series.map((s) => s.key);
  const label = (key) => (D.events.find((e) => e.key === key) || { label: key }).label;
  const swatch = (key, shape) => el("span", { class: `key key-${shape} s-${key}`, "aria-hidden": "true" });

  // Link to the Économie feed with the matching filters.
  function feedLink(extra) {
    const p = new URLSearchParams({ domain: state.domain, days: state.days });
    if (state.region) p.set("regions", state.region);
    Object.entries(extra).forEach(([k, v]) => p.set(k, v));
    return `${root}economie.html#${p.toString()}`;
  }

  // ------------------------------------------------------------ data slice
  function slice(offset) {
    const lo = offset || 0;
    return D.records.filter((r) =>
      r.a >= lo && r.a < lo + state.days
      && (state.domain === "all" || r.t.includes(state.domain))
      && (!state.region || r.r.includes(state.region)));
  }
  const countEvent = (recs, key) => recs.reduce((n, r) => n + (r.e.includes(key) ? 1 : 0), 0);

  // ------------------------------------------------------------ KPI tiles
  function renderKpis(recs) {
    const box = document.querySelector("[data-kpis]");
    const comparable = D.complete_days >= 2 * state.days;
    const prev = comparable ? slice(state.days) : null;
    box.replaceChildren(...D.series.map((s) => {
      const value = countEvent(recs, s.key);
      let delta;
      if (comparable) {
        const d = value - countEvent(prev, s.key);
        delta = `${d > 0 ? "+" : d < 0 ? "−" : "±"}${fmt(Math.abs(d))} vs les ${state.days} j précédents`;
      } else {
        delta = "Comparaison dès 2 périodes collectées";
      }
      return el("a", { class: "kpi", href: feedLink({ events: s.key }) },
        el("span", { class: "kpi-label" }, swatch(s.key, "rect"), s.label),
        el("span", { class: "kpi-value" }, fmt(value)),
        el("span", { class: "kpi-delta" }, delta));
    }));
  }

  // ------------------------------------------------------------ weekly stacked columns
  function niceMax(v) {
    if (v <= 4) return Math.max(v, 1);
    const p = Math.pow(10, Math.floor(Math.log10(v)));
    const n = v / p;
    return (n <= 1 ? 1 : n <= 2 ? 2 : n <= 5 ? 5 : 10) * p;
  }

  function renderWeekly(recs) {
    const card = document.querySelector("[data-weekly]");
    const nWeeks = Math.max(1, Math.min(Math.ceil(state.days / 7), Math.ceil(D.coverage_days / 7)));
    const weeks = [];
    for (let w = nWeeks - 1; w >= 0; w--) {
      const inWeek = recs.filter((r) => Math.floor(r.a / 7) === w);
      const values = seriesKeys.map((k) => countEvent(inWeek, k));
      weeks.push({ label: D.weeks[D.weeks.length - 1 - w], values, total: values.reduce((a, b) => a + b, 0) });
    }

    card.querySelector("[data-legend]").replaceChildren(
      ...D.series.map((s) => el("span", { class: "legend-item" }, swatch(s.key, "rect"), s.label)));

    // Table view: the accessible twin of the chart.
    card.querySelector("[data-table]").replaceChildren(el("table", { class: "data-table" },
      el("thead", {}, el("tr", {}, el("th", { scope: "col" }, "Semaine du"),
        ...D.series.map((s) => el("th", { scope: "col" }, s.label)), el("th", { scope: "col" }, "Total"))),
      el("tbody", {}, ...weeks.map((w) => el("tr", {}, el("th", { scope: "row" }, w.label),
        ...w.values.map((v) => el("td", {}, fmt(v))), el("td", {}, fmt(w.total)))))));

    const body = card.querySelector("[data-chart]");
    const W = Math.max(280, body.clientWidth);
    const H = 220, axisH = 26, left = 30, right = 8, top = 16;
    const plotW = W - left - right, plotH = H - top;
    const max = niceMax(Math.max(...weeks.map((w) => w.total), 1));
    const y = (v) => top + plotH - (v / max) * plotH;
    const band = plotW / weeks.length;
    const barW = Math.min(24, band * 0.6);
    const s = svg("svg", { viewBox: `0 0 ${W} ${H + axisH}`, width: W, height: H + axisH, role: "img",
      "aria-label": "Articles par semaine et par type d'événement (détail dans le tableau)" });

    const ticks = max <= 4 ? Array.from({ length: max + 1 }, (_, i) => i) : [0, max / 4, max / 2, (3 * max) / 4, max];
    ticks.forEach((t) => {
      s.append(svg("line", { x1: left, x2: W - right, y1: y(t), y2: y(t), class: t === 0 ? "axis" : "grid" }));
      const tx = svg("text", { x: left - 6, y: y(t) + 4, class: "tick", "text-anchor": "end" });
      tx.textContent = fmt(t);
      s.append(tx);
    });

    const every = weeks.length > 8 ? 2 : 1;
    weeks.forEach((w, i) => {
      const cx = left + band * i + band / 2;
      const x = cx - barW / 2;
      let acc = 0;
      const drawn = w.values.map((v, k) => ({ v, k })).filter((d) => d.v > 0);
      drawn.forEach((d, j) => {
        const y0 = y(acc), y1 = y(acc + d.v);
        acc += d.v;
        const isTop = j === drawn.length - 1;
        const gap = j > 0 ? 2 : 0;                      // 2px surface gap between segments
        const bottom = y0 - gap, h = Math.max(0, bottom - y1);
        if (h <= 0) return;
        const r = isTop ? Math.min(4, h, barW / 2) : 0;
        const path = `M${x},${bottom} L${x},${y1 + r} Q${x},${y1} ${x + r},${y1} L${x + barW - r},${y1}`
          + ` Q${x + barW},${y1} ${x + barW},${y1 + r} L${x + barW},${bottom} Z`;
        s.append(svg("path", { d: path, class: `mark s-${seriesKeys[d.k]}` }));
      });
      if (i % every === 0 || i === weeks.length - 1) {
        const lx = svg("text", { x: cx, y: H + 17, class: "tick", "text-anchor": "middle" });
        lx.textContent = w.label;
        s.append(lx);
      }
      // Selective direct label: the most recent week's total.
      if (i === weeks.length - 1 && w.total > 0) {
        const vl = svg("text", { x: cx, y: y(w.total) - 5, class: "value-label", "text-anchor": "middle" });
        vl.textContent = fmt(w.total);
        s.append(vl);
      }
      const hit = svg("rect", { x: left + band * i, y: top, width: band, height: plotH, class: "hit",
        tabindex: 0, "aria-label": `Semaine du ${w.label} : ${w.total} articles` });
      hit.addEventListener("pointerenter", () => showWeekTip(card, w, cx));
      hit.addEventListener("focus", () => showWeekTip(card, w, cx));
      hit.addEventListener("pointerleave", () => hideTip(card));
      hit.addEventListener("blur", () => hideTip(card));
      s.append(hit);
    });
    body.replaceChildren(s);
  }

  function showWeekTip(card, w, cx) {
    const tip = card.querySelector(".tip");
    tip.replaceChildren(el("p", { class: "tip-title" }, `Semaine du ${w.label}`),
      ...D.series.map((s, k) => el("p", { class: "tip-row" }, swatch(s.key, "line"),
        el("strong", {}, fmt(w.values[k])), el("span", {}, s.label))),
      el("p", { class: "tip-row tip-total" }, el("strong", {}, fmt(w.total)), el("span", {}, "au total")));
    placeTip(card, tip, cx + card.querySelector("[data-chart]").offsetLeft);
  }
  function placeTip(card, tip, x) {
    tip.hidden = false;
    const max = card.clientWidth - tip.offsetWidth - 8;
    tip.style.left = `${Math.max(8, Math.min(max, x + 12))}px`;
  }
  const hideTip = (card) => { card.querySelector(".tip").hidden = true; };

  // ------------------------------------------------------------ horizontal bars
  function hbar(opts) {
    // opts: name, href, total, max, segments [{key, v}] | null (single neutral bar), cls
    const segs = opts.segments
      ? opts.segments.filter((s) => s.v > 0).map((s) => el("span", {
        class: `hseg s-${s.key}`, style: `flex-grow:${s.v}` }))
      : [el("span", { class: `hseg ${opts.cls || "s-total"}`, style: "flex-grow:1" })];
    const width = opts.max ? (100 * opts.total) / opts.max : 0;
    return el("a", { class: "hbar", href: opts.href, "data-tip": opts.tip || null },
      el("span", { class: "hbar-label" }, opts.name),
      el("span", { class: "hbar-track" }, el("span", { class: "hbar-fill", style: `width:${width}%` }, ...segs)),
      el("span", { class: "hbar-value" }, fmt(opts.total)));
  }

  function renderRegions(recs) {
    const counts = D.regions.map((r) => ({ ...r, n: recs.filter((x) => x.e.length && x.r.includes(r.key)).length }))
      .filter((r) => r.n > 0).sort((a, b) => b.n - a.n);
    const max = Math.max(1, ...counts.map((r) => r.n));
    const box = document.querySelector("[data-regions] [data-bars]");
    box.replaceChildren(...(counts.length ? counts.map((r) => hbar({ name: r.label, total: r.n, max,
      href: `${root}economie.html#${new URLSearchParams({ domain: state.domain, days: state.days, regions: r.key })}` }))
      : [el("p", { class: "muted" }, "Aucun événement sur cette sélection.")]));
  }

  function renderCompanies(recs) {
    const by = new Map();
    recs.forEach((r) => {
      if (!r.e.length) return;
      r.c.forEach((id) => {
        const row = by.get(id) || { id, total: 0, parts: Object.fromEntries([...seriesKeys, "other"].map((k) => [k, 0])) };
        r.e.forEach((ev) => { row.parts[seriesKeys.includes(ev) ? ev : "other"] += 1; row.total += 1; });
        by.set(id, row);
      });
    });
    const rows = [...by.values()].sort((a, b) => b.total - a.total || D.names[a.id].localeCompare(D.names[b.id]))
      .slice(0, D.top_companies);
    const max = Math.max(1, ...rows.map((r) => r.total));
    const card = document.querySelector("[data-companies]");
    const legend = el("div", { class: "legend" }, ...D.series.map((s) => el("span", { class: "legend-item" },
      swatch(s.key, "rect"), s.label)), el("span", { class: "legend-item" }, swatch("other", "rect"), "Autres types"));
    const bars = rows.map((r) => {
      const node = hbar({ name: D.names[r.id], href: `${root}entite/${r.id}.html`, total: r.total, max,
        segments: [...seriesKeys, "other"].map((k) => ({ key: k, v: r.parts[k] })) });
      const parts = [...seriesKeys, "other"].filter((k) => r.parts[k]);
      node.addEventListener("pointerenter", () => {
        const tip = card.querySelector(".tip");
        tip.replaceChildren(el("p", { class: "tip-title" }, D.names[r.id]),
          ...parts.map((k) => el("p", { class: "tip-row" }, swatch(k, "line"), el("strong", {}, fmt(r.parts[k])),
            el("span", {}, k === "other" ? "autres types" : label(k)))));
        tip.hidden = false;
        tip.style.left = "auto"; tip.style.right = "12px";
        tip.style.top = `${node.offsetTop + node.offsetHeight + 4}px`;
      });
      node.addEventListener("pointerleave", () => { card.querySelector(".tip").hidden = true; });
      return node;
    });
    card.querySelector("[data-bars]").replaceChildren(legend,
      ...(bars.length ? bars : [el("p", { class: "muted" }, "Aucune entreprise suivie avec un événement sur cette sélection.")]));
  }

  function renderCustomers(recs) {
    const by = new Map();
    recs.forEach((r) => { if (r.e.includes("contract_won")) r.i.forEach((id) => by.set(id, (by.get(id) || 0) + 1)); });
    const rows = [...by.entries()].sort((a, b) => b[1] - a[1]).slice(0, D.top_customers);
    const max = Math.max(1, ...rows.map((r) => r[1]));
    document.querySelector("[data-customers] [data-bars]").replaceChildren(...(rows.length
      ? rows.map(([id, n]) => hbar({ name: D.names[id], href: `${root}entite/${id}.html`, total: n, max, cls: "s-contract_won" }))
      : [el("p", { class: "muted" }, "Aucune institution citée dans un contrat sur cette sélection.")]));
  }

  // ------------------------------------------------------------ heatmap
  function renderHeatmap(recs) {
    const events = D.events.filter((e) => recs.some((r) => r.e.includes(e.key)));
    const cells = D.segments.map((s) => ({ seg: s, values: events.map((e) =>
      recs.filter((r) => r.s.includes(s.key) && r.e.includes(e.key)).length) }))
      .filter((row) => row.values.some((v) => v > 0));
    const max = Math.max(1, ...cells.flatMap((c) => c.values));
    const bin = (v) => (v === 0 ? 0 : 1 + Math.min(4, Math.floor((5 * (v - 1)) / max)));
    const box = document.querySelector("[data-heat]");
    if (!cells.length) {
      box.replaceChildren(el("p", { class: "muted" }, "Aucun événement rattaché à un segment sur cette sélection."));
      document.querySelector("[data-scale]").replaceChildren();
      return;
    }
    box.replaceChildren(el("table", { class: "heat" },
      el("thead", {}, el("tr", {}, el("th", { scope: "col" }, "Segment"),
        ...events.map((e) => el("th", { scope: "col" }, e.label)))),
      el("tbody", {}, ...cells.map((row) => el("tr", {}, el("th", { scope: "row" }, row.seg.label),
        ...row.values.map((v, j) => el("td", { class: `h${bin(v)}` }, v
          ? el("a", { href: feedLink({ segments: row.seg.key, events: events[j].key }),
            title: `${row.seg.label} · ${events[j].label} : ${v}` }, fmt(v))
          : el("span", { "aria-label": "0" }, "·"))))))));
    document.querySelector("[data-scale]").replaceChildren(el("span", {}, "Moins"),
      ...[1, 2, 3, 4, 5].map((b) => el("span", { class: `scale-step h${b}`, "aria-hidden": "true" })),
      el("span", {}, `Plus (max ${fmt(max)})`));
  }

  // ------------------------------------------------------------ latest lists
  function renderLists(recs) {
    const sorted = [...recs].sort((a, b) => a.a - b.a);
    document.querySelector("[data-lists]").replaceChildren(...D.lists.map((l) => {
      const rows = sorted.filter((r) => r.e.includes(l.key)).slice(0, l.n);
      return el("section", { class: "card list-card" },
        el("h2", {}, swatch(l.key, "rect"), `Derniers : ${l.label.toLowerCase()}`),
        rows.length ? el("ul", {}, ...rows.map((r) => el("li", {},
          el("a", { href: r.u, target: "_blank", rel: "noopener noreferrer" }, r.ti),
          el("span", { class: "list-meta" }, `${r.d} · ${r.so}`),
          r.c.length ? el("span", { class: "list-chips" }, ...r.c.slice(0, 3).map((id) =>
            el("a", { class: "chip chip-company", href: `${root}entite/${id}.html` }, D.names[id]))) : "")))
          : el("p", { class: "muted" }, "Rien sur cette sélection."),
        el("a", { class: "more", href: feedLink({ events: l.key }) }, "Tous les articles →"));
    }));
  }

  // ------------------------------------------------------------ orchestration
  function render() {
    const recs = slice(0);
    const since = D.complete_days < state.days
      ? `Collecte quotidienne depuis le ${D.complete_since} : avant cette date, l'historique vient d'un `
        + "rattrapage limité et les semaines anciennes sont sous-représentées." : "";
    document.querySelector("[data-coverage]").textContent = since;
    renderKpis(recs);
    renderWeekly(recs);
    renderRegions(recs);
    renderHeatmap(recs);
    renderCompanies(recs);
    renderCustomers(recs);
    renderLists(recs);
  }

  document.querySelectorAll("[data-market-filters] [data-domain]").forEach((b) => b.addEventListener("click", () => {
    state.domain = b.dataset.domain;
    document.querySelectorAll("[data-market-filters] [data-domain]").forEach((o) =>
      o.setAttribute("aria-pressed", String(o === b)));
    render();
  }));
  document.querySelectorAll("[data-mfilter]").forEach((s) => s.addEventListener("change", () => {
    state[s.dataset.mfilter] = s.dataset.mfilter === "days" ? Number(s.value) : s.value;
    render();
  }));
  const toggle = document.querySelector("[data-table-toggle]");
  toggle.addEventListener("click", () => {
    const on = toggle.getAttribute("aria-pressed") !== "true";
    toggle.setAttribute("aria-pressed", String(on));
    document.querySelector("[data-weekly] [data-table]").hidden = !on;
    document.querySelector("[data-weekly] [data-chart]").hidden = on;
  });
  let resizeTimer;
  new ResizeObserver(() => { clearTimeout(resizeTimer); resizeTimer = setTimeout(() => renderWeekly(slice(0)), 120); })
    .observe(document.querySelector("[data-weekly] [data-chart]"));

  render();
})();
