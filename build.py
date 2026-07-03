#!/usr/bin/env python3
"""
Build the OneDome public help center as a static site from the Confluence
"Customer Support KB" (CSKB) space.

At build time this authenticates to Confluence with a service account, walks the
CSKB page tree (Home -> categories -> sections -> articles), and emits a single
self-contained static site (dist/index.html) styled to match onedome.com
(navbar, black footer, brand colours, Proxima Nova / Inter) with the
help.onedome.com layout (hero + search + topic cards + article views).

Because the fetch happens server-side at build time, the published static HTML
is fully public while the Confluence space itself stays private. Runs daily via
GitHub Actions and deploys to Cloudflare Pages.

Env:
  ATLASSIAN_EMAIL, ATLASSIAN_API_TOKEN   Confluence service account (basic auth)
  CONFLUENCE_DOMAIN                       default onedome.atlassian.net
  CSKB_SPACE_ID                           default 591855635

Usage:
  python3 build.py            # writes dist/index.html
"""

import os
import re
import json
import html
from pathlib import Path

import requests

ROOT = Path(__file__).parent
OUT_DIR = ROOT / "dist"
OUT = OUT_DIR / "index.html"

EMAIL = os.getenv("ATLASSIAN_EMAIL", "")
TOKEN = os.getenv("ATLASSIAN_API_TOKEN", "")
DOMAIN = os.getenv("CONFLUENCE_DOMAIN", "onedome.atlassian.net")
SPACE_ID = os.getenv("CSKB_SPACE_ID", "591855635")
BASE = f"https://{DOMAIN}/wiki"
AUTH = (EMAIL, TOKEN)

# Articles carrying this Confluence label are surfaced in "Popular articles".
PROMOTED_LABEL = "promoted"


def _get(path, params=None):
    for _ in range(6):
        r = requests.get(f"{BASE}{path}", auth=AUTH, params=params,
                         headers={"Accept": "application/json"}, timeout=60)
        if r.status_code in (429, 500, 502, 503, 504):
            continue
        return r
    return r


def fetch_pages():
    """All published (current) pages in the space, with storage bodies."""
    pages, cursor = [], None
    while True:
        params = {"status": "current", "limit": 100, "body-format": "storage"}
        if cursor:
            params["cursor"] = cursor
        r = _get(f"/api/v2/spaces/{SPACE_ID}/pages", params)
        if r.status_code != 200:
            raise SystemExit(f"Confluence fetch failed: {r.status_code} {r.text[:300]}")
        data = r.json()
        pages.extend(data.get("results", []))
        nxt = data.get("_links", {}).get("next")
        if not nxt or "cursor=" not in nxt:
            break
        cursor = nxt.split("cursor=")[1].split("&")[0]
    return pages


def fetch_promoted_ids():
    """Page ids labelled `promoted` (best-effort; empty if the label is unused)."""
    ids, cursor = set(), None
    r = _get(f"/api/v2/labels", {"prefix": "global"})  # not all sites expose; ignore errors
    # Simpler + reliable: query pages by label via CQL search.
    r = _get("/rest/api/content/search",
             {"cql": f'space.id={SPACE_ID} and label="{PROMOTED_LABEL}"', "limit": 50})
    if r.status_code == 200:
        for res in r.json().get("results", []):
            ids.add(str(res.get("id")))
    return ids


def strip_tags(s):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]*>", " ", s or "")).strip()


def build_tree(pages):
    by_id = {p["id"]: p for p in pages}

    def depth(p):
        d, pid = 0, p.get("parentId")
        while pid and pid in by_id:
            d += 1
            pid = by_id[pid].get("parentId")
        return d

    # Home = depth 0; categories = depth 1; sections = depth 2; articles = depth 3
    categories, sections, articles = [], [], []
    for p in pages:
        d = depth(p)
        body = p.get("body", {}).get("storage", {}).get("value", "")
        rec = {"id": p["id"], "title": p["title"], "parentId": p.get("parentId"),
               "body": body}
        if d == 1:
            rec["description"] = strip_tags(body)[:160]
            categories.append(rec)
        elif d == 2:
            rec["description"] = strip_tags(body)[:160]
            sections.append(rec)
        elif d == 3:
            articles.append(rec)
    return categories, sections, articles


