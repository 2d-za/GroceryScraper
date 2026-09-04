"""
Core scraping and matching logic for comparing a grocery product's price
across Checkers Sixty60, Pick n Pay, and Woolworths.

Shared by ComparePrices.py (CLI) and api.py (web backend). Uses Playwright
since all three sites render client-side.
"""

import asyncio
import re
import sys
import time
import unicodedata
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from playwright.async_api import async_playwright, Page, TimeoutError as PWTimeoutError

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

# Images/fonts/media and known trackers — narrow patterns so unrelated
# requests skip our handler entirely (a "**/*" catch-all round-trips every
# request through Python and was measurably slower under concurrent load).
BLOCKED_EXTENSIONS_RE = re.compile(
    r"\.(png|jpe?g|gif|webp|svg|ico|bmp|woff2?|ttf|eot|mp4|webm|avif)(\?|$)", re.I
)
BLOCKED_HOSTS_RE = re.compile(
    r"(google-analytics\.com|googletagmanager\.com|doubleclick\.net|"
    r"connect\.facebook\.net|facebook\.net|hotjar\.com|criteo\.(com|net)|"
    r"outbrain\.com|taboola\.com|newrelic\.com|nr-data\.net|clarity\.ms|mypurecloud)",
    re.I,
)


async def abort_request(route):
    await route.abort()


async def new_page(browser) -> Page:
    page = await browser.new_page(user_agent=USER_AGENT, viewport={"width": 1400, "height": 1200})
    await page.route(BLOCKED_EXTENSIONS_RE, abort_request)
    await page.route(BLOCKED_HOSTS_RE, abort_request)
    return page


@dataclass
class Offer:
    retailer: str
    name: str
    price: float
    deal_label: str | None
    url: str | None


async def accept_cookies(page: Page) -> None:
    for text in ["Accept", "Accept All", "Accept All Cookies", "I Accept"]:
        try:
            await page.click(f"text={text}", timeout=3000)
            return
        except PWTimeoutError:
            continue


def normalize(text: str) -> str:
    # Strip diacritics: "Kronung" <-> "Krönung", "Nescafe" <-> "Nescafé".
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(c for c in decomposed if not unicodedata.combining(c)).lower()


# Retailers abbreviate pack counts differently (Woolworths "100 pk" vs
# "100 Pack" elsewhere).
WORD_SYNONYMS = {
    "pk": "pack",
    "pkt": "pack",
    "pkts": "pack",
}


def normalize_for_match(text: str) -> str:
    # Also drops whitespace: "200g" <-> "200 g".
    words = [WORD_SYNONYMS.get(w, w) for w in normalize(text).split()]
    return "".join(words)


def word_variants(word: str) -> list[str]:
    """Singular/plural forms a plain substring check would miss."""
    base = normalize_for_match(word)
    forms = {base}
    if base.endswith("ies") and len(base) > 4:
        forms.add(base[:-3] + "y")
    if base.endswith("s") and len(base) > 3:
        forms.add(base[:-1])
    return list(forms)


def word_hits(name: str, require: list[str]) -> int:
    low = normalize_for_match(name)
    return sum(1 for r in require if any(f in low for f in word_variants(r)))


def min_required_hits(require: list[str]) -> int:
    """Tolerate one missing word for 3+ word queries — retailers sometimes
    omit a generic category word entirely (e.g. "Lemon Creams" vs "...Cream
    Biscuits"). Short queries still need a full match."""
    return len(require) if len(require) <= 2 else len(require) - 1


def is_excluded(name: str, exclude: list[str]) -> bool:
    low = normalize_for_match(name)
    exclude_forms = [f for e in exclude for f in word_variants(e)]
    return any(f in low for f in exclude_forms)


def offer_matches(name: str, require: list[str], exclude: list[str]) -> bool:
    if is_excluded(name, exclude):
        return False
    return word_hits(name, require) >= min_required_hits(require)


def pick_best_match(offers: list[Offer], require: list[str], exclude: list[str]) -> Offer | None:
    candidates = [o for o in offers if offer_matches(o.name, require, exclude)]
    if not candidates:
        return None
    return max(candidates, key=lambda o: word_hits(o.name, require))


SIZE_RE = re.compile(r"(\d+(?:\.\d+)?)(kg|g|ml|l)\b")


def extract_size(name: str) -> str | None:
    m = SIZE_RE.search(normalize_for_match(name))
    return f"{m.group(1)}{m.group(2)}" if m else None


