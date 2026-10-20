"""history.html: how the board's EV held up, from every graded side (grade.py). A local page at the
repo root, outside site/, so it is never uploaded. Rewritten each time grading runs (once a slate
day in publish, or `python -m props grade`).

Each side a betting site offered is counted once: at the first publish that showed it +EV if one
did (what the board flagged), else at the first publish that priced it with a confident Fair. The
page buckets them by that EV and compares how often they won with how often Fair said they would.
"""
import json
from pathlib import Path

from . import store

OUT = store.ROOT / "history.html"


def write(picks: list[dict], generated: str, out: Path | None = None) -> Path:
    out = out or OUT
    data = json.dumps({"generated": generated, "picks": sorted(picks, key=lambda p: (p["game"][:10], p["seen"]))},
                      separators=(",", ":"), ensure_ascii=False).replace("</", "<\\/")
    out.write_text(PAGE.replace("__DATA__", data), encoding="utf-8")
    return out


PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>SC Board – EV history</title>
<style>
:root { --page:#f2f5f8; --surface:#fff; --ink:#14202b; --ink-2:#3c4a57; --ink-3:#6f7f8c; --rule:#d6dfe6;
  --accent:#1d5fc4; --ref:#9aa7b2; --hit:#12805f; --miss:#c23333; --body:-apple-system,system-ui,"Segoe UI",sans-serif; }
@media (prefers-color-scheme: dark) { :root { --page:#0c0f12; --surface:#151a1f; --ink:#e6ebef; --ink-2:#b6c2cc;
  --ink-3:#8796a3; --rule:#28323b; --accent:#6ea3ff; --ref:#5d6a75; --hit:#3fcf9a; --miss:#ff7a7a; } }
* { box-sizing: border-box; }
body { margin: 0; background: var(--page); color: var(--ink); font: 14px/1.45 var(--body); }
main { max-width: 1100px; margin: 0 auto; padding: 20px 16px 60px; }
h1 { margin: 0; font-size: 22px; } h2 { margin: 28px 0 10px; font-size: 15px; }
.sub { color: var(--ink-3); font-size: 13px; margin: 4px 0 0; }
.filters { display: flex; flex-wrap: wrap; gap: 10px; align-items: center; margin: 18px 0 6px; }
.filters label { display: flex; gap: 6px; align-items: center; color: var(--ink-3); font-size: 13px; }
select { font: inherit; color: var(--ink); background: var(--surface); border: 1px solid var(--rule); border-radius: 8px; height: 34px; padding: 0 8px; }
.tiles { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 10px; margin-top: 14px; }
.tile { background: var(--surface); border: 1px solid var(--rule); border-radius: 12px; padding: 12px 14px; }
.tile .k { color: var(--ink-3); font-size: 12px; } .tile .v { font-size: 24px; font-weight: 700; font-variant-numeric: tabular-nums; }
.tile .n { color: var(--ink-3); font-size: 12px; }
.card { background: var(--surface); border: 1px solid var(--rule); border-radius: 12px; padding: 14px; }
.note { color: var(--ink-3); font-size: 12.5px; margin: 8px 0 0; }
.legend { display: flex; gap: 16px; font-size: 12.5px; color: var(--ink-2); margin-bottom: 6px; }
.legend i { display: inline-block; vertical-align: middle; margin-right: 6px; }
.legend .dot { width: 10px; height: 10px; border-radius: 50%; background: var(--accent); }
.legend .tick { width: 14px; height: 2px; background: var(--ref); }
svg { display: block; width: 100%; height: auto; overflow: visible; }
svg text { fill: var(--ink-3); font: 11px var(--body); }
.wrap { overflow-x: auto; }
table { width: 100%; border-collapse: collapse; font-variant-numeric: tabular-nums; white-space: nowrap; }
th, td { padding: 7px 9px; border-bottom: 1px solid var(--rule); text-align: right; font-size: 13px; }
th { color: var(--ink-3); font-weight: 600; font-size: 12px; }
th:first-child, td:first-child, .l { text-align: left; }
tr:last-child td { border-bottom: 0; }
.pos { color: var(--hit); font-weight: 600; } .neg { color: var(--miss); }
.mark { display: inline-block; width: 18px; height: 18px; border-radius: 50%; color: #fff; font: 700 11px/18px var(--body); text-align: center; }
.mark.w { background: var(--hit); } .mark.m { background: var(--miss); }
#tip { position: fixed; pointer-events: none; z-index: 9; background: var(--surface); border: 1px solid var(--rule); border-radius: 8px;
  padding: 7px 10px; font-size: 12.5px; box-shadow: 0 6px 20px rgb(0 0 0 / .18); white-space: nowrap; }
#tip b { display: block; margin-bottom: 2px; }
.empty { padding: 30px; text-align: center; color: var(--ink-3); }
</style></head><body><main>
<h1>EV history</h1>
<p class="sub" id="sub"></p>
<div class="filters">
  <label>League <select id="league"></select></label>
  <label>Site <select id="site"></select></label>
  <label>Stake <select id="stake"><option value="ev">+EV picks only</option><option value="all">every side</option></select></label>
</div>
<div class="tiles" id="tiles"></div>

<h2>Win rate by EV shown</h2>
<div class="card">
  <div class="legend"><span><i class="dot"></i>won</span><span><i class="tick"></i>Fair's chance (expected)</span></div>
  <div id="evchart"></div>
  <p class="note">Every side, bucketed by the EV the board showed. If Fair is right, each dot sits on its tick, and the +EV buckets return about the EV shown. Small buckets swing a lot: check the n.</p>
</div>
<div class="card wrap" style="margin-top:10px"><table id="evtable"></table></div>

<h2>Running result, 1 unit a pick</h2>
<div class="card"><div id="runchart"></div><p class="note" id="runnote"></p></div>

<h2>By site</h2>
<div class="card wrap"><table id="sitetable"></table></div>

<h2>Latest graded picks</h2>
<div class="card wrap"><table id="picktable"></table></div>
</main>
<div id="tip" hidden></div>
<script id="data" type="application/json">__DATA__</script>
<script>
"use strict";
const D = JSON.parse(document.getElementById("data").textContent), $ = s => document.querySelector(s);
const SITES = { underdog: "Underdog", betano: "Betano", mise: "Mise-o-jeu", stake: "Stake", fanduel: "FanDuel", pick6: "DK Pick6", bovada: "Bovada" };
const siteName = k => SITES[k] || k;
const BUCKETS = [[-Infinity, -0.10, "below −10%"], [-0.10, -0.05, "−10 to −5%"], [-0.05, -0.02, "−5 to −2%"], [-0.02, 0, "−2 to 0%"],
  [0, 0.02, "0 to 2%"], [0.02, 0.04, "2 to 4%"], [0.04, 0.07, "4 to 7%"], [0.07, 0.10, "7 to 10%"], [0.10, Infinity, "10% +"]];
const pct = (v, d = 1) => v == null || isNaN(v) ? "–" : (v * 100).toFixed(d) + "%";
const spct = v => v == null || isNaN(v) ? "–" : (v > 0 ? "+" : v < 0 ? "−" : "") + Math.abs(v * 100).toFixed(1) + "%";
const sgn = v => v > 0 ? "pos" : v < 0 ? "neg" : "";
const mean = a => a.length ? a.reduce((x, y) => x + y, 0) / a.length : null;
const esc = s => String(s).replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);
const profit = p => p.won ? p.odds - 1 : -1;
function stats(list) {
  const clv = list.filter(p => p.clv != null).map(p => p.clv);
  return { n: list.length, won: mean(list.map(p => p.won)), fair: mean(list.map(p => p.fair)), ev: mean(list.map(p => p.ev)),
    roi: mean(list.map(profit)), units: list.reduce((s, p) => s + profit(p), 0), odds: mean(list.map(p => p.odds)), clv: mean(clv), clvN: clv.length };
}

const leagues = [...new Set(D.picks.map(p => p.league))].sort(), sites = [...new Set(D.picks.map(p => p.site))].sort();
$("#league").innerHTML = `<option value="">All</option>` + leagues.map(l => `<option value="${l}">${l.toUpperCase()}</option>`).join("");
$("#site").innerHTML = `<option value="">All</option>` + sites.map(s => `<option value="${s}">${esc(siteName(s))}</option>`).join("");
const days = D.picks.map(p => p.game.slice(0, 10)).sort();
$("#sub").textContent = D.picks.length
  ? `${D.picks.length.toLocaleString()} graded sides, games ${days[0]} to ${days[days.length - 1]} · graded ${D.generated.replace("T", " ").slice(0, 16)} UTC · local page, not uploaded`
  : "Nothing graded yet: snapshots need games that have finished and game logs that include them.";

// tooltip
const tip = $("#tip");
document.addEventListener("pointermove", e => {
  const t = e.target.closest("[data-tip]");
  if (!t) { tip.hidden = true; return; }
  tip.innerHTML = t.dataset.tip; tip.hidden = false;
  const w = tip.offsetWidth, x = Math.min(e.clientX + 14, innerWidth - w - 8);
  tip.style.left = x + "px"; tip.style.top = (e.clientY + 16) + "px";
});

function evChart(rows) {
  const W = 760, H = 240, L = 40, R = 10, T = 12, B = 34, iw = W - L - R, ih = H - T - B, step = iw / rows.length;
  const y = v => T + ih * (1 - v);
  let g = "";
  for (const v of [0, .25, .5, .75, 1]) g += `<line x1="${L}" x2="${W - R}" y1="${y(v)}" y2="${y(v)}" stroke="var(--rule)" stroke-width="1"/><text x="${L - 6}" y="${y(v) + 4}" text-anchor="end">${v * 100}%</text>`;
  g += `<line x1="${L + step * 4}" x2="${L + step * 4}" y1="${T}" y2="${T + ih}" stroke="var(--ink-3)" stroke-dasharray="3 3" stroke-width="1"/>`;
  rows.forEach((r, i) => {
    const cx = L + step * (i + .5);
    g += `<text x="${cx}" y="${H - 18}" text-anchor="middle">${r.label}</text><text x="${cx}" y="${H - 4}" text-anchor="middle">n ${r.s.n}</text>`;
    if (!r.s.n) return;
    const t = `<b>EV ${r.label}</b>${r.s.n} sides · won ${pct(r.s.won)} · Fair said ${pct(r.s.fair)}<br>ROI ${spct(r.s.roi)} · avg odds ${r.s.odds.toFixed(2)}`;
    g += `<line x1="${cx - 13}" x2="${cx + 13}" y1="${y(r.s.fair)}" y2="${y(r.s.fair)}" stroke="var(--ref)" stroke-width="2" stroke-linecap="round"/>`;
    g += `<circle cx="${cx}" cy="${y(r.s.won)}" r="5" fill="var(--accent)" stroke="var(--surface)" stroke-width="2"/>`;
    g += `<rect x="${cx - step / 2}" y="${T}" width="${step}" height="${ih}" fill="transparent" data-tip="${esc(t)}"/>`;
  });
  return `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="Win rate against Fair's chance, by EV bucket">${g}</svg>`;
}

function runChart(list) {
  if (list.length < 2) return `<p class="empty">Not enough graded picks yet.</p>`;
  const pts = []; let u = 0;
  list.forEach((p, i) => { u += profit(p); pts.push([i + 1, u, p]); });
  const W = 760, H = 220, L = 44, R = 12, T = 12, B = 24, iw = W - L - R, ih = H - T - B;
  const lo = Math.min(0, ...pts.map(p => p[1])), hi = Math.max(0, ...pts.map(p => p[1])), span = hi - lo || 1;
  const x = i => L + iw * (i - 1) / (pts.length - 1), y = v => T + ih * (hi - v) / span;
  let g = "";
  const ticks = 4;
  for (let k = 0; k <= ticks; k++) { const v = lo + span * k / ticks; g += `<line x1="${L}" x2="${W - R}" y1="${y(v)}" y2="${y(v)}" stroke="var(--rule)"/><text x="${L - 6}" y="${y(v) + 4}" text-anchor="end">${v.toFixed(1)}u</text>`; }
  g += `<line x1="${L}" x2="${W - R}" y1="${y(0)}" y2="${y(0)}" stroke="var(--ink-3)" stroke-width="1"/>`;
  g += `<path d="${pts.map((p, i) => (i ? "L" : "M") + x(p[0]).toFixed(1) + " " + y(p[1]).toFixed(1)).join("")}" fill="none" stroke="var(--accent)" stroke-width="2" stroke-linejoin="round"/>`;
  g += `<text x="${L}" y="${H - 6}">pick 1</text><text x="${W - R}" y="${H - 6}" text-anchor="end">pick ${pts.length}</text>`;
  g += `<line id="xh" x1="0" x2="0" y1="${T}" y2="${T + ih}" stroke="var(--ink-3)" visibility="hidden"/><circle id="xd" r="4.5" fill="var(--accent)" stroke="var(--surface)" stroke-width="2" visibility="hidden"/>`;
  g += `<rect id="xr" x="${L}" y="${T}" width="${iw}" height="${ih}" fill="transparent"/>`;
  setTimeout(() => {
    const svg = $("#runchart svg"), r = $("#xr");
    if (!svg) return;
    r.addEventListener("pointermove", e => {
      const box = svg.getBoundingClientRect(), vx = (e.clientX - box.left) * W / box.width;
      const i = Math.max(1, Math.min(pts.length, Math.round(1 + (vx - L) / iw * (pts.length - 1)))), [n, v, p] = pts[i - 1];
      for (const id of ["#xh", "#xd"]) $(id).setAttribute("visibility", "visible");
      $("#xh").setAttribute("x1", x(n)); $("#xh").setAttribute("x2", x(n)); $("#xd").setAttribute("cx", x(n)); $("#xd").setAttribute("cy", y(v));
      r.dataset.tip = `<b>${v >= 0 ? "+" : "−"}${Math.abs(v).toFixed(2)}u after ${n} picks</b>${esc(p.player)} ${p.side} ${p.line} ${esc(p.stat)} · ${esc(siteName(p.site))} ${p.odds.toFixed(2)} · ${p.won ? "won" : "lost"}`;
    });
    r.addEventListener("pointerleave", () => { for (const id of ["#xh", "#xd"]) $(id).setAttribute("visibility", "hidden"); });
  });
  return `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="Cumulative units">${g}</svg>`;
}

function render() {
  const lg = $("#league").value, st = $("#site").value, evOnly = $("#stake").value === "ev";
  const all = D.picks.filter(p => (!lg || p.league === lg) && (!st || p.site === st));
  const picked = all.filter(p => !evOnly || p.ev > 0);
  const s = stats(picked);
  const tile = (k, v, n, cls = "") => `<div class="tile"><div class="k">${k}</div><div class="v ${cls}">${v}</div><div class="n">${n}</div></div>`;
  $("#tiles").innerHTML = tile(evOnly ? "+EV picks graded" : "Sides graded", s.n.toLocaleString(), "one per side")
    + tile("Won", pct(s.won), `Fair said ${pct(s.fair)}`)
    + tile("ROI", spct(s.roi), `${s.units >= 0 ? "+" : "−"}${Math.abs(s.units).toFixed(1)}u at 1u a pick`, sgn(s.roi))
    + tile("EV shown", spct(s.ev), "average when flagged")
    + tile("CLV", spct(s.clv), `vs Fair at close (n ${s.clvN})`, sgn(s.clv));

  const rows = BUCKETS.map(([lo, hi, label]) => ({ label, s: stats(all.filter(p => p.ev >= lo && p.ev < hi)) }));
  $("#evchart").innerHTML = evChart(rows);
  $("#evtable").innerHTML = `<tr><th>EV shown</th><th>Sides</th><th>Avg odds</th><th>Fair's chance</th><th>Won</th><th>Won − Fair</th><th>ROI</th><th>CLV</th></tr>`
    + rows.map(r => `<tr><td>${r.label}</td><td>${r.s.n}</td><td>${r.s.n ? r.s.odds.toFixed(2) : "–"}</td><td>${pct(r.s.fair)}</td><td>${pct(r.s.won)}</td>`
      + `<td class="${sgn(r.s.won - r.s.fair)}">${r.s.n ? spct(r.s.won - r.s.fair) : "–"}</td><td class="${sgn(r.s.roi)}">${spct(r.s.roi)}</td><td class="${sgn(r.s.clv)}">${spct(r.s.clv)}</td></tr>`).join("");

  const ordered = [...picked].sort((a, b) => a.game.localeCompare(b.game) || a.seen.localeCompare(b.seen));
  $("#runchart").innerHTML = runChart(ordered);
  $("#runnote").textContent = `${evOnly ? "+EV picks" : "Every side"} in game order, each at the price the board showed when it first flagged it.`;

  const bySite = sites.map(k => [k, stats(picked.filter(p => p.site === k))]).filter(([, x]) => x.n);
  $("#sitetable").innerHTML = `<tr><th>Site</th><th>Picks</th><th>EV shown</th><th>Fair's chance</th><th>Won</th><th>ROI</th><th>Units</th><th>CLV</th></tr>`
    + bySite.map(([k, x]) => `<tr><td>${esc(siteName(k))}</td><td>${x.n}</td><td>${spct(x.ev)}</td><td>${pct(x.fair)}</td><td>${pct(x.won)}</td>`
      + `<td class="${sgn(x.roi)}">${spct(x.roi)}</td><td class="${sgn(x.units)}">${x.units.toFixed(1)}</td><td class="${sgn(x.clv)}">${spct(x.clv)}</td></tr>`).join("");

  const last = ordered.slice(-150).reverse();
  $("#picktable").innerHTML = `<tr><th>Game</th><th class="l">League</th><th class="l">Player</th><th class="l">Pick</th><th class="l">Site</th><th>Odds</th><th>Fair</th><th>EV</th><th>CLV</th><th>Result</th></tr>`
    + last.map(p => `<tr><td>${p.game.slice(0, 10)}</td><td class="l">${p.league.toUpperCase()}</td><td class="l">${esc(p.player)}</td>`
      + `<td class="l">${p.side === "over" ? "Over" : "Under"} ${p.line} ${esc(p.stat)}</td><td class="l">${esc(siteName(p.site))}</td><td>${p.odds.toFixed(2)}</td>`
      + `<td>${pct(p.fair)}</td><td class="${sgn(p.ev)}">${spct(p.ev)}</td><td class="${sgn(p.clv)}">${spct(p.clv)}</td>`
      + `<td><span class="mark ${p.won ? "w" : "m"}">${p.won ? "W" : "L"}</span></td></tr>`).join("")
    || `<tr><td class="empty" colspan="10">No graded picks for this filter.</td></tr>`;
}
for (const id of ["#league", "#site", "#stake"]) $(id).addEventListener("change", render);
render();
</script></body></html>
"""
