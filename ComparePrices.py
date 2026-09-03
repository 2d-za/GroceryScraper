"""
Compares a grocery product's price across Checkers Sixty60 (checkers.co.za),
Pick n Pay (pnp.co.za), and Woolworths (woolworths.co.za), and reports which
store is cheapest and which (if any) currently has a deal on it.

All three sites render their catalogs client-side, so this drives a real
headless browser (Playwright) rather than plain HTTP requests:
  - Checkers requires a delivery address before it shows real prices.
  - Pick n Pay shows a dismissible "delivery details" prompt.
  - Woolworths shows national pricing without a location, but its listed
    price on a promotion may still differ from the "SAVE"/limited-item deal
    price shown in the promo badge.

The three stores are scraped concurrently (each gets its own page, all
sharing one browser), and each page blocks images/fonts/media and known
analytics/ad trackers to cut network noise — both because we never look at
those resources and because they were measurably slowing down Checkers'
and Pick n Pay's `networkidle` wait (trackers keep firing in the
background, which resets the "quiet" timer networkidle waits for).

Run it with no arguments and it will prompt for a delivery address and a
product to search for. If the product doesn't mention a size (e.g. just
"Jacobs Gold Instant Coffee" instead of "...200g"), it compares the two
most common pack sizes found across the three stores instead of guessing
which one you meant.

Usage:
    py ComparePrices.py
    py ComparePrices.py "Jacobs Gold Instant Coffee 200g"
    py ComparePrices.py "Jacobs Gold Instant Coffee 200g" --address "1 Sandton Drive, Sandton"
    py ComparePrices.py "Jacobs Gold Instant Coffee 200g" --require gold 200g --exclude refill kronung stick sachet decaf
"""

import argparse
import asyncio
import re
import sys
import time
import unicodedata
from collections import Counter
from dataclasses import dataclass

from playwright.async_api import async_playwright, Page, TimeoutError as PWTimeoutError

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

# We only ever read text/attributes out of the DOM, never look at images or
# render anything visually, so these are pure dead weight. Blocking them
# also means three concurrent pages aren't competing for bandwidth with junk
# they don't need.
BLOCKED_RESOURCE_TYPES = {"image", "media", "font"}
BLOCKED_HOST_SNIPPETS = (
    "google-analytics.com", "googletagmanager.com", "doubleclick.net",
    "connect.facebook.net", "facebook.net", "hotjar.com", "criteo.com",
    "criteo.net", "outbrain.com", "taboola.com", "newrelic.com",
    "nr-data.net", "clarity.ms", "mypurecloud",
)


async def block_unnecessary_requests(route):
    request = route.request
    if request.resource_type in BLOCKED_RESOURCE_TYPES or any(
        host in request.url for host in BLOCKED_HOST_SNIPPETS
    ):
        await route.abort()
    else:
        await route.continue_()


async def new_page(browser) -> Page:
    page = await browser.new_page(user_agent=USER_AGENT, viewport={"width": 1400, "height": 1200})
    await page.route("**/*", block_unnecessary_requests)
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
    # Strip diacritics so "Kronung" matches "Krönung", "Nescafe" matches "Nescafé", etc.
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(c for c in decomposed if not unicodedata.combining(c)).lower()


# Retailers abbreviate the same qualifier differently (e.g. Woolworths' "100 pk"
# vs Checkers/PnP's "100 Pack"). Map known abbreviations to a shared form
# before whitespace is stripped, so a query for one still matches the other.
WORD_SYNONYMS = {
    "pk": "pack",
    "pkt": "pack",
    "pkts": "pack",
}


def normalize_for_match(text: str) -> str:
    # Also drop whitespace so "200g" matches Woolworths' "200 g".
    words = [WORD_SYNONYMS.get(w, w) for w in normalize(text).split()]
    return "".join(words)


def word_variants(word: str) -> list[str]:
    """Alternate spellings a plain substring check would otherwise miss:
    singular/plural forms ("cream" vs "creams", "berries" vs "berry"), since
    retailers are inconsistent about which one they use in product names."""
    base = normalize_for_match(word)
    forms = {base}
    if base.endswith("ies") and len(base) > 4:
        forms.add(base[:-3] + "y")
    if base.endswith("s") and len(base) > 3:
        forms.add(base[:-1])
    return list(forms)


