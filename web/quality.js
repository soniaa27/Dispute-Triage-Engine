// Quality over time: GET /quality -> stat tiles, one line chart (shared 0-1 axis), tables.
"use strict";

const SERIES = [
  { key: "groundedness", label: "Groundedness", color: "var(--series-1)" },
  { key: "faithfulness", label: "Faithfulness", color: "var(--series-2)" },
  { key: "context_relevance", label: "Context relevance", color: "var(--series-3)" },
  { key: "hallucination_rate", label: "Hallucination rate (lower is better)", short: "Hallucination", color: "var(--series-4)" },
];
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const pct = (v) => (v == null ? "–" : `${Math.round(v * 100)}%`);

let data = null;

function bucketLabel(iso, bucket) {
  const d = new Date(iso);
  const opts = bucket === "hour"
    ? { hour: "2-digit", minute: "2-digit", timeZone: "Asia/Kolkata" }
    : { day: "numeric", month: "short", timeZone: "Asia/Kolkata" };
  return new Intl.DateTimeFormat("en-IN", opts).format(d);
}

function renderTiles(o) {
  const tiles = [
    ["Runs scored", o.runs_scored, o.judge_errors ? `${o.judge_errors} judge errors` : "in range"],
    ["Groundedness", pct(o.groundedness), "claims supported"],
    ["Faithfulness", pct(o.faithfulness), "routes that follow"],
    ["Hallucination", pct(o.hallucination_rate), "lower is better"],
    ["Analyst satisfied", pct(o.satisfactory_rate), `${o.feedback} feedback`],
  ];
  $("q-tiles").innerHTML = tiles.map(([l, v, n]) =>
    `<div class="tile"><div class="tile__label">${esc(l)}</div><div class="tile__value">${esc(v)}</div><div class="tile__note">${esc(n)}</div></div>`).join("");
}

function renderChart() {
  const root = $("q-chart");
  const buckets = data.buckets;
  if (!buckets.some((b) => b.runs_scored > 0)) {
    root.innerHTML = '<div class="q-empty">No runs scored in this range yet. The quality service scores a sample of finished runs every <code>QUALITY_INTERVAL_SECONDS</code>.</div>';
    return;
  }
  const W = Math.max(root.clientWidth || 700, 320), H = 280;
  const m = { t: 12, r: W < 520 ? 12 : 104, b: 28, l: 40 };
  const iw = W - m.l - m.r, ih = H - m.t - m.b;
  const n = buckets.length;
  const x = (i) => m.l + (n === 1 ? iw / 2 : (i / (n - 1)) * iw);
  const y = (v) => m.t + (1 - v) * ih;

  let svg = `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="Judge scores per ${esc(data.bucket)}; see the table view for values">`;
  for (const t of [0, 0.25, 0.5, 0.75, 1]) {
    svg += `<line x1="${m.l}" x2="${m.l + iw}" y1="${y(t)}" y2="${y(t)}" stroke="var(--grid)" stroke-width="1"/>`;
    svg += `<text x="${m.l - 6}" y="${y(t) + 4}" text-anchor="end">${t * 100}%</text>`;
  }
  svg += `<line x1="${m.l}" x2="${m.l + iw}" y1="${y(0)}" y2="${y(0)}" stroke="var(--axis)" stroke-width="1"/>`;
  const every = Math.max(1, Math.ceil(n / Math.max(2, Math.floor(iw / 70))));
  buckets.forEach((b, i) => {
    if (i % every === 0 || i === n - 1) svg += `<text x="${x(i)}" y="${H - 8}" text-anchor="middle">${esc(bucketLabel(b.start, data.bucket))}</text>`;
  });

  const ends = [];
  for (const s of SERIES) {
    // Break the line at buckets with no scored run instead of drawing through them.
    let d = "", pen = false;
    buckets.forEach((b, i) => {
      const v = b[s.key];
      if (v == null) { pen = false; return; }
      d += `${pen ? "L" : "M"}${x(i).toFixed(1)},${y(v).toFixed(1)}`;
      pen = true;
    });
    svg += `<path d="${d}" fill="none" stroke="${s.color}" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>`;
    buckets.forEach((b, i) => {
      const v = b[s.key];
      const isolated = v != null && buckets[i - 1]?.[s.key] == null && buckets[i + 1]?.[s.key] == null;
      if (isolated) svg += `<circle cx="${x(i)}" cy="${y(v)}" r="4" fill="${s.color}" stroke="var(--surface-1)" stroke-width="2"/>`;
    });
    const last = [...buckets.keys()].reverse().find((i) => buckets[i][s.key] != null);
    if (last != null) ends.push({ s, i: last, v: buckets[last][s.key] });
  }
  // Direct labels at line ends (wide screens), nudged apart so they never collide.
  if (m.r > 40) {
    ends.sort((a, b) => y(a.v) - y(b.v));
    let prev = -Infinity;
    for (const e of ends) {
      const ly = Math.max(y(e.v) + 4, prev + 13);
      prev = ly;
      svg += `<text x="${x(e.i) + 8}" y="${ly}" style="fill:var(--text-primary)">${esc(e.s.short || e.s.label)}</text>`;
    }
  }
  svg += `<line id="q-cross" x1="0" x2="0" y1="${m.t}" y2="${m.t + ih}" stroke="var(--axis)" stroke-width="1" visibility="hidden"/>`;
  svg += `<rect x="${m.l}" y="${m.t}" width="${iw}" height="${ih}" fill="transparent" id="q-hit"/></svg>`;
  root.innerHTML = `${svg}<div class="tip" id="q-tip" hidden></div>`;

  const svgEl = root.querySelector("svg"), tip = $("q-tip"), cross = $("q-cross");
  const move = (ev) => {
    const r = svgEl.getBoundingClientRect();
    const px = ((ev.clientX - r.left) / r.width) * W;
    const i = Math.min(n - 1, Math.max(0, Math.round(((px - m.l) / iw) * (n - 1))));
    const b = buckets[i];
    cross.setAttribute("x1", x(i)); cross.setAttribute("x2", x(i)); cross.setAttribute("visibility", "visible");
    tip.innerHTML = `<div><strong>${esc(bucketLabel(b.start, data.bucket))}</strong> · ${b.runs_scored} runs scored</div>` +
      SERIES.map((s) => `<div><i style="background:${s.color}"></i>${esc(s.short || s.label)} <b>${pct(b[s.key])}</b></div>`).join("") +
      `<div>Analyst satisfied <b>${pct(b.satisfactory_rate)}</b> (${b.feedback})</div>`;
    tip.hidden = false;
    const left = (x(i) / W) * r.width;
    tip.style.left = `${Math.min(Math.max(left + 12, 0), r.width - tip.offsetWidth)}px`;
    tip.style.top = "8px";
  };
  $("q-hit").addEventListener("pointermove", move);
  $("q-hit").addEventListener("pointerleave", () => { tip.hidden = true; cross.setAttribute("visibility", "hidden"); });
}

