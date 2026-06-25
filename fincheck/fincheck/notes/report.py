"""Render a self-contained, offline, interactive HTML review report.

The report has no external dependencies (CSS and JS are inlined) so it opens
straight from disk. For every difference the checker can mark **Accept**
(expected) or **Ignore** (irrelevant); decisions persist in the browser's
``localStorage`` and can be **exported** to / **imported** from the same JSON
ledger the CLI consumes, so a quarter's sign-offs carry forward.
"""

from __future__ import annotations

import html
import json

from .compare import ComparisonResult, Difference
from .ledger import Ledger, summarize


def _esc(text: str) -> str:
    return html.escape(text or "")


def _diff_side(docname: str, before: str, fragment: str, after: str, cls: str) -> str:
    chg = (
        f'<span class="chg {cls}">{_esc(fragment)}</span>'
        if fragment
        else '<span class="absent">— (absent here) —</span>'
    )
    return (
        f'<div class="side">'
        f'<span class="docname">{_esc(docname)}</span>'
        f'<span class="ctx">{_esc(before)}</span>{chg}'
        f'<span class="ctx">{_esc(after)}</span>'
        f"</div>"
    )


def _diff_card(d: Difference) -> str:
    return (
        f'<div class="diff" data-hash="{d.hash}" data-note="{_esc(d.topic)}">'
        f'<div class="diff-head">'
        f'<span class="kind kind-{d.kind}">{d.kind}</span>'
        f'<span class="status-pill"></span>'
        f'<span class="buttons">'
        f'<button class="b-accept" onclick="decide(\'{d.hash}\',\'accepted\')">Accept</button>'
        f'<button class="b-ignore" onclick="decide(\'{d.hash}\',\'ignored\')">Ignore</button>'
        f'<button class="b-open" onclick="decide(\'{d.hash}\',\'open\')">Reopen</button>'
        f"</span></div>"
        f'<div class="sides">'
        f'{_diff_side(d.left_doc, d.left.before, d.left.text, d.left.after, "del")}'
        f'{_diff_side(d.right_doc, d.right.before, d.right.text, d.right.after, "ins")}'
        f"</div></div>"
    )


def _matrix_html(result: ComparisonResult) -> str:
    docs = result.documents
    n = len(docs)
    head = "".join(f"<th>{_esc(d.name)}</th>" for d in docs)
    rows = []
    for topic, present in result.matrix.items():
        common = len(present) == n
        title = next(iter(present.values()))
        cells = []
        for doc in docs:
            if doc.name in present:
                cells.append(f'<td class="yes" title="{_esc(present[doc.name])}">✓</td>')
            else:
                cells.append('<td class="no">·</td>')
        cls = "common" if common else "partial"
        rows.append(
            f'<tr class="{cls}"><td class="topic">{_esc(title)}</td>{"".join(cells)}</tr>'
        )
    return (
        '<table class="matrix"><thead><tr><th>Note</th>'
        f"{head}</tr></thead><tbody>{''.join(rows)}</tbody></table>"
    )


def _pairs_html(result: ComparisonResult) -> str:
    blocks = []
    for p in result.pairs:
        if not p.differences:
            body = '<p class="none">No substantive text differences in shared notes.</p>'
        else:
            by_note: dict[str, list[Difference]] = {}
            for d in p.differences:
                by_note.setdefault(d.title, []).append(d)
            groups = []
            for title, diffs in by_note.items():
                cards = "".join(_diff_card(d) for d in diffs)
                groups.append(
                    f'<details class="note-group" open>'
                    f"<summary>{_esc(title)} "
                    f'<span class="count">{len(diffs)}</span></summary>{cards}</details>'
                )
            body = "".join(groups)
        sim_pct = round(p.similarity * 100)
        blocks.append(
            f'<section class="pair" data-pair="{_esc(p.left_doc)}|{_esc(p.right_doc)}">'
            f'<h3>{_esc(p.left_doc)} <span class="vs">vs</span> {_esc(p.right_doc)}'
            f'<span class="sim">avg similarity {sim_pct}% · '
            f"{len(p.differences)} difference(s)</span></h3>{body}</section>"
        )
    return "".join(blocks)


def render_html(result: ComparisonResult, ledger: Ledger | None = None) -> str:
    ledger = ledger or Ledger()
    diffs = result.all_differences
    counts = summarize(diffs, ledger)

    docs_meta = "".join(
        f'<li><b>{_esc(d.name)}</b> — {_esc(d.kind.label)} '
        f'<span class="muted">({len(d.notes)} notes)</span></li>'
        for d in result.documents
    )

    # Seed decisions (from the supplied ledger) and a stable storage key.
    seed = {h: ledger.status_of(h) for h in {d.hash for d in diffs}}
    seed_resolved = {h: s for h, s in seed.items() if s != "open"}
    meta = {d.hash: {"note": d.title} for d in diffs}
    storage_key = "fincheck-notes:" + "|".join(sorted(d.name for d in result.documents))

    common = len(result.common_topics)
    total_notes = len(result.matrix)

    # The JS carries literal braces, so substitute its data via replace() rather
    # than str.format(); only the HTML template (which has no literal braces) is
    # formatted.
    js = (
        _JS.replace("__KEY__", json.dumps(storage_key))
        .replace("__META__", json.dumps(meta))
        .replace("__SEED__", json.dumps(seed_resolved))
    )

    return _TEMPLATE.format(
        css=_CSS,
        js=js,
        docs_meta=docs_meta,
        matrix=_matrix_html(result),
        pairs=_pairs_html(result),
        n_docs=len(result.documents),
        common=common,
        total_notes=total_notes,
        n_diffs=len(diffs),
        open=counts["open"],
        accepted=counts["accepted"],
        ignored=counts["ignored"],
    )


