#!/usr/bin/env python3
"""SEO-007: audit meta title/description/canonical/og/robots on live key pages."""
import json
import re
import urllib.request
from html.parser import HTMLParser

BASE = "https://twocomms.shop"
UA = "Mozilla/5.0 (compatible; TwoCommsAudit/1.0; SEO-007 meta audit)"

PAGES = [
    "/",
    "/catalog/",
    "/catalog/hoodie/",
    "/catalog/tshirts/",
    "/catalog/long-sleeve/",
    "/about/",
    "/contacts/",
    "/delivery/",
    "/blog/",
    "/en/",
    "/ru/",
    "/en/catalog/",
    "/ru/catalog/",
]


class MetaParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.title = ""
        self._in_title = False
        self.metas = []
        self.links = []
        self.h1 = []
        self._in_h1 = False

    def handle_starttag(self, tag, attrs):
        d = dict(attrs)
        if tag == "title":
            self._in_title = True
        elif tag == "meta":
            self.metas.append(d)
        elif tag == "link":
            self.links.append(d)
        elif tag == "h1":
            self._in_h1 = True
            self.h1.append("")

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        elif tag == "h1":
            self._in_h1 = False

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        if self._in_h1 and self.h1:
            self.h1[-1] += data


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "uk"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.status, r.read().decode("utf-8", "replace")


def audit(path):
    url = BASE + path
    try:
        status, html = fetch(url)
    except Exception as e:
        return {"url": path, "error": str(e)}
    p = MetaParser()
    try:
        p.feed(html)
    except Exception:
        pass
    meta = {}
    for m in p.metas:
        name = (m.get("name") or m.get("property") or "").lower()
        if name in ("description", "robots", "og:title", "og:description", "og:image", "twitter:title", "twitter:description"):
            meta.setdefault(name, m.get("content", ""))
    canonical = ""
    hreflangs = []
    for l in p.links:
        rel = (l.get("rel") or "").lower()
        if rel == "canonical":
            canonical = l.get("href", "")
        elif rel == "alternate" and l.get("hreflang"):
            hreflangs.append(l.get("hreflang"))
    title = re.sub(r"\s+", " ", p.title).strip()
    desc = re.sub(r"\s+", " ", meta.get("description", "")).strip()
    return {
        "url": path,
        "status": status,
        "title": title,
        "title_len": len(title),
        "description": desc,
        "desc_len": len(desc),
        "canonical": canonical,
        "robots": meta.get("robots", ""),
        "og:title": bool(meta.get("og:title")),
        "og:description": bool(meta.get("og:description")),
        "og:image": bool(meta.get("og:image")),
        "hreflang": sorted(set(hreflangs)),
        "h1_count": len([h for h in p.h1 if h.strip()]),
        "h1": [re.sub(r"\s+", " ", h).strip()[:80] for h in p.h1 if h.strip()][:3],
    }


def get_product_and_blog_urls():
    """Grab a handful of product/blog URLs from the sitemap for spot checks."""
    _, body = fetch(BASE + "/sitemap.xml")
    subs = re.findall(r"<loc>(.*?)</loc>", body)
    urls = []
    for sm in subs:
        if "product" in sm or "blog" in sm or "static" in sm or "page" in sm:
            try:
                _, b = fetch(sm)
            except Exception:
                continue
            urls += re.findall(r"<loc>(.*?)</loc>", b)
    prods = [u for u in urls if "/product/" in u and "/en/" not in u and "/ru/" not in u][:6]
    blogs = [u for u in urls if "/blog/" in u and u.rstrip("/") != BASE + "/blog"][:4]
    return [u.replace(BASE, "") for u in prods + blogs]


def main():
    pages = list(PAGES)
    try:
        pages += get_product_and_blog_urls()
    except Exception as e:
        print("sitemap sample failed:", e)

    results = []
    for path in pages:
        r = audit(path)
        results.append(r)
        print(json.dumps(r, ensure_ascii=False), flush=True)

    # Duplicate detection
    print("\n== duplicates ==")
    by_title = {}
    by_desc = {}
    for r in results:
        if "error" in r:
            continue
        by_title.setdefault(r["title"], []).append(r["url"])
        if r["description"]:
            by_desc.setdefault(r["description"], []).append(r["url"])
    for t, us in by_title.items():
        if len(us) > 1:
            print("DUP_TITLE", json.dumps({"title": t[:80], "urls": us}, ensure_ascii=False))
    for d, us in by_desc.items():
        if len(us) > 1:
            print("DUP_DESC", json.dumps({"desc": d[:80], "urls": us}, ensure_ascii=False))

    print("\n== issues ==")
    for r in results:
        if "error" in r:
            print("ERROR", r["url"], r["error"])
            continue
        issues = []
        if not r["title"]:
            issues.append("no-title")
        elif r["title_len"] > 70:
            issues.append(f"title-long({r['title_len']})")
        elif r["title_len"] < 25:
            issues.append(f"title-short({r['title_len']})")
        if not r["description"]:
            issues.append("no-description")
        elif r["desc_len"] > 170:
            issues.append(f"desc-long({r['desc_len']})")
        elif r["desc_len"] < 60:
            issues.append(f"desc-short({r['desc_len']})")
        if not r["canonical"]:
            issues.append("no-canonical")
        if not r["og:title"]:
            issues.append("no-og-title")
        if not r["og:image"]:
            issues.append("no-og-image")
        if r["h1_count"] == 0:
            issues.append("no-h1")
        elif r["h1_count"] > 1:
            issues.append(f"multiple-h1({r['h1_count']})")
        if "noindex" in r["robots"]:
            issues.append("NOINDEX")
        if issues:
            print("ISSUES", r["url"], ",".join(issues))
    print("DONE")


if __name__ == "__main__":
    main()
