// Marché page, tenders view: filters, KPI tiles, monthly segment table, bars and tables,
// all computed in the browser from the records embedded in marche.html (#tender-data).
// Also switches between the two views of the page (tenders / press signals).
// Titles, buyers and winners come from public procurement notices: textContent only.
(function () {
  "use strict";

  const dataEl = document.getElementById("tender-data");
  if (!dataEl) return;
  const D = JSON.parse(dataEl.textContent);
  const root = document.body.dataset.root || "";
  const fmt = (n) => n.toLocaleString("fr-FR");
  const DAY = 86400000;
  const today = new Date(`${D.today}T00:00:00`);
  const daysLeft = (iso) => (iso ? Math.round((new Date(`${iso}T00:00:00`) - today) / DAY) : null);
  const norm = (s) => (s || "").normalize("NFD").replace(/[̀-ͯ]/g, "").toLowerCase();
  const segLabel = Object.fromEntries(D.segments.map((s) => [s.key, s.label]));
  const countryLabel = Object.fromEntries(D.countries.map((c) => [c.key, c.label]));

  D.records.forEach((r) => {
    r._q = norm([r.t, r.b, r.sm, r.cpv, segLabel[r.g], countryLabel[r.c], ...r.w.map((w) => w[0])].join(" "));
  });

  const state = {
    view: "appels",
    source: "",
    days: Number(document.querySelector('[data-tfilter="days"]').value),
    region: "",
    country: "",
    segment: "",
    q: "",
    monthKind: "calls",
    measure: "n",
  };

  // ------------------------------------------------------------ formatting
  function eur(v) {   // same rules as market.fmt_eur
    if (v === null || v === undefined) return "";
    for (const [div, unit] of [[1e9, "Md€"], [1e6, "M€"], [1e3, "k€"]]) {
      if (v >= div) {
        let t = v / div < 10 ? (v / div).toFixed(1) : (v / div).toFixed(0);
        if (t.includes(".")) t = t.replace(/\.?0+$/, "");
        return `${t.replace(".", ",")} ${unit}`;
      }
    }
    return `${Math.round(v)} €`;
  }
  function dateLabel(iso) {
    if (!iso) return "";
    const [y, m, d] = iso.split("-").map(Number);
    return `${d} ${D.month_names[m - 1]} ${y}`;
  }

  // ------------------------------------------------------------ DOM helpers
  function el(tag, attrs, ...children) {
    const node = document.createElement(tag);
    Object.entries(attrs || {}).forEach(([k, v]) => {
      if (v === null || v === undefined || v === false) return;
      if (k === "class") node.className = v;
      else if (k === "style") node.style.cssText = v;
      else node.setAttribute(k, v);
    });
    children.flat().forEach((c) => {
      if (c === null || c === undefined || c === "") return;
      node.append(c instanceof Node ? c : document.createTextNode(String(c)));
    });
    return node;
  }
  const swatch = (cls) => el("span", { class: `key key-rect ${cls}`, "aria-hidden": "true" });
  const empty = (text) => el("p", { class: "muted" }, text);
  const roleClass = (role) => (role === "own" ? "s-own" : role === "rival" ? "s-rival" : "s-other");

  // ------------------------------------------------------------ data slices
  function base() {
    const q = norm(state.q).trim();
    return D.records.filter((r) =>
      (!state.source || r.s === state.source)
      && (!state.region || r.r === state.region)
      && (!state.country || r.c === state.country)
      && (!state.segment || r.g === state.segment)
      && (!q || r._q.includes(q)));
  }
  const inPeriod = (r) => r.a < state.days;
  const sum = (recs) => recs.reduce((t, r) => t + (r.v || 0), 0);

  // ------------------------------------------------------------ KPI tiles
  function tile(label, value, note, cls) {
    return el("div", { class: `kpi kpi-static${cls ? ` ${cls}` : ""}` },
      el("span", { class: "kpi-label" }, label),
      el("span", { class: "kpi-value" }, value),
      el("span", { class: "kpi-delta" }, note));
  }

  function renderKpis(recs) {
    const open = recs.filter((r) => r.o);
    const soon = open.filter((r) => { const n = daysLeft(r.dl); return n !== null && n >= 0 && n <= D.closing_soon; });
    const strong = open.filter((r) => r.f === "forte").length;
    const valued = open.filter((r) => r.v);
    const awards = recs.filter((r) => r.k === "award" && inPeriod(r));
    const named = awards.filter((r) => r.w.length);
    const rival = named.filter((r) => r.w.some((w) => w[2] === "rival")).length;
    const own = named.filter((r) => r.w.some((w) => w[2] === "own")).length;
    const awardValued = awards.filter((r) => r.v);
    document.querySelector("[data-tkpis]").replaceChildren(
      tile("Appels ouverts", fmt(open.length), strong ? `dont ${fmt(strong)} à forte adéquation Airbus` : "date limite à venir"),
      tile("Clôture sous 7 jours", fmt(soon.length), soon.length ? "voir le tableau ci-dessous" : "aucune échéance proche"),
      tile("Valeur des appels ouverts", valued.length ? eur(sum(valued)) : "—",
        `${fmt(valued.length)} appel${valued.length > 1 ? "s" : ""} chiffré${valued.length > 1 ? "s" : ""} sur ${fmt(open.length)}`),
      tile("Attributions", fmt(awards.length), `sur les ${state.days} derniers jours`),
      tile("Montant attribué", awardValued.length ? eur(sum(awardValued)) : "—",
        `${fmt(awardValued.length)} attribution${awardValued.length > 1 ? "s" : ""} chiffrée${awardValued.length > 1 ? "s" : ""}`),
      tile("Part des concurrents suivis", named.length ? `${Math.round((100 * rival) / named.length)} %` : "—",
        named.length ? `${fmt(rival)} sur ${fmt(named.length)} attributions · Airbus : ${fmt(own)}` : "aucun attributaire connu"));
  }

  // ------------------------------------------------------------ monthly table (segments x months)
  function renderMonths(recs) {
    const card = document.querySelector("[data-tmonths]");
    const keep = state.monthKind === "award" ? (r) => r.k === "award" : (r) => r.k !== "award";
    const rows = D.segments.map((s) => ({
      seg: s,
      values: D.months.map((m) => recs.filter((r) => keep(r) && r.g === s.key && r.p.startsWith(m.key)).length),
    })).filter((row) => row.values.some((v) => v > 0));
    const totals = D.months.map((_, j) => rows.reduce((t, row) => t + row.values[j], 0));
    const max = Math.max(1, ...rows.flatMap((row) => row.values));
    const bin = (v) => (v === 0 ? 0 : 1 + Math.min(4, Math.floor((5 * (v - 1)) / max)));
    const what = state.monthKind === "award" ? "attributions" : "appels et préinformations";
    card.querySelector("[data-tmonths-sub]").textContent =
      `${what[0].toUpperCase()}${what.slice(1)} publiés par mois (mois en cours partiel ; hors filtre de période) · clic : filtrer sur le segment`;
    const box = card.querySelector("[data-theat]");
    if (!rows.length) {
      box.replaceChildren(empty("Aucun avis sur cette sélection."));
      card.querySelector("[data-tscale]").replaceChildren();
      return;
    }
    box.replaceChildren(el("table", { class: "heat" },
      el("thead", {}, el("tr", {}, el("th", { scope: "col" }, "Segment"),
        ...D.months.map((m) => el("th", { scope: "col" }, m.label)))),
      el("tbody", {}, ...rows.map((row) => el("tr", {}, el("th", { scope: "row" }, row.seg.label),
        ...row.values.map((v, j) => el("td", { class: `h${bin(v)}` }, v
          ? el("button", { type: "button", "data-seg": row.seg.key,
            title: `${row.seg.label} · ${D.months[j].label} : ${v} ${what}` }, fmt(v))
          : el("span", { "aria-label": "0" }, "·")))))),
      el("tfoot", {}, el("tr", {}, el("th", { scope: "row" }, "Total"),
        ...totals.map((t) => el("td", { class: "heat-total" }, fmt(t)))))));
    box.querySelectorAll("button[data-seg]").forEach((b) => b.addEventListener("click", () => {
      setFilter("segment", state.segment === b.dataset.seg ? "" : b.dataset.seg);
    }));
    card.querySelector("[data-tscale]").replaceChildren(el("span", {}, "Moins"),
      ...[1, 2, 3, 4, 5].map((b) => el("span", { class: `scale-step h${b}`, "aria-hidden": "true" })),
      el("span", {}, `Plus (max ${fmt(max)})`));
  }

  // ------------------------------------------------------------ horizontal bars
  function bar(opts) {
    // opts: name, value (number for the width), text, max, cls, href | onClick, title, pressed
    const width = opts.max ? (100 * opts.value) / opts.max : 0;
    const tag = opts.href ? "a" : opts.onClick ? "button" : "div";
    const node = el(tag, {
      class: `hbar${opts.onClick ? " hbar-button" : ""}`, href: opts.href || null,
      type: tag === "button" ? "button" : null, title: opts.title || null,
      "aria-pressed": opts.onClick ? String(Boolean(opts.pressed)) : null,
    },
    el("span", { class: "hbar-label" }, opts.name),
    el("span", { class: "hbar-track" }, el("span", { class: "hbar-fill", style: `width:${width}%` },
      el("span", { class: `hseg ${opts.cls || "s-total"}`, style: "flex-grow:1" }))),
    el("span", { class: "hbar-value" }, opts.text));
    if (opts.onClick) node.addEventListener("click", opts.onClick);
    return node;
  }

  function renderCountries(recs) {
    const card = document.querySelector("[data-tcountries]");
    const by = new Map();
    recs.filter(inPeriod).forEach((r) => {
      if (!r.c) return;
      const row = by.get(r.c) || { code: r.c, n: 0, v: 0 };
      row.n += 1; row.v += r.v || 0;
      by.set(r.c, row);
    });
    const m = state.measure;
    const rows = [...by.values()].filter((r) => r[m] > 0).sort((a, b) => b[m] - a[m] || b.n - a.n)
      .slice(0, D.top.countries);
    const max = Math.max(1, ...rows.map((r) => r[m]));
    const box = card.querySelector("[data-bars]");
    box.classList.toggle("wide-values", m === "v");
    card.querySelector("[data-tcountries-sub]").textContent = m === "v"
      ? `Montant des avis chiffrés (estimé ou attribué) sur ${state.days} jours · clic : filtrer sur le pays`
      : `Avis publiés sur ${state.days} jours · clic : filtrer sur le pays`;
    box.replaceChildren(...(rows.length ? rows.map((r) => bar({
      name: countryLabel[r.code] || r.code, value: r[m], max, text: m === "v" ? eur(r.v) : fmt(r.n),
      title: `${countryLabel[r.code] || r.code} : ${fmt(r.n)} avis${r.v ? ` · ${eur(r.v)}` : ""}`,
      pressed: state.country === r.code,
      onClick: () => setFilter("country", state.country === r.code ? "" : r.code),
    })) : [empty(m === "v" ? "Aucun montant publié sur cette sélection." : "Aucun avis sur cette sélection.")]));
  }

  function renderWinners(recs) {
    const by = new Map();
    recs.filter((r) => r.k === "award" && inPeriod(r)).forEach((r) => {
      r.w.forEach(([name, id, role]) => {
        const key = id || norm(name);
        const row = by.get(key) || { name: id ? D.names[id] || name : name, id, role, n: 0, v: 0 };
        row.n += 1; row.v += r.v || 0;
        by.set(key, row);
      });
    });
    const rows = [...by.values()].sort((a, b) => b.n - a.n || b.v - a.v).slice(0, D.top.winners);
    const max = Math.max(1, ...rows.map((r) => r.n));
    document.querySelector("[data-twinners] [data-bars]").replaceChildren(...(rows.length ? rows.map((r) => bar({
      name: r.name, value: r.n, max, text: fmt(r.n), cls: roleClass(r.role),
      href: r.id ? `${root}entite/${r.id}.html` : null,
      title: `${r.name} : ${fmt(r.n)} attribution${r.n > 1 ? "s" : ""}${r.v ? ` · ${eur(r.v)} (montant total des avis)` : ""}`
        + (r.role === "own" ? " · Airbus" : r.role === "rival" ? " · concurrent suivi" : ""),
    })) : [empty("Aucune attribution avec attributaire connu sur cette sélection.")]));
  }

  function renderBuyers(recs) {
    const by = new Map();
    recs.filter(inPeriod).forEach((r) => {
      if (!r.b) return;
      const key = r.be || norm(r.b);
      const row = by.get(key) || { name: r.be ? D.names[r.be] || r.b : r.b, id: r.be, country: r.c, n: 0, v: 0 };
      row.n += 1; row.v += r.v || 0;
      by.set(key, row);
    });
    const rows = [...by.values()].sort((a, b) => b.n - a.n || b.v - a.v).slice(0, D.top.buyers);
    const max = Math.max(1, ...rows.map((r) => r.n));
    document.querySelector("[data-tbuyers] [data-bars]").replaceChildren(...(rows.length ? rows.map((r) => bar({
      name: r.name, value: r.n, max, text: fmt(r.n), href: r.id ? `${root}entite/${r.id}.html` : null,
      title: `${r.name}${r.country ? ` (${countryLabel[r.country] || r.country})` : ""} : ${fmt(r.n)} avis`
        + (r.v ? ` · ${eur(r.v)}` : ""),
    })) : [empty("Aucun acheteur sur cette sélection.")]));
  }

  // ------------------------------------------------------------ tables
  function objectCell(r) {
    return el("td", { class: "t-object" },
      el("a", { href: r.u, target: "_blank", rel: "noopener noreferrer" }, r.t),
      r.sm ? el("span", { class: "t-summary" }, r.sm) : (r.cpv ? el("span", { class: "t-summary" }, r.cpv) : ""),
      r.f ? el("span", { class: `fit-badge fit-${r.f}` }, `Adéquation ${r.f}`) : "",
      r.an ? "" : el("span", { class: "t-pending" }, "en attente d'analyse"));
  }
  function buyerCell(r) {
    return el("td", {}, r.be ? el("a", { href: `${root}entite/${r.be}.html` }, r.b) : r.b);
  }
  function countryCell(r) {
    return el("td", {}, r.c ? el("a", { href: `${root}pays/${r.c}.html` }, countryLabel[r.c] || r.c) : "—");
  }
  function table(head, rows) {
    return el("table", { class: "tender-table" },
      el("thead", {}, el("tr", {}, ...head.map((h) => el("th", { scope: "col", class: h[1] || null }, h[0])))),
      el("tbody", {}, ...rows));
  }

  function renderOpen(recs) {
    const open = recs.filter((r) => r.o).sort((a, b) =>
      (a.dl || "9999").localeCompare(b.dl || "9999") || (b.f === "forte") - (a.f === "forte"));
    const shown = open.slice(0, D.top.open);
    const box = document.querySelector("[data-topen] [data-table]");
    if (!open.length) { box.replaceChildren(empty("Aucun appel d'offres ouvert sur cette sélection.")); return; }
    box.replaceChildren(table(
      [["Échéance"], ["Objet"], ["Acheteur"], ["Pays"], ["Segment"], ["Valeur estimée", "num"], ["Source"]],
      shown.map((r) => {
        const n = daysLeft(r.dl);
        return el("tr", {},
          el("td", { class: "t-date" }, r.dl ? dateLabel(r.dl) : "non précisée",
            n !== null ? el("span", { class: `t-left${n <= D.closing_soon ? " soon" : ""}` }, n === 0 ? "aujourd'hui" : `J-${n}`) : ""),
          objectCell(r), buyerCell(r), countryCell(r),
          el("td", {}, segLabel[r.g] || ""),
          el("td", { class: "num" }, r.v ? eur(r.v) : "—"),
          el("td", { class: "t-source" }, D.sources[r.s], r.k === "planned" ? el("span", { class: "t-kind" }, D.kinds.planned) : ""));
      })),
    open.length > shown.length ? el("p", { class: "muted table-more" },
      `${fmt(open.length - shown.length)} autres appels ouverts : affinez les filtres pour les voir.`) : "");
  }

  function renderAwards(recs) {
    const awards = recs.filter((r) => r.k === "award" && inPeriod(r)).sort((a, b) => b.p.localeCompare(a.p));
    const shown = awards.slice(0, D.top.awards);
    const box = document.querySelector("[data-tawards] [data-table]");
    if (!awards.length) { box.replaceChildren(empty("Aucune attribution sur cette sélection.")); return; }
    box.replaceChildren(table(
      [["Date"], ["Objet"], ["Acheteur"], ["Pays"], ["Attributaire(s)"], ["Montant", "num"]],
      shown.map((r) => el("tr", {},
        el("td", { class: "t-date" }, dateLabel(r.p)),
        objectCell(r), buyerCell(r), countryCell(r),
        el("td", { class: "t-winners" }, r.w.length ? r.w.slice(0, 4).map(([name, id, role]) => el("span", { class: "t-winner" },
          role ? swatch(roleClass(role)) : "",
          id ? el("a", { href: `${root}entite/${id}.html` }, name) : name)) : "non publié",
        r.w.length > 4 ? el("span", { class: "t-winner muted" }, `+ ${r.w.length - 4}`) : ""),
        el("td", { class: "num" }, r.v ? eur(r.v) : "—")))),
    awards.length > shown.length ? el("p", { class: "muted table-more" },
      `${fmt(awards.length - shown.length)} autres attributions : affinez les filtres pour les voir.`) : "");
  }

  // ------------------------------------------------------------ state, hash, wiring
  const HASH = { vue: "view", src: "source", days: "days", region: "region", pays: "country", seg: "segment", q: "q" };

  function readHash() {
    const p = new URLSearchParams(location.hash.slice(1));
    Object.entries(HASH).forEach(([k, field]) => {
      if (!p.has(k)) return;
      state[field] = field === "days" ? Number(p.get(k)) || state.days : p.get(k);
    });
    if (!D.periods.includes(state.days)) state.days = Number(document.querySelector('[data-tfilter="days"]').value);
    if (!["appels", "presse"].includes(state.view)) state.view = "appels";
  }
  function writeHash() {
    const p = new URLSearchParams();
    const dflt = Number(document.querySelector('[data-tfilter="days"] option[selected]')?.value);
    Object.entries(HASH).forEach(([k, field]) => {
      const v = state[field];
      if (!v || (field === "view" && v === "appels") || (field === "days" && v === dflt)) return;
      p.set(k, v);
    });
    const hash = p.toString();
    history.replaceState(null, "", hash ? `#${hash}` : location.pathname + location.search);
  }
  function syncControls() {
    document.querySelectorAll("[data-tsource]").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.tsource === state.source)));
    document.querySelectorAll("[data-tfilter]").forEach((s) => {
      const v = String(state[s.dataset.tfilter] || "");
      if (s.tagName === "SELECT" && ![...s.options].some((o) => o.value === v)) return;
      if (s.value !== v) s.value = v;
    });
    document.querySelectorAll("[data-tmonth-kind]").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.tmonthKind === state.monthKind)));
    document.querySelectorAll("[data-tmeasure]").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.tmeasure === state.measure)));
    document.querySelectorAll("[data-view]").forEach((b) => b.setAttribute("aria-selected", String(b.dataset.view === state.view)));
    document.querySelectorAll("[data-view-panel]").forEach((p) => { p.hidden = p.dataset.viewPanel !== state.view; });
  }
  function render() {
    syncControls();
    const recs = base();
    renderKpis(recs);
    renderMonths(recs);
    renderCountries(recs);
    renderWinners(recs);
    renderBuyers(recs);
    renderOpen(recs);
    renderAwards(recs);
  }
  function setFilter(field, value) {
    state[field] = value;
    writeHash();
    render();
  }

  document.querySelectorAll("[data-tsource]").forEach((b) => b.addEventListener("click", () => setFilter("source", b.dataset.tsource)));
  document.querySelectorAll("select[data-tfilter]").forEach((s) => s.addEventListener("change", () =>
    setFilter(s.dataset.tfilter, s.dataset.tfilter === "days" ? Number(s.value) : s.value)));
  let typing;
  document.querySelector('input[data-tfilter="q"]').addEventListener("input", (e) => {
    clearTimeout(typing);
    typing = setTimeout(() => setFilter("q", e.target.value), 150);
  });
  document.querySelectorAll("[data-tmonth-kind]").forEach((b) => b.addEventListener("click", () => setFilter("monthKind", b.dataset.tmonthKind)));
  document.querySelectorAll("[data-tmeasure]").forEach((b) => b.addEventListener("click", () => setFilter("measure", b.dataset.tmeasure)));
  document.querySelectorAll("[data-view]").forEach((b) => b.addEventListener("click", () => setFilter("view", b.dataset.view)));
  window.addEventListener("hashchange", () => { readHash(); render(); });

  readHash();
  render();
})();
