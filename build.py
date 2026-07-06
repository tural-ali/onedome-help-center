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
import shutil
from pathlib import Path
from urllib.parse import unquote

import requests

try:  # optional: load a local .env for standalone local builds (CI uses secrets)
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent / ".env")
except ImportError:
    pass

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

# Canonical public host. The built-in *.pages.dev URL 301-redirects here so the
# site is reachable at exactly one domain.
CANONICAL_HOST = os.getenv("CANONICAL_HOST", "help.onedome.com")

# Article bodies carry legacy Zendesk image URLs (help.onedome.com/hc/article_attachments/…),
# which no longer resolve now that help.onedome.com is this site. We download each image once
# into a committed cache and self-host it under /images/, so the site has no runtime dependency
# on Zendesk. Zendesk still serves the originals at onedome.zendesk.com for the initial fetch.
ASSETS_IMG = ROOT / "assets" / "images"          # committed cache (survives Zendesk shutdown)
IMG_FETCH_HOST = "onedome.zendesk.com"
ZENDESK_IMG_RE = re.compile(
    r"https?://(?:help\.onedome\.com|onedome\.zendesk\.com)/hc/"
    r"(?:[^\"'?\s]*?/)?article_attachments/(\d+)/([^\"'?\s>]+)"
)


def _local_img_name(aid, fname):
    fname = unquote(fname)
    fname = re.sub(r"[^A-Za-z0-9._-]", "_", fname)
    return f"{aid}-{fname}"


