"""Polite public-web scraping for the demo generator (scripts/create_demo.py).

Reusable by design — anything that needs to fetch a public Shopify
storefront's products/policies should use PoliteFetcher rather than calling
requests directly, so User-Agent identification, robots.txt, rate limiting
and timeouts stay consistent in one place.
"""

import re
import time
import urllib.robotparser
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from app.catalog import ProductResult, ProductVariant, format_price_summary, parse_price

USER_AGENT = "TillbirdDemoBot/1.0 (AI customer-support demo generator; contact lev@tillbird.co.uk)"
DEFAULT_TIMEOUT_SECONDS = 10
DEFAULT_MIN_DELAY_SECONDS = 1.0

POLICY_PATHS = {
    "shipping": "/policies/shipping-policy",
    "returns": "/policies/refund-policy",
    "privacy": "/policies/privacy-policy",
    "terms": "/policies/terms-of-service",
    "contact_info": "/policies/contact-information",
}
BONUS_PATHS = {
    "contact_page": "/pages/contact",
}
# FAQ has no single agreed URL across themes — confirmed live,
# ohmymelt.co.uk's is at the plural "/pages/faqs", not the singular
# "/pages/faq" this used to be hardcoded to, so its subscription/vegan/
# paraffin-free answers were never scraped at all even though the page
# existed. Tried in order, same "first found wins" pattern as
# ABOUT_PATH_CANDIDATES — see fetch_faq_text, which also falls back to a
# footer/nav link and the Shopify-app-embedded /a/faq if none of these
# guesses land.
FAQ_PATH_CANDIDATES = [
    "/pages/faq",
    "/pages/faqs",
    "/pages/frequently-asked-questions",
    "/pages/frequently-asked-question",
]
# Matches a nav/footer link whose own text names it as the FAQ, regardless
# of what URL it actually points to — see fetch_faq_text's link-scanning
# fallback. \bfaqs?\b also matches "FAQ" and "FAQs" as whole words without
# matching e.g. "faqtory".
_FAQ_LINK_TEXT_RE = re.compile(r"\bfaqs?\b|frequently asked questions", re.IGNORECASE)
# An About/Our Story page is where brand claims actually live (pet-safe,
# vegan, handmade, made in the UK, sustainability, etc.) — but Shopify
# stores don't agree on a URL for it, so these are tried in order and the
# first one found wins (unlike POLICY_PATHS/BONUS_PATHS, which are each a
# single fixed path).
ABOUT_PATH_CANDIDATES = ["/pages/about", "/pages/about-us", "/pages/our-story", "/pages/story"]
# Garment-care/washing instructions and size guides: same "no agreed URL"
# problem as About, tried the same way (first found wins). Even when one of
# these exists, it won't always contain the actual care text — e.g. a
# "/pages/size-guide" can be a pure fit/sizing quiz with no washing
# instructions at all — so this is a best-effort extra source, not a
# replacement for fetch_product_care_text below.
CARE_PATH_CANDIDATES = [
    "/pages/care",
    "/pages/garment-care",
    "/pages/care-guide",
    "/pages/product-care",
    "/pages/fabric-care",
    "/pages/how-to-care",
    "/pages/washing-instructions",
    "/pages/size-guide",
    "/pages/sizing",
    "/pages/size-chart",
]
# A size-guide page is a distinct source from care — e.g. one clothing store
# Clothing's /pages/size-guide is a genuine measurement chart with no care
# content at all, while a knitwear store's /pages/size-guide (also tried
# above, under "care") is an interactive fit quiz whose actual page shell
# has no static sizing text either. Captured separately, gated on its own
# keyword check, so a real chart isn't silently dropped just because it
# doesn't mention washing.
SIZE_GUIDE_PATH_CANDIDATES = [
    "/pages/size-guide",
    "/pages/sizing",
    "/pages/size-chart",
    "/pages/size-guides",
    "/pages/fit-guide",
    "/pages/measurements",
]
# A store can write its own custom "Delivery Information"/"Returns" page
# with real, current dispatch times/prices or return steps, distinct from
# (and more specific/current than) Shopify's generic policy pages at
# POLICY_PATHS — confirmed live: a store's homepage FAQ stated a stale
# "2-3 working days" while its own dedicated /pages/delivery-information
# said "1-2 working days", and the generic /policies/shipping-policy page
# had no real content at all, so the drafting prompt had nothing to prefer
# over the stale homepage figure. These are captured under their own keys
# ("delivery_info"/"refund_info") so the drafting prompt can treat them as
# the single most authoritative source for that topic — see app.demo_kb.
DELIVERY_PATH_CANDIDATES = [
    "/pages/delivery-information",
    "/pages/delivery",
    "/pages/delivery-info",
    "/pages/shipping-info",
    "/pages/shipping-information",
    "/pages/dispatch-information",
]
REFUND_PATH_CANDIDATES = [
    "/pages/returns-information",
    "/pages/returns",
    "/pages/refund-information",
    "/pages/refunds",
    "/pages/returns-policy",
]
# A store's in-person offerings (workshops, classes, experiences, booked
# sessions) are easy to miss entirely — confirmed live: a candle shop sells
# workshops as ordinary catalog products, but when a dedicated page like
# this also exists, it's the more reliable source for whether/what the
# store offers (a product catalog line doesn't explain the offering the
# way a page written for customers does). Same "first found wins" pattern
# as About/Care above.
SERVICES_PATH_CANDIDATES = [
    "/pages/workshops",
    "/pages/workshops-and-events",
    "/pages/events",
    "/pages/experiences",
    "/pages/services",
    "/pages/classes",
]
# A store-wide multi-buy/bundle deal ("5 for £15", "Buy 2 Get 1 Free") is
# usually rendered as a short badge/label on each product card on a
# collection listing page — never in /products.json's body_html, and never
# on any of the dedicated policy/about/care pages above. "/collections/all"
# is the one collection URL virtually every Shopify store has (the platform
# auto-generates it unless a merchant explicitly deletes it), making it the
# closest thing to a universal place to look for a store-wide promo rather
# than guessing a specific collection's handle.
COLLECTION_PATH_CANDIDATES = ["/collections/all"]
# A store occasionally documents accepted payment methods on their own page
# rather than relying on footer icons alone — tried the same "first found
# wins" way as About/Care above.
PAYMENT_PATH_CANDIDATES = ["/pages/payment", "/pages/payment-options", "/pages/payment-methods"]
# A whole scraped page's text is capped at this length before being combined
# with everything else — a page like a big interactive size-guide quiz can
# run to hundreds of thousands of characters of mostly app chrome, and
# without a per-page cap that alone can crowd out every other (shorter, more
# useful) page once the combined text gets truncated for the drafting
# prompt — the same failure class as the locale-selector/homepage-truncation
# bug above, just at the multi-page level instead of within one page.
MAX_PAGE_TEXT_CHARS = 5000
# Wash/care instructions are often only on the individual PRODUCT page (an
# expandable "Garment Care" section), not in /products.json's body_html and
# not on any dedicated page — see fetch_product_care_text.
_CARE_KEYWORDS_RE = re.compile(r"garment care|wash|dry clean|tumble dry|do not bleach|care instructions", re.IGNORECASE)
# A genuine size guide has actual measurements/guidance, not just the words
# "size guide" in a nav label over an otherwise-empty JS-rendered page shell
# (confirmed live: a knitwear store's /pages/size-guide server HTML is 366
# chars of pure chrome once stripped — its real sizing quiz is client-side
# JS, so there's nothing here worth capturing; one clothing store's
# /pages/size-guide, by contrast, has a full UK/US/EU chart in cm/inches).
_SIZE_GUIDE_KEYWORDS_RE = re.compile(
    r"how to measure|size chart|true to size|runs small|runs large|runs big|\bbust\b|\bwaist\b|\bhips\b"
    r"|\d+\s?(cm|inches|in)\b",
    re.IGNORECASE,
)
# A genuine delivery/returns info page states actual dispatch/return
# mechanics, not just the words "delivery"/"returns" in a nav label.
_DELIVERY_KEYWORDS_RE = re.compile(
    r"dispatch|working day|royal mail|tracked|courier|business day|delivery time|days? to arrive",
    re.IGNORECASE,
)
_REFUND_KEYWORDS_RE = re.compile(
    r"\brefund\b|\breturn\b|\bexchange\b|\bdays? to return\b|money back",
    re.IGNORECASE,
)
# A genuine services/workshops page describes an actual bookable offering,
# not just the word "events" in an unrelated nav label (e.g. a blog
# category called "Events" that's really just news posts).
_SERVICES_KEYWORDS_RE = re.compile(
    r"workshop|\bclass(es)?\b|\bsession\b|\bexperience\b|book (a|your) (place|spot|ticket)",
    re.IGNORECASE,
)
# Confirmed live: charlartscrafts.co.uk renders "5 for £15" on nearly every
# wax melt product card via this kind of badge, but nothing scraped ever
# mentioned it, so the demo bot flatly denied any bulk discount existed
# when asked. Matches short promo-phrase patterns rather than accepting a
# collection page's whole text, since the rest of that page is just
# product titles/prices already captured from /products.json and would
# otherwise crowd out the one short phrase that matters once combined text
# is truncated downstream for the drafting prompt.
_PROMO_KEYWORDS_RE = re.compile(
    r"\d+\s*for\s*£\d+(?:\.\d+)?"
    r"|buy\s*\d+\s*get\s*\d+"
    r"|\d+%\s*off"
    r"|mix\s*(?:and|&)\s*match"
    r"|multi-?buy"
    r"|bundle\s*(?:deal|offer|price)"
    r"|free\s*gift\s*(?:with|over)",
    re.IGNORECASE,
)
def _extract_promo_phrases(text: str) -> str:
    """Deduplicated list of distinct promo phrases _PROMO_KEYWORDS_RE finds
    in `text`, joined for display — e.g. "5 for £15". A collection page
    typically repeats the SAME badge on every product card, so only the
    first occurrence of each distinct phrase is kept.

    Returns just the matched phrase itself, not a windowed snippet of
    surrounding page chrome ("Add to cart", review counts, etc.) — these
    exact figures get used VERBATIM in the knowledge base rather than
    handed to an LLM to paraphrase (see app.demo_kb.scrape_and_draft_store),
    after a real store's drafted promotions field invented an extra "10
    for £25" tier that appeared nowhere in the scraped text, no matter how
    explicitly the drafting prompt forbade it — the only reliable fix was
    to stop asking an LLM to reproduce these figures at all.
    """
    seen = set()
    phrases = []
    for match in _PROMO_KEYWORDS_RE.finditer(text):
        phrase = match.group(0)
        key = phrase.lower()
        if key in seen:
            continue
        seen.add(key)
        phrases.append(phrase)
    return ", ".join(phrases)


