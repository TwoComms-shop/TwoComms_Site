# Product-card community redesign — 2026-09-30

Implemented in the existing product detail page. The cart, configurator,
gallery and overall section order remain outside this change. Screenshots use
isolated local demonstration products and reviews; none are production reviews.

## Customer experience

- Existing product-page section, with a quiet anchor near the product heading
  even when there are no ratings. No additional floating overlay.
- A compact single-column list and one write button on desktop and mobile.
  No sidebar, histogram, type selector or competing promotional panel.
- One form with optional 1–5 stars for first-hand experience. Without stars,
  the text is stored as an unrated comment and excluded from averages.
- Guest: display name, text and private email required. No account required.
- Member: name prefilled; purchase badge requires their paid, non-cancelled order
  containing this product. Guest email is never used to infer order ownership.
- Only text, name and email are visible; photos are under a small disclosure.
  Legacy city, headline, pros and cons still display if present.
- Unpublished content is visible to its browser/account owner and moderators.
  Email and internal moderation notes are never in public HTML or structured data.
- One submission per product, identity and type. An initial comment does not block
  a later rated review. A database unique constraint protects concurrent retries.
- A text draft is retained in sessionStorage for 24 hours, scoped to product and
  account. Email and photos are excluded from this client-side draft. Failed
  non-AJAX submissions retain bounded text in the server session for redisplay.
- Session ownership uses a random token and a keyed hash, independent of changing
  IPs and session-key rotation. It survives signing in within the same session.
  Clearing/expiring cookies loses guest ownership; shared-device visibility is
  disclosed. Existing legacy guest records cannot be retroactively reassigned.

## Moderation and abuse protection

Custom admin section: `/admin-panel/?section=reviews` (label: `Коментарі`).
Pending, published and rejected lists are searchable and paginated. Moderators
see the full text, photos and private email; can publish, reject, remove from
publication or return to review. Notes remain internal, including in the account
review list. Critical and positive ratings follow the same publication rules.

CSRF; honeypot; server-side text/contact validation; fixed-hour database counters
(20 attempts/IP, 5/guest identity, 8/account); per-type database deduplication.
Counters expire after two days. REMOTE_ADDR is used, not an untrusted XFF header;
confirm proxy handling as part of deployment QA.

Uploaded photos are decoded with Pillow, capped at 20 megapixels and 5 MB each,
limited to five, resized and re-encoded to JPEG under random names. Original file
metadata and embedded content are not retained. Moderation is still necessary
for advertising, abuse and obfuscated links that text validation cannot identify.

Private state/CSRF endpoint is never cached. Personalized PDP responses bypass
shared application caching and have `private, no-store`. Publication changes
invalidate the public product cache. SEO reviews follow the visible page slice.

## Google and future gifts

Public averages contain only approved rated reviews. The XML endpoint
`/reviews/merchant.xml` uses Google Product Review Feed 2.4 and the existing product
feed's brand/MPN identifiers, with verified-purchase and incentive flags. It
exports approved, verified-purchase product reviews at all star levels; guest
reviews without purchase verification and unrated comments are not exported.
Links resolve the individual review inside the product card, including old
paginated reviews. No separate public review page was added.

Merchant enrollment/feed registration and actual Google ingestion were not
performed. Google determines eligibility/display; a valid feed alone does not
promise stars. Guest email is syntax-validated and private, **not email-verified**;
there is no verification email or claim that an address proves a real person.

The admin displays progress towards 100 approved rated reviews across products.
The future gift program is OFF by default and has no public promise before
published HTTPS rules are supplied and the owner enables it. The milestone does
not auto-enable the program. If enabled later, the pitch is a secondary compact
item, participation is opt-in for verified purchasers, any rating qualifies, and
staff can confirm/revoke a story for one additional entry. There is no automated
winner selection, retroactive enrollment, 10% coupon issuance, or guaranteed gift.

Primary references:
- https://support.google.com/merchants/answer/6098512?hl=en
- https://developers.google.com/product-review-feeds/schema
- https://www.google.com/shopping/reviews/schema/product/2.4/product_reviews.xsd

## Validation and release boundary

- CPython 3.14.6 / Django 6.1 from the shared project virtualenv.
- 94 tests in `reviews.tests`, `storefront.tests.test_admin_reviews_seo`, and
  `storefront.tests.test_pdp_content_order`: 93 passed, one MariaDB-only skip.
- XML validated against the official 2.4 XSD, including empty and incentivized /
  low-rating feeds; identifier consistency tested.
- Real migration `reviews.0004` applied to isolated SQLite with an existing review:
  content/rating preserved and new comment/review coexistence verified.
- Migration drift check, JavaScript syntax check and `git diff --check` pass.
- Browser: guest draft/reload/submission, private reload, second visitor isolation,
  verified buyer, staff moderation, public rating update, English/Russian copy and
  optional stars without a type selector. No page JavaScript errors in the exercised flow.
- Layout: 320, 390, 768, 1440 px, no review-block horizontal overflow.
- Broader configurator suite has two failures also reproduced with the HEAD
  product template: `test_all_sizes_unavailable_renders_one_primary_notify_action`
  and `test_versioned_pdp_assets_use_one_fresh_release_key`. These are outside
  the review change and were not rewritten to hide the failures.

Release requires migration 0004, updated static assets and production verification
through the documented release procedure. Merchant account setup remains separate. Translation catalogs are scoped to `reviews/locale`; the pre-existing
storefront translation changes were preserved.

Screenshots: `desktop.png`, `mobile.png`, `mobile-form.png`.