def offer_matches(name: str, require: list[str], exclude: list[str]) -> bool:
    low = normalize_for_match(name)
    require_forms = [word_variants(r) for r in require]
    exclude_forms = [f for e in exclude for f in word_variants(e)]
    return (
        all(any(f in low for f in forms) for forms in require_forms)
        and not any(f in low for f in exclude_forms)
    )


def pick_best_match(offers: list[Offer], require: list[str], exclude: list[str]) -> Offer | None:
    for offer in offers:
        if offer_matches(offer.name, require, exclude):
            return offer
    return None


SIZE_RE = re.compile(r"(\d+(?:\.\d+)?)(kg|g|ml|l)\b")


def extract_size(name: str) -> str | None:
    m = SIZE_RE.search(normalize_for_match(name))
    return f"{m.group(1)}{m.group(2)}" if m else None


def rank_sizes(all_offers: dict[str, list[Offer]], require: list[str], exclude: list[str], limit: int = 2) -> list[str]:
    """Pack sizes among offers matching require/exclude, ranked by how many
    different stores carry that size (most useful for cross-store comparison)
    and, as a tiebreak, how often it shows up overall."""
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

    await page.click("text=Enter your address", timeout=10000)
    await page.wait_for_timeout(800)
    addr_input = page.locator("input[type='text']").first
    await addr_input.click()
    await addr_input.type(address, delay=80)

    # Scoped to the actual autocomplete dropdown (an unscoped "li, [role=option]"
    # locator also matches unrelated <li> elements elsewhere on the page, e.g.
    # footer nav links) — take the top prediction rather than text-matching it,
    # since the site's suggestion formatting ("Street, Suburb, City, Country")
    # rarely matches whatever format the user typed the address in.
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
        await page.wait_for_selector("a[data-testid$='-product-card-link']", timeout=15000)
    except PWTimeoutError:
        return []

    # Checkers lazy-loads results via infinite scroll — often only ~16-20 of
    # potentially hundreds of matches are in the DOM until you scroll, so a
    # relevant product ranked just past that first batch would be missed.
    # Extraction runs in-browser as a single batched call each pass (rather
    # than one round-trip per card), and if we already have a confident
    # match we stop scrolling immediately instead of loading everything.
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
        await page.wait_for_selector("div.product-grid-item", timeout=15000)
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
        if item.get("promoPrice"):
            pm = re.search(r"[\d.]+", item["promoPrice"])
            if pm:
                deal_price = float(pm.group(0))

        price = deal_price if deal_price is not None else regular_price
        deal_label = None
        if deal_price is not None and deal_price < regular_price:
            deal_label = item.get("badgeLabel") or "On promotion"

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
        await page.wait_for_selector("[data-testid='product-card']", timeout=15000)
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

DEFAULT_ADDRESS = "1 Sandton Drive, Sandton"


def prompt(text: str, default: str | None = None) -> str | None:
    suffix = f" [{default}]" if default else ""
    try:
        value = input(f"{text}{suffix}: ").strip()
    except EOFError:
        value = ""
    return value or default


def print_comparison(label: str, all_offers: dict[str, list[Offer]], require: list[str], exclude: list[str]) -> None:
    matches = []
    for retailer, offers in all_offers.items():
        offer = pick_best_match(offers, require, exclude)
        if offer:
            matches.append(offer)

    print()
    print(f"Results for: {label}")
    print("-" * 70)
    for retailer in SCRAPERS:
        offer = next((m for m in matches if m.retailer == retailer), None)
        if offer:
            deal = f"  [DEAL: {offer.deal_label}]" if offer.deal_label else ""
            print(f"{retailer:<12} R{offer.price:>8.2f}{deal}  —  {offer.name}")
        else:
            print(f"{retailer:<12} no confident match found")

    if not matches:
        print("-" * 70)
        print("No matches found anywhere — try --require/--exclude to loosen matching.")
        return

    matches.sort(key=lambda o: o.price)
    cheapest = matches[0]
    print("-" * 70)
    deal_note = " — and it's currently on a deal there" if cheapest.deal_label else ""
    print(f"Best price: {cheapest.retailer} at R{cheapest.price:.2f}{deal_note}")

    on_deal = [m for m in matches if m.deal_label]
    if on_deal and on_deal[0] is not cheapest:
        for offer in on_deal:
            print(f"Also on deal: {offer.retailer} at R{offer.price:.2f} [{offer.deal_label}]")


