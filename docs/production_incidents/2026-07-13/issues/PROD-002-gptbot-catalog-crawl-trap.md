# PROD-002 — GPTBot catalog crawl trap and file-cache churn

**Priority:** P0
**State:** `[o]` application containment deployed in `20079875`; traffic monitoring, bounded cache cleanup and edge rate control remain
**Owner:** storefront/SEO plus web edge
**Fixability:** fully fixable in application and edge configuration

## Symptom and impact

GPTBot follows color-filter chip links in different orders, creating almost a new URL and cache entry for every ordered subset. It dominated production traffic, forced ORM work on mostly unique catalog URLs and was the requester for every catalog 500 observed on 13 July.

## Production evidence

- 330,359 access requests were analysed; GPTBot generated 202,765, or 61.4%.
- 11 July: 44,989 GPTBot catalog requests.
- 12 July: 98,227 catalog requests, 97,871 unique URLs.
- 13 July through about 16:06: 58,745 catalog requests, 58,435 unique URLs.
- GPTBot accounted for roughly 80–90% of daily traffic on 11–13 July, with a median interval of one second.
- All 13 July 500s on `/catalog/` and `/en/catalog/` were GPTBot requests.
- Effective cache is `FileBasedCache`, not Redis. The default cache was about 6,000 files and 200 MB; thousands of files were created/updated per hour during the crawl.

## Code path

- `twocomms/storefront/services/color_filter.py:84-128` preserves selected slug order and emits toggle URLs in that order.
- `twocomms/twocomms_django_theme/templates/partials/color_filter_chips.html:18` renders followable links.
- `twocomms/twocomms_django_theme/templates/pages/catalog.html:47` uses `noindex, follow` for active filters, explicitly allowing traversal of links.
- `twocomms/storefront/views/utils.py:16-42` fingerprints the raw query string, so permutations of the same set miss the anonymous-page cache.
- `twocomms/storefront/views/catalog.py:558` applies that cache to catalog.
- `twocomms/storefront/views/static_pages.py` permits GPTBot, while the AI-specific rules do not repeat query-noise restrictions.
- `twocomms/twocomms/middleware.py` uses a non-atomic approximate per-IP limiter around 100 requests/minute. GPTBot stays near 60/minute and largely passes.

## Why the state space explodes

There are eight published color slugs. Because order is significant to the URL builder, the crawl graph contains:

```text
1 + sum(P(8, k), k=1..8) = 109,601 URLs
```

A canonical set representation has at most `2^8 = 256` sets, about 428 times fewer. If only the base catalog and eight curated single-color landings are crawlable, the intended crawl surface is nine pages.

On 12 July GPTBot traversed about 89% of the full ordered-permutation space.

## Root cause

The application treats selection order as URL identity even though product results depend on the set of colors. Every chip publishes another crawlable permutation, and the cache repeats the same identity mistake. `noindex, follow` prevents indexing but instructs compliant bots to follow the graph.

## Implementation plan

1. [x] Write failing tests for duplicate, reversed and unknown color slugs.
2. [x] Parse color input into allowed published slugs, deduplicate and sort by one stable domain order.
3. [x] Redirect GET requests with non-canonical ordering/duplicates to the canonical URL. Preserve unrelated allowed filters and pagination rules deliberately.
4. [x] Build anonymous cache fingerprints from canonical semantic parameters, never raw query order.
5. [x] Define SEO policy:
   - base catalog and curated single-color pages may be index/follow;
   - multi-select combinations should be `noindex, nofollow` and their chips should not expose an unbounded crawl graph;
   - reject or cap excessive selection count.
6. [x] Add immediate robots protection for catalog query variants from GPTBot and confirm the exact live generated block.
7. [ ] Make rate limiting atomic and route-aware. Distinguish legitimate users from high-cardinality crawler requests.
8. [ ] Remove stale file-cache entries after deployment using a safe, bounded procedure; do not delete unrelated caches blindly.

## Tests

- `color=red,blue` and `color=blue,red` resolve to one canonical URL and one cache key.
- duplicate/unknown slugs cannot create new cache identities.
- chip URLs cannot enumerate ordered permutations.
- bot-oriented HTML has the agreed robots directives and link attributes.
- base/single-color SEO landings remain discoverable.
- a synthetic crawl has a bounded number of unique URLs and DB queries.

## Production verification

- Deployed commit `20079875` on 16 July 2026; server focused suites passed 28/28 and `manage.py check` reported no issues.
- A noisy URL with duplicate, reversed and unknown slugs plus `page=7` returned 301 to `/catalog/?utm_source=instagram&color=black%2Ccoyote`; an unknown-only filter returned 301 to `/catalog/`.
- The canonical multi-select URL returned 200 with `noindex, nofollow`; generated facet links carried `rel="nofollow"`.
- The live `User-agent: GPTBot` block included `Disallow: /*?color=` and `Disallow: /*&color=`.
- Remaining `[o]` evidence: compare unique catalog URLs/hour, GPTBot requests/hour, cache files/hour, catalog DB connects and 5xx after enough post-deploy traffic has accumulated.
- Compare unique catalog URLs/hour, GPTBot requests/hour, cache files/hour, catalog DB connects and 5xx before/after.
- Expect a steep fall in unique URLs and file-cache churn without harming human catalog conversion.
- Confirm no new 1040 occurs under the reduced traffic, while remembering that shared DB saturation still requires PROD-001.

## Risks

Canonical redirects can change analytics attribution and indexed URLs. Preserve unrelated query parameters explicitly and update sitemap/canonical tests before shipping.