class ScrapeError(Exception):
    """Raised for conditions the caller should stop and report clearly:
    non-Shopify site, password-protected store, or a fatal fetch failure.
    """


def normalise_base_url(store_url: str) -> str:
    parsed = urlparse(store_url if "://" in store_url else f"https://{store_url}")
    # urlparse is lenient (e.g. "not a url at all" parses with a non-empty
    # netloc) — require something host-shaped: no spaces, at least one dot
    # (or "localhost", for local testing).
    if not parsed.netloc or " " in parsed.netloc or ("." not in parsed.netloc and "localhost" not in parsed.netloc):
        raise ScrapeError(f"{store_url!r} doesn't look like a valid URL")
    return f"{parsed.scheme}://{parsed.netloc}"


class PoliteFetcher:
    """Fetches pages from one site with User-Agent identification,
    robots.txt compliance, a minimum delay between requests, and clean
    timeout/error handling (every method returns None on failure rather
    than raising, except where the caller explicitly wants a hard error).
    """

    def __init__(
        self,
        base_url: str,
        user_agent: str = USER_AGENT,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        min_delay: float = DEFAULT_MIN_DELAY_SECONDS,
    ):
        self.base_url = base_url
        self.user_agent = user_agent
        self.timeout = timeout
        self.min_delay = min_delay
        self._session = requests.Session()
        self._session.headers["User-Agent"] = user_agent
        self._last_request_at = 0.0
        self._robots = self._load_robots()

    def _load_robots(self) -> urllib.robotparser.RobotFileParser:
        rp = urllib.robotparser.RobotFileParser()
        try:
            resp = self._session.get(urljoin(self.base_url, "/robots.txt"), timeout=self.timeout)
            if resp.status_code == 200:
                rp.parse(resp.text.splitlines())
            else:
                rp.parse([])  # no robots.txt => nothing disallowed
        except requests.RequestException:
            rp.parse([])
        return rp

    def _wait_for_rate_limit(self) -> None:
        elapsed = time.monotonic() - self._last_request_at
        if elapsed < self.min_delay:
            time.sleep(self.min_delay - elapsed)

    def get(self, path: str) -> requests.Response | None:
        url = urljoin(self.base_url, path)
        if not self._robots.can_fetch(self.user_agent, url):
            return None
        self._wait_for_rate_limit()
        try:
            response = self._session.get(url, timeout=self.timeout)
        except requests.RequestException:
            return None
        finally:
            self._last_request_at = time.monotonic()
        return response

    def get_json(self, path: str) -> dict | list | None:
        response = self.get(path)
        if response is None or response.status_code != 200:
            return None
        try:
            return response.json()
        except ValueError:
            return None

    def get_text(self, path: str) -> str | None:
        response = self.get(path)
        if response is None or response.status_code != 200:
            return None
        return response.text