def rank_sizes(all_offers: dict[str, list[Offer]], require: list[str], exclude: list[str], limit: int = 2) -> list[str]:
    """Pack sizes ranked by cross-store coverage, for comparing the same
    size across retailers when the query doesn't name one."""
    stores_by_size: dict[str, set] = {}
    counts: Counter[str] = Counter()
    for retailer, offers in all_offers.items():
        for offer in offers:
            if not offer_matches(offer.name, require, exclude):
                continue
            size = extract_size(offer.name)
            if size:
                stores_by_size.setdefault(size, set()).add(retailer)
                counts[size] += 1
    sizes = list(stores_by_size)
    sizes.sort(key=lambda s: (-len(stores_by_size[s]), -counts[s]))
    return sizes[:limit]


CHECKERS_EXTRACT_JS = """
() => Array.from(document.querySelectorAll("a[data-testid$='-product-card-link']")).map(a => {
    const card = a.closest('[class*=product-card_card]');
    const whole = card && card.querySelector("[class*='price-display_full']");
    const cents = card && card.querySelector("[class*='price-display_half']");
    const promo = card && card.querySelector('[class*=product-card_promotion]');
    return {
        name: a.getAttribute('aria-label') || '',
        href: a.getAttribute('href') || '',
        price: (whole && cents) ? (whole.textContent.trim() + cents.textContent.trim()) : null,
        dealLabel: promo ? (promo.innerText || '').trim() : null,
    };
})
"""


def _build_checkers_offers(raw: list[dict]) -> list[Offer]:
    offers = []
    for item in raw:
        name = (item.get("name") or "").strip()
        price_text = item.get("price")
        if not name or not price_text:
            continue
        price = float(re.sub(r"[^\d.]", "", price_text))
        href = item.get("href") or ""
        offers.append(Offer(
            retailer="Checkers",
            name=name,
            price=price,
            deal_label=item.get("dealLabel") or None,
            url=f"https://www.checkers.co.za{href}" if href.startswith("/") else (href or None),
        ))
    return offers


async def scrape_checkers(
    page: Page, query: str, address: str,
    require: list[str] | None = None, exclude: list[str] | None = None,
) -> list[Offer]:
    await page.goto("https://www.checkers.co.za/", wait_until="networkidle", timeout=30000)
    await accept_cookies(page)

    # Address is remembered for the session, so on a retry this prompt is
    # simply gone — only run the entry flow when it's actually present.
    try:
        await page.click("text=Enter your address", timeout=3000)
    except PWTimeoutError:
        pass
    else:
        await page.wait_for_timeout(800)
        addr_input = page.locator("input[type='text']").first
        await addr_input.click()
        await addr_input.type(address, delay=80)

        # Take the top autocomplete prediction rather than text-matching it —
        # the site's "Street, Suburb, City, Country" format rarely matches
        # whatever format the user typed.
        suggestion = page.locator(
            "[class*='address-search_address-dropdown'] li[class*='prediction-list-item']"
        ).first
        await suggestion.wait_for(state="visible", timeout=10000)
        await suggestion.click(timeout=8000)
        await page.wait_for_timeout(1500)

    search_box = page.locator("input[placeholder*='Search']").first
    await search_box.click()
    await search_box.fill(query)
    await search_box.press("Enter")
    try:
        await page.wait_for_selector("a[data-testid$='-product-card-link']", timeout=25000)
    except PWTimeoutError:
        return []

    # Results lazy-load via infinite scroll; stop as soon as we have a
    # confident match instead of always loading everything.
    offers: list[Offer] = []
    previous_count = -1
    for _ in range(6):
        raw = await page.evaluate(CHECKERS_EXTRACT_JS)
        offers = _build_checkers_offers(raw)
        if require is not None and pick_best_match(offers, require, exclude or []):
            break
        if len(raw) == previous_count:
            break
        previous_count = len(raw)
        await page.mouse.wheel(0, 4000)
        await page.wait_for_timeout(1200)

    return offers


PNP_EXTRACT_JS = """
() => Array.from(document.querySelectorAll('div.product-grid-item')).map(card => {
    const promoBox = card.querySelector('.product-grid-item__promotion-container.has-value #promotion');
    const badge = card.querySelector('[class*=group4-badge] li, [class*=group4-badge]');
    const link = card.querySelector('a.product-grid-item__info-container__name');
    return {
        name: card.getAttribute('aria-label') || '',
        desc: card.getAttribute('aria-description') || '',
        promoPrice: promoBox ? promoBox.textContent.trim() : null,
        badgeLabel: badge ? (badge.getAttribute('title') || badge.textContent.trim()) : null,
        href: link ? link.getAttribute('href') : null,
    };
})
"""