def build():
    if not (EMAIL and TOKEN):
        raise SystemExit("ERROR: ATLASSIAN_EMAIL / ATLASSIAN_API_TOKEN not set")
    pages = fetch_pages()
    categories, sections, articles = build_tree(pages)
    promoted = fetch_promoted_ids()
    for a in articles:
        a["promoted"] = a["id"] in promoted

    payload = {
        "categories": [{"id": c["id"], "name": c["title"], "description": c["description"]}
                       for c in categories],
        "sections": [{"id": s["id"], "name": s["title"], "category_id": s["parentId"],
                      "description": s["description"]} for s in sections],
        "articles": [{"id": a["id"], "title": a["title"], "section_id": a["parentId"],
                      "promoted": a["promoted"], "body": a["body"]} for a in articles],
    }

    html_out = TEMPLATE.replace("__DATA__", json.dumps(payload, ensure_ascii=False))
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    OUT.write_text(html_out, encoding="utf-8")
    print(f"Wrote {OUT}")
    print(f"  {len(payload['categories'])} categories, {len(payload['sections'])} sections, "
          f"{len(payload['articles'])} articles, {sum(a['promoted'] for a in payload['articles'])} promoted")


# --- Template: onedome.com branding + help.onedome.com layout ----------------
# Real deployed site (not a CSP-restricted artifact), so external fonts are fine.

