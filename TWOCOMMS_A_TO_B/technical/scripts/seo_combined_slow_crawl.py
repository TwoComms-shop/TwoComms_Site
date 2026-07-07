#!/usr/bin/env python3
"""SEO-006 + SEO-007 combined SLOW crawler (single-threaded, rate-limited).

Lessons from previous session: parallel crawls (ThreadPoolExecutor(8)) got the
sandbox IP banned. This crawler:
  - 1 request at a time, DELAY seconds between requests
  - retries 503/000 once after RETRY_WAIT seconds (intermittent 503 = Passenger overload)
  - one GET per sitemap URL collects BOTH status/redirects (SEO-006) AND
    title/description/canonical/robots/h1/og (SEO-007) AND internal <a href> links
  - results streamed as JSONL to OUT_FILE for offline analysis

Usage: python3 seo_combined_slow_crawl.py [--limit N]
"""
import json
import re
import sys
import time
import urllib.request
import urllib.error
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

BASE = "https://twocomms.shop"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36 TwoCommsAudit/2.0"
DELAY = 2.5
RETRY_WAIT = 12
OUT_FILE = "/tmp/seo_crawl_results.jsonl"


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def raw_fetch(url, timeout=30):
    """Single fetch without following redirects. Returns (status, headers, body)."""
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "uk"})
    opener = urllib.request.build_opener(NoRedirect())
    try:
        with opener.open(req, timeout=timeout) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        try:
            body = e.read()
        except Exception:
            body = b""
        return e.code, dict(e.headers), body
    except Exception as e:
        return 0, {"error": str(e)}, b""


def fetch(url, max_redirects=6):
    """Follow redirects manually, rate-limited. Returns (status, final_url, headers, body, chain)."""
    chain = []
    cur = url
    for _ in range(max_redirects):
        status, headers, body = raw_fetch(cur)
        if status in (301, 302, 303, 307, 308):
            loc = headers.get("Location", "")
            nxt = urljoin(cur, loc)
            chain.append((status, cur, nxt))
            cur = nxt
            time.sleep(DELAY)
            continue
        return status, cur, headers, body, chain
    return -2, cur, {}, b"", chain


class PageParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.title = ""
        self._in_title = False
        self.metas = []
        self.link_tags = []
        self.h1 = []
        self._in_h1 = False
        self.hrefs = set()

    def handle_starttag(self, tag, attrs):
        d = dict(attrs)
        if tag == "title":
            self._in_title = True
        elif tag == "meta":
            self.metas.append(d)
        elif tag == "link":
            self.link_tags.append(d)
        elif tag == "h1":
            self._in_h1 = True
            self.h1.append("")
        elif tag == "a":
            href = d.get("href")
            if href:
                self.hrefs.add(href)

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


def parse_page(page_url, body):
    p = PageParser()
    try:
        p.feed(body.decode("utf-8", "replace"))
    except Exception:
        pass
    meta = {}
    for m in p.metas:
        name = (m.get("name") or m.get("property") or "").lower()
        if name in ("description", "robots", "og:title", "og:description", "og:image"):
            meta.setdefault(name, m.get("content", ""))
    canonical = ""
    for l in p.link_tags:
        if (l.get("rel") or "").lower() == "canonical":
            canonical = l.get("href", "")
    links = set()
    for href in p.hrefs:
        href = href.strip()
        if href.startswith(("mailto:", "tel:", "javascript:", "#", "viber:", "data:")):
            continue
        absu = urljoin(page_url, href)
        pr = urlparse(absu)
        if pr.netloc in ("twocomms.shop", "www.twocomms.shop"):
            links.add(absu.split("#")[0])
    title = re.sub(r"\s+", " ", p.title).strip()
    desc = re.sub(r"\s+", " ", meta.get("description", "")).strip()
    return {
        "title": title,
        "title_len": len(title),
        "description": desc,
        "desc_len": len(desc),
        "canonical": canonical,
        "robots": meta.get("robots", ""),
        "og_title": bool(meta.get("og:title")),
        "og_image": bool(meta.get("og:image")),
        "h1_count": len([h for h in p.h1 if h.strip()]),
        "h1": [re.sub(r"\s+", " ", h).strip()[:80] for h in p.h1 if h.strip()][:2],
    }, links


