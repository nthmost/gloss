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
  // text measurement for collision-aware label placement
  const mctx = document.createElement("canvas").getContext("2d");
  function textW(s, font) { mctx.font = font; return mctx.measureText(s || "").width; }

  /** Build a label spec (lines + bounding box) for a node at angle a (radians). */
  function labelSpec(p, a, compact) {
    const right = Math.cos(a) > 0.2, left = Math.cos(a) < -0.2;
    const anchor = right ? "start" : left ? "end" : "middle";
    const dx = right ? 10 : left ? -10 : 0;
    const below = Math.sin(a) > 0;
    const wordMax = p.isMore ? 40 : p.cousin ? 16 : compact ? 16 : 22;
    const word = p.isMore ? p.node.word : trunc(p.node.word, wordMax);
    const meta = p.isMore ? "" : (p.node.lang_name || "").toUpperCase() + (p.node.roman && !p.cousin ? `  ·  ${p.node.roman}` : "");
    const gloss = p.isMore || p.cousin || compact || !p.node.gloss ? "" : trunc(p.node.gloss.split(" · ")[0], 34);
    const lines = [[word, "word"], ...(meta ? [[meta, "meta"]] : []), ...(gloss ? [[gloss, "gloss"]] : [])];
    const wW = textW(word, `${p.isMore ? "12px Inter" : "16px 'Gentium Plus', Georgia, serif"}`);
    const w = Math.max(wW, textW(meta, "10.5px Inter, sans-serif"), textW(gloss, "11px Inter, sans-serif")) + 4;
    const h = 6 + lines.length * 13;
    // vertical offset: side labels centre on the dot; top/bottom labels sit above/below it
    const dy = anchor === "middle" ? (below ? 14 : -(h - 4)) : -(h / 2) + 6;
    const x0 = anchor === "start" ? dx : anchor === "end" ? dx - w : -w / 2;
    return { anchor, dx, dy, lines, box: { x: x0, y: dy - 2, w, h } };
  }
  const overlaps = (a, b, pad = 4) => !(a.x + a.w + pad < b.x || b.x + b.w + pad < a.x || a.y + a.h + pad < b.y || b.y + b.h + pad < a.y);

  /**
   * Place `items` evenly across a sector on an ellipse; if labels collide, drop
   * the tail of the list (replaced by a "+n more" node) until they fit.
   */
  function placeArc(items, sec, rx, ry, cx, cy, taken, compact, opts = {}) {
    const maxStep = opts.maxStep ?? 30;
    for (let n = items.length + 1; n >= 1; n--) {
      const show = items.slice(0, n <= items.length ? n : items.length);
      const more = items.length - show.length;
      const list = show.map((node) => ({ node, cousin: !!opts.cousin }));
      if (more > 0) list.push({ node: { more, grp: sec.key, word: `+${more} more`, lang_name: "", gloss: "" }, isMore: true });
      const k = list.length;
      const full = opts.full && k > 1;
      const span = full ? 360 : Math.min(sec.span, (k - 1) * maxStep);
      const step = k > 1 ? span / (full ? k : k - 1) : 0;
      const start = full ? sec.mid : sec.mid - span / 2;
      const out = [];
      let ok = true;
      for (let i = 0; i < k && ok; i++) {
        const angle = start + i * step;
        const a = (angle * Math.PI) / 180;
        const x = cx + rx * Math.cos(a), y = cy + ry * Math.sin(a);
        const spec = labelSpec(list[i], a, compact);
        const box = { x: x + spec.box.x, y: y + spec.box.y, w: spec.box.w, h: spec.box.h };
        if (taken.some((b) => overlaps(box, b)) || out.some((o) => overlaps(box, o.box))) ok = false;
        out.push({ ...list[i], angle, x, y, spec, box });
      }
      if (ok || k === 1) { taken.push(...out.map((o) => o.box)); return out; }
    }
    return [];
  }

  function render(d) {
    svg.replaceChildren();
    const W = svg.clientWidth || 800, H = Math.max(svg.clientHeight, 520);
    svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
    const cx = W / 2, cy = H / 2;
    const compact = W < 600;
    // inner ring (direct relations) and outer ring (cousins) are ellipses so
    // wide screens get wide layouts; labels hang outward from each ring.
    const rx1 = Math.max(110, W / 2 - (compact ? 120 : 250));
    const ry1 = Math.max(110, H / 2 - (compact ? 90 : 120));
    const rx2 = Math.max(rx1 + 60, W / 2 - (compact ? 60 : 130));
    const ry2 = Math.max(ry1 + 50, H / 2 - 42);

    const g = el("g", {}, svg);
    el("circle", { class: "center-ring", cx, cy, r: 78 }, g);
    // the centre's own label box is reserved so ring labels never cover it
    const taken = [{ x: cx - 110, y: cy - 60, w: 220, h: 120 }];

    const kin = [...d.components.map((n) => ({ ...n, grp: "components" })),
                 ...d.formations.map((n) => ({ ...n, grp: "formations" })),
                 ...d.doublets.map((n) => ({ ...n, grp: "doublets" }))];
    const groups = { ancestors: d.ancestors, descendants: d.descendants, cognates: d.cognates, kin };

    // --- ring 1: direct relations by sector (biggest sectors first so they get room)
    const placed = [];
    const order = [...SECTORS].sort((a, b) => (groups[b.key].length - groups[a.key].length));
    for (const sec of order) {
      const items = groups[sec.key];
      if (!items.length) continue;
      const la = (sec.mid * Math.PI) / 180;
      el("text", { class: "sector-label", x: cx + Math.cos(la) * (rx1 - 48), y: cy + Math.sin(la) * (ry1 - 40) + 4,
                   "text-anchor": "middle" }, g).textContent = sec.label;
      placed.push(...placeArc(items, sec, rx1, ry1, cx, cy, taken, compact));
    }

    // --- ring 2: cousins around the whole outer ellipse
    const cous = d.cousins || [];
    if (cous.length) {
      placed.push(...placeArc(cous, { key: "cousins", mid: -75, span: 360 }, rx2, ry2, cx, cy, taken, compact, { cousin: true, full: true, maxStep: 360 }));
    }

    // --- edges first (under nodes)
    for (const p of placed) {
      const rel = p.cousin ? "cousin" : p.node.rel;
      const col = REL_COLOR[rel] || "var(--rel-cousin)";
      if (p.cousin) {
        const mx = cx + (p.x - cx) * 0.55, my = cy + (p.y - cy) * 0.55;
        const q = el("path", { class: "edge cousin", d: `M${cx},${cy} Q${mx},${my} ${p.x},${p.y}`, stroke: col }, g);
        q.dataset.key = p.node.lang + "|" + p.node.word;
      } else {
        const line = el("line", { class: "edge", x1: cx, y1: cy, x2: p.x, y2: p.y, stroke: p.isMore ? "var(--line)" : col }, g);
        line.dataset.key = p.node.lang + "|" + p.node.word;
      }
    }

    // --- nodes
    for (const p of placed) {
      const rel = p.cousin ? "cousin" : p.node.rel;
      const col = REL_COLOR[rel] || "var(--rel-cousin)";
      const node = el("g", { class: "node" + (p.isMore ? " more" : ""), transform: `translate(${p.x},${p.y})` }, g);
      el("circle", { r: p.isMore ? 3 : p.cousin ? 3.5 : 5, fill: p.isMore ? "var(--line)" : col }, node);
      const { anchor, dx, dy, lines } = p.spec;
      lines.forEach(([text, cls], i) => {
        const t = el("text", { class: cls + (cls === "word" && p.node.known === false ? " unknown" : ""), x: dx, y: dy + 12 + i * 13, "text-anchor": anchor }, node);
        t.textContent = text;
      });
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