async def scrape_pnp(page: Page, query: str, *_ignored) -> list[Offer]:
    await page.goto("https://www.pnp.co.za/", wait_until="networkidle", timeout=30000)
    await accept_cookies(page)
    await page.wait_for_timeout(800)
    try:
        await page.click("text=Do this later", timeout=5000)
    except PWTimeoutError:
        pass
    await page.wait_for_timeout(800)

    search_box = page.locator("input[placeholder*='Search']").first
    await search_box.click()
    await search_box.fill(query)
    await search_box.press("Enter")
    try:
        await page.wait_for_selector("div.product-grid-item", timeout=25000)
    except PWTimeoutError:
        return []

    raw = await page.evaluate(PNP_EXTRACT_JS)
    offers = []
    for item in raw:
        name = (item.get("name") or "").strip()
        desc = item.get("desc") or ""
        m = re.search(r"([\d.]+)\s*rands", desc)
        if not name or not m:
            continue
        regular_price = float(m.group(1))

        deal_price = None
        deal_text = None
        promo_text = item.get("promoPrice")
        if promo_text:
            # "N for RXX.XX" bundles need dividing to a per-unit price —
            # grabbing the first number in the string catches the "N", not
            # the price.
            bundle_m = re.search(r"(\d+)\s*for\s*R\s?([\d,]+(?:\.\d{2})?)", promo_text, re.I)
            if bundle_m:
                qty = int(bundle_m.group(1))
                total = float(bundle_m.group(2).replace(",", ""))
                if qty > 0:
                    deal_price = total / qty
                    deal_text = f"{promo_text.strip()} (R{deal_price:.2f} each)"
            else:
                price_m = re.search(r"R\s?([\d,]+(?:\.\d{2})?)", promo_text)
                if price_m:
                    deal_price = float(price_m.group(1).replace(",", ""))
                    deal_text = promo_text.strip()

        price = deal_price if deal_price is not None else regular_price
        deal_label = None
        if deal_price is not None and deal_price < regular_price:
            badge_label = item.get("badgeLabel")
            if badge_label and deal_text:
                deal_label = f"{badge_label}: {deal_text}"
            else:
                deal_label = deal_text or badge_label or "On promotion"

        href = item.get("href") or ""
        offers.append(Offer(
            retailer="Pick n Pay",
            name=name,
            price=price,
            deal_label=deal_label,
            url=f"https://www.pnp.co.za{href}" if href.startswith("/") else (href or None),
        ))
    return offers


WOOLWORTHS_EXTRACT_JS = """
() => Array.from(document.querySelectorAll("[data-testid='product-card']")).map(card => {
    const promo = card.querySelector("[data-testid='product-card-promotion']");
    return {
        name: card.getAttribute('data-cnstrc-item-name'),
        price: card.getAttribute('data-cnstrc-item-price'),
        dealLabel: promo ? (promo.innerText || '').trim() : null,
    };
})
"""


async def scrape_woolworths(page: Page, query: str, *_ignored) -> list[Offer]:
    url = f"https://www.woolworths.co.za/browse?searchterm={query.replace(' ', '%20')}&fr=1"
    await page.goto(url, timeout=30000, wait_until="domcontentloaded")
    await accept_cookies(page)
    try:
        await page.wait_for_selector("[data-testid='product-card']", timeout=25000)
    except PWTimeoutError:
        return []

    raw = await page.evaluate(WOOLWORTHS_EXTRACT_JS)
    offers = []
    for item in raw:
        if not item.get("name") or not item.get("price"):
            continue
        offers.append(Offer(
            retailer="Woolworths",
            name=re.sub(r"\s+", " ", item["name"]).strip(),
            price=float(item["price"]),
            deal_label=item.get("dealLabel") or None,
            url=url,
        ))
    return offers


SCRAPERS = {
    "Checkers": scrape_checkers,
    "Pick n Pay": scrape_pnp,
    "Woolworths": scrape_woolworths,
}


