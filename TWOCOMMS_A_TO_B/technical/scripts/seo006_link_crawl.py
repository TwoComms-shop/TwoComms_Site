#!/usr/bin/env python3
"""SEO-006: crawl twocomms.shop — sitemap URL statuses + internal link check + 410 behavior.

Read-only HTTP audit. Run from any machine with outbound HTTPS:
    python3 seo006_link_crawl.py
"""
import concurrent.futures as cf
import json
import re
import ssl
import urllib.request
import urllib.error
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

BASE = "https://twocomms.shop"
UA = "Mozilla/5.0 (compatible; TwoCommsAudit/1.0; SEO-006 internal link check)"


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def fetch(url, method="GET", timeout=25, redirects=True):
    """Return (status, final_url, body_or_empty, redirect_chain[(code, from, to)])."""
    chain = []
    cur = url
    for _ in range(6):
        req = urllib.request.Request(cur, method=method, headers={"User-Agent": UA, "Accept-Language": "uk"})
        try:
            opener = urllib.request.build_opener(NoRedirect())
            with opener.open(req, timeout=timeout) as r:
                status = r.status
                if status in (301, 302, 303, 307, 308) and redirects:
                    loc = r.headers.get("Location", "")
                    nxt = urljoin(cur, loc)
                    chain.append((status, cur, nxt))
                    cur = nxt
                    continue
                body = r.read() if method == "GET" else b""
                return status, cur, body, chain
        except urllib.error.HTTPError as e:
            if e.code in (301, 302, 303, 307, 308) and redirects:
                loc = e.headers.get("Location", "")
                nxt = urljoin(cur, loc)
                chain.append((e.code, cur, nxt))
                cur = nxt
                continue
            try:
                body = e.read() if method == "GET" else b""
            except Exception:
                body = b""
            return e.code, cur, body, chain
        except Exception as e:
            return -1, cur, str(e).encode(), chain
    return -2, cur, b"too many redirects", chain


class LinkParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = set()

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            for k, v in attrs:
                if k == "href" and v:
                    self.links.add(v)


def extract_links(page_url, body):
    p = LinkParser()
    try:
        p.feed(body.decode("utf-8", "replace"))
    except Exception:
        pass
    out = set()
    for href in p.links:
        href = href.strip()
        if href.startswith(("mailto:", "tel:", "javascript:", "#", "viber:", "data:")):
            continue
        absu = urljoin(page_url, href)
        pr = urlparse(absu)
        if pr.netloc in ("twocomms.shop", "www.twocomms.shop"):
            out.add(absu.split("#")[0])
    return out


def get_sitemap_urls():
    urls = set()
    _, _, body, _ = fetch(BASE + "/sitemap.xml")
    subs = re.findall(r"<loc>(.*?)</loc>", body.decode())
    for sm in subs:
        s, _, b, _ = fetch(sm)
        if s != 200:
            print(f"SITEMAP_FETCH_FAIL {sm} -> {s}")
            continue
        locs = re.findall(r"<loc>(.*?)</loc>", b.decode())
        for u in locs:
            if urlparse(u).netloc.endswith("twocomms.shop"):
                urls.add(u)
    return urls


def main():
    import sys
    skip_sitemap_check = "--skip-sitemap-check" in sys.argv

    print("== collecting sitemap urls ==", flush=True)
    sm_urls = get_sitemap_urls()
    print(f"sitemap urls total: {len(sm_urls)}", flush=True)

    def check_url(u):
        status, final, _, chain = fetch(u, method="GET")
        return u, status, final, chain

    if not skip_sitemap_check:
        bad = []
        redirected = []
        with cf.ThreadPoolExecutor(8) as ex:
            for u, status, final, chain in ex.map(check_url, sorted(sm_urls)):
                if status != 200:
                    bad.append((u, status, final))
                elif chain:
                    redirected.append((u, [c[0] for c in chain], final))
        print(f"sitemap non-200: {len(bad)}; redirected-but-200: {len(redirected)}", flush=True)
        for b in bad[:80]:
            print("  SM_BAD", json.dumps(b, ensure_ascii=False))
        for r in redirected[:80]:
            print("  SM_RED", json.dumps(r, ensure_ascii=False))

    print("== crawling internal links from key pages ==", flush=True)
    seeds = [
        BASE + "/",
        BASE + "/catalog/",
        BASE + "/catalog/long-sleeve/",
        BASE + "/catalog/hoodie/",
        BASE + "/catalog/tshirts/",
    ]
    prods = [u for u in sorted(sm_urls) if "/product/" in u][:12]
    blog = [u for u in sorted(sm_urls) if "/blog" in u][:5]
    other = [u for u in sorted(sm_urls) if u not in prods and u not in blog and u not in seeds][:15]
    seeds += prods + blog + other

    all_links = {}
    for s in seeds:
        st, fin, body, _ = fetch(s)
        if st != 200:
            print(f"  SEED_FAIL {s} -> {st}")
            continue
        for l in extract_links(fin, body):
            all_links.setdefault(l, set()).add(s)
    print(f"unique internal links found: {len(all_links)}", flush=True)

    link_bad = []
    link_redirect = []
    with cf.ThreadPoolExecutor(8) as ex:
        for u, status, final, chain in ex.map(check_url, sorted(all_links)):
            if status != 200:
                link_bad.append((u, status, sorted(all_links[u])[:3]))
            elif chain:
                link_redirect.append((u, [f"{c[0]}->{c[2]}" for c in chain], sorted(all_links[u])[:2]))
    print(f"internal link non-200: {len(link_bad)}; redirect chains: {len(link_redirect)}", flush=True)
    for b in link_bad:
        print("  LINK_BAD", json.dumps(b, ensure_ascii=False))
    for r in link_redirect[:80]:
        print("  LINK_RED", json.dumps(r, ensure_ascii=False))

    print("== deleted/nonexistent url behavior (404 vs 410) ==", flush=True)
    tests = [
        BASE + "/product/nonexistent-slug-xyz-123/",
        BASE + "/product/99999/",
        BASE + "/product/1/",
        BASE + "/p/nonexistent-slug/",
        BASE + "/catalog/nonexistent-category/",
        BASE + "/some-random-page-404/",
    ]
    for t in tests:
        status, final, body, chain = fetch(t)
        print(f"  TEST {t} -> {status} final={final} chain={[c[0] for c in chain]} len={len(body)}")

    print("DONE")


if __name__ == "__main__":
    main()
