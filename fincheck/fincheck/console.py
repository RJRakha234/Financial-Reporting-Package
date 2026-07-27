"""Render the comparison as an interactive accept/reject review console.

:mod:`fincheck.sidebyside` produces a reading document — two columns, read top
to bottom. This produces a *working surface*: one card per numbered section,
one column per document, and a decision recorded against every difference.
It is scanned and operated rather than read, so state is encoded in form as
well as colour — pills, rails, chips — and the summary comes before the detail.

The sections are the page-wise ones: page 1 of the benchmark is item 1, page 2
is item 2, and so on, which covers every word of the benchmark by construction
and needs nobody to sit with a highlighter first.

The word diff is computed in the page rather than here. Only the texts are
embedded, so the three view states — track changes, before, after — can move
the strikethrough without re-flowing anything, and a reviewer never loses their
place because the words moved under them.
"""

import html
import json


def _e(text) -> str:
    return html.escape("" if text is None else str(text))


def _pair_text(pair, side: str) -> str:
    """One side of a pair as plain text, figures included."""
    unit = pair.a if side == "a" else pair.b
    if unit is None:
        return ""
    if unit.kind == "row" and unit.row is not None:
        return unit.row.as_text()
    return unit.text


def _section_text(section, side: str) -> str:
    parts = [_pair_text(p, side) for p in section.pairs]
    return "\n".join(p for p in parts if p).strip()


def _reasons(section, side_present: bool) -> list[str]:
    if not side_present:
        return ["not present"]
    out = []
    status = section.status
    if status == "same":
        return []
    if status == "formatting":
        out.append("punctuation or spacing only")
    elif status == "reordered":
        out.append("same words, different order")
    else:
        out.append("text differs")
    figures = sum(len(p.changed_figures) for p in section.pairs)
    if figures:
        out.append(f"{figures} figure cell(s) differ")
    if section.moved:
        out.append("appears elsewhere in the other document")
    return out


def build_payload(sections, meta) -> dict:
    """The data the page renders itself from."""
    label_a, label_b = meta.label_a, meta.label_b
    docs = [
        {
            "label": label_a,
            "main": label_a,
            "sub": meta.made_a or "benchmark",
            "href": meta.marked_href_a or "",
        },
        {
            "label": label_b,
            "main": label_b,
            "sub": meta.made_b or "",
            "href": meta.marked_href_b or "",
        },
    ]

    items = []
    for section in sections:
        text_a = _section_text(section, "a")
        text_b = _section_text(section, "b")
        present_a, present_b = bool(text_a), bool(text_b)
        pages = section.pages
        differs = section.status not in ("same",)

        cells = [
            {
                "doc": label_a,
                "present": present_a,
                "absent": not present_a,
                "page": pages[0],
                "meta": f"serial {section.serial}",
                "text": text_a,
                "isBenchmark": True,
                "differs": False,
                "sim": 1.0,
                "reasons": [],
            },
            {
                "doc": label_b,
                "present": present_b,
                "absent": not present_b,
                "page": pages[1],
                "meta": f"serial {section.serial}",
                "text": text_b,
                "isBenchmark": False,
                "differs": bool(present_a and present_b and differs),
                "sim": round(section.score / 100.0, 4),
                "reasons": _reasons(section, present_b),
            },
        ]
        title = f"Item {section.serial}"
        if section.marked is not None:
            title = f"Item {section.serial} · §{section.marked}"
        items.append(
            {
                "id": str(section.serial),
                "title": title,
                "subtitle": section.title or "",
                "benchmarkDoc": label_a,
                "fallbackUsed": False,
                "absentDocs": [c["doc"] for c in cells if c["absent"]],
                "refText": text_a,
                "kind": section.kind,
                "cells": cells,
            }
        )

    summary = meta.summary
    return {
        "benchmark": label_a,
        "generated": "",
        "docs": docs,
        "items": items,
        "stats": {
            "overall": summary.overall,
            "figures": summary.changed_figures,
            "rows": summary.rows_matched,
        },
    }


