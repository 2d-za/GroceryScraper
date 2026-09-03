"""
Compares the price of a grocery product between Checkers Sixty60
(checkers.co.za) and Pick n Pay (pnp.co.za).

Both sites render their catalogs client-side and gate real prices behind a
delivery location, so this uses Playwright (a real headless browser) rather
than plain HTTP requests:
  - Checkers requires a delivery address to be set before it shows prices.
  - Pick n Pay shows a "delivery details" prompt that can be dismissed with
    "Do this later" to browse at default/national pricing.

Usage:
    py ComparePrices.py "Jacobs Gold Instant Coffee 200g"
    py ComparePrices.py "Jacobs Gold Instant Coffee 200g" --address "1 Sandton Drive, Sandton"
    py ComparePrices.py "Jacobs Gold Instant Coffee 200g" --require gold 200g --exclude refill kronung stick sachet decaf
"""

import argparse
import re
import sys

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeoutError

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


def accept_cookies(page):
    for text in ["Accept", "Accept All", "I Accept"]:
        try:
            page.click(f"text={text}", timeout=3000)
            return
        except PWTimeoutError:
            continue


def pick_best_match(candidates, require, exclude):
    require = [r.lower() for r in require]
    exclude = [e.lower() for e in exclude]
    for name, price in candidates:
        low = name.lower()
        if all(r in low for r in require) and not any(e in low for e in exclude):
            return name, price
    return None


def scrape_checkers(page, query, address):
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

    cards = page.locator("a[data-testid$='-product-card-link']")
    count = cards.count()
    results = []
    for i in range(count):
        card = cards.nth(i)
        name = card.get_attribute("aria-label") or ""
        container = card.locator(
            "xpath=following-sibling::div[contains(@class,'product-card_container')]"
        )
        try:
            whole = container.locator("[class*='price-display_full']").first.inner_text(timeout=1000)
            cents = container.locator("[class*='price-display_half']").first.inner_text(timeout=1000)
            price = f"{whole}{cents}"
        except PWTimeoutError:
            price = None
        if name and price:
            results.append((name.strip(), price.strip()))
    return results


def scrape_pnp(page, query):
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
    count = cards.count()
    results = []
    for i in range(count):
        card = cards.nth(i)
        name = card.get_attribute("aria-label") or ""
        desc = card.get_attribute("aria-description") or ""
        m = re.search(r"([\d.]+)\s*rands", desc)
        price = f"R{m.group(1)}" if m else None
        if name and price:
            results.append((name.strip(), price.strip()))
    return results


def main():
    parser = argparse.ArgumentParser(description="Compare a grocery product's price between Checkers and Pick n Pay.")
    parser.add_argument("product", help='Product to search for, e.g. "Jacobs Gold Instant Coffee 200g"')
    parser.add_argument("--address", default="1 Sandton Drive, Sandton",
                         help="Delivery address for Checkers Sixty60 (affects price/availability)")
    parser.add_argument("--require", nargs="*", default=None,
                         help="Words that must all appear in the matched product name (default: every word in the product query)")
    parser.add_argument("--exclude", nargs="*", default=[],
                         help="Words that must NOT appear in the matched product name")
    args = parser.parse_args()

    require = args.require if args.require is not None else [w.lower() for w in re.findall(r"\w+", args.product)]

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)

        print(f"Checking Checkers Sixty60 (delivering to: {args.address}) ...")
        page = browser.new_page(user_agent=USER_AGENT, viewport={"width": 1400, "height": 1200})
        try:
            checkers_results = scrape_checkers(page, args.product, args.address)
        except Exception as e:
            print(f"  Checkers scrape failed: {e}", file=sys.stderr)
            checkers_results = []
        page.close()

        print("Checking Pick n Pay ...")
        page = browser.new_page(user_agent=USER_AGENT, viewport={"width": 1400, "height": 1200})
        try:
            pnp_results = scrape_pnp(page, args.product)
        except Exception as e:
            print(f"  Pick n Pay scrape failed: {e}", file=sys.stderr)
            pnp_results = []
        page.close()

        browser.close()

    checkers_match = pick_best_match(checkers_results, require, args.exclude)
    pnp_match = pick_best_match(pnp_results, require, args.exclude)

    print()
    print(f"Results for: {args.product}")
    print("-" * 60)
    if checkers_match:
        print(f"Checkers (Sixty60): {checkers_match[1]}  —  {checkers_match[0]}")
    else:
        print("Checkers (Sixty60): no confident match found")
    if pnp_match:
        print(f"Pick n Pay:         {pnp_match[1]}  —  {pnp_match[0]}")
    else:
        print("Pick n Pay:         no confident match found")

    if checkers_match and pnp_match:
        def to_float(p):
            return float(p.replace("R", "").replace(",", "").strip())
        c_price, p_price = to_float(checkers_match[1]), to_float(pnp_match[1])
        diff = abs(c_price - p_price)
        cheaper = "Checkers" if c_price < p_price else ("Pick n Pay" if p_price < c_price else "Neither (tied)")
        print("-" * 60)
        print(f"Cheaper: {cheaper} (by R{diff:.2f})" if diff else "Prices are equal")


if __name__ == "__main__":
    main()