async def search_with_fallback(
    scraper, page: Page, product: str, address: str, require: list[str], exclude: list[str],
    scroll_require: list[str] | None, scroll_exclude: list[str] | None,
) -> list[Offer]:
    """Some sites' own search boxes handle certain phrasings (extra qualifier
    words, pack counts, etc.) poorly and return nothing useful even though the
    product exists. If the first search doesn't produce a client-side match,
    retry once with the last word dropped (repeated down to two words).

    scroll_require/scroll_exclude are separate from require/exclude: they
    control Checkers' early-exit-while-scrolling and must be None whenever
    the caller still needs the *full* result set (e.g. ranking pack sizes
    for a query with no size named) — otherwise stopping at the first match
    would starve that ranking of everything past it."""
    offers = await scraper(page, product, address, scroll_require, scroll_exclude)
    if pick_best_match(offers, require, exclude):
        return offers

    words = product.split()
    while len(words) > 2:
        words = words[:-1]
        fallback_query = " ".join(words)
        try:
            fallback_offers = await scraper(page, fallback_query, address, scroll_require, scroll_exclude)
        except Exception:
            continue
        offers = offers + fallback_offers
        if pick_best_match(offers, require, exclude):
            break
    return offers


async def run_retailer(browser, retailer: str, scraper, product: str, address: str,
                        require: list[str], exclude: list[str],
                        scroll_require: list[str] | None, scroll_exclude: list[str] | None) -> tuple[str, list[Offer]]:
    print(f"Checking {retailer} ...")
    start = time.monotonic()
    page = await new_page(browser)
    try:
        offers = await search_with_fallback(
            scraper, page, product, address, require, exclude, scroll_require, scroll_exclude
        )
    except Exception as e:
        print(f"  {retailer} scrape failed: {e}", file=sys.stderr)
        offers = []
    finally:
        await page.close()
    elapsed = time.monotonic() - start
    print(f"  {retailer} done in {elapsed:.1f}s ({len(offers)} offers)")
    return retailer, offers


async def run(product: str, address: str, require: list[str], exclude: list[str]) -> dict[str, list[Offer]]:
    # Only let Checkers stop scrolling early when the query already names a
    # size — otherwise the later "no size specified" size-ranking needs to
    # see the full result set, not just whatever loaded before the first
    # (size-agnostic) match.
    has_size = extract_size(product) is not None
    scroll_require = require if has_size else None
    scroll_exclude = exclude if has_size else None

    start = time.monotonic()
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        results = await asyncio.gather(*(
            run_retailer(browser, retailer, scraper, product, address, require, exclude, scroll_require, scroll_exclude)
            for retailer, scraper in SCRAPERS.items()
        ))
        await browser.close()
    print(f"All stores checked in {time.monotonic() - start:.1f}s")
    return dict(results)


def main():
    parser = argparse.ArgumentParser(
        description="Compare a grocery product's price across Checkers, Pick n Pay, and Woolworths."
    )
    parser.add_argument("product", nargs="?", default=None,
                         help='Product to search for, e.g. "Jacobs Gold Instant Coffee 200g". '
                              "Prompted for if omitted.")
    parser.add_argument("--address", default=None,
                         help="Delivery address for Checkers Sixty60 (affects price/availability). Prompted for if omitted.")
    parser.add_argument("--require", nargs="*", default=None,
                         help="Words that must all appear in the matched product name (default: every word in the product query)")
    parser.add_argument("--exclude", nargs="*", default=[],
                         help="Words that must NOT appear in the matched product name")
    args = parser.parse_args()

    address = args.address or prompt(
        "Delivery address (Checkers needs one to show real prices)", DEFAULT_ADDRESS
    )
    product = args.product or prompt("Product to search for (e.g. 'Jacobs Gold Instant Coffee')")
    if not product:
        print("No product entered.", file=sys.stderr)
        sys.exit(1)

    require = args.require if args.require is not None else re.findall(r"\w+", normalize(product))
    exclude = args.exclude

    all_offers = asyncio.run(run(product, address, require, exclude))

    if extract_size(product) is not None:
        # User already named a size (e.g. "200g") — compare that exact one.
        print_comparison(product, all_offers, require, exclude)
        return

    sizes = rank_sizes(all_offers, require, exclude, limit=2)
    if not sizes:
        print("\nNo pack size could be detected in the results — showing the single best match per store instead.")
        print_comparison(product, all_offers, require, exclude)
        return

    print(f"\nNo size specified — comparing the {len(sizes)} most common pack size(s) found: {', '.join(sizes)}")
    for size in sizes:
        print_comparison(f"{product} ({size})", all_offers, require + [size], exclude)


if __name__ == "__main__":
    main()