_CSS = """
:root{
  --accent:#33506E; --accent-soft:#E4EAF2; --accent-ink:#FFFFFF;
  --paper:#FAFAF8; --card:#FFFFFF; --sunken:#F1F2EF;
  --ink:#1B1F24; --ink-soft:#4A5158; --muted:#767E86;
  --rule:#D7DBDE; --rule-soft:#E8EBED;
  --ins:#9A5406; --ins-bg:#FBEEDC;
  --ext:#1F5A93; --ext-bg:#E2EDF8;
  --good:#1E6B45; --good-bg:#E2F1E8;
  --bad:#9A2F26; --bad-bg:#F8E5E2;
  --warn:#8A6410;
  --sans:ui-sans-serif,-apple-system,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
  --serif:Georgia,"Iowan Old Style","Times New Roman",serif;
  --mono:ui-monospace,"SF Mono","Cascadia Mono",Menlo,Consolas,monospace;
}
@media (prefers-color-scheme:dark){:root{
  --accent:#8FB0D4; --accent-soft:#232E3C; --accent-ink:#101720;
  --paper:#12151A; --card:#191D23; --sunken:#1F242B;
  --ink:#E6E9EC; --ink-soft:#B2B9C0; --muted:#828A93;
  --rule:#333A43; --rule-soft:#252B33;
  --ins:#E0A567; --ins-bg:#33270F;
  --ext:#7FB2E0; --ext-bg:#15293C;
  --good:#7CC79E; --good-bg:#162C20;
  --bad:#E28A7C; --bad-bg:#34201C;
  --warn:#D3AE5F;
}}
:root[data-theme=dark]{
  --accent:#8FB0D4; --accent-soft:#232E3C; --accent-ink:#101720;
  --paper:#12151A; --card:#191D23; --sunken:#1F242B;
  --ink:#E6E9EC; --ink-soft:#B2B9C0; --muted:#828A93;
  --rule:#333A43; --rule-soft:#252B33;
  --ins:#E0A567; --ins-bg:#33270F;
  --ext:#7FB2E0; --ext-bg:#15293C;
  --good:#7CC79E; --good-bg:#162C20;
  --bad:#E28A7C; --bad-bg:#34201C;
  --warn:#D3AE5F;
}
:root[data-theme=light]{
  --accent:#33506E; --accent-soft:#E4EAF2; --accent-ink:#FFFFFF;
  --paper:#FAFAF8; --card:#FFFFFF; --sunken:#F1F2EF;
  --ink:#1B1F24; --ink-soft:#4A5158; --muted:#767E86;
  --rule:#D7DBDE; --rule-soft:#E8EBED;
  --ins:#9A5406; --ins-bg:#FBEEDC;
  --ext:#1F5A93; --ext-bg:#E2EDF8;
  --good:#1E6B45; --good-bg:#E2F1E8;
  --bad:#9A2F26; --bad-bg:#F8E5E2;
  --warn:#8A6410;
}
*{box-sizing:border-box}
body{margin:0;background:var(--paper);color:var(--ink);font-family:var(--sans);
  font-size:15px;line-height:1.55;overflow-x:hidden}
.wrap{max-width:96rem;margin:0 auto;padding:2.4rem 1.15rem 5rem}

.eyebrow{font-size:.68rem;letter-spacing:.15em;text-transform:uppercase;
  color:var(--accent);font-weight:700;margin:0 0 .6rem}
h1{font-family:var(--serif);font-weight:400;font-size:clamp(1.6rem,3.4vw,2.3rem);
  line-height:1.15;margin:0 0 .7rem;text-wrap:balance}
.intro{color:var(--ink-soft);max-width:66ch;margin:0 0 1.6rem;font-size:.94rem}
.intro .k{padding:.05rem .3rem;border-radius:3px;font-weight:600}
.k-ins{background:var(--ins-bg);color:var(--ins)}
.k-ext{background:var(--ext-bg);color:var(--ext)}

.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(9.5rem,1fr));
  border-top:2px solid var(--ink);border-bottom:1px solid var(--rule);margin:0 0 1.4rem}
.kpi{padding:.85rem 1rem .95rem}
.kpi+.kpi{border-left:1px solid var(--rule-soft)}
.kpi b{font-family:var(--mono);font-variant-numeric:tabular-nums;font-size:1.6rem;
  font-weight:400;display:block;line-height:1}
.kpi span{display:block;margin-top:.4rem;font-size:.67rem;letter-spacing:.09em;
  text-transform:uppercase;color:var(--muted)}
.kpi--warn b{color:var(--warn)} .kpi--good b{color:var(--good)}

.toolbar{position:sticky;top:0;z-index:20;background:var(--paper);
  border-bottom:1px solid var(--rule);padding:.7rem 0 .75rem;margin:0 0 1.5rem;
  display:flex;gap:1rem;align-items:center;flex-wrap:wrap}
.prog{flex:1 1 14rem;min-width:11rem}
.prog-top{display:flex;justify-content:space-between;font-size:.68rem;
  letter-spacing:.08em;text-transform:uppercase;color:var(--muted);margin-bottom:.3rem}
.prog-pct{font-family:var(--mono);font-variant-numeric:tabular-nums;
  color:var(--ink);font-weight:600}
.track{height:5px;background:var(--rule-soft);border-radius:3px;overflow:hidden}
.bar{height:100%;width:0;background:var(--good);border-radius:3px;
  transition:width .45s cubic-bezier(.22,.61,.36,1)}

.seg{display:inline-flex;border:1px solid var(--rule);border-radius:5px;overflow:hidden}
.seg button{font:inherit;font-size:.75rem;font-weight:600;padding:.34rem .7rem;
  border:0;border-left:1px solid var(--rule-soft);background:var(--card);
  color:var(--ink-soft);cursor:pointer;white-space:nowrap}
.seg button:first-child{border-left:0}
.seg button[aria-pressed=true]{background:var(--accent);color:var(--accent-ink)}
.btn{font:inherit;font-size:.75rem;font-weight:600;padding:.36rem .75rem;
  border:1px solid var(--rule);border-radius:5px;background:var(--card);
  color:var(--ink-soft);cursor:pointer}
.btn:hover{border-color:var(--muted);color:var(--ink)}
.switch{display:flex;gap:.4rem;align-items:center;font-size:.78rem;
  color:var(--ink-soft);cursor:pointer}

.card{background:var(--card);border:1px solid var(--rule);border-radius:8px;
  margin:0 0 1.15rem;overflow:hidden}
.card-head{display:flex;gap:.7rem;align-items:flex-start;flex-wrap:wrap;
  padding:.85rem 1rem;border-bottom:1px solid var(--rule-soft);background:var(--sunken)}
.badge{font-family:var(--mono);font-variant-numeric:tabular-nums;font-size:.9rem;
  font-weight:700;color:var(--accent-ink);background:var(--accent);
  border-radius:5px;min-width:2.1rem;height:2.1rem;display:inline-flex;
  align-items:center;justify-content:center;padding:0 .45rem;text-decoration:none;flex:none}
a.badge:hover{filter:brightness(1.12)}
.head-text{flex:1 1 18rem;min-width:0}
.head-text h2{font-family:var(--serif);font-weight:400;font-size:1rem;margin:0}
.head-text p{margin:.16rem 0 0;font-size:.8rem;color:var(--muted);
  overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.pill{font-size:.62rem;letter-spacing:.08em;text-transform:uppercase;font-weight:700;
  padding:.16rem .5rem;border-radius:99px;border:1px solid currentColor;white-space:nowrap}
.pill--pend{color:var(--warn)} .pill--ok{color:var(--good)}
.pill--rej{color:var(--bad)} .pill--same{color:var(--muted)}
.note-line{flex-basis:100%;font-size:.76rem;color:var(--muted);margin-top:.15rem}
.bulk{display:inline-flex;gap:.35rem}
.bulk button{font:inherit;font-size:.66rem;font-weight:700;letter-spacing:.05em;
  text-transform:uppercase;padding:.2rem .5rem;border-radius:4px;cursor:pointer;
  border:1px solid currentColor;background:none}
.bulk .acc{color:var(--good)} .bulk .rej{color:var(--bad)}

.cols{display:grid;gap:1px;background:var(--rule-soft)}
.cell{background:var(--card);padding:.85rem 1rem 1rem;min-width:0}
.cell--bench{background:var(--accent-soft);box-shadow:inset 3px 0 0 var(--accent)}
.cell--absent{background:repeating-linear-gradient(135deg,transparent,transparent 6px,
  var(--rule-soft) 6px,var(--rule-soft) 7px)}
.cell--acc{box-shadow:inset 3px 0 0 var(--good);background:var(--good-bg)}
.cell--rej{box-shadow:inset 3px 0 0 var(--bad);background:var(--bad-bg)}
.cell-top{display:flex;gap:.45rem;align-items:center;flex-wrap:wrap;margin-bottom:.5rem}
.doc{font-size:.74rem;font-weight:700;color:var(--ink);overflow:hidden;
  text-overflow:ellipsis;white-space:nowrap;flex:1 1 6rem;min-width:0}
.tag{font-size:.6rem;letter-spacing:.07em;text-transform:uppercase;font-weight:700;
  padding:.1rem .4rem;border-radius:3px;white-space:nowrap}
.tag--bench{background:var(--accent);color:var(--accent-ink)}
.tag--match{background:var(--good-bg);color:var(--good)}
.tag--diff{background:var(--ins-bg);color:var(--ins)}
.chip{font-family:var(--mono);font-size:.68rem;color:var(--muted);
  text-decoration:none;border-bottom:1px dotted currentColor;white-space:nowrap}
.chip:hover{color:var(--accent)}
.sim{font-family:var(--mono);font-variant-numeric:tabular-nums;font-size:.68rem;
  color:var(--muted)}

.body{font-size:.86rem;white-space:pre-wrap;word-break:break-word;
  overflow-wrap:anywhere;margin:0}
.body.clipped{display:-webkit-box;-webkit-line-clamp:6;-webkit-box-orient:vertical;
  overflow:hidden}
.more{font:inherit;font-size:.72rem;font-weight:600;color:var(--accent);
  background:none;border:0;padding:.25rem 0 0;cursor:pointer}
.ins{background:var(--ins-bg);color:var(--ins);border-radius:2px;padding:0 .1rem}
.ext{background:var(--ext-bg);color:var(--ext);border-radius:2px;padding:0 .1rem}
/* Only the strikethrough moves between states; nothing is hidden or re-flowed. */
.v-before .ins{text-decoration:line-through;opacity:.62}
.v-after  .ext{text-decoration:line-through;opacity:.62}
.legend{font-size:.7rem;color:var(--muted);margin:.45rem 0 0}
.legend b{font-weight:700}
.absent-note{font-size:.8rem;color:var(--muted);font-style:italic}
.cell-actions{display:flex;gap:.5rem;align-items:center;margin-top:.6rem;flex-wrap:wrap}
.decide{display:inline-flex;border:1px solid var(--rule);border-radius:5px;overflow:hidden}
.decide button{font:inherit;font-size:.68rem;font-weight:700;letter-spacing:.05em;
  text-transform:uppercase;padding:.24rem .6rem;border:0;background:var(--card);
  color:var(--ink-soft);cursor:pointer;border-left:1px solid var(--rule-soft)}
.decide button:first-child{border-left:0}
.decide button[aria-pressed=true].acc{background:var(--good);color:var(--card)}
.decide button[aria-pressed=true].rej{background:var(--bad);color:var(--card)}
.cellview{margin-left:auto}
.cellview button{font-size:.64rem;padding:.22rem .5rem}

.scroll{overflow-x:auto}
body.only-open .card--done{display:none}
:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
@media (prefers-reduced-motion:reduce){*{transition:none!important;animation:none!important}}
@media (max-width:820px){.cols{grid-template-columns:1fr!important}}
#confetti{position:fixed;inset:0;pointer-events:none;z-index:60;display:none}
.done-banner{display:none;align-items:center;gap:.7rem;background:var(--good-bg);
  border-left:3px solid var(--good);color:var(--good);font-weight:600;
  padding:.75rem 1rem;border-radius:0 6px 6px 0;margin:0 0 1.2rem;font-size:.9rem}
body.all-done .done-banner{display:flex}
footer{border-top:1px solid var(--rule);margin-top:2rem;padding-top:1rem;
  color:var(--muted);font-size:.78rem;max-width:74ch}
"""


