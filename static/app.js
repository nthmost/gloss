/* gloss — radial word-root explorer (vanilla JS + SVG) */
(() => {
  "use strict";

  const $ = (s) => document.querySelector(s);
  const svg = $("#graph"), tip = $("#tip"), panel = $("#panel"), empty = $("#empty");
  const qInput = $("#q"), suggest = $("#suggest");

  const REL_COLOR = {
    inh: "var(--rel-inh)", bor: "var(--rel-bor)", der: "var(--rel-der)", calque: "var(--rel-calque)",
    root: "var(--rel-root)", cog: "var(--rel-cog)", noncog: "var(--rel-noncog)", affix: "var(--rel-affix)",
    doublet: "var(--rel-doublet)", cousin: "var(--rel-cousin)",
  };
  const REL_NAME = {
    inh: "inherited", bor: "borrowed", der: "derived", calque: "calque", root: "root",
    cog: "cognate", noncog: "not cognate", affix: "formed from / forms", doublet: "doublet", cousin: "shares an ancestor",
  };

  // Where each group sits around the centre. Angles in degrees, SVG convention
  // (0 = right, 90 = down). `span` is the total arc the group may occupy.
  const SECTORS = [
    { key: "ancestors",   label: "came from",    mid: -90, span: 110 },
    { key: "descendants", label: "went on to",   mid: 90,  span: 130 },
    { key: "cognates",    label: "cognates",     mid: 180, span: 70 },
    { key: "kin",         label: "kin",          mid: 0,   span: 70 },
  ];

  const EXAMPLES = [
    ["en", "gloss"], ["grc", "γλῶσσα"], ["la", "glossa"], ["ar", "قهوة"], ["sa", "योग"],
    ["zh", "茶"], ["ine-pro", "*wódr̥"], ["ga", "uisce"], ["ja", "寿司"], ["fa", "شکر"],
  ];

  // ------------------------------------------------------------------ utils
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const trunc = (s, n) => (s && s.length > n ? s.slice(0, n - 1).trimEnd() + "…" : s || "");
  const wordUrl = (lang, word) => `/w/${encodeURIComponent(lang)}/${encodeURIComponent(word)}`;
  const wiktUrl = (lang, word, langName) => {
    const w = word.replace(/^\*/, "");
    const recon = word.startsWith("*") || /-pro$/.test(lang);
    const title = recon ? `Reconstruction:${langName}/${w}` : w;
    return `https://en.wiktionary.org/wiki/${encodeURIComponent(title).replace(/%2F/g, "/")}#${encodeURIComponent(langName.replace(/ /g, "_"))}`;
  };
  const el = (name, attrs = {}, parent) => {
    const n = document.createElementNS("http://www.w3.org/2000/svg", name);
    for (const [k, v] of Object.entries(attrs)) n.setAttribute(k, v);
    if (parent) parent.appendChild(n);
    return n;
  };
  const debounce = (fn, ms) => { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; };

  async function api(path) {
    const r = await fetch(path);
    if (!r.ok) throw new Error(`${r.status} ${path}`);
    return r.json();
  }

  // ------------------------------------------------------------------ state
  let current = null; // {lang, word}
  let graphData = null;

  function navigate(lang, word, push = true) {
    if (push) history.pushState({ lang, word }, "", wordUrl(lang, word));
    load(lang, word);
  }

  async function load(lang, word) {
    current = { lang, word };
    document.title = `${word} · gloss`;
    qInput.value = word;
    hideSuggest();
    try {
      graphData = await api(`/api/graph/${encodeURIComponent(lang)}/${encodeURIComponent(word)}`);
    } catch (e) {
      graphData = null;
      showEmpty(`Couldn't find “${esc(word)}” in ${esc(lang)}.`);
      return;
    }
    empty.hidden = true;
    panel.hidden = false;
    render(graphData);
    renderPanel(graphData);
  }

  function showEmpty(msg) {
    empty.hidden = false;
    panel.hidden = true;
    svg.replaceChildren();
    if (msg) empty.querySelector("h1").textContent = msg;
  }

  // ------------------------------------------------------------------ render: graph
  function render(d) {
    svg.replaceChildren();
    const W = svg.clientWidth || 800, H = Math.max(svg.clientHeight, 520);
    svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
    const cx = W / 2, cy = H / 2;
    const compact = W < 600;
    const labelPad = compact ? 120 : 190;             // room for outward labels
    const R1 = Math.max(120, Math.min(W, H) / 2 - labelPad);
    const R2 = R1 + (compact ? 70 : 95);              // cousin ring
    const lineH = compact ? 30 : 36;                  // vertical space per label

    const defs = el("defs", {}, svg);
    // soft halo behind centre
    const g = el("g", {}, svg);
    el("circle", { class: "center-ring", cx, cy, r: 78 }, g);

    const kin = [...d.components.map((n) => ({ ...n, grp: "components" })),
                 ...d.formations.map((n) => ({ ...n, grp: "formations" })),
                 ...d.doublets.map((n) => ({ ...n, grp: "doublets" }))];
    const groups = { ancestors: d.ancestors, descendants: d.descendants, cognates: d.cognates, kin };

    // --- ring 1: direct relations by sector
    const placed = [];
    for (const sec of SECTORS) {
      const items = groups[sec.key];
      if (!items.length) continue;
      // how many labels fit along this arc without overlapping (approx)
      const arcLen = (Math.PI * R1 * sec.span) / 180;
      const fit = Math.max(2, Math.floor(arcLen / (lineH * 0.75)));
      const show = items.slice(0, fit);
      const more = items.length - show.length;
      const n = show.length + (more > 0 ? 1 : 0);
      const span = n === 1 ? 0 : Math.min(sec.span, (n - 1) * (sec.span / Math.max(n - 1, 1)));
      const start = sec.mid - span / 2;
      const step = n > 1 ? span / (n - 1) : 0;
      // sector label
      const la = (sec.mid * Math.PI) / 180;
      el("text", { class: "sector-label", x: cx + Math.cos(la) * (R1 - 44), y: cy + Math.sin(la) * (R1 - 44) + 4,
                   "text-anchor": "middle" }, g).textContent = sec.label;
      show.forEach((node, i) => placed.push({ node, angle: start + i * step, r: R1 }));
      if (more > 0) placed.push({ node: { more, grp: sec.key, word: `+${more} more`, lang_name: "", gloss: "" }, angle: start + show.length * step, r: R1, isMore: true });
    }

    // --- ring 2: cousins, spread over the full circle avoiding the top/bottom axes a bit
    const cous = d.cousins || [];
    const fit2 = Math.max(4, Math.floor((2 * Math.PI * R2) / (lineH * 0.9)));
    const show2 = cous.slice(0, fit2);
    show2.forEach((node, i) => placed.push({ node, angle: -90 + 360 * (i + 0.5) / show2.length, r: R2, cousin: true }));

    // --- edges first (under nodes)
    for (const p of placed) {
      const a = (p.angle * Math.PI) / 180;
      const x = cx + Math.cos(a) * p.r, y = cy + Math.sin(a) * p.r;
      const rel = p.cousin ? "cousin" : p.node.rel;
      const col = REL_COLOR[rel] || "var(--rel-cousin)";
      if (p.cousin && p.node.via) {
        // cousin lines curve toward the shared ancestor's rough direction
        const q = el("path", { class: "edge cousin", d: `M${cx},${cy} Q${cx + Math.cos(a) * p.r * 0.5},${cy + Math.sin(a) * p.r * 0.5} ${x},${y}`, stroke: col }, g);
        q.dataset.key = p.node.lang + "|" + p.node.word;
      } else {
        const line = el("line", { class: "edge", x1: cx, y1: cy, x2: x, y2: y, stroke: p.isMore ? "var(--line)" : col }, g);
        line.dataset.key = p.node.lang + "|" + p.node.word;
      }
    }

    // --- nodes
    for (const p of placed) {
      const a = (p.angle * Math.PI) / 180;
      const x = cx + Math.cos(a) * p.r, y = cy + Math.sin(a) * p.r;
      const rel = p.cousin ? "cousin" : p.node.rel;
      const col = REL_COLOR[rel] || "var(--rel-cousin)";
      const node = el("g", { class: "node" + (p.isMore ? " more" : ""), transform: `translate(${x},${y})` }, g);
      el("circle", { r: p.isMore ? 3 : p.cousin ? 3.5 : 5, fill: p.isMore ? "var(--line)" : col }, node);
      const right = Math.cos(a) > 0.12, left = Math.cos(a) < -0.12;
      const anchor = right ? "start" : left ? "end" : "middle";
      const dx = right ? 10 : left ? -10 : 0;
      const dy = !right && !left ? (Math.sin(a) > 0 ? 18 : -10) : 0;
      const wordMax = p.cousin ? 16 : compact ? 16 : 22;
      const w = el("text", { class: "word" + (p.node.known === false ? " unknown" : ""), x: dx, y: dy + 4, "text-anchor": anchor }, node);
      w.textContent = p.isMore ? p.node.word : trunc(p.node.word, wordMax);
      if (!p.isMore) {
        const meta = el("text", { class: "meta", x: dx, y: dy + 17, "text-anchor": anchor }, node);
        meta.textContent = (p.node.lang_name || "").toUpperCase() + (p.node.roman ? `  ·  ${p.node.roman}` : "");
        if (p.node.gloss && !compact) {
          const gl = el("text", { class: "gloss", x: dx, y: dy + 30, "text-anchor": anchor }, node);
          gl.textContent = trunc(p.node.gloss.split(" · ")[0], p.cousin ? 26 : 34);
        }
      }
      // interaction
      if (p.isMore) {
        node.addEventListener("click", () => {
          const h = document.getElementById(`grp-${p.node.grp}`);
          if (h) { h.scrollIntoView({ behavior: "smooth", block: "start" }); h.classList.add("flash"); }
        });
      } else {
        node.addEventListener("click", () => navigate(p.node.lang, p.node.word));
        node.addEventListener("mouseenter", (ev) => showTip(ev, p.node, rel));
        node.addEventListener("mousemove", moveTip);
        node.addEventListener("mouseleave", hideTip);
      }
    }

    // --- centre
    const c = d.center;
    const cg = el("g", { class: "node center", transform: `translate(${cx},${cy})` }, g);
    const cw = el("text", { class: "word", x: 0, y: -4, "text-anchor": "middle" }, cg);
    cw.textContent = trunc(c.word, 22);
    if (c.word.length > 22) cw.textContent = c.word;
    const cm = el("text", { class: "meta", x: 0, y: 16, "text-anchor": "middle" }, cg);
    cm.textContent = (c.lang_name || c.lang).toUpperCase() + (c.roman ? `  ·  ${c.roman}` : "");
    if (c.gloss) {
      const cgl = el("text", { class: "gloss", x: 0, y: 34, "text-anchor": "middle" }, cg);
      cgl.textContent = trunc(c.gloss.split(" · ")[0], 30);
    }
  }

  // ------------------------------------------------------------------ tooltip
  function showTip(ev, node, rel) {
    const via = node.via ? ` · also from ${esc(node.via.lang_name)} ${esc(node.via.word)}` : "";
    tip.innerHTML = `<div class="r">${esc(REL_NAME[rel] || rel)}${via}</div><b>${esc(node.word)}</b>${node.roman ? ` <i>${esc(node.roman)}</i>` : ""} <span style="opacity:.7">${esc(node.lang_name)}</span>${node.gloss ? `<div>${esc(node.gloss)}</div>` : ""}`;
    tip.hidden = false;
    moveTip(ev);
    svg.querySelectorAll(".edge").forEach((e) => { e.style.opacity = e.dataset.key === node.lang + "|" + node.word ? 1 : 0.15; });
  }
  function moveTip(ev) {
    const r = $("#stage").getBoundingClientRect();
    let x = ev.clientX - r.left + 14, y = ev.clientY - r.top + 14;
    if (x + 330 > r.width) x = ev.clientX - r.left - 330;
    tip.style.left = x + "px"; tip.style.top = y + "px";
  }
  function hideTip() {
    tip.hidden = true;
    svg.querySelectorAll(".edge").forEach((e) => { e.style.opacity = ""; });
  }

  // ------------------------------------------------------------------ render: panel
  function renderPanel(d) {
    const c = d.center;
    $("#p-lang").textContent = c.lang_name || c.lang;
    $("#p-word").textContent = c.word;
    $("#p-roman").textContent = c.roman || "";
    $("#p-wikt").href = wiktUrl(c.lang, c.word, c.lang_name || c.lang);

    const ent = $("#p-entries");
    ent.innerHTML = (c.entries || []).map((e) => `
      <div class="entry">
        <div class="pos">${esc(e.pos || "")}${e.etym_no ? ` · etymology ${e.etym_no}` : ""}</div>
        <div class="g">${esc(e.gloss || "—")}</div>
        ${e.etymology ? `<details class="e"><summary>etymology</summary><div>${esc(e.etymology)}</div></details>` : ""}
      </div>`).join("") || `<div class="entry"><div class="g" style="color:var(--muted)">No entry of its own in Wiktionary — only mentioned in other words' etymologies.</div></div>`;

    const groups = [
      ["ancestors", "came from", d.ancestors, "inh"],
      ["descendants", "went on to", d.descendants, "inh"],
      ["cognates", "cognates", d.cognates, "cog"],
      ["components", "formed from", d.components, "affix"],
      ["formations", "forms part of", d.formations, "affix"],
      ["doublets", "doublets", d.doublets, "doublet"],
      ["cousins", "shares an ancestor with", d.cousins, "cousin"],
    ];
    const gp = $("#p-groups");
    gp.innerHTML = groups.filter(([, , items]) => items.length).map(([key, label, items, defRel]) => {
      const total = d.totals[key] ?? items.length;
      const li = (n, i) => `<li ${i >= 12 ? 'class="x" hidden' : ""}>
          <a href="${wordUrl(n.lang, n.word)}" data-lang="${esc(n.lang)}" data-word="${esc(n.word)}">${esc(n.word)}</a>
          <span class="l" style="color:${REL_COLOR[n.rel] || "inherit"}">${esc(n.lang_name)}${n.rel && n.rel !== defRel && n.rel !== "cousin" ? ` · ${esc(REL_NAME[n.rel] || n.rel)}` : ""}${n.via ? ` · via ${esc(n.via.word)}` : ""}</span>
          ${n.gloss ? `<span class="g">${esc(trunc(n.gloss, 110))}</span>` : ""}
        </li>`;
      return `<h3 id="grp-${key}" style="--c:${REL_COLOR[defRel]}">${label} <span style="opacity:.6">${total}</span></h3>
        <ul>${items.map(li).join("")}</ul>
        ${items.length > 12 ? `<button data-more>show all ${items.length}</button>` : ""}`;
    }).join("");
    gp.querySelectorAll("a[data-lang]").forEach((a) => a.addEventListener("click", (ev) => { ev.preventDefault(); navigate(a.dataset.lang, a.dataset.word); }));
    gp.querySelectorAll("button[data-more]").forEach((b) => b.addEventListener("click", () => {
      b.previousElementSibling.querySelectorAll(".x").forEach((li) => (li.hidden = false));
      b.remove();
    }));
  }

  // ------------------------------------------------------------------ search
  let sugItems = [], sugIdx = -1;
  const doSearch = debounce(async () => {
    const q = qInput.value.trim();
    if (q.length < 1) return hideSuggest();
    let res;
    try { res = await api(`/api/search?q=${encodeURIComponent(q)}&limit=30`); } catch { return; }
    if (qInput.value.trim() !== q) return;
    sugItems = res.results;
    sugIdx = -1;
    if (!sugItems.length) { suggest.innerHTML = `<div class="suggest-item"><span class="g">nothing found for “${esc(q)}”</span></div>`; suggest.hidden = false; return; }
    suggest.innerHTML = sugItems.map((r, i) => `
      <div class="suggest-item" data-i="${i}">
        <span class="w">${esc(r.word)}${r.roman ? ` <i style="color:var(--muted);font-size:14px">${esc(r.roman)}</i>` : ""}</span>
        <span class="l">${esc(r.lang_name)}</span>
        ${r.gloss ? `<span class="g">${esc(r.gloss)}</span>` : ""}
      </div>`).join("");
    suggest.hidden = false;
    suggest.querySelectorAll(".suggest-item[data-i]").forEach((n) => n.addEventListener("mousedown", (ev) => { ev.preventDefault(); pick(+n.dataset.i); }));
  }, 140);

  function pick(i) {
    const r = sugItems[i];
    if (!r) return;
    hideSuggest();
    navigate(r.lang, r.word);
  }
  function hideSuggest() { suggest.hidden = true; suggest.innerHTML = ""; sugItems = []; sugIdx = -1; }
  function highlight() {
    suggest.querySelectorAll(".suggest-item").forEach((n, i) => n.classList.toggle("active", i === sugIdx));
    const a = suggest.querySelector(".active"); if (a) a.scrollIntoView({ block: "nearest" });
  }

  qInput.addEventListener("input", doSearch);
  qInput.addEventListener("focus", () => { if (qInput.value.trim() && !current) doSearch(); });
  qInput.addEventListener("keydown", (ev) => {
    if (ev.key === "ArrowDown") { ev.preventDefault(); if (sugItems.length) { sugIdx = (sugIdx + 1) % sugItems.length; highlight(); } }
    else if (ev.key === "ArrowUp") { ev.preventDefault(); if (sugItems.length) { sugIdx = (sugIdx - 1 + sugItems.length) % sugItems.length; highlight(); } }
    else if (ev.key === "Escape") hideSuggest();
  });
  $("#search-form").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    if (sugItems.length) return pick(sugIdx >= 0 ? sugIdx : 0);
    const q = qInput.value.trim();
    if (!q) return;
    try {
      const res = await api(`/api/search?q=${encodeURIComponent(q)}&limit=1`);
      if (res.results.length) navigate(res.results[0].lang, res.results[0].word);
      else showEmpty(`Nothing found for “${q}”.`);
    } catch { /* ignore */ }
  });
  document.addEventListener("click", (ev) => { if (!ev.target.closest(".search")) hideSuggest(); });

  $("#random").addEventListener("click", async () => {
    try { const r = await api("/api/random"); navigate(r.lang, r.word); } catch { /* ignore */ }
  });
  $("#brand").addEventListener("click", (ev) => { ev.preventDefault(); history.pushState({}, "", "/"); current = null; graphData = null; qInput.value = ""; document.title = "gloss"; showEmpty("Where did that word come from, and where did it go?"); });

  // ------------------------------------------------------------------ legend, examples, routing
  $("#legend").innerHTML = ["inh", "bor", "der", "calque", "root", "cog", "affix", "doublet", "cousin"]
    .map((r) => `<span style="--c:${REL_COLOR[r]}">${REL_NAME[r]}</span>`).join("") +
    `<span class="meta" id="legend-meta"></span>`;
  $("#examples").innerHTML = "try " + EXAMPLES.map(([l, w]) => `<a href="${wordUrl(l, w)}" data-lang="${esc(l)}" data-word="${esc(w)}">${esc(w)}</a>`).join("");
  $("#examples").querySelectorAll("a").forEach((a) => a.addEventListener("click", (ev) => { ev.preventDefault(); navigate(a.dataset.lang, a.dataset.word); }));

  api("/api/meta").then((m) => {
    const n = (k) => Number(m[k] || 0).toLocaleString();
    $("#credit").innerHTML = `Built from <a href="https://en.wiktionary.org" target="_blank" rel="noopener">Wiktionary</a> via <a href="https://kaikki.org" target="_blank" rel="noopener">kaikki.org</a> · ${n("n_words")} words · ${n("n_edges")} connections · ${n("n_langs")} languages · data ${esc((m.built_at || "").slice(0, 10))}`;
    $("#legend-meta").textContent = `CC BY-SA 4.0 · data ${(m.built_at || "").slice(0, 10)}`;
  }).catch(() => {});

  function route() {
    const m = location.pathname.match(/^\/w\/([^/]+)\/(.+)$/);
    if (m) load(decodeURIComponent(m[1]), decodeURIComponent(m[2]));
    else showEmpty();
  }
  window.addEventListener("popstate", route);
  window.addEventListener("resize", debounce(() => { if (graphData) render(graphData); }, 120));
  route();
})();
