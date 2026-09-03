"""
Generic web scraper: fetches a page, extracts elements matching a CSS
selector, and saves the results as CSV or JSON.

Usage:
    python WebScraper.py <url> <selector> [--attr ATTR] [--out FILE] [--format csv|json]

Examples:
    python WebScraper.py https://example.com "h2.title" --out titles.csv
    python WebScraper.py https://example.com "a.link" --attr href --out links.json --format json
"""

import argparse
import csv
import json
import sys
import time

import requests
from bs4 import BeautifulSoup

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


def fetch_html(url: str, timeout: int = 15, retries: int = 3) -> str:
    headers = {"User-Agent": USER_AGENT}
    last_error = None
    for attempt in range(1, retries + 1):
        try:
            response = requests.get(url, headers=headers, timeout=timeout)
            response.raise_for_status()
            return response.text
        except requests.RequestException as error:
            last_error = error
            if attempt < retries:
                time.sleep(2 ** attempt)
    raise RuntimeError(f"Failed to fetch {url} after {retries} attempts: {last_error}")


def extract(html: str, selector: str, attr: str | None) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    elements = soup.select(selector)
    if attr:
        return [el.get(attr, "").strip() for el in elements if el.get(attr)]
    return [el.get_text(strip=True) for el in elements]


def save_csv(rows: list[str], out_path: str) -> None:
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["value"])
        for row in rows:
            writer.writerow([row])


def save_json(rows: list[str], out_path: str) -> None:
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, indent=2)


def main() -> None:
    parser = argparse.ArgumentParser(description="Scrape a webpage by CSS selector.")
    parser.add_argument("url", help="Page URL to scrape")
    parser.add_argument("selector", help="CSS selector for target elements")
    parser.add_argument("--attr", help="Extract this attribute instead of text (e.g. href, src)")
    parser.add_argument("--out", default="output.csv", help="Output file path")
    parser.add_argument("--format", choices=["csv", "json"], default="csv", help="Output format")
    args = parser.parse_args()

    print(f"Fetching {args.url} ...")
    html = fetch_html(args.url)

    print(f"Extracting elements matching '{args.selector}' ...")
    results = extract(html, args.selector, args.attr)

    if not results:
        print("No matching elements found. Check your selector and try again.", file=sys.stderr)
        sys.exit(1)

    if args.format == "csv":
        save_csv(results, args.out)
    else:
        save_json(results, args.out)

    print(f"Saved {len(results)} results to {args.out}")


if __name__ == "__main__":
    main()