def get_sitemap_urls():
    urls = []
    status, _, _, body, _ = fetch(BASE + "/sitemap.xml")
    if status != 200:
        print(f"FATAL sitemap index -> {status}", flush=True)
        return urls
    subs = re.findall(r"<loc>(.*?)</loc>", body.decode())
    for sm in subs:
        time.sleep(DELAY)
        s, _, _, b, _ = fetch(sm)
        if s != 200:
            print(f"SITEMAP_FETCH_FAIL {sm} -> {s}", flush=True)
            continue
        for u in re.findall(r"<loc>(.*?)</loc>", b.decode()):
            if urlparse(u).netloc.endswith("twocomms.shop") and u not in urls:
                urls.append(u)
    return urls


def check_with_retry(url):
    """Fetch with one retry on 503/0 (intermittent overload)."""
    status, final, headers, body, chain = fetch(url)
    retried = False
    if status in (0, 503, -2):
        time.sleep(RETRY_WAIT)
        status, final, headers, body, chain = fetch(url)
        retried = True
    return status, final, headers, body, chain, retried


def main():
    limit = None
    if "--limit" in sys.argv:
        limit = int(sys.argv[sys.argv.index("--limit") + 1])

    out = open(OUT_FILE, "w", encoding="utf-8")

    def emit(rec):
        out.write(json.dumps(rec, ensure_ascii=False) + "\n")
        out.flush()

    print("== phase 1: sitemap collection ==", flush=True)
    sm_urls = get_sitemap_urls()
    print(f"sitemap urls: {len(sm_urls)}", flush=True)
    emit({"type": "sitemap_count", "count": len(sm_urls)})
    if limit:
        sm_urls = sm_urls[:limit]

    print("== phase 2: crawl sitemap urls (status + meta + links) ==", flush=True)
    all_links = {}
    seen = set()
    for i, u in enumerate(sm_urls):
        time.sleep(DELAY)
        status, final, headers, body, chain, retried = check_with_retry(u)
        rec = {"type": "page", "url": u, "status": status, "final": final,
               "chain": [c[0] for c in chain], "retried": retried}
        if status == 200 and body:
            meta, links = parse_page(final, body)
            rec.update(meta)
            for l in links:
                all_links.setdefault(l, set()).add(u)
        emit(rec)
        seen.add(u.rstrip("/") + "/")
        seen.add(u)
        if (i + 1) % 25 == 0:
            print(f"  progress {i+1}/{len(sm_urls)}", flush=True)

    print("== phase 3: check internal links not in sitemap ==", flush=True)
    unseen = sorted(l for l in all_links if l not in seen and (l.rstrip("/") + "/") not in seen)
    # skip obvious non-content links to save requests
    skip_pat = re.compile(r"/(login|logout|register|account|cart|checkout|admin|api|set-language|search\?|favorites|profile|order)")
    to_check = [l for l in unseen if not skip_pat.search(l)]
    skipped = [l for l in unseen if skip_pat.search(l)]
    emit({"type": "links_summary", "unique_links": len(all_links), "unseen": len(unseen),
          "to_check": len(to_check), "skipped_auth_cart": len(skipped)})
    print(f"unique links {len(all_links)}, not-in-sitemap to check: {len(to_check)} (skipped {len(skipped)} auth/cart)", flush=True)
    for i, l in enumerate(to_check):
        time.sleep(DELAY)
        status, final, headers, body, chain, retried = check_with_retry(l)
        emit({"type": "link", "url": l, "status": status, "final": final,
              "chain": [f"{c[0]}->{c[2]}" for c in chain], "retried": retried,
              "sources": sorted(all_links[l])[:3]})
        if (i + 1) % 25 == 0:
            print(f"  link progress {i+1}/{len(to_check)}", flush=True)

    print("== phase 4: 404/410 behavior for deleted/nonexistent urls ==", flush=True)
    tests = [
        BASE + "/product/nonexistent-slug-xyz-123/",
        BASE + "/product/99999/",
        BASE + "/p/nonexistent-slug/",
        BASE + "/catalog/nonexistent-category/",
        BASE + "/some-random-page-404/",
        BASE + "/blog/nonexistent-post-xyz/",
    ]
    for t in tests:
        time.sleep(DELAY)
        status, final, headers, body, chain, retried = check_with_retry(t)
        emit({"type": "notfound_test", "url": t, "status": status, "final": final,
              "chain": [f"{c[0]}->{c[2]}" for c in chain], "body_len": len(body)})
        print(f"  TEST {t} -> {status}", flush=True)

    out.close()
    print("DONE. Results:", OUT_FILE, flush=True)


if __name__ == "__main__":
    main()