def write_html_report(result: ComparisonResult, path: str, ledger: Ledger | None = None) -> str:
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(render_html(result, ledger))
    return path


_CSS = """
:root{--bg:#f6f7f9;--card:#fff;--line:#e2e5ea;--ink:#1f2733;--muted:#6b7480;
--accept:#1f9d57;--ignore:#9aa3af;--open:#d97706;--del:#b42318;--ins:#067647;}
*{box-sizing:border-box}
body{margin:0;font:14px/1.5 -apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;
background:var(--bg);color:var(--ink)}
header{background:#0f172a;color:#fff;padding:18px 24px}
header h1{margin:0 0 6px;font-size:18px}
header ul{margin:8px 0 0;padding-left:18px;font-size:13px}
header .muted{color:#94a3b8}
.wrap{max-width:1080px;margin:0 auto;padding:20px 24px 80px}
h2{font-size:15px;margin:26px 0 10px;border-bottom:2px solid var(--line);padding-bottom:6px}
.toolbar{position:sticky;top:0;z-index:5;background:var(--bg);border-bottom:1px solid var(--line);
padding:10px 24px;display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.toolbar .spacer{flex:1}
.tally{display:flex;gap:14px;font-size:13px}
.tally b{font-variant-numeric:tabular-nums}
.tally .open{color:var(--open)} .tally .accepted{color:var(--accept)} .tally .ignored{color:var(--ignore)}
button{font:inherit;border:1px solid var(--line);background:#fff;border-radius:6px;
padding:5px 10px;cursor:pointer}
button:hover{border-color:#9aa3af}
button.active{background:#0f172a;color:#fff;border-color:#0f172a}
.matrix{border-collapse:collapse;width:100%;background:var(--card);font-size:13px}
.matrix th,.matrix td{border:1px solid var(--line);padding:5px 8px;text-align:center}
.matrix th:first-child,.matrix td.topic{text-align:left}
.matrix td.yes{color:var(--accept);font-weight:700}
.matrix td.no{color:#cbd2da}
.matrix tr.partial td.topic{color:var(--muted)}
.matrix tr.common td.topic{font-weight:600}
.pair{background:var(--card);border:1px solid var(--line);border-radius:10px;margin:16px 0;padding:6px 16px 14px}
.pair h3{font-size:14px;margin:12px 0}
.pair h3 .vs{color:var(--muted);font-weight:400;margin:0 4px}
.pair h3 .sim{float:right;color:var(--muted);font-weight:400;font-size:12px}
.none{color:var(--muted);font-style:italic}
.note-group{border-top:1px solid var(--line);padding:6px 0}
.note-group summary{cursor:pointer;font-weight:600}
.note-group summary .count{color:var(--muted);font-weight:400;margin-left:6px}
.diff{border:1px solid var(--line);border-left:4px solid var(--open);border-radius:8px;
margin:8px 0;padding:8px 10px;background:#fff}
.diff[data-status=accepted]{border-left-color:var(--accept);background:#f5fbf7}
.diff[data-status=ignored]{border-left-color:var(--ignore);background:#fafafa;opacity:.7}
.diff-head{display:flex;align-items:center;gap:8px;margin-bottom:6px}
.kind{font-size:11px;text-transform:uppercase;letter-spacing:.04em;color:var(--muted);
border:1px solid var(--line);border-radius:4px;padding:1px 6px}
.status-pill{font-size:11px;font-weight:700;text-transform:uppercase}
.diff[data-status=open] .status-pill{color:var(--open)}
.diff[data-status=open] .status-pill::after{content:"open"}
.diff[data-status=accepted] .status-pill{color:var(--accept)}
.diff[data-status=accepted] .status-pill::after{content:"accepted"}
.diff[data-status=ignored] .status-pill{color:var(--ignore)}
.diff[data-status=ignored] .status-pill::after{content:"ignored"}
.diff-head .buttons{margin-left:auto;display:flex;gap:6px}
.sides{display:grid;grid-template-columns:1fr 1fr;gap:10px}
.side{border:1px solid var(--line);border-radius:6px;padding:6px 8px;background:#fbfbfc;
overflow-wrap:anywhere}
.docname{display:block;font-size:11px;font-weight:700;color:var(--muted);margin-bottom:3px}
.ctx{color:#8a929c}
.chg{padding:0 1px;border-radius:3px;font-weight:600}
.chg.del{background:#fde7e6;color:var(--del)}
.chg.ins{background:#e6f6ec;color:var(--ins)}
.absent{color:#b9c0c9;font-style:italic}
.hidden{display:none!important}
"""

