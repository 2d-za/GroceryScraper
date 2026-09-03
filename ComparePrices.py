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

Usage:
    py ComparePrices.py "Jacobs Gold Instant Coffee 200g"
    py ComparePrices.py "Jacobs Gold Instant Coffee 200g" --address "1 Sandton Drive, Sandton"
    py ComparePrices.py "Jacobs Gold Instant Coffee 200g" --require gold 200g --exclude refill kronung stick sachet decaf
"""

import argparse
import re
import sys
import unicodedata
from dataclasses import dataclass

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeoutError

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


@dataclass
class Offer:
    retailer: str
    name: str
    price: float
    deal_label: str | None
    url: str | None


def accept_cookies(page):
    for text in ["Accept", "Accept All", "Accept All Cookies", "I Accept"]:
        try:
            page.click(f"text={text}", timeout=3000)
            return
        except PWTimeoutError:
            continue


def normalize(text: str) -> str:
    # Strip diacritics so "Kronung" matches "Krönung", "Nescafe" matches "Nescafé", etc.
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(c for c in decomposed if not unicodedata.combining(c)).lower()


def normalize_for_match(text: str) -> str:
    # Also drop whitespace so "200g" matches Woolworths' "200 g".
    return re.sub(r"\s+", "", normalize(text))


def pick_best_match(offers: list[Offer], require: list[str], exclude: list[str]) -> Offer | None:
    require = [normalize_for_match(r) for r in require]
    exclude = [normalize_for_match(e) for e in exclude]
    for offer in offers:
        low = normalize_for_match(offer.name)
        if all(r in low for r in require) and not any(e in low for e in exclude):
            return offer
    return None


def scrape_checkers(page, query: str, address: str) -> list[Offer]:
    page.goto("https://www.checkers.co.za/", wait_until="networkidle", timeout=30000)
    accept_cookies(page)

    page.click("text=Enter your address", timeout=10000)
    page.wait_for_timeout(800)
    addr_input = page.locator("input[type='text']").first
    addr_input.click()
    addr_input.type(address, delay=80)
    page.wait_for_timeout(2000)

    suggestion = page.locator("li, [role='option']").filter(has_text=address.split(",")[0]).first
    suggestion.click(timeout=8000)
    page.wait_for_timeout(1500)

    search_box = page.locator("input[placeholder*='Search']").first
    search_box.click()
    search_box.fill(query)
    search_box.press("Enter")
    page.wait_for_timeout(4000)

    links = page.locator("a[data-testid$='-product-card-link']")
    offers = []
    for i in range(links.count()):
        link = links.nth(i)
        name = link.get_attribute("aria-label") or ""
        href = link.get_attribute("href") or ""
        info = link.evaluate(
            """
            e => {
                const card = e.closest('[class*=product-card_card]');
                if (!card) return null;
                const whole = card.querySelector("[class*='price-display_full']");
                const cents = card.querySelector("[class*='price-display_half']");
                const promo = card.querySelector('[class*=product-card_promotion]');
                const label = promo ? promo.textContent.trim() : '';
                return {
                    price: whole && cents ? whole.textContent.trim() + cents.textContent.trim() : null,
                    dealLabel: label || null,
                };
            }
            """
        )
        if not name or not info or not info["price"]:
            continue
        price = float(re.sub(r"[^\d.]", "", info["price"]))
        offers.append(Offer(
            retailer="Checkers",
            name=name.strip(),
            price=price,
            deal_label=info["dealLabel"],
            url=f"https://www.checkers.co.za{href}" if href.startswith("/") else (href or None),
        ))
    return offers


def scrape_pnp(page, query: str) -> list[Offer]:
    page.goto("https://www.pnp.co.za/", wait_until="networkidle", timeout=30000)
    accept_cookies(page)
    page.wait_for_timeout(800)
    try:
        page.click("text=Do this later", timeout=5000)
    except PWTimeoutError:
        pass
    page.wait_for_timeout(800)

    search_box = page.locator("input[placeholder*='Search']").first
    search_box.click()
    search_box.fill(query)
    search_box.press("Enter")
    page.wait_for_timeout(4000)

    cards = page.locator("div.product-grid-item")
    offers = []
    for i in range(cards.count()):
        card = cards.nth(i)
        name = card.get_attribute("aria-label") or ""
        desc = card.get_attribute("aria-description") or ""
        m = re.search(r"([\d.]+)\s*rands", desc)
        if not name or not m:
            continue
        regular_price = float(m.group(1))

        info = card.evaluate(
            """
            e => {
                const promoBox = e.querySelector('.product-grid-item__promotion-container.has-value #promotion');
                const badge = e.querySelector('[class*=group4-badge] li, [class*=group4-badge]');
                const link = e.querySelector('a.product-grid-item__info-container__name');
                return {
                    promoPrice: promoBox ? promoBox.textContent.trim() : null,
                    badgeLabel: badge ? (badge.getAttribute('title') || badge.textContent.trim()) : null,
                    href: link ? link.getAttribute('href') : null,
                };
            }
            """
        )
        deal_price = None
        if info["promoPrice"]:
            pm = re.search(r"[\d.]+", info["promoPrice"])
            if pm:
                deal_price = float(pm.group(0))

        price = deal_price if deal_price is not None else regular_price
        deal_label = None
        if deal_price is not None and deal_price < regular_price:
            deal_label = info["badgeLabel"] or "On promotion"

        href = info["href"] or ""
        offers.append(Offer(
            retailer="Pick n Pay",
            name=name.strip(),
            price=price,
            deal_label=deal_label,
            url=f"https://www.pnp.co.za{href}" if href.startswith("/") else (href or None),
        ))
    return offers


def scrape_woolworths(page, query: str) -> list[Offer]:
    url = f"https://www.woolworths.co.za/browse?searchterm={query.replace(' ', '%20')}&fr=1"
    page.goto(url, timeout=30000, wait_until="domcontentloaded")
    page.wait_for_timeout(3000)
    accept_cookies(page)
    page.wait_for_timeout(3000)

    cards = page.locator("[data-testid='product-card']")
    offers = []
    for i in range(cards.count()):
        card = cards.nth(i)
        data = card.evaluate(
            """
            e => ({
                name: e.getAttribute('data-cnstrc-item-name'),
                price: e.getAttribute('data-cnstrc-item-price'),
            })
            """
        )
        if not data["name"] or not data["price"]:
            continue
        promo = card.locator("[data-testid='product-card-promotion']")
        deal_label = promo.inner_text().strip() if promo.count() > 0 else None
        offers.append(Offer(
            retailer="Woolworths",
            name=re.sub(r"\s+", " ", data["name"]).strip(),
            price=float(data["price"]),
            deal_label=deal_label,
            url=url,
        ))
    return offers


SCRAPERS = {
    "Checkers": lambda page, query, address: scrape_checkers(page, query, address),
    "Pick n Pay": lambda page, query, address: scrape_pnp(page, query),
    "Woolworths": lambda page, query, address: scrape_woolworths(page, query),
}


def main():
    parser = argparse.ArgumentParser(
        description="Compare a grocery product's price across Checkers, Pick n Pay, and Woolworths."
    )
    parser.add_argument("product", help='Product to search for, e.g. "Jacobs Gold Instant Coffee 200g"')
    parser.add_argument("--address", default="1 Sandton Drive, Sandton",
                         help="Delivery address for Checkers Sixty60 (affects price/availability)")
    parser.add_argument("--require", nargs="*", default=None,
                         help="Words that must all appear in the matched product name (default: every word in the product query)")
    parser.add_argument("--exclude", nargs="*", default=[],
                         help="Words that must NOT appear in the matched product name")
    args = parser.parse_args()

    require = args.require if args.require is not None else re.findall(r"\w+", normalize(args.product))

    matches: list[Offer] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        for retailer, scraper in SCRAPERS.items():
            print(f"Checking {retailer} ...")
            page = browser.new_page(user_agent=USER_AGENT, viewport={"width": 1400, "height": 1200})
            try:
                offers = scraper(page, args.product, args.address)
                match = pick_best_match(offers, require, args.exclude)
                if match:
                    matches.append(match)
            except Exception as e:
                print(f"  {retailer} scrape failed: {e}", file=sys.stderr)
            finally:
                page.close()
        browser.close()

    print()
    print(f"Results for: {args.product}")
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


if __name__ == "__main__":
    main()