#  A Shopify country/currency selector ("Albania (EUR €) Algeria (USD $) ...")
# can run to hundreds of countries and isn't nested inside a <header>/<footer>
# landmark on every theme, so it survives the tag-based strip below and can
# push genuinely useful content (e.g. a brand-claims badge row right after
# it) past any truncation limit applied later. 15+ consecutive
# "Name (CODE symbol)"-shaped chunks is specific enough to this boilerplate
# that it won't false-positive on real prose (checked against policy/price/
# sizing text).
_LOCALE_SELECTOR_RE = re.compile(r"(?:[A-Za-z][A-Za-z .,'&-]{1,40}\s\([A-Z]{3}\s?[^)]{0,3}\)\s*){15,}")


def strip_html_to_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "nav", "header", "footer", "noscript"]):
        tag.decompose()
    text = soup.get_text(separator=" ", strip=True)
    text = _LOCALE_SELECTOR_RE.sub(" ", text)
    return " ".join(text.split())


def strip_html_to_text_keep_banner(html: str) -> str:
    """Like strip_html_to_text, but keeps <header>/<nav> content — an
    announcement bar ("30 DAY RETURNS", "FREE DELIVERY OVER £30") is often
    marked up as part of the page header/nav, so the normal strip silently
    drops exactly the kind of claim the audit (app.audit) needs to
    cross-check against the store's actual policy pages. Only used for
    that purpose — everywhere else, header/nav really is just chrome.
    """
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    text = soup.get_text(separator=" ", strip=True)
    text = _LOCALE_SELECTOR_RE.sub(" ", text)
    return " ".join(text.split())