_JS = """
const KEY = __KEY__;
const META = __META__;
const SEED = __SEED__;
let state = Object.assign({}, SEED);
try { const ls = JSON.parse(localStorage.getItem(KEY)||"{}"); Object.assign(state, ls); } catch(e) {}

function statusOf(h){ return state[h] || "open"; }
function persist(){ try { localStorage.setItem(KEY, JSON.stringify(state)); } catch(e) {} }

function applyAll(){
  document.querySelectorAll(".diff").forEach(el=>{ el.dataset.status = statusOf(el.dataset.hash); });
  recount(); applyFilter();
}
function decide(h, status){
  if(status==="open") delete state[h]; else state[h]=status;
  persist();
  const el=document.querySelector('.diff[data-hash="'+h+'"]');
  if(el) el.dataset.status=status;
  recount(); applyFilter();
}
function recount(){
  let o=0,a=0,i=0;
  document.querySelectorAll(".diff").forEach(el=>{
    const s=statusOf(el.dataset.hash);
    if(s==="accepted")a++; else if(s==="ignored")i++; else o++;
  });
  document.getElementById("n-open").textContent=o;
  document.getElementById("n-accepted").textContent=a;
  document.getElementById("n-ignored").textContent=i;
}
let filter="all";
function setFilter(f, btn){
  filter=f;
  document.querySelectorAll(".toolbar .filt").forEach(b=>b.classList.remove("active"));
  if(btn) btn.classList.add("active");
  applyFilter();
}
function applyFilter(){
  document.querySelectorAll(".diff").forEach(el=>{
    const s=statusOf(el.dataset.hash);
    const show = filter==="all" || (filter==="open"&&s==="open") ||
                 (filter==="accepted"&&s==="accepted") || (filter==="ignored"&&s==="ignored");
    el.classList.toggle("hidden", !show);
  });
  // Hide note groups / pairs that became empty under the filter.
  document.querySelectorAll(".note-group").forEach(g=>{
    const any=[...g.querySelectorAll(".diff")].some(d=>!d.classList.contains("hidden"));
    g.classList.toggle("hidden", !any);
  });
  document.querySelectorAll(".pair").forEach(p=>{
    const any=[...p.querySelectorAll(".diff")].some(d=>!d.classList.contains("hidden"));
    const hasDiffs=p.querySelector(".diff");
    p.classList.toggle("hidden", hasDiffs && !any);
  });
}
function exportDecisions(){
  const decisions={};
  for(const h in state){
    if(state[h]==="open") continue;
    decisions[h]={status:state[h], note:(META[h]&&META[h].note)||""};
  }
  const blob=new Blob([JSON.stringify({version:1,decisions},null,2)],{type:"application/json"});
  const a=document.createElement("a");
  a.href=URL.createObjectURL(blob); a.download="decisions.json"; a.click();
  URL.revokeObjectURL(a.href);
}
function importDecisions(input){
  const f=input.files[0]; if(!f) return;
  const r=new FileReader();
  r.onload=()=>{
    try{
      const data=JSON.parse(r.result);
      const dec=data.decisions||data;
      for(const h in dec){ if(dec[h]&&dec[h].status) state[h]=dec[h].status; }
      persist(); applyAll();
    }catch(e){ alert("Could not read decisions file: "+e); }
  };
  r.readAsText(f);
  input.value="";
}
window.addEventListener("DOMContentLoaded", applyAll);
"""

_TEMPLATE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Common-notes comparison</title>
<style>{css}</style></head>
<body>
<header>
  <h1>Common-notes comparison — {n_docs} financial statements</h1>
  <div>{common} of {total_notes} notes are common to all documents · {n_diffs} difference(s) to review</div>
  <ul>{docs_meta}</ul>
</header>
<div class="toolbar">
  <div class="tally">
    <span class="open">open <b id="n-open">{open}</b></span>
    <span class="accepted">accepted <b id="n-accepted">{accepted}</b></span>
    <span class="ignored">ignored <b id="n-ignored">{ignored}</b></span>
  </div>
  <span class="spacer"></span>
  <button class="filt active" onclick="setFilter('all',this)">All</button>
  <button class="filt" onclick="setFilter('open',this)">Open</button>
  <button class="filt" onclick="setFilter('accepted',this)">Accepted</button>
  <button class="filt" onclick="setFilter('ignored',this)">Ignored</button>
  <button onclick="exportDecisions()">Export decisions</button>
  <label class="imp"><button onclick="this.nextElementSibling.click()">Load decisions</button>
    <input type="file" accept="application/json" style="display:none" onchange="importDecisions(this)"></label>
</div>
<div class="wrap">
  <h2>Note alignment matrix</h2>
  {matrix}
  <h2>Differences by document pair</h2>
  {pairs}
</div>
<script>{js}</script>
</body></html>
"""