_JS = r"""
(function(){
'use strict';
var DATA = JSON.parse(document.getElementById('data').textContent);
var app = document.getElementById('app');
var STATES = ['markup','before','after'];
var view = 'markup';

function esc(s){
  return String(s == null ? '' : s).replace(/[&<>"']/g, function(c){
    return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c];
  });
}

// ---- persistence, keyed to the data so a regenerated report starts clean ---
var KEY = 'fincheck-console:' + DATA.benchmark + ':' + DATA.items.length + ':' +
          DATA.docs.map(function(d){return d.label;}).join('|');
var store = {};
try { store = JSON.parse(localStorage.getItem(KEY) || '{}') || {}; } catch(e){ store = {}; }
function save(){ try { localStorage.setItem(KEY, JSON.stringify(store)); } catch(e){} }
function slot(id, doc){ return id + '||' + doc; }

// ---- word-level LCS diff -------------------------------------------------
function words(s){ return String(s || '').split(/(\s+)/).filter(function(w){ return w.length; }); }
function diff(ref, cur){
  var a = words(ref), b = words(cur), m = a.length, n = b.length;
  // Guard: the quadratic table is fine for a passage, not for a whole note.
  if (m * n > 4000000) return [{op:'-',t:ref},{op:'+',t:cur}];
  var dp = new Array(m + 1);
  for (var i = 0; i <= m; i++) dp[i] = new Int32Array(n + 1);
  for (var i = m - 1; i >= 0; i--)
    for (var j = n - 1; j >= 0; j--)
      dp[i][j] = a[i] === b[j] ? dp[i+1][j+1] + 1 : Math.max(dp[i+1][j], dp[i][j+1]);
  var out = [], x = 0, y = 0;
  while (x < m && y < n){
    if (a[x] === b[y]) { out.push({op:'=',t:b[y]}); x++; y++; }
    else if (dp[x+1][y] >= dp[x][y+1]) { out.push({op:'-',t:a[x]}); x++; }
    else { out.push({op:'+',t:b[y]}); y++; }
  }
  while (x < m) out.push({op:'-',t:a[x++]});
  while (y < n) out.push({op:'+',t:b[y++]});
  return out;
}
function render(ops){
  var ins = 0, ext = 0, html = '';
  for (var i = 0; i < ops.length; i++){
    var p = ops[i];
    if (p.op === '=') { html += esc(p.t); continue; }
    if (/^\s+$/.test(p.t)) { html += esc(p.t); continue; }
    if (p.op === '-') { ins++; html += '<span class="ins">' + esc(p.t) + '</span>'; }
    else { ext++; html += '<span class="ext">' + esc(p.t) + '</span>'; }
  }
  return {html: html, ins: ins, ext: ext};
}

// ---- bookkeeping ---------------------------------------------------------
function required(item){ return item.cells.filter(function(c){ return c.differs; }); }
function decided(item){
  return required(item).filter(function(c){ return store[slot(item.id, c.doc)]; }).length;
}
function totals(){
  var req = 0, dec = 0;
  DATA.items.forEach(function(it){ req += required(it).length; dec += decided(it); });
  return {req: req, dec: dec};
}

// ---- markup --------------------------------------------------------------
function cellHTML(item, cell){
  var cls = 'cell';
  if (cell.isBenchmark) cls += ' cell--bench';
  if (cell.absent) cls += ' cell--absent';
  var top = '<div class="cell-top"><span class="doc">' + esc(cell.doc) + '</span>';
  if (cell.isBenchmark) top += '<span class="tag tag--bench">Benchmark</span>';
  else if (cell.absent) {}
  else if (!cell.differs) top += '<span class="tag tag--match">Matches benchmark</span>';
  else top += '<span class="tag tag--diff">Differs</span>';
  if (!cell.absent && cell.page){
    var doc = DATA.docs.filter(function(d){ return d.label === cell.doc; })[0] || {};
    var p = String(cell.page).split(/[–-]/)[0].trim();
    top += doc.href
      ? '<a class="chip" href="' + esc(doc.href) + '#page=' + esc(p) + '" target="_blank" rel="noopener">p' + esc(cell.page) + '</a>'
      : '<span class="chip">p' + esc(cell.page) + '</span>';
  }
  if (!cell.isBenchmark && !cell.absent)
    top += '<span class="sim">' + Math.round(cell.sim * 100) + '%</span>';
  top += '</div>';

  if (cell.absent)
    return '<div class="' + cls + '" data-doc="' + esc(cell.doc) + '">' + top +
           '<p class="absent-note">not present — no review needed</p></div>';

  var body, legend = '';
  if (cell.differs){
    var r = render(diff(item.refText, cell.text));
    body = r.html;
    legend = '<p class="legend"><b class="k k-ins" style="color:var(--ins)">insertions (' + r.ins +
             ')</b> · <b class="k k-ext" style="color:var(--ext)">extras (' + r.ext +
             ')</b>, vs ' + esc(item.benchmarkDoc) + '</p>';
  } else {
    body = esc(cell.text);
  }
  var clip = cell.text.length > 420 ? ' clipped' : '';
  var out = '<div class="' + cls + '" data-doc="' + esc(cell.doc) + '">' + top +
            '<p class="body v-' + view + clip + '">' + body + '</p>';
  if (clip) out += '<button class="more" type="button">Show full text</button>';
  out += legend;
  if (cell.differs){
    out += '<div class="cell-actions"><span class="decide">' +
           '<button class="acc" data-decide="accepted" aria-pressed="false">Accept</button>' +
           '<button class="rej" data-decide="rejected" aria-pressed="false">Reject</button>' +
           '</span><span class="seg cellview">' +
           STATES.map(function(s){
             return '<button data-view="' + s + '" aria-pressed="' + (s === view) +
                    '">' + (s === 'markup' ? 'Track changes' : s === 'before' ? 'Before' : 'After') + '</button>';
           }).join('') + '</span></div>';
  }
  return out + '</div>';
}

function cardHTML(item){
  var doc0 = DATA.docs.filter(function(d){ return d.label === item.benchmarkDoc; })[0] || {};
  var badge = doc0.href
    ? '<a class="badge" href="' + esc(doc0.href) + '#page=' + esc(String(item.cells[0].page).split(/[–-]/)[0]) + '" target="_blank" rel="noopener">' + esc(item.id) + '</a>'
    : '<span class="badge">' + esc(item.id) + '</span>';
  var need = required(item).length;
  var head = '<div class="card-head">' + badge +
    '<div class="head-text"><h2>' + esc(item.title) + '</h2>' +
    (item.subtitle ? '<p title="' + esc(item.subtitle) + '">' + esc(item.subtitle) + '</p>' : '') +
    '</div><span class="pill" data-pill></span>';
  if (need) head += '<span class="bulk">' +
    '<button class="acc" data-bulk="accepted">Accept all</button>' +
    '<button class="rej" data-bulk="rejected">Reject all</button></span>';
  if (item.absentDocs.length)
    head += '<div class="note-line">Not present in: ' + esc(item.absentDocs.join(', ')) + '</div>';
  if (item.fallbackUsed)
    head += '<div class="note-line">Benchmarked against ' + esc(item.benchmarkDoc) +
            ' (absent from the benchmark)</div>';
  head += '</div>';

  var cols = '<div class="scroll"><div class="cols" style="grid-template-columns:repeat(' +
             item.cells.length + ',minmax(0,1fr))">' +
             item.cells.map(function(c){ return cellHTML(item, c); }).join('') +
             '</div></div>';
  return '<section class="card" data-id="' + esc(item.id) + '">' + head + cols + '</section>';
}

function paintCard(card){
  var item = byId[card.dataset.id];
  var need = required(item), done = decided(item);
  var pill = card.querySelector('[data-pill]');
  var rej = need.filter(function(c){ return store[slot(item.id, c.doc)] === 'rejected'; }).length;
  if (!need.length){
    pill.className = 'pill pill--same';
    pill.textContent = item.absentDocs.length === item.cells.length - 1
      ? 'not present elsewhere' : 'consistent';
  } else if (done < need.length){
    pill.className = 'pill pill--pend';
    pill.textContent = (need.length - done) + ' of ' + need.length + ' pending';
  } else {
    pill.className = rej ? 'pill pill--rej' : 'pill pill--ok';
    pill.textContent = rej ? ('Resolved · ' + rej + ' rejected') : 'Resolved · all accepted';
  }
  card.classList.toggle('card--done', need.length > 0 && done === need.length);
  if (!need.length) card.classList.add('card--done');

  card.querySelectorAll('.cell[data-doc]').forEach(function(cell){
    var verdict = store[slot(item.id, cell.dataset.doc)];
    cell.classList.toggle('cell--acc', verdict === 'accepted');
    cell.classList.toggle('cell--rej', verdict === 'rejected');
    cell.querySelectorAll('[data-decide]').forEach(function(b){
      b.setAttribute('aria-pressed', String(b.dataset.decide === verdict));
    });
  });
}

var byId = {};
function paintProgress(){
  var t = totals();
  var pct = t.req ? Math.round(100 * t.dec / t.req) : 100;
  document.getElementById('bar').style.width = pct + '%';
  document.getElementById('pct').textContent = pct + '%';
  document.getElementById('count').textContent = t.dec + ' of ' + t.req;
  document.getElementById('kResolved').textContent = t.dec;
  var done = t.req > 0 && t.dec === t.req;
  document.body.classList.toggle('all-done', done);
  if (done && !celebrated){ celebrated = true; celebrate(); }
}
var celebrated = false;

// ---- build ---------------------------------------------------------------
DATA.items.forEach(function(it){ byId[it.id] = it; });
var need = DATA.items.reduce(function(n, it){ return n + required(it).length; }, 0);
app.innerHTML =
  '<div class="done-banner"><span>✓</span><span>Every difference has been reviewed. ' +
  'Export the decisions to keep the record with the file.</span></div>' +
  DATA.items.map(cardHTML).join('');
document.getElementById('kNeed').textContent = need;
document.querySelectorAll('.card').forEach(paintCard);
paintProgress();

// ---- interaction ---------------------------------------------------------
document.addEventListener('click', function(e){
  var t = e.target;

  var more = t.closest('.more');
  if (more){
    var body = more.parentElement.querySelector('.body');
    var open = body.classList.toggle('clipped');
    more.textContent = open ? 'Show full text' : 'Show less';
    return;
  }

  var dec = t.closest('[data-decide]');
  if (dec){
    var cell = dec.closest('.cell'), card = dec.closest('.card');
    var key = slot(card.dataset.id, cell.dataset.doc);
    store[key] = store[key] === dec.dataset.decide ? undefined : dec.dataset.decide;
    if (!store[key]) delete store[key];
    save(); paintCard(card); paintProgress();
    return;
  }

  var bulk = t.closest('[data-bulk]');
  if (bulk){
    var card2 = bulk.closest('.card'), item = byId[card2.dataset.id];
    required(item).forEach(function(c){ store[slot(item.id, c.doc)] = bulk.dataset.bulk; });
    save(); paintCard(card2); paintProgress();
    return;
  }

  var cv = t.closest('.cellview [data-view]');
  if (cv){
    var body2 = cv.closest('.cell').querySelector('.body');
    body2.className = 'body v-' + cv.dataset.view +
      (body2.classList.contains('clipped') ? ' clipped' : '');
    cv.parentElement.querySelectorAll('[data-view]').forEach(function(b){
      b.setAttribute('aria-pressed', String(b === cv));
    });
    return;
  }

  var gv = t.closest('#gview [data-view]');
  if (gv){
    view = gv.dataset.view;
    document.querySelectorAll('#gview [data-view]').forEach(function(b){
      b.setAttribute('aria-pressed', String(b === gv));
    });
    document.querySelectorAll('.body').forEach(function(b){
      b.className = 'body v-' + view + (b.classList.contains('clipped') ? ' clipped' : '');
    });
    document.querySelectorAll('.cellview [data-view]').forEach(function(b){
      b.setAttribute('aria-pressed', String(b.dataset.view === view));
    });
  }
});

document.getElementById('only').addEventListener('change', function(e){
  document.body.classList.toggle('only-open', e.target.checked);
});

// ---- export --------------------------------------------------------------
function download(name, text, mime){
  var blob = new Blob([text], {type: mime});
  var url = URL.createObjectURL(blob);
  var a = document.createElement('a');
  a.href = url; a.download = name; a.rel = 'noopener';
  document.body.appendChild(a); a.click(); a.remove();
  setTimeout(function(){ URL.revokeObjectURL(url); }, 1000);
}
function records(){
  var rows = [];
  DATA.items.forEach(function(it){
    it.cells.forEach(function(c){
      rows.push({
        item: it.id, title: it.title, section: it.subtitle, document: c.doc,
        role: c.isBenchmark ? 'benchmark' : (c.absent ? 'absent' : (c.differs ? 'differs' : 'matches')),
        page: c.page || '', match: c.absent ? '' : Math.round(c.sim * 100),
        reasons: (c.reasons || []).join('; '),
        decision: c.differs ? (store[slot(it.id, c.doc)] || 'open') : 'n/a',
        text: c.text || ''
      });
    });
  });
  return rows;
}
document.getElementById('csv').addEventListener('click', function(){
  var q = function(v){ return '"' + String(v).replace(/"/g, '""') + '"'; };
  var head = ['Item','Title','Section','Document','Role','Page','Match %','Reasons','Decision','Text'];
  var out = [head.map(q).join(',')];
  records().forEach(function(r){
    out.push([r.item,r.title,r.section,r.document,r.role,r.page,r.match,r.reasons,r.decision,r.text]
             .map(q).join(','));
  });
  download('review_decisions.csv', out.join('\r\n'), 'text/csv;charset=utf-8');
});
document.getElementById('json').addEventListener('click', function(){
  download('review_decisions.json', JSON.stringify({
    benchmark: DATA.benchmark, exported: new Date().toISOString(),
    documents: DATA.docs.map(function(d){ return d.label; }),
    rows: records()
  }, null, 2), 'application/json');
});
document.getElementById('reset').addEventListener('click', function(){
  if (!Object.keys(store).length) return;
  if (!confirm('Clear every accept/reject decision in this report?')) return;
  store = {}; save();
  document.querySelectorAll('.card').forEach(paintCard);
  celebrated = false;
  paintProgress();
});

// ---- celebration ---------------------------------------------------------
function celebrate(){
  if (window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches) return;
  var cv = document.getElementById('confetti');
  var ctx = cv.getContext('2d');
  cv.width = innerWidth; cv.height = innerHeight; cv.style.display = 'block';
  var colours = ['#1E6B45','#33506E','#9A5406','#1F5A93'];
  var bits = [];
  for (var i = 0; i < 140; i++) bits.push({
    x: Math.random() * cv.width, y: -20 - Math.random() * cv.height * 0.5,
    r: 3 + Math.random() * 4, vy: 1.6 + Math.random() * 2.6,
    vx: -1 + Math.random() * 2, a: Math.random() * Math.PI,
    c: colours[i % colours.length]
  });
  var frames = 0;
  (function tick(){
    ctx.clearRect(0, 0, cv.width, cv.height);
    bits.forEach(function(b){
      b.y += b.vy; b.x += b.vx; b.a += 0.08;
      ctx.save(); ctx.translate(b.x, b.y); ctx.rotate(b.a);
      ctx.fillStyle = b.c; ctx.fillRect(-b.r, -b.r * 0.6, b.r * 2, b.r * 1.2);
      ctx.restore();
    });
    if (++frames < 190) requestAnimationFrame(tick);
    else { ctx.clearRect(0, 0, cv.width, cv.height); cv.style.display = 'none'; }
  })();
}
})();
"""


