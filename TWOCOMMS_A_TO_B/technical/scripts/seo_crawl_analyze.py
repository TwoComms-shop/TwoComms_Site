#!/usr/bin/env python3
"""Analyze /tmp/seo_crawl_results.jsonl -> SEO-006 + SEO-007 findings (markdown to stdout)."""
import json
import sys
from collections import Counter, defaultdict

IN_FILE = sys.argv[1] if len(sys.argv) > 1 else "/tmp/seo_crawl_results.jsonl"

pages, links, nf_tests = [], [], []
sitemap_count = 0
links_summary = {}
done = False
for line in open(IN_FILE, encoding="utf-8"):
    r = json.loads(line)
    t = r.get("type")
    if t == "page":
        pages.append(r)
    elif t == "link":
        links.append(r)
    elif t == "notfound_test":
        nf_tests.append(r)
    elif t == "sitemap_count":
        sitemap_count = r["count"]
    elif t == "links_summary":
        links_summary = r
    elif t == "done":
        done = True

print(f"# Crawl analysis ({IN_FILE})")
print(f"- sitemap URLs: {sitemap_count}; pages crawled: {len(pages)}; extra links checked: {len(links)}; complete: {done}")

# ---------- SEO-006: statuses / redirects / broken links ----------
print("\n## SEO-006: statuses & redirects")
st = Counter(p["status"] for p in pages)
print(f"- sitemap page statuses: {dict(st)}")
for p in pages:
    if p["status"] != 200:
        print(f"  - [{p['status']}] {p['url']} (retried={p.get('retried')})")
redirected = [p for p in pages if p.get("chain")]
print(f"- sitemap URLs that redirect before 200: {len(redirected)}")
for p in redirected[:30]:
    print(f"  - {p['url']} chain={p['chain']} -> {p['final']}")

lst = Counter(l["status"] for l in links)
print(f"- internal (non-sitemap) link statuses: {dict(lst)}")
for l in links:
    if l["status"] in (404, 410, 500, 0, -2):
        print(f"  - [{l['status']}] {l['url']}  sources={l.get('sources')}")
multi_hop = [l for l in links if len(l.get("chain", [])) > 1]
print(f"- redirect chains >1 hop among links: {len(multi_hop)}")
for l in multi_hop[:20]:
    print(f"  - {l['url']} chain={l['chain']}")
single_redir = [l for l in links if len(l.get("chain", [])) == 1]
print(f"- internal links causing 1 redirect (should be direct): {len(single_redir)}")
for l in single_redir[:30]:
    print(f"  - {l['url']} -> {l['final']}  sources={l.get('sources')}")

print("\n### 404/410 behavior tests")
for t in nf_tests:
    print(f"- {t['url']} -> {t['status']} (final={t['final']}, chain={t.get('chain')})")

# ---------- SEO-007: meta audit ----------
ok = [p for p in pages if p["status"] == 200 and "title" in p]
print(f"\n## SEO-007: meta audit ({len(ok)} pages with parsed meta)")

no_title = [p for p in ok if not p["title"]]
no_desc = [p for p in ok if not p["description"]]
short_t = [p for p in ok if 0 < p["title_len"] < 30]
long_t = [p for p in ok if p["title_len"] > 65]
short_d = [p for p in ok if 0 < p["desc_len"] < 70]
long_d = [p for p in ok if p["desc_len"] > 165]
no_canon = [p for p in ok if not p.get("canonical")]
noindex = [p for p in ok if "noindex" in (p.get("robots") or "").lower()]
no_og = [p for p in ok if not p.get("og_title") or not p.get("og_image")]
h1_zero = [p for p in ok if p.get("h1_count", 0) == 0]
h1_multi = [p for p in ok if p.get("h1_count", 0) > 1]

def section(name, items, fmt):
    print(f"\n### {name}: {len(items)}")
    for p in items[:25]:
        print(f"  - {fmt(p)}")
    if len(items) > 25:
        print(f"  ... and {len(items)-25} more")

section("Missing <title>", no_title, lambda p: p["url"])
section("Missing meta description", no_desc, lambda p: p["url"])
section("Title too short (<30)", short_t, lambda p: f"{p['url']} ({p['title_len']}) \"{p['title']}\"")
section("Title too long (>65)", long_t, lambda p: f"{p['url']} ({p['title_len']}) \"{p['title'][:70]}...\"")
section("Description too short (<70)", short_d, lambda p: f"{p['url']} ({p['desc_len']})")
section("Description too long (>165)", long_d, lambda p: f"{p['url']} ({p['desc_len']})")
section("Missing canonical", no_canon, lambda p: p["url"])
section("noindex in sitemap (!)", noindex, lambda p: f"{p['url']} robots=\"{p['robots']}\"")
section("Missing og:title/og:image", no_og, lambda p: p["url"])
section("No H1", h1_zero, lambda p: p["url"])
section("Multiple H1", h1_multi, lambda p: f"{p['url']} h1_count={p['h1_count']} {p.get('h1')}")

# duplicates
by_title = defaultdict(list)
by_desc = defaultdict(list)
for p in ok:
    if p["title"]:
        by_title[p["title"]].append(p["url"])
    if p["description"]:
        by_desc[p["description"]].append(p["url"])
dup_t = {k: v for k, v in by_title.items() if len(v) > 1}
dup_d = {k: v for k, v in by_desc.items() if len(v) > 1}
print(f"\n### Duplicate titles: {len(dup_t)} groups")
for k, v in list(dup_t.items())[:15]:
    print(f"  - \"{k[:70]}\" x{len(v)}: {v[:4]}")
print(f"\n### Duplicate descriptions: {len(dup_d)} groups")
for k, v in list(dup_d.items())[:15]:
    print(f"  - x{len(v)}: {v[:4]}")

# canonical mismatches
mism = [p for p in ok if p.get("canonical") and p["canonical"].rstrip("/") != p["final"].rstrip("/")]
print(f"\n### Canonical != final URL: {len(mism)}")
for p in mism[:25]:
    print(f"  - {p['final']} canonical={p['canonical']}")