def is_password_protected(fetcher: PoliteFetcher) -> bool:
    response = fetcher.get("/")
    if response is None:
        return False
    if response.status_code == 401:
        return True
    return 'name="password"' in response.text.lower()


def is_shopify_store(fetcher: PoliteFetcher) -> bool:
    data = fetcher.get_json("/products.json?limit=1")
    return isinstance(data, dict) and "products" in data


def fetch_homepage(fetcher: PoliteFetcher) -> BeautifulSoup | None:
    html = fetcher.get_text("/")
    if html is None:
        return None
    return BeautifulSoup(html, "html.parser")


def _logo_alt_text(soup: BeautifulSoup) -> str | None:
    """The alt text of the first <img> that looks like a site logo (its
    own class/id/src, or its immediate parent's class, mentions "logo") —
    themes almost always set this to the real brand name, even when
    og:site_name is missing and the <title> tag leads with a generic
    product category instead (confirmed live: ohmymelt.co.uk's <title>
    put "Wax Melts" before the brand name, so title-splitting alone
    picked the wrong one — see detect_shop_name).
    """
    for img in soup.find_all("img"):
        alt = (img.get("alt") or "").strip()
        if not alt:
            continue
        classes = " ".join(img.get("class") or [])
        parent_classes = " ".join(img.parent.get("class") or []) if img.parent else ""
        haystack = f"{classes} {img.get('id', '')} {img.get('src', '')} {parent_classes}".lower()
        if "logo" in haystack:
            return alt
    return None