TEMPLATE = r"""<!doctype html>
<html lang="en-GB">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>OneDome Help</title>
<meta name="description" content="OneDome help centre - guides and FAQs for home movers, estate agents, conveyancers and mortgages.">
<link rel="preconnect" href="https://cdn.onedome.com">
<link rel="stylesheet" href="https://cdn.onedome.com/fonts/fonts.css">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Inter:wght@400;600;700;800&display=swap">
<style>
  :root{
    --yellow:#FBBB30; --navy:#010022; --black:#000000; --white:#FFFFFF;
    --ink:#323238; --blue:#0038A6; --muted:#6B6C74; --border:#E7E7EC;
    --bg:#FFFFFF; --card:#FFFFFF;
    --body:"Proxima Nova",Helvetica,Arial,sans-serif;
    --head:"Inter","Proxima Nova",Helvetica,Arial,sans-serif;
  }
  *{box-sizing:border-box}
  html,body{margin:0;padding:0}
  body{font-family:var(--body); color:var(--ink); background:var(--bg); line-height:1.55; font-size:16px; -webkit-font-smoothing:antialiased}
  a{color:var(--blue); text-decoration:none}
  a:hover{text-decoration:underline}
  .wrap{max-width:1120px; margin:0 auto; padding:0 24px}
  h1,h2,h3{font-family:var(--head); color:var(--navy)}

  /* ---- navbar (onedome.com style) ---- */
  header.nav{background:var(--navy); position:sticky; top:0; z-index:50}
  header.nav .wrap{display:flex; align-items:center; height:72px; gap:28px}
  .brand{display:flex; align-items:center; gap:10px; cursor:pointer; text-decoration:none}
  .brand .pin{width:26px;height:26px;flex:0 0 auto}
  .brand .word{color:#fff; font-family:var(--head); font-weight:800; letter-spacing:.14em; font-size:16px}
  nav.menu{display:flex; gap:26px; margin-left:8px}
  nav.menu a{color:#EDEDF2; font-size:16px}
  nav.menu a:hover{color:#fff; text-decoration:none}
  .nav-spacer{flex:1}
  .call{background:var(--blue); color:#fff; border-radius:999px; padding:11px 20px; font-weight:600; font-size:15px; white-space:nowrap}
  .call:hover{filter:brightness(1.08); text-decoration:none}
  @media(max-width:860px){ nav.menu{display:none} }

  /* ---- hero (help.onedome.com layout, onedome colours) ---- */
  .hero{background:radial-gradient(1200px 400px at 50% -40%, #1b1746 0%, var(--navy) 60%); color:#fff; padding:72px 24px 88px; text-align:center}
  .hero h1{color:#fff; font-size:40px; font-weight:800; margin:0 0 28px; letter-spacing:-.01em; text-wrap:balance}
  .search{max-width:660px; margin:0 auto; position:relative}
  .search input{width:100%; height:56px; border:0; border-radius:999px; padding:0 22px 0 52px; font-size:16px; font-family:var(--body); color:var(--ink); box-shadow:0 10px 30px rgba(0,0,0,.28)}
  .search input:focus{outline:3px solid var(--yellow)}
  .search svg{position:absolute; left:20px; top:18px; width:20px; height:20px; opacity:.45}

  /* ---- sections ---- */
  main{padding:56px 0 72px; min-height:44vh}
  h2.block{text-align:center; font-size:26px; font-weight:800; margin:0 0 8px}
  .sub{text-align:center; color:var(--muted); margin:0 0 40px}
  .cards{display:grid; grid-template-columns:repeat(3,1fr); gap:24px}
  .card{border:1px solid var(--border); border-radius:18px; padding:28px 24px; background:var(--card); display:flex; flex-direction:column; transition:box-shadow .15s,transform .15s}
  .card:hover{box-shadow:0 12px 34px rgba(1,0,34,.10); transform:translateY(-2px)}
  .card h3{margin:0 0 10px; font-size:20px; font-weight:700}
  .card h3 a{color:var(--navy)}
  .card p{margin:0 0 20px; color:var(--muted); font-size:15px; flex:1}
  .learn{align-self:flex-start; background:var(--yellow); color:var(--navy); font-weight:700; font-size:14px; padding:11px 22px; border-radius:999px}
  .learn:hover{filter:brightness(.97); text-decoration:none}

  .popular{margin-top:64px}
  .popular ul{list-style:none; max-width:640px; margin:0 auto; padding:0}
  .popular li{padding:14px 4px; border-bottom:1px solid var(--border); font-size:16px}
  .popular li::before{content:""; }

  .breadcrumb{font-size:14px; color:var(--muted); margin:0 0 20px}
  .view h1.page{font-size:32px; font-weight:800; margin:0 0 6px; text-wrap:balance}
  .view .lead{color:var(--muted); margin:0 0 30px}
  .sec-group{margin:0 0 36px}
  .sec-group h3{font-size:18px; font-weight:700; margin:0 0 12px; padding-bottom:10px; border-bottom:1px solid var(--border)}
  .article-list{list-style:none; margin:0; padding:0}
  .article-list li{padding:10px 0}
  .article-list li::before{content:"›"; color:var(--yellow); font-weight:700; margin-right:12px}

  .article{max-width:760px; margin:0 auto}
  .article .body{font-size:17px; line-height:1.75; color:#2f3138}
  .article .body h2,.article .body h3{color:var(--navy)}
  .article .body h2{font-size:23px; margin:30px 0 10px}
  .article .body h3{font-size:19px; margin:24px 0 8px}
  .article .body p{margin:0 0 16px}
  .article .body ul,.article .body ol{margin:0 0 16px 22px}
  .article .body img{max-width:100%; height:auto; border-radius:8px}
  .article .body table{border-collapse:collapse; width:100%; display:block; overflow-x:auto}
  .article .body td,.article .body th{border:1px solid var(--border); padding:8px}
  .search-results{padding:0;margin:0;list-style:none}
  .search-results li{padding:14px 0; border-bottom:1px solid var(--border)}
  .search-results .snip{color:var(--muted); font-size:14px; margin-top:4px}
  .empty{color:var(--muted); text-align:center; padding:48px 0}

  /* ---- footer (onedome.com style) ---- */
  footer.site{background:var(--black); color:#fff; margin-top:56px}
  footer.site .cols{display:grid; grid-template-columns:repeat(4,1fr); gap:28px; padding:56px 24px 32px; max-width:1120px; margin:0 auto}
  footer.site h4{font-family:var(--head); font-size:13px; text-transform:uppercase; letter-spacing:.08em; color:#fff; margin:0 0 16px; font-weight:700}
  footer.site a{display:block; color:#B9B9C2; font-size:14px; padding:6px 0}
  footer.site a:hover{color:#fff}
  .foot-legal{border-top:1px solid rgba(255,255,255,.12)}
  .foot-legal .wrap{padding:18px 24px; font-size:13px; color:#8E8E99}
  @media(max-width:860px){ .cards{grid-template-columns:1fr} footer.site .cols{grid-template-columns:1fr 1fr} }
  @media(max-width:520px){ footer.site .cols{grid-template-columns:1fr} }
</style>
</head>
<body>
<header class="nav">
  <div class="wrap">
    <a class="brand" href="https://onedome.com">
      <svg class="pin" viewBox="0 0 24 24" fill="none"><path d="M12 2C7.6 2 4 5.6 4 10c0 5.2 6.6 11.3 7.4 12 .35.3.85.3 1.2 0C13.4 21.3 20 15.2 20 10c0-4.4-3.6-8-8-8Z" fill="#FBBB30"/><circle cx="12" cy="10" r="3" fill="#010022"/></svg>
      <span class="word">ONEDOME</span>
    </a>
    <nav class="menu">
      <a href="https://onedome.com/search">Buy</a>
      <a href="https://onedome.com/mortgages">Mortgages</a>
      <a href="https://onedome.com/conveyancing">Conveyancing</a>
      <a href="https://onedome.com/growth-partners">Growth Partners</a>
    </nav>
    <div class="nav-spacer"></div>
    <a class="call" href="tel:08081751255">Call Us 0808 175 1255</a>
  </div>
</header>

<section class="hero">
  <h1>How can we help you?</h1>
  <div class="search">
    <svg viewBox="0 0 24 24" fill="none" stroke="#333" stroke-width="2"><circle cx="11" cy="11" r="7"/><path d="M21 21l-4.3-4.3"/></svg>
    <input id="q" type="search" placeholder="Search for answers" autocomplete="off" aria-label="Search help articles">
  </div>
</section>

<main><div class="wrap" id="app"></div></main>

<footer class="site">
  <div class="cols">
    <div><h4>Company</h4>
      <a href="https://onedome.com/uk-properties">UK properties</a>
      <a href="https://onedome.com/estate-agents">All estate agents</a>
      <a href="https://onedome.com/buyer-passport">Buyer passport</a>
      <a href="#">FAQ</a>
      <a href="https://onedome.com/blog">Blog</a>
    </div>
    <div><h4>Explore</h4>
      <a href="https://onedome.com/properties-to-rent">Properties to rent</a>
      <a href="https://onedome.com/new-homes">New build homes for sale</a>
      <a href="https://onedome.com/conveyancing">Find a conveyancer</a>
      <a href="https://onedome.com/mortgages">Get a mortgage</a>
      <a href="https://onedome.com/community">Community</a>
    </div>
    <div><h4>OneDome</h4>
      <a href="https://onedome.com/about">About</a>
      <a href="https://onedome.com/careers">Careers</a>
      <a href="https://onedome.com/contact">Contact us</a>
    </div>
    <div><h4>Documentation</h4>
      <a href="https://onedome.com/privacy">Privacy Policy</a>
      <a href="https://onedome.com/terms">Terms and Conditions</a>
    </div>
  </div>
  <div class="foot-legal"><div class="wrap">© OneDome Ltd. All rights reserved.</div></div>
</footer>

<script>
const DATA = __DATA__;
const byId = (arr) => Object.fromEntries(arr.map(x => [String(x.id), x]));
const CAT = byId(DATA.categories), SEC = byId(DATA.sections), ART = byId(DATA.articles);
const secByCat = (cid) => DATA.sections.filter(s => String(s.category_id) === String(cid));
const artBySec = (sid) => DATA.articles.filter(a => String(a.section_id) === String(sid));
const esc = (s) => (s||"").replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const strip = (h) => (h||"").replace(/<[^>]*>/g," ").replace(/\s+/g," ").trim();
const app = document.getElementById('app');

function home(){
  const cards = DATA.categories.map(c => `
    <div class="card">
      <h3><a href="#/category/${c.id}">${esc(c.name)}</a></h3>
      <p>${esc(c.description||"")}</p>
      <a class="learn" href="#/category/${c.id}">Learn more</a>
    </div>`).join("");
  const promoted = DATA.articles.filter(a => a.promoted);
  const promo = promoted.length ? `
    <div class="popular">
      <h2 class="block">Popular articles</h2>
      <ul>${promoted.map(a => `<li><a href="#/article/${a.id}">${esc(a.title)}</a></li>`).join("")}</ul>
    </div>` : "";
  app.innerHTML = `<h2 class="block">Select a topic to get started</h2>
    <p class="sub">Guides and answers for buyers, estate agents, conveyancers and mortgages.</p>
    <div class="cards">${cards}</div>${promo}`;
}
function category(cid){
  const c = CAT[cid]; if(!c) return home();
  const groups = secByCat(cid).map(s => {
    const items = artBySec(s.id).map(a => `<li><a href="#/article/${a.id}">${esc(a.title)}</a></li>`).join("");
    if(!items) return "";
    return `<div class="sec-group"><h3><a href="#/section/${s.id}">${esc(s.name)}</a></h3><ul class="article-list">${items}</ul></div>`;
  }).join("");
  app.innerHTML = `<div class="view"><div class="breadcrumb"><a href="#">Help</a> › ${esc(c.name)}</div>
    <h1 class="page">${esc(c.name)}</h1><p class="lead">${esc(c.description||"")}</p>
    ${groups || '<p class="empty">No articles in this topic yet.</p>'}</div>`;
}
function section(sid){
  const s = SEC[sid]; if(!s) return home();
  const c = CAT[String(s.category_id)];
  const items = artBySec(sid).map(a => `<li><a href="#/article/${a.id}">${esc(a.title)}</a></li>`).join("");
  app.innerHTML = `<div class="view"><div class="breadcrumb"><a href="#">Help</a> › <a href="#/category/${s.category_id}">${esc(c?c.name:'')}</a> › ${esc(s.name)}</div>
    <h1 class="page">${esc(s.name)}</h1><ul class="article-list">${items || '<li>No articles.</li>'}</ul></div>`;
}
function article(aid){
  const a = ART[aid]; if(!a) return home();
  const s = SEC[String(a.section_id)]; const c = s ? CAT[String(s.category_id)] : null;
  const crumbs = ['<a href="#">Help</a>'];
  if(c) crumbs.push(`<a href="#/category/${c.id}">${esc(c.name)}</a>`);
  if(s) crumbs.push(`<a href="#/section/${s.id}">${esc(s.name)}</a>`);
  app.innerHTML = `<div class="view article"><div class="breadcrumb">${crumbs.join(" › ")}</div>
    <h1 class="page">${esc(a.title)}</h1><div class="body">${a.body||""}</div></div>`;
  window.scrollTo(0,0);
}
function search(term){
  const t = term.toLowerCase();
  const hits = DATA.articles.map(a => {
    const body = strip(a.body).toLowerCase();
    const score = (a.title.toLowerCase().includes(t)?2:0) + (body.includes(t)?1:0);
    return {a, score, body};
  }).filter(x => x.score>0).sort((x,y)=>y.score-x.score).slice(0,40);
  app.innerHTML = `<div class="view"><div class="breadcrumb"><a href="#">Help</a> › Search</div>
    <h1 class="page">${hits.length} result${hits.length===1?'':'s'} for "${esc(term)}"</h1>
    <ul class="search-results">${hits.map(h=>{
      const i=h.body.indexOf(t); const snip=i>=0?h.body.slice(Math.max(0,i-40),i+80):"";
      return `<li><a href="#/article/${h.a.id}">${esc(h.a.title)}</a><div class="snip">…${esc(snip)}…</div></li>`;
    }).join("") || '<li class="empty">No matching articles.</li>'}</ul></div>`;
}
function route(){
  const h = location.hash.replace(/^#\/?/,""); const [kind,id] = h.split("/");
  if(kind==="category") category(id); else if(kind==="section") section(id);
  else if(kind==="article") article(id); else home();
}
window.addEventListener("hashchange", route);
let timer;
document.getElementById("q").addEventListener("input",(e)=>{clearTimeout(timer);const v=e.target.value.trim();timer=setTimeout(()=>{v.length>=2?search(v):route();},160);});
route();
</script>
</body>
</html>
"""

if __name__ == "__main__":
    build()
