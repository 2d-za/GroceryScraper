"""
Compares the price of a grocery product between Checkers (checkers.co.za)
and Pick n Pay (pnp.co.za) by driving a real headless browser, since both
sites render search results client-side and Checkers sits behind AWS WAF
bot protection that plain HTTP requests can't get past.

Usage:
    python ComparePrices.py "Jacobs Gold Instant Coffee 200g"

Requires: pip install playwright && playwright install chromium
"""

import argparse
import re
import sys

from playwright.sync_api import sync_playwright

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

PRICE_RE = re.compile(r"R\s?\d[\d,]*\.\d{2}")


def clean_price(text: str) -> float | None:
    # Some sites render "R204" and ".99" as separate text nodes, so strip
    # whitespace between digits/decimal rather than just collapsing it.
    flattened = re.sub(r"\s+", "", text.replace("\xa0", " "))
    match = PRICE_RE.search(flattened)
    if not match:
        return None
    return float(match.group(0).replace("R", "").replace(",", "").strip())


def matches_product(name: str, terms: list[str], exclude: list[str]) -> bool:
    lowered = name.lower()
    return all(t in lowered for t in terms) and not any(x in lowered for x in exclude)


def scrape_checkers(page, query: str, terms: list[str], exclude: list[str]) -> list[dict]:
    url = f"https://www.checkers.co.za/search?Search={query.replace(' ', '%20')}"
    page.goto(url, timeout=30000, wait_until="domcontentloaded")
    page.wait_for_timeout(4000)

    results = []
    links = page.locator("a[data-testid*='product-card-link']")
    for i in range(links.count()):
        link = links.nth(i)
        name = link.get_attribute("aria-label") or ""
        if not matches_product(name, terms, exclude):
            continue
        href = link.get_attribute("href") or ""
        card_text = link.evaluate(
            "el => el.closest('[class*=product-card]')?.innerText || ''"
        )
        price = clean_price(card_text)
        if price is None:
            continue
        results.append({
            "retailer": "Checkers",
            "name": name,
            "price": price,
            "url": f"https://www.checkers.co.za{href}" if href.startswith("/") else href,
        })
    return results


def scrape_pnp(page, query: str, terms: list[str], exclude: list[str]) -> list[dict]:
    url = f"https://www.pnp.co.za/search/{query.replace(' ', '%20')}"
    page.goto(url, timeout=30000, wait_until="domcontentloaded")
    page.wait_for_timeout(4000)

    results = []
    links = page.locator("a.product-grid-item__info-container__name")
    for i in range(links.count()):
        link = links.nth(i)
        name = (link.inner_text() or "").strip()
        if not matches_product(name, terms, exclude):
            continue
        href = link.get_attribute("href") or ""
        card = link.evaluate(
            "el => el.closest('.product-grid-item')?.innerText "
            "|| el.closest('[class*=product-grid-item]')?.innerText || ''"
        )
        flattened = re.sub(r"\s+", "", card)
        prices = [clean_price(p) for p in PRICE_RE.findall(flattened)]
        prices = [p for p in prices if p is not None]
        if not prices:
            continue
        # The lower of the listed prices is the one actually charged
        # (promotional / Smart Shopper price undercuts the list price).
        price = min(prices)
        results.append({
            "retailer": "Pick n Pay",
            "name": name,
            "price": price,
            "url": f"https://www.pnp.co.za{href}" if href.startswith("/") else href,
        })
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description="Compare a product's price between Checkers and Pick n Pay.")
    parser.add_argument("product", help='Product to search for, e.g. "Jacobs Gold Instant Coffee 200g"')
    args = parser.parse_args()

    query = args.product
    # Loose matching: require every significant word, ignore pack-size/variant noise.
    words = [w.lower() for w in re.findall(r"[A-Za-z0-9]+", query)]
    terms = [w for w in words if w not in ("instant", "coffee")]
    exclude = ["kronung", "decaf", "stick", "refill", "6 x", "x 6"]

    all_results = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        for scraper in (scrape_checkers, scrape_pnp):
            page = browser.new_page(user_agent=USER_AGENT)
            try:
                all_results.extend(scraper(page, query, terms, exclude))
            except Exception as error:
                print(f"Warning: {scraper.__name__} failed: {error}", file=sys.stderr)
            finally:
                page.close()
        browser.close()

    if not all_results:
        print("No matching products found on either site.", file=sys.stderr)
        sys.exit(1)

    all_results.sort(key=lambda r: r["price"])
    print(f"\nResults for '{query}':\n")
    for r in all_results:
        print(f"{r['retailer']:<12} R{r['price']:>7.2f}   {r['name']}")
        print(f"{'':<12} {r['url']}")
    print()

    cheapest = all_results[0]
    print(f"Cheapest: {cheapest['retailer']} at R{cheapest['price']:.2f}")


if __name__ == "__main__":
    main()