def detect_shop_name(soup: BeautifulSoup | None, base_url: str) -> str:
    if soup is not None:
        og_site_name = soup.find("meta", attrs={"property": "og:site_name"})
        if og_site_name and og_site_name.get("content"):
            return og_site_name["content"].strip()
        logo_alt = _logo_alt_text(soup)
        if logo_alt:
            return logo_alt
        if soup.title and soup.title.string:
            title = soup.title.string.strip()
            for sep in (" – ", " — ", " | ", " - "):
                if sep in title:
                    title = title.split(sep)[0].strip()
                    break
            if title:
                return title
    # Fallback: derive from the domain, e.g. "emberhome.uk" -> "Emberhome"
    host = urlparse(base_url).netloc.split(".")[0]
    return host.replace("-", " ").title()


def detect_brand_color(soup: BeautifulSoup | None) -> str | None:
    if soup is None:
        return None
    theme_color = soup.find("meta", attrs={"name": "theme-color"})
    if theme_color and theme_color.get("content"):
        content = theme_color["content"].strip()
        if content.startswith("#"):
            return content
    return None


def fetch_all_products(fetcher: PoliteFetcher, max_pages: int = 10, page_size: int = 250) -> list[ProductResult]:
    """Paginate the public /products.json endpoint.

    Best-effort: Shopify's public products.json still generally supports
    ?page=N&limit=... for the first several hundred products, which is
    plenty for a sales demo — this isn't meant to mirror a store's full
    catalogue. Stops on an empty page, a fetch failure, or max_pages.
    """
    products: list[ProductResult] = []
    for page in range(1, max_pages + 1):
        data = fetcher.get_json(f"/products.json?limit={page_size}&page={page}")
        if not isinstance(data, dict):
            break
        raw_products = data.get("products") or []
        if not raw_products:
            break
        for p in raw_products:
            raw_variants = p.get("variants") or []
            variants = []
            prices = []
            for v in raw_variants:
                price_raw = v.get("price")
                variants.append(
                    ProductVariant(
                        title=v.get("title", ""),
                        price=f"£{price_raw}" if price_raw else "",
                        available=bool(v.get("available")),
                    )
                )
                parsed = parse_price(price_raw)
                if parsed is not None:
                    prices.append(parsed)
            available = any(v.available for v in variants)
            handle = p.get("handle", "")
            products.append(
                ProductResult(
                    title=p.get("title", "Untitled product"),
                    price=format_price_summary(prices),
                    variants=variants,
                    available=available,
                    url=urljoin(fetcher.base_url, f"/products/{handle}") if handle else fetcher.base_url,
                    description=strip_html_to_text(p.get("body_html") or ""),
                )
            )
        if len(raw_products) < page_size:
            break
    return products


def find_faq_link(soup: BeautifulSoup | None, base_url: str) -> str | None:
    """The href of the first nav/footer (or anywhere else) link whose own
    text names it as the FAQ — e.g. a store whose FAQ lives at some
    unguessable path, but whose footer has a plain "FAQs" link to it. Only
    returns a link on the SAME site (an external help-desk/FAQ widget on
    another domain is out of scope here); returns None if none found.
    """
    if soup is None:
        return None
    base_host = urlparse(base_url).netloc
    for a in soup.find_all("a", href=True):
        if not _FAQ_LINK_TEXT_RE.search(a.get_text(" ", strip=True)):
            continue
        href = a["href"].strip()
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        resolved = urljoin(base_url, href)
        if urlparse(resolved).netloc != base_host:
            continue
        return resolved
    return None