function renderTables() {
  const head = ["Bucket", "Runs", ...SERIES.map((s) => s.short || s.label), "Judge errors", "Feedback", "Satisfied"];
  const rows = data.buckets.map((b) => [bucketLabel(b.start, data.bucket), b.runs_scored,
    ...SERIES.map((s) => pct(b[s.key])), b.judge_errors, b.feedback, pct(b.satisfactory_rate)]);
  const table = (h, r) => `<table class="q-table"><thead><tr>${h.map((c) => `<th scope="col">${esc(c)}</th>`).join("")}</tr></thead>
    <tbody>${r.map((row) => `<tr>${row.map((c) => `<td>${esc(c)}</td>`).join("")}</tr>`).join("")}</tbody></table>`;
  $("q-table").innerHTML = table(head, rows);
  const recent = data.recent.map((r) => [new Date(r.run_finished_at).toLocaleString("en-IN"), r.run_id.slice(0, 8),
    r.status, r.route ?? "–", r.prompt_version, pct(r.groundedness), r.faithful == null ? "–" : (r.faithful ? "yes" : "no"),
    pct(r.hallucination_rate), r.error ? "judge error" : ""]);
  $("q-recent").innerHTML = recent.length
    ? table(["Finished", "Run", "Status", "Route", "Prompt version", "Groundedness", "Faithful", "Hallucination", ""], recent)
    : '<p class="hint">No scored runs yet.</p>';
}

async function load() {
  const [days, bucket] = $("q-range").value.split("|");
  $("q-status").textContent = "Loading…";
  try {
    const res = await fetch(`/quality?days=${days}&bucket=${bucket}`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    data = await res.json();
    $("q-bucket").textContent = data.bucket;
    $("q-legend").innerHTML = SERIES.map((s) => `<span style="--swatch:${s.color}">${esc(s.label)}</span>`).join("");
    renderTiles(data.overall);
    renderChart();
    renderTables();
    $("q-status").textContent = `Updated ${new Date().toLocaleTimeString("en-IN")}`;
  } catch (err) {
    $("q-status").textContent = `Could not load scores: ${err.message}`;
  }
}

$("q-range").addEventListener("change", load);
$("q-refresh").addEventListener("click", load);
window.addEventListener("resize", () => data && renderChart());
load();
