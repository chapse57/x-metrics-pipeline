"""Deliverables: the client CSV (same columns as the 2026-09 hand-made sheet) and a
single-file HTML dashboard with no external dependencies (opens from disk, emailable)."""
from __future__ import annotations

import csv
import html
import json
from datetime import datetime, timezone
from pathlib import Path

from .store import Store
from .validate import Issue

CSV_COLUMNS = [
    "Handle", "Profile Link", "Followers",
    "Engagement Rate (median likes+replies+reposts / followers)",
    "Views-to-Followers (median views / followers)",
    "Median Likes", "Median Replies", "Median Reposts", "Median Views",
    "Posts Measured", "Date Range Measured", "Tier", "Niche", "Bio Contact",
    "Hook Link", "Hook Note", "Why This Account Fits",
]


def _num(x: float) -> str:
    return str(int(x)) if float(x).is_integer() else f"{x:g}"


def rows_for_export(store: Store, issues: list[Issue]) -> tuple[list[dict], list[str]]:
    blocked = {i.handle for i in issues if i.severity == "error"}
    flagged = {i.handle for i in issues if i.severity == "warn"}
    out = []
    for r in store.latest_measurements():
        if r["handle"] in blocked:
            continue
        contact = "; ".join(x for x in (("DM open" if r["dm_open"] else ""), r["bio_url"] or "") if x)
        out.append({
            "handle": r["display"] or r["handle"], "profile": f"https://x.com/{r['display'] or r['handle']}",
            "followers": r["followers"], "er": r["engagement_rate"], "vr": r["views_to_followers"],
            "m_likes": r["median_likes"], "m_replies": r["median_replies"], "m_reposts": r["median_reposts"],
            "m_views": r["median_views"], "n": r["posts_measured"],
            "range": f"{r['range_start']} to {r['range_end']}", "tier": r["tier"], "niche": r["niche"] or "",
            "contact": contact, "hook_link": r["hook_link"] or "", "hook_note": r["hook_note"] or "",
            "fit": r["fit_note"] or "", "bio": r["bio"] or "", "flagged": r["handle"] in flagged,
            "days_since": r["days_since_last_post"], "measured_at": r["measured_at"],
        })
    return out, sorted(blocked)