def fetch_faq_text(fetcher: PoliteFetcher, homepage_soup: BeautifulSoup | None = None) -> tuple[str, str] | None:
    """(path_or_url, text) for this store's FAQ, or None if none found.
    FAQ has no single agreed URL across themes (confirmed live,
    ohmymelt.co.uk's is "/pages/faqs", plural) — tries, in order:
    FAQ_PATH_CANDIDATES, a footer/nav link on the homepage whose own text
    names it as the FAQ (see find_faq_link), then the Shopify-app-embedded
    "/a/faq" some themes use instead of a plain server-rendered page.
    """
    for path in FAQ_PATH_CANDIDATES:
        html = fetcher.get_text(path)
        if html is None:
            continue
        text = strip_html_to_text(html)
        if text:
            return path, text

    link = find_faq_link(homepage_soup, fetcher.base_url)
    if link:
        link_path = urlparse(link).path or link
        if link_path not in FAQ_PATH_CANDIDATES:
            html = fetcher.get_text(link_path)
            if html is not None:
                text = strip_html_to_text(html)
                if text:
                    return link_path, text

    app_html = fetcher.get_text("/a/faq")
    if app_html is not None:
        app_text = strip_html_to_text(app_html)
        if app_text:
            return "/a/faq", app_text

    return None


def fetch_policy_texts(fetcher: PoliteFetcher, homepage_soup: BeautifulSoup | None = None) -> dict[str, str]:
    """Fetch known Shopify policy paths, a couple of bonus pages, and (if one
    exists under any of the usual URLs and actually contains relevant
    content, not just a page that happens to exist at a guessed URL) an
    About/Our Story page, a garment-care page, a size-guide page, and an
    FAQ page (see fetch_faq_text — pass the already-fetched homepage_soup
    so its footer/nav-link fallback doesn't need a second request for it).
    Missing pages are simply absent from the result — never guessed at.
    Each page's text is capped at MAX_PAGE_TEXT_CHARS so one unusually
    large page (e.g. an interactive size-guide quiz) can't crowd out every
    other page once the combined text is truncated downstream for the
    drafting prompt.
    """
    texts: dict[str, str] = {}
    for key, path in {**POLICY_PATHS, **BONUS_PATHS}.items():
        html = fetcher.get_text(path)
        if html is None:
            continue
        text = strip_html_to_text(html)
        if text:
            texts[key] = text[:MAX_PAGE_TEXT_CHARS]

    faq = fetch_faq_text(fetcher, homepage_soup)
    if faq:
        texts["faq"] = faq[1][:MAX_PAGE_TEXT_CHARS]

    for path in ABOUT_PATH_CANDIDATES:
        html = fetcher.get_text(path)
        if html is None:
            continue
        text = strip_html_to_text(html)
        if text:
            texts["about"] = text[:MAX_PAGE_TEXT_CHARS]
            break

    for path in CARE_PATH_CANDIDATES:
        html = fetcher.get_text(path)
        if html is None:
            continue
        text = strip_html_to_text(html)
        # Unlike About, a URL guess landing on SOME real page isn't enough —
        # e.g. a "/pages/size-guide" that's a pure fit quiz with no actual
        # care content would otherwise get accepted here, which then skips
        # the more reliable fetch_product_care_text fallback in build_demo
        # (confirmed live: this exact page existed and returned 200, but
        # had no washing instructions in it at all).
        if text and _CARE_KEYWORDS_RE.search(text):
            texts["care"] = text[:MAX_PAGE_TEXT_CHARS]
            break

    for path in SIZE_GUIDE_PATH_CANDIDATES:
        html = fetcher.get_text(path)
        if html is None:
            continue
        text = strip_html_to_text(html)
        if text and _SIZE_GUIDE_KEYWORDS_RE.search(text):
            texts["size_guide"] = text[:MAX_PAGE_TEXT_CHARS]
            break

    for path in DELIVERY_PATH_CANDIDATES:
        html = fetcher.get_text(path)
        if html is None:
            continue
        text = strip_html_to_text(html)
        if text and _DELIVERY_KEYWORDS_RE.search(text):
            texts["delivery_info"] = text[:MAX_PAGE_TEXT_CHARS]
            break

    for path in REFUND_PATH_CANDIDATES:
        html = fetcher.get_text(path)
        if html is None:
            continue
        text = strip_html_to_text(html)
        if text and _REFUND_KEYWORDS_RE.search(text):
            texts["refund_info"] = text[:MAX_PAGE_TEXT_CHARS]
            break

    for path in SERVICES_PATH_CANDIDATES:
        html = fetcher.get_text(path)
        if html is None:
            continue
        text = strip_html_to_text(html)
        if text and _SERVICES_KEYWORDS_RE.search(text):
            texts["services"] = text[:MAX_PAGE_TEXT_CHARS]
            break

    for path in COLLECTION_PATH_CANDIDATES:
        html = fetcher.get_text(path)
        if html is None:
            continue
        text = strip_html_to_text(html)
        promo_text = _extract_promo_phrases(text)
        if promo_text:
            texts["promotions"] = promo_text[:MAX_PAGE_TEXT_CHARS]
            break

    return texts


