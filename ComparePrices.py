"""
CLI for comparing a grocery product's price across Checkers Sixty60,
Pick n Pay, and Woolworths. Scraping/matching logic lives in comparer.py.

Usage:
    py ComparePrices.py
    py ComparePrices.py "Jacobs Gold Instant Coffee 200g"
    py ComparePrices.py "Jacobs Gold Instant Coffee 200g" --address "1 Sandton Drive, Sandton"
    py ComparePrices.py "Jacobs Gold Instant Coffee 200g" --require gold 200g --exclude refill kronung stick sachet decaf
    py ComparePrices.py "Joko Teabags 100 Pack" --debug

--debug saves a screenshot to ./debug/ whenever a store returns zero
offers, and prints the closest near-miss when offers were found but none
matched.
"""

import argparse
import asyncio
import re
import sys

from comparer import (
    SCRAPERS, extract_size, is_excluded, min_required_hits, normalize,
    pick_best_match, rank_sizes, run, word_hits,
)


def prompt(text: str, default: str | None = None) -> str | None:
    suffix = f" [{default}]" if default else ""
    try:
        value = input(f"{text}{suffix}: ").strip()
    except EOFError:
        value = ""
    return value or default


def print_comparison(
    label: str, all_offers: dict, require: list[str], exclude: list[str], debug: bool = False
) -> None:
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
            if debug:
                offers = all_offers.get(retailer, [])
                if not offers:
                    print("             [debug] scrape returned 0 offers — see ./debug/ for a screenshot")
                else:
                    candidates = [o for o in offers if not is_excluded(o.name, exclude)]
                    if not candidates:
                        print(f"             [debug] {len(offers)} offers found, all excluded by --exclude")
                    else:
                        needed = min_required_hits(require)
                        closest = max(candidates, key=lambda o: word_hits(o.name, require))
                        hits = word_hits(closest.name, require)
                        print(
                            f"             [debug] {len(offers)} offers found; closest match was "
                            f"\"{closest.name}\" ({hits}/{len(require)} words, needed {needed})"
                        )

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


def main():
    parser = argparse.ArgumentParser(
        description="Compare a grocery product's price across Checkers, Pick n Pay, and Woolworths."
    )
    parser.add_argument("product", nargs="?", default=None,
                         help="Product to search for. Prompted for if omitted.")
    parser.add_argument("--address", default=None,
                         help="Delivery address for Checkers Sixty60. Prompted for if omitted.")
    parser.add_argument("--require", nargs="*", default=None,
                         help="Words that must all appear in the matched product name (default: every word in the product query)")
    parser.add_argument("--exclude", nargs="*", default=[],
                         help="Words that must NOT appear in the matched product name")
    parser.add_argument("--debug", action="store_true",
                         help="Save a screenshot to ./debug/ whenever a store returns zero offers, "
                              "and print the closest near-miss when offers were found but none matched.")
    args = parser.parse_args()

    address = args.address or prompt("Delivery address (Checkers needs one to show real prices)")
    product = args.product or prompt("Product to search for")
    if not address:
        print("No delivery address entered.", file=sys.stderr)
        sys.exit(1)
    if not product:
        print("No product entered.", file=sys.stderr)
        sys.exit(1)

    require = args.require if args.require is not None else re.findall(r"\w+", normalize(product))
    exclude = args.exclude

    all_offers = asyncio.run(run(product, address, require, exclude, args.debug))

    if extract_size(product) is not None:
        print_comparison(product, all_offers, require, exclude, args.debug)
        return

    sizes = rank_sizes(all_offers, require, exclude, limit=2)
    if not sizes:
        print("\nNo pack size could be detected in the results — showing the single best match per store instead.")
        print_comparison(product, all_offers, require, exclude, args.debug)
        return

    print(f"\nNo size specified — comparing the {len(sizes)} most common pack size(s) found: {', '.join(sizes)}")
    for size in sizes:
        print_comparison(f"{product} ({size})", all_offers, require + [size], exclude, args.debug)


if __name__ == "__main__":
    main()