def write_csv(rows: list[dict], path: str | Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(CSV_COLUMNS)
        for r in rows:
            w.writerow([
                "@" + r["handle"].lstrip("@"), r["profile"], f"{r['followers']:,}",
                f"{r['er']:.2f}%", f"{r['vr']:.1f}%",
                _num(r["m_likes"]), _num(r["m_replies"]), _num(r["m_reposts"]), f"{r['m_views']:,.0f}" if float(r["m_views"]).is_integer() else f"{r['m_views']:,}",
                r["n"], r["range"], r["tier"], r["niche"], r["contact"], r["hook_link"], r["hook_note"], r["fit"],
            ])
    return path


_DASH = r"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>__TITLE__</title>
<style>
:root{--bg:#f4f6f8;--card:#ffffff;--ink:#12181f;--mut:#5f6f7e;--line:#dfe6ec;--acc:#0f7fc4;--warn:#9a5b00;--warnbg:#fff1d6}
@media(prefers-color-scheme:dark){:root:not([data-theme="light"]){--bg:#0e141a;--card:#161f28;--ink:#e6ebef;--mut:#8a9aa8;--line:#2b3742;--acc:#3ea6e8;--warn:#f0b35a;--warnbg:#2c2314}}
:root[data-theme="dark"]{--bg:#0e141a;--card:#161f28;--ink:#e6ebef;--mut:#8a9aa8;--line:#2b3742;--acc:#3ea6e8;--warn:#f0b35a;--warnbg:#2c2314}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.45 -apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif}
:focus-visible{outline:2px solid var(--acc);outline-offset:2px}@media(prefers-reduced-motion:reduce){*{transition:none!important}}
header{padding:20px 24px 8px}h1{margin:0 0 4px;font-size:20px}.sub{color:var(--mut);font-size:13px}
.wrap{padding:0 24px 40px;display:grid;gap:16px}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 14px}
.card b{display:block;font-size:22px}.card span{color:var(--mut);font-size:12px}
.controls{display:flex;flex-wrap:wrap;gap:10px;align-items:center;background:var(--card);border:1px solid var(--line);border-radius:10px;padding:10px 14px}
.controls label{display:flex;gap:6px;align-items:center;color:var(--mut);font-size:13px}
input,select{background:var(--bg);color:var(--ink);border:1px solid var(--line);border-radius:6px;padding:6px 8px;font:inherit}
input[type=range]{width:120px}
.grid{display:grid;grid-template-columns:2fr 1fr;gap:16px}@media(max-width:900px){.grid{grid-template-columns:1fr}}
.tbl{background:var(--card);border:1px solid var(--line);border-radius:10px;overflow:auto}
table{border-collapse:collapse;width:100%;min-width:760px}th,td{padding:8px 10px;border-bottom:1px solid var(--line);text-align:left;white-space:nowrap}
th{position:sticky;top:0;background:var(--card);cursor:pointer;user-select:none;font-size:12px;color:var(--mut)}th.on{color:var(--acc)}
td.num{text-align:right;font-variant-numeric:tabular-nums}tr:hover td{background:rgba(29,155,240,.06)}tr.sel td{background:rgba(29,155,240,.14)}
.tag{display:inline-block;padding:1px 7px;border-radius:999px;font-size:11px;border:1px solid var(--line);color:var(--mut)}
.flag{color:var(--warn);background:var(--warnbg);border-color:transparent}
.detail{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px;min-height:200px}
.detail h2{margin:0 0 6px;font-size:16px}.detail p{margin:6px 0}.detail a{color:var(--acc);text-decoration:none}
canvas{width:100%;height:240px;display:block;background:var(--card);border:1px solid var(--line);border-radius:10px}
.foot{color:var(--mut);font-size:12px}
</style></head><body>
<header><h1>__TITLE__</h1><div class="sub">__SUBTITLE__</div></header>
<div class="wrap">
<div class="cards" id="cards"></div>
<div class="controls">
 <label>Search <input id="q" placeholder="handle, bio, niche"></label>
 <label>Niche <select id="niche"><option value="">all</option></select></label>
 <label>Tier <select id="tier"><option value="">all</option><option>Micro (<25K)</option><option>Mid (25-100K)</option><option>Macro (100K+)</option></select></label>
 <label>Min engagement % <input id="er" type="range" min="0" max="2" step="0.01" value="0"><b id="erv">0.00</b></label>
 <label><input id="hideflag" type="checkbox"> hide flagged</label>
 <span class="foot" id="count"></span>
</div>
<div class="grid">
 <div><div class="tbl"><table id="t"><thead><tr>
  <th data-k="handle">Handle</th><th data-k="followers" class="num">Followers</th><th data-k="er" class="num">Engagement %</th>
  <th data-k="vr" class="num">Views/Followers %</th><th data-k="m_likes" class="num">Med. likes</th><th data-k="n" class="num">Posts</th>
  <th data-k="days_since" class="num">Last post (d)</th><th data-k="tier">Tier</th><th data-k="niche">Niche</th></tr></thead><tbody></tbody></table></div></div>
 <div style="display:grid;gap:16px;align-content:start"><canvas id="c" width="600" height="240"></canvas><div class="detail" id="d"><p class="foot">Select a row to see bio, contact and the hook post.</p></div></div>
</div>
<div class="foot">Engagement % = median(likes+replies+reposts) of the last N original posts ÷ followers. Views/Followers % = median views ÷ followers. Reposts and pinned posts excluded. Flagged rows carry a validation warning (see validation_report.md). Generated __GENERATED__.</div>
</div>
<script>
const DATA=__DATA__;
const $=s=>document.querySelector(s);const esc=s=>String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
let sortK='er',sortDir=-1,sel=null;
const niches=[...new Set(DATA.map(r=>r.niche).filter(Boolean))].sort();for(const n of niches){const o=document.createElement('option');o.textContent=n;$('#niche').append(o)}
function med(a){if(!a.length)return 0;a=[...a].sort((x,y)=>x-y);const m=a.length>>1;return a.length%2?a[m]:(a[m-1]+a[m])/2}
function filtered(){const q=$('#q').value.toLowerCase(),n=$('#niche').value,t=$('#tier').value,er=+$('#er').value,hf=$('#hideflag').checked;
 return DATA.filter(r=>(!q||(r.handle+' '+r.bio+' '+r.niche+' '+r.fit).toLowerCase().includes(q))&&(!n||r.niche===n)&&(!t||r.tier===t)&&r.er>=er&&(!hf||!r.flagged))
 .sort((a,b)=>{const x=a[sortK],y=b[sortK];return (typeof x==='number'?x-y:String(x).localeCompare(String(y)))*sortDir})}
function render(){const rows=filtered();$('#erv').textContent=(+$('#er').value).toFixed(2);$('#count').textContent=rows.length+' / '+DATA.length+' accounts';
 $('#cards').innerHTML=[['Accounts',rows.length],['Median engagement %',med(rows.map(r=>r.er)).toFixed(2)],['Median views/followers %',med(rows.map(r=>r.vr)).toFixed(1)],
  ['Micro / Mid / Macro',['Micro (<25K)','Mid (25-100K)','Macro (100K+)'].map(t=>rows.filter(r=>r.tier===t).length).join(' / ')],['Flagged',rows.filter(r=>r.flagged).length]]
  .map(([k,v])=>`<div class="card"><b>${v}</b><span>${k}</span></div>`).join('');
 $('#t tbody').innerHTML=rows.map((r,i)=>`<tr data-h="${esc(r.handle)}" class="${sel===r.handle?'sel':''}"><td>@${esc(r.handle)} ${r.flagged?'<span class="tag flag">flag</span>':''}</td><td class="num">${r.followers.toLocaleString()}</td><td class="num">${r.er.toFixed(2)}</td><td class="num">${r.vr.toFixed(1)}</td><td class="num">${r.m_likes}</td><td class="num">${r.n}</td><td class="num">${r.days_since??''}</td><td><span class="tag">${esc(r.tier)}</span></td><td>${esc(r.niche)}</td></tr>`).join('');
 document.querySelectorAll('th').forEach(th=>th.classList.toggle('on',th.dataset.k===sortK));chart(rows)}
function chart(rows){const c=$('#c'),ctx=c.getContext('2d'),W=c.width,H=c.height,P=36;ctx.clearRect(0,0,W,H);
 const cs=getComputedStyle(document.body);ctx.fillStyle=cs.getPropertyValue('--mut');ctx.font='11px sans-serif';ctx.fillText('followers (log) →',P,H-6);ctx.save();ctx.translate(10,H/2);ctx.rotate(-Math.PI/2);ctx.fillText('engagement %',-40,0);ctx.restore();
 if(!rows.length)return;const lx=r=>Math.log10(Math.max(r.followers,100)),xs=rows.map(lx),ys=rows.map(r=>r.er);const x0=Math.min(...xs),x1=Math.max(...xs)+.01,y1=Math.max(...ys)+.01;
 ctx.strokeStyle=cs.getPropertyValue('--line');ctx.strokeRect(P,8,W-P-8,H-P-8);
 for(const r of rows){const x=P+(lx(r)-x0)/(x1-x0)*(W-P-8),y=8+(1-r.er/y1)*(H-P-8);ctx.beginPath();ctx.arc(x,y,r.handle===sel?6:3.5,0,7);ctx.fillStyle=r.handle===sel?'#f4212e':cs.getPropertyValue('--acc');ctx.globalAlpha=r.handle===sel?1:.7;ctx.fill();ctx.globalAlpha=1}}
function detail(h){sel=h;const r=DATA.find(x=>x.handle===h);if(!r)return;$('#d').innerHTML=`<h2>@${esc(r.handle)} <span class="tag">${esc(r.tier)}</span></h2><p><a href="${esc(r.profile)}" target="_blank">${esc(r.profile)}</a></p><p>${esc(r.bio)}</p><p><b>Contact:</b> ${esc(r.contact)||'—'}</p><p><b>Niche:</b> ${esc(r.niche)}</p><p><b>Why it fits:</b> ${esc(r.fit)}</p>${r.hook_link?`<p><b>Hook:</b> <a href="${esc(r.hook_link)}" target="_blank">post</a> — ${esc(r.hook_note)}</p>`:''}<p class="foot">${r.n} posts, ${esc(r.range)} · medians L/R/RP/V ${r.m_likes}/${r.m_replies}/${r.m_reposts}/${r.m_views} · measured ${esc(r.measured_at)}</p>`;render()}
document.querySelectorAll('th').forEach(th=>th.onclick=()=>{const k=th.dataset.k;sortDir=sortK===k?-sortDir:(k==='handle'||k==='tier'||k==='niche'?1:-1);sortK=k;render()});
$('#t').addEventListener('click',e=>{const tr=e.target.closest('tr[data-h]');if(tr)detail(tr.dataset.h)});
for(const id of['q','niche','tier','er','hideflag'])$('#'+id).addEventListener('input',render);
render();
</script></body></html>"""


def write_dashboard(rows: list[dict], path: str | Path, *, title: str = "X influencer metrics", subtitle: str = "") -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = (_DASH.replace("__TITLE__", html.escape(title)).replace("__SUBTITLE__", html.escape(subtitle))
           .replace("__GENERATED__", datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"))
           .replace("__DATA__", json.dumps(rows, ensure_ascii=False).replace("</", "<\\/")))
    path.write_text(doc, encoding="utf-8")
    return path
