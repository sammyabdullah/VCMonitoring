#!/usr/bin/env python3
"""
resolve_urls_playwright.py
──────────────────────────
Uses a real Chromium browser (via Playwright) to follow every redirect —
including JavaScript-based ones — and records the final landing URL.

Setup (run once):
    pip install playwright
    playwright install chromium

Usage:
    python resolve_urls_playwright.py --input product_hunt_posts.csv
    python resolve_urls_playwright.py --input links.csv --url-column url --output resolved.csv
"""

import argparse
import asyncio
import csv
import sys
import time

try:
    from playwright.async_api import async_playwright, TimeoutError as PwTimeout
except ImportError:
    sys.exit(
        "Playwright is not installed.\n"
        "Run:  pip install playwright && playwright install chromium"
    )

# ── defaults ──────────────────────────────────────────────────────────────────
DEFAULT_OUTPUT  = "resolved_urls.csv"
DEFAULT_TIMEOUT = 15_000   # milliseconds (15 s per page)


# ── core logic ─────────────────────────────────────────────────────────────────
async def resolve_all(urls: list[str], timeout_ms: int) -> list[str]:
    """Open each URL in a single browser instance and return final URLs."""
    results = [""] * len(urls)

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0.0.0 Safari/537.36"
            )
        )

        for i, url in enumerate(urls):
            if not url or not url.strip():
                results[i] = ""
                continue

            url = url.strip()
            if not url.startswith(("http://", "https://")):
                url = "https://" + url

            page = await context.new_page()
            try:
                await page.goto(url, timeout=timeout_ms, wait_until="networkidle")
                results[i] = page.url
            except PwTimeout:
                # networkidle timed out — grab wherever we landed
                results[i] = page.url if page.url != "about:blank" else f"ERROR: timeout ({url})"
            except Exception as e:
                results[i] = f"ERROR: {e}"
            finally:
                await page.close()

            # progress
            done = i + 1
            total = len(urls)
            bar = f"  {done:>6,}/{total:,}"
            print(bar, end="\r", flush=True)

        await context.close()
        await browser.close()

    return results


# ── helpers ────────────────────────────────────────────────────────────────────
def detect_url_column(fieldnames: list[str], hint: str | None) -> str:
    if hint:
        if hint in fieldnames:
            return hint
        raise SystemExit(f"Column '{hint}' not found. Available: {fieldnames}")

    candidates = [c for c in fieldnames if any(k in c.lower() for k in ("url", "link", "website"))]
    if len(candidates) == 1:
        print(f"Auto-detected URL column: '{candidates[0]}'")
        return candidates[0]
    if len(candidates) > 1:
        print(f"Multiple URL-like columns: {candidates}. Using '{candidates[0]}'.")
        return candidates[0]
    raise SystemExit(f"No URL column found. Pass --url-column with one of: {fieldnames}")


# ── main ───────────────────────────────────────────────────────────────────────
def main() -> None:
    parser = argparse.ArgumentParser(description="Resolve redirect URLs using a real browser.")
    parser.add_argument("--input",      required=True,              help="Input CSV file path")
    parser.add_argument("--output",     default=DEFAULT_OUTPUT,     help=f"Output CSV (default: {DEFAULT_OUTPUT})")
    parser.add_argument("--url-column", default=None,               help="Column containing URLs (auto-detected if omitted)")
    parser.add_argument("--timeout",    type=int, default=15,       help="Seconds to wait per page (default: 15)")
    args = parser.parse_args()

    try:
        with open(args.input, newline="", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            fieldnames = reader.fieldnames or []
            rows = list(reader)
    except FileNotFoundError:
        raise SystemExit(f"Input file not found: {args.input}")

    if not rows:
        raise SystemExit("Input CSV is empty.")

    url_col = detect_url_column(list(fieldnames), args.url_column)
    urls    = [row[url_col] for row in rows]
    total   = len(urls)

    print(f"Resolving {total:,} URLs with a real browser (timeout: {args.timeout}s each) …\n")
    start = time.time()

    resolved = asyncio.run(resolve_all(urls, timeout_ms=args.timeout * 1000))

    elapsed = time.time() - start
    errors  = sum(1 for r in resolved if r.startswith("ERROR"))
    print(f"\n\nDone in {elapsed:.0f}s. {errors} errors out of {total:,} URLs.")

    out_fields = list(fieldnames) + ["resolved_url"]
    try:
        with open(args.output, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=out_fields)
            writer.writeheader()
            for row, result in zip(rows, resolved):
                writer.writerow({**row, "resolved_url": result})
    except PermissionError:
        raise SystemExit(f"Permission denied writing '{args.output}'. Close it if open in Excel.")

    print(f"Output saved to: {args.output}")


if __name__ == "__main__":
    main()