async def search_with_fallback(
    scraper, page: Page, product: str, address: str, require: list[str], exclude: list[str],
    scroll_require: list[str] | None, scroll_exclude: list[str] | None,
) -> list[Offer]:
    """Retry once with the last word dropped if nothing matched — capped at
    one retry since a failed attempt is usually transient load, not the
    query, and retrying repeatedly just adds more concurrent load.

    scroll_require/scroll_exclude must be None when the caller needs the
    full result set (e.g. size-ranking a query with no size named)."""
    offers = await scraper(page, product, address, scroll_require, scroll_exclude)
    if pick_best_match(offers, require, exclude):
        return offers

    words = product.split()
    if len(words) > 2:
        fallback_query = " ".join(words[:-1])
        try:
            fallback_offers = await scraper(page, fallback_query, address, scroll_require, scroll_exclude)
        except Exception:
            fallback_offers = []
        offers = offers + fallback_offers
    return offers


DEBUG_DIR = Path("debug")


async def save_debug_snapshot(page: Page, retailer: str) -> None:
    DEBUG_DIR.mkdir(exist_ok=True)
    safe_name = re.sub(r"[^a-z0-9]+", "-", retailer.lower()).strip("-")
    screenshot_path = DEBUG_DIR / f"{safe_name}-{int(time.time())}.png"
    try:
        await page.screenshot(path=str(screenshot_path), full_page=True)
        print(f"  [debug] {retailer}: 0 offers — saved {screenshot_path} (page was at {page.url})", file=sys.stderr)
    except Exception as e:
        print(f"  [debug] {retailer}: couldn't capture a debug screenshot: {e}", file=sys.stderr)


async def run_retailer(browser, retailer: str, scraper, product: str, address: str,
                        require: list[str], exclude: list[str],
                        scroll_require: list[str] | None, scroll_exclude: list[str] | None,
                        debug: bool = False) -> tuple[str, list[Offer]]:
    print(f"Checking {retailer} ...")
    start = time.monotonic()
    page = await new_page(browser)
    offers: list[Offer] = []
    try:
        offers = await search_with_fallback(
            scraper, page, product, address, require, exclude, scroll_require, scroll_exclude
        )
    except Exception as e:
        print(f"  {retailer} scrape failed: {e}", file=sys.stderr)
    if debug and not offers:
        await save_debug_snapshot(page, retailer)
    await page.close()
    elapsed = time.monotonic() - start
    print(f"  {retailer} done in {elapsed:.1f}s ({len(offers)} offers)")
    return retailer, offers


async def run(
    product: str, address: str, require: list[str], exclude: list[str], debug: bool = False
) -> dict[str, list[Offer]]:
    # Early-exit scrolling only kicks in when the query already names a
    # size, so a size-less query still gets the full result set to rank.
    has_size = extract_size(product) is not None
    scroll_require = require if has_size else None
    scroll_exclude = exclude if has_size else None

    start = time.monotonic()
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        results = await asyncio.gather(*(
            run_retailer(browser, retailer, scraper, product, address, require, exclude,
                         scroll_require, scroll_exclude, debug)
            for retailer, scraper in SCRAPERS.items()
        ))
        await browser.close()
    print(f"All stores checked in {time.monotonic() - start:.1f}s")
    return dict(results)


def default_require(product: str) -> list[str]:
    return re.findall(r"\w+", normalize(product))


def build_comparison(label: str, all_offers: dict[str, list[Offer]], require: list[str], exclude: list[str]) -> dict:
    """Best match per retailer for one label, plus cheapest and on-deal."""
    matches: dict[str, Offer | None] = {
        retailer: pick_best_match(offers, require, exclude)
        for retailer, offers in all_offers.items()
    }
    found = [o for o in matches.values() if o]
    found.sort(key=lambda o: o.price)
    cheapest = found[0] if found else None
    on_deal = [o for o in found if o.deal_label]
    return {
        "label": label,
        "require": require,
        "exclude": exclude,
        "matches": matches,
        "cheapest": cheapest,
        "on_deal": on_deal,
    }


async def compare_product(
    product: str, address: str,
    require: list[str] | None = None, exclude: list[str] | None = None,
    debug: bool = False,
) -> list[dict]:
    """Scrapes all three stores and returns one or two build_comparison()
    results — two when the query has no size and multiple comparable sizes
    were found, one otherwise."""
    require = require if require is not None else default_require(product)
    exclude = exclude or []

    all_offers = await run(product, address, require, exclude, debug)

    if extract_size(product) is not None:
        return [build_comparison(product, all_offers, require, exclude)]

    sizes = rank_sizes(all_offers, require, exclude, limit=2)
    if not sizes:
        return [build_comparison(product, all_offers, require, exclude)]

    return [
        build_comparison(f"{product} ({size})", all_offers, require + [size], exclude)
        for size in sizes
    ]