def write_console(sections, meta, output_path: str) -> str:
    """Write the interactive accept/reject console for a comparison."""
    payload = build_payload(sections, meta)
    blob = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    # An embedded JSON blob must never be able to close its own script tag.
    blob = blob.replace("</", "<\\/")

    s = meta.summary
    need = sum(
        1 for x in sections if x.status in ("changed", "added", "removed")
    )
    page = f"""<title>Review console — {_e(meta.label_b)} against {_e(meta.label_a)}</title>
<style>{_CSS}</style>
<div class="wrap">
  <p class="eyebrow">Financial reporting &middot; interactive review</p>
  <h1>{_e(meta.label_b)} against benchmark {_e(meta.label_a)}</h1>
  <p class="intro">Each item is one page of the benchmark, numbered in order, shown
  beside the same content in the compared document. Where they differ, only the
  differing <em>words</em> are marked:
  <span class="k k-ins">insertions</span> are words the benchmark has that this
  document does not, <span class="k k-ext">extras</span> are words here that the
  benchmark does not have. Switch <b>Before</b> to strike the insertions (change
  not yet made) or <b>After</b> to strike the extras (change incorporated) — the
  text and both colours stay on screen in every state, so nothing moves under
  you. Accept a variation you are content with, reject one that needs
  correcting; decisions are saved in this browser and can be exported.</p>

  <div class="kpis">
    <div class="kpi"><b>{len(payload["docs"])}</b><span>documents</span></div>
    <div class="kpi"><b>{len(sections)}</b><span>items compared</span></div>
    <div class="kpi kpi--warn"><b id="kNeed">{need}</b><span>needing review</span></div>
    <div class="kpi kpi--good"><b id="kResolved">0</b><span>resolved</span></div>
    <div class="kpi"><b>{s.overall}%</b><span>composite match</span></div>
  </div>

  <div class="toolbar">
    <div class="prog">
      <div class="prog-top"><span>Decisions</span>
        <span><span id="count">0 of 0</span> &middot;
        <span class="prog-pct" id="pct">0%</span></span></div>
      <div class="track"><div class="bar" id="bar"></div></div>
    </div>
    <span class="seg" id="gview" role="group" aria-label="View all differences as">
      <button data-view="markup" aria-pressed="true">Track changes</button>
      <button data-view="before" aria-pressed="false">Before</button>
      <button data-view="after" aria-pressed="false">After</button>
    </span>
    <label class="switch"><input type="checkbox" id="only"> Only unresolved</label>
    <button class="btn" id="csv" type="button">Export CSV</button>
    <button class="btn" id="json" type="button">Export JSON</button>
    <button class="btn" id="reset" type="button">Reset</button>
  </div>

  <div id="app">Loading review board&hellip;</div>

  <footer>Items are the benchmark's pages, in order, so every word of it is
  covered. Differences are computed word by word; a cell counts as matching only
  when it is identical. Absent content is not a difference and needs no
  decision.</footer>
</div>
<canvas id="confetti" aria-hidden="true"></canvas>
<script id="data" type="application/json">{blob}</script>
<script>{_JS}</script>
"""
    with open(output_path, "w", encoding="utf-8") as fh:
        fh.write(page)
    return output_path