def shipping_info_is_missing(policy_texts: dict[str, str]) -> bool:
    """True if neither a dedicated delivery_info page nor the generic
    "shipping" policy page actually contains real dispatch/delivery
    content — as opposed to merely existing at some URL.

    `policy_texts["shipping"]` (from POLICY_PATHS) is accepted above
    whenever the fetched page returns ANY non-empty text, with no keyword
    gate, unlike delivery_info/care/size_guide/refund_info/services —
    confirmed live: crouchendcandles.co.uk's /policies/shipping-policy
    returns 200 and several hundred characters of text once stripped, but
    that text is pure theme chrome (nav links, footer, payment icons) with
    zero actual shipping information, so it was silently accepted as a
    real source. This check re-applies the same delivery-keyword gate used
    for delivery_info after the fact, so a caller (scrape_and_draft_store)
    can surface the gap to a human rather than generating a demo that will
    have nothing real to say about shipping.
    """
    for key in ("delivery_info", "shipping"):
        text = policy_texts.get(key, "")
        if text and _DELIVERY_KEYWORDS_RE.search(text):
            return False
    return True


def fetch_product_care_text(fetcher: PoliteFetcher, products: list[ProductResult], max_products: int = 3) -> str:
    """Washing/garment-care instructions are often only on the individual
    product page (an expandable "Garment Care" section in the theme), not in
    /products.json's body_html and not on any dedicated page — confirmed on
    a real store whose demo answered a washing question with "no washing
    guide to hand" despite the instructions being right there on every
    product page. This is normally a shared snippet repeated across a
    store's whole catalogue rather than written per product, so checking a
    handful of real product pages and stopping at the first match is enough
    — no need to fetch every product.
    """
    for product in products[:max_products]:
        if not product.url:
            continue
        html = fetcher.get_text(product.url)
        if not html:
            continue
        text = strip_html_to_text(html)
        match = _CARE_KEYWORDS_RE.search(text)
        if match:
            start = max(0, match.start() - 100)
            return text[start : start + MAX_PAGE_TEXT_CHARS]
    return ""


def fetch_product_pages_text(
    fetcher: PoliteFetcher, products: list[ProductResult], max_products: int = 5
) -> list[tuple[str, str]]:
    """Full (capped) text of a handful of real product pages, each tagged
    with its URL — unlike fetch_product_care_text (one keyword-gated
    snippet), this is for callers that need broad product-page content with
    per-page provenance (e.g. app.audit, which must cite the exact page a
    claim came from). Bundle pricing, ingredients, materials, engraving
    options and a product-page "Delivery & Returns" accordion are all
    commonly only on the product page itself, not /products.json's
    body_html or any store-wide page.
    """
    pages = []
    for product in products[:max_products]:
        if not product.url:
            continue
        html = fetcher.get_text(product.url)
        if not html:
            continue
        text = strip_html_to_text(html)
        if text:
            pages.append((product.url, text[:MAX_PAGE_TEXT_CHARS]))
    return pages