def localize_images(body, stats):
    """Rewrite legacy Zendesk image URLs to self-hosted /images/… paths, downloading
    any not yet cached. Leaves the original URL in place if a download fails."""
    if not body:
        return body

    def repl(m):
        aid, fname = m.group(1), m.group(2)
        local = _local_img_name(aid, fname)
        dest = ASSETS_IMG / local
        if not dest.exists():
            src_url = re.sub(r"https?://[^/]+", f"https://{IMG_FETCH_HOST}", m.group(0))
            try:
                r = requests.get(src_url, timeout=60)
            except requests.RequestException as e:
                print(f"  WARN image fetch error {src_url}: {e}")
                stats["failed"] += 1
                return m.group(0)
            if r.status_code != 200 or not r.content:
                print(f"  WARN image {r.status_code}: {src_url}")
                stats["failed"] += 1
                return m.group(0)
            ASSETS_IMG.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(r.content)
            stats["downloaded"] += 1
        stats["localized"] += 1
        return f"/images/{local}"

    return ZENDESK_IMG_RE.sub(repl, body)


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

    img_stats = {"localized": 0, "downloaded": 0, "failed": 0}
    for a in articles:
        a["body"] = localize_images(a["body"], img_stats)

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

    # Advanced-mode Worker: redirect the built-in *.pages.dev host to the
    # canonical domain; serve static assets unchanged everywhere else.
    worker_js = (
        "export default {\n"
        "  async fetch(request, env) {\n"
        "    const url = new URL(request.url);\n"
        "    if (url.hostname.endsWith('.pages.dev')) {\n"
        f"      return Response.redirect('https://{CANONICAL_HOST}' + url.pathname + url.search, 301);\n"
        "    }\n"
        "    return env.ASSETS.fetch(request);\n"
        "  }\n"
        "};\n"
    )
    (OUT_DIR / "_worker.js").write_text(worker_js, encoding="utf-8")

    # Self-host the cached images alongside the site.
    if ASSETS_IMG.exists():
        shutil.copytree(ASSETS_IMG, OUT_DIR / "images", dirs_exist_ok=True)

    print(f"Wrote {OUT}")
    print(f"  _worker.js redirects *.pages.dev -> {CANONICAL_HOST}")
    print(f"  images: {img_stats['localized']} localized, "
          f"{img_stats['downloaded']} downloaded, {img_stats['failed']} failed")
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
<link rel="canonical" href="https://help.onedome.com/">
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

  /* ---- navbar (full-width, onedome.com style with dropdowns) ---- */
  header.nav{background:var(--navy); position:sticky; top:0; z-index:50}
  header.nav .bar{display:flex; align-items:center; height:72px; padding:0 40px}
  .brand{display:flex; align-items:center; cursor:pointer; text-decoration:none; margin-right:auto}
  .brand .logo{height:30px; width:auto; display:block}
  nav.menu{display:flex; align-items:stretch; gap:30px; height:72px}
  .menu-item{position:relative; display:flex; align-items:center}
  .menu .top{display:inline-flex; align-items:center; gap:7px; height:72px; color:#EDEDF2; font-size:16px; cursor:pointer; text-decoration:none; white-space:nowrap}
  .menu .top:hover, .menu-item:hover .top, .menu-item:focus-within .top{color:var(--yellow); text-decoration:none}
  .menu .caret{width:10px; height:6px; transition:transform .15s}
  .menu-item:hover .caret, .menu-item:focus-within .caret{transform:rotate(180deg)}
  .dropdown{position:absolute; top:100%; left:0; min-width:232px; background:#fff; border-radius:14px;
            box-shadow:0 16px 40px rgba(1,0,34,.20); padding:10px; z-index:60;
            opacity:0; visibility:hidden; transform:translateY(6px); transition:opacity .15s, transform .15s}
  .menu-item:hover .dropdown, .menu-item:focus-within .dropdown{opacity:1; visibility:visible; transform:translateY(0)}
  .dropdown a{display:block; color:var(--navy); font-size:15px; padding:10px 14px; border-radius:9px; white-space:nowrap}
  .dropdown a:hover{background:#f4f4f7; text-decoration:none}
  .call{background:var(--blue); color:#fff; border-radius:999px; padding:11px 20px; font-weight:600; font-size:15px; white-space:nowrap; margin-left:30px}
  .call:hover{filter:brightness(1.08); text-decoration:none}
  @media(max-width:980px){ nav.menu{display:none} header.nav .bar{padding:0 24px} }

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

  /* ---- article feedback ---- */
  .feedback{max-width:760px; margin:52px auto 0; padding:28px 0 0; border-top:1px solid var(--border); text-align:center}
  .feedback .fb-q{font-family:var(--head); font-weight:700; color:var(--navy); font-size:17px; margin:0 0 16px}
  .fb-btns{display:flex; gap:12px; justify-content:center}
  .fb-btn{background:#fff; border:1px solid var(--border); border-radius:999px; padding:9px 28px; font-family:var(--body); font-size:15px; font-weight:600; color:var(--navy); cursor:pointer; transition:background .15s,border-color .15s}
  .fb-btn:hover{background:var(--yellow); border-color:var(--yellow)}
  .fb-no{display:none; color:var(--muted); font-size:15px; margin:2px 0 0}

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
  <div class="bar">
    <a class="brand" href="https://onedome.com">
      <svg class="logo" role="img" aria-label="OneDome" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 284 57"><path fill="#fff" d="M217.3 19.5c-7.2 0-13 5.8-13 13s5.8 13 13 13 13-5.8 13-13-5.8-13-13-13zm0 4.3c4.8 0 8.7 3.9 8.7 8.7 0 4.8-3.9 8.7-8.7 8.7-4.8 0-8.7-3.9-8.7-8.7 0-4.9 3.9-8.7 8.7-8.7M130.7 37.2 117.4 20h-4.1v24.9h4.3V27.2l13.7 17.7h3.7V20h-4.3v17.2zm15-2.9h12.5v-3.9h-12.5v-6.5h14.1V20h-18.4v24.9H160V41h-14.3v-6.7zm40.2 6.2h-6.7v-21h6.7c6.1 0 9.6 5 9.6 10.7-.1 5.5-3.8 10.3-9.6 10.3zm-.1-25.4h-15.2l1.9 4.4h2.4v25.4h11c4 0 6.8-1 9.5-4 2.8-2.9 4.4-7 4.4-11.1 0-8-5.9-14.7-14-14.7zm62.1 16.6L240.3 20h-4.7v24.9h4.3V27.1l7.8 11.6h.2l7.8-11.7v17.9h4.4V20h-4.7l-7.5 11.7zm21.9 9.3v-6.7h12.4v-3.9h-12.4v-6.5h14V20h-18.4v24.9H284V41h-14.2zM92.9 15.2c-8.4 0-15.2 6.7-15.2 15.1 0 8.3 6.8 15.1 15.2 15.1 8.4 0 15.2-6.7 15.2-15.1 0-8.4-6.8-15.1-15.2-15.1zm0 4.3c6 0 10.8 4.8 10.8 10.7 0 5.9-4.9 10.7-10.8 10.7-6 0-10.8-4.8-10.8-10.7 0-5.9 4.8-10.7 10.8-10.7"></path><path fill="#fbbb30" d="M30.7 0C14 0 .5 13.5.5 30.2v.6C.7 38.9 4 46.2 9.4 51.5l6.1-6.1C12 41.9 9.7 37.2 9.3 32c-.1-.6-.1-1.2-.1-1.9 0-11.9 9.7-21.6 21.6-21.6 11.9 0 21.6 9.6 21.6 21.6 0 .6 0 1.3-.1 1.9-.4 5.2-2.7 9.9-6.2 13.4l6.1 6.1c5.3-5.3 8.7-12.6 8.8-20.7v-.6C60.9 13.5 47.4 0 30.7 0zm0 35.4c-1.4 0-2.7-.6-3.7-1.5-.9-.9-1.5-2.2-1.5-3.7 0-2.9 2.3-5.2 5.2-5.2 2.9 0 5.2 2.3 5.2 5.2 0 1.4-.6 2.7-1.5 3.7-1 .9-2.3 1.5-3.7 1.5zm0-18.2c-7.1 0-12.9 5.8-12.9 12.9 0 3.6 1.4 6.8 3.8 9.1l.2.2c3.4 3.4 6 7.5 7.5 12.1.4 1.4.8 2.7 1 4.2v.1c0 .2.2.3.4.3s.4-.1.4-.3v-.1c.2-1.4.6-2.8 1-4.2 1.5-4.6 4.1-8.8 7.5-12.1l.2-.2c2.3-2.3 3.8-5.6 3.8-9.1.1-7.1-5.7-12.9-12.9-12.9z"></path></svg>
    </a>
    <nav class="menu">
      <div class="menu-item">
        <a class="top" href="https://onedome.com/search?q=actionType=SALE">Buy <svg class="caret" viewBox="0 0 10 6" fill="none" stroke="currentColor" stroke-width="1.6" aria-hidden="true"><path d="M1 1l4 4 4-4"/></svg></a>
        <div class="dropdown">
          <a href="https://onedome.com/search?q=actionType=SALE">Search</a>
          <a href="https://onedome.com/services/get-buyer-passport/">Buyer Passport</a>
          <a href="https://onedome.com/locations/uk/england/">Area guides</a>
          <a href="https://onedome.com/locality-reality/explore/">Locality Reality</a>
          <a href="https://www.onedome.com/new-homes/">New homes</a>
          <a href="https://onedome.com/services/onedome-guarantee/">OneDome Guarantee</a>
        </div>
      </div>
      <div class="menu-item">
        <a class="top" href="https://onedome.com/services/mortgage-passport/">Mortgages <svg class="caret" viewBox="0 0 10 6" fill="none" stroke="currentColor" stroke-width="1.6" aria-hidden="true"><path d="M1 1l4 4 4-4"/></svg></a>
        <div class="dropdown">
          <a href="https://onedome.com/services/mortgage-passport/">Get a Mortgage</a>
          <a href="https://onedome.com/mortgages/mortgages-explained/">Mortgage guides</a>
        </div>
      </div>
      <div class="menu-item">
        <a class="top" href="https://onedome.com/services/conveyancing/">Conveyancing <svg class="caret" viewBox="0 0 10 6" fill="none" stroke="currentColor" stroke-width="1.6" aria-hidden="true"><path d="M1 1l4 4 4-4"/></svg></a>
        <div class="dropdown">
          <a href="https://onedome.com/services/conveyancing/">Find a conveyancer</a>
          <a href="https://onedome.com/conveyance/home-buying-and-selling-guides/">Conveyancing guides</a>
        </div>
      </div>
      <a class="top" href="https://growthpartners.onedome.com/">Growth Partners</a>
      <a class="top" href="https://onedome.com/registration/personal">My OneDome</a>
    </nav>
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
  const mailHref = `mailto:hello@onedome.com?subject=${encodeURIComponent("Help request: "+a.title)}`;
  app.innerHTML = `<div class="view article"><div class="breadcrumb">${crumbs.join(" › ")}</div>
    <h1 class="page">${esc(a.title)}</h1><div class="body">${a.body||""}</div>
    <div class="feedback" id="fb">
      <p class="fb-q">Did it help?</p>
      <div class="fb-btns"><button class="fb-btn" type="button" onclick="fbVote(true)">Yes</button><button class="fb-btn" type="button" onclick="fbVote(false)">No</button></div>
      <p class="fb-no">Sorry this didn't help. <a href="${mailHref}">Email us with your detailed request</a> and we'll get back to you.</p>
    </div></div>`;
  window.scrollTo(0,0);
}
function fbVote(ok){
  const fb=document.getElementById("fb"); if(!fb||fb.dataset.done) return;
  fb.dataset.done="1";
  const btns=fb.querySelector(".fb-btns"); if(btns) btns.remove();
  const q=fb.querySelector(".fb-q");
  if(ok){ q.textContent="Thanks for your feedback."; }
  else { q.style.display="none"; fb.querySelector(".fb-no").style.display="block"; }
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
