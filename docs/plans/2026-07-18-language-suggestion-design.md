# Language Suggestion Prompt Design

## Goal

Offer visitors who arrive on a Russian or English locale a delayed, non-blocking suggestion to switch to Ukrainian, while preserving the server-rendered locale, SEO metadata, URL structure, and crawler output.

## Architecture

The prompt is a progressive-enhancement layer mounted from `base.html`. Django remains the sole authority for locale selection and `set_language` redirects. A deferred, dependency-free module reads the active `<html lang>` value, waits until the page is idle and visible, and opens a dialog only for human browsers that have not already made a decision. The choice is stored in `localStorage` with a 180-day cooldown; no database or anonymous request is required.

The dialog is accessible (`role=dialog`, labelled title, focus management, Escape/backdrop dismissal, reduced-motion support), responsive from 320px upward, and rendered in the active locale. The Ukrainian action posts to Django's existing language endpoint using the current URL as `next`, so path, query string, and attribution parameters survive the switch.

## SEO and caching invariants

- No language-dependent text is injected into the initial HTML.
- No redirects, cookies, or network calls happen before the delayed user interaction.
- Existing canonical, hreflang, JSON-LD, sitemap, and `robots` output is untouched.
- Crawlers that do not execute JavaScript receive identical HTML and metadata.
- The prompt is suppressed for `navigator.webdriver` and when `prefers-reduced-motion` only changes animation, not functionality.

## Failure handling

If `localStorage` is unavailable, the prompt fails closed. If the language form cannot obtain a CSRF cookie, it falls back to the existing `/api/bootstrap/` flow before submitting. Any DOM or storage error is swallowed so storefront navigation remains unaffected.

## Verification

Add focused JavaScript tests for language labels, timing/storage gates, and navigation target generation. Run Django checks and the existing storefront test subset. Perform a production smoke check after deploy for `/`, `/ru/`, and `/en/`, verifying status, `html[lang]`, and absence of prompt markup in raw server HTML.
