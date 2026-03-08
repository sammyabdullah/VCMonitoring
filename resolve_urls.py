#!/usr/bin/env python3
"""
resolve_urls.py
───────────────
Reads a CSV containing URLs, follows redirects for each one, and writes
the final destination URL to a new CSV.

Usage:
    python resolve_urls.py --input product_hunt_posts.csv --url-column website_url
    python resolve_urls.py --input links.csv --url-column url --output resolved.csv --workers 20
"""

import argparse
import csv
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urljoin

import requests

# ── defaults ──────────────────────────────────────────────────────────────────
DEFAULT_WORKERS  = 10   # parallel threads
DEFAULT_TIMEOUT  = 10   # seconds per request
DEFAULT_OUTPUT   = "resolved_urls.csv"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    )
}


# ── core logic ─────────────────────────────────────────────────────────────────
def resolve(url: str, timeout: int, session: requests.Session) -> str:
    """Follow redirects and return the final URL. Returns empty string on error."""
    if not url or not url.strip():
        return ""
    url = url.strip()
    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    # Product Hunt redirect URLs (producthunt.com/r/...) use a multi-hop chain
    # and block shared-session requests via cookie fingerprinting.
    # Walk the chain hop-by-hop with a clean session until we land off PH.
    if "producthunt.com/r/" in url:
        clean = requests.Session()
        clean.headers.update(HEADERS)
        current = url
        for _ in range(10):
            try:
                resp = clean.get(current, timeout=timeout, allow_redirects=False)
            except Exception:
                break
            location = resp.headers.get("Location", "")
            if not location:
                break
            if not location.startswith("http"):
                location = urljoin(current, location)
            if "producthunt.com" not in location:
                return location
            current = location
        # fall through to normal resolution if chain didn't escape PH

    try:
        resp = session.get(url, timeout=timeout, allow_redirects=True)
        return resp.url
    except requests.exceptions.TooManyRedirects:
        return f"ERROR: too many redirects ({url})"
    except requests.exceptions.Timeout:
        return f"ERROR: timeout ({url})"
    except requests.exceptions.ConnectionError:
        return f"ERROR: connection failed ({url})"
    except Exception as e:
        return f"ERROR: {e}"


def detect_url_column(fieldnames: list[str], hint: str | None) -> str:
    """Pick the URL column automatically or validate the user's hint."""
    if hint:
        if hint in fieldnames:
            return hint
        raise SystemExit(f"Column '{hint}' not found. Available columns: {fieldnames}")

    candidates = [c for c in fieldnames if "url" in c.lower() or "link" in c.lower() or "website" in c.lower()]
    if len(candidates) == 1:
        print(f"Auto-detected URL column: '{candidates[0]}'")
        return candidates[0]
    if len(candidates) > 1:
        print(f"Multiple URL-like columns found: {candidates}")
        print(f"Using '{candidates[0]}'. Use --url-column to override.")
        return candidates[0]
    raise SystemExit(
        f"Could not detect a URL column. Pass --url-column with one of: {fieldnames}"
    )


# ── main ───────────────────────────────────────────────────────────────────────
def main() -> None:
    parser = argparse.ArgumentParser(description="Resolve redirect URLs in a CSV.")
    parser.add_argument("--input",      required=True,               help="Input CSV file path")
    parser.add_argument("--output",     default=DEFAULT_OUTPUT,      help=f"Output CSV file path (default: {DEFAULT_OUTPUT})")
    parser.add_argument("--url-column", default=None,                help="Column name containing URLs (auto-detected if omitted)")
    parser.add_argument("--workers",    type=int, default=DEFAULT_WORKERS, help=f"Parallel threads (default: {DEFAULT_WORKERS})")
    parser.add_argument("--timeout",    type=int, default=DEFAULT_TIMEOUT, help=f"Request timeout in seconds (default: {DEFAULT_TIMEOUT})")
    args = parser.parse_args()

    # ── read input ────────────────────────────────────────────────────────────
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
    total   = len(rows)
    print(f"Resolving {total:,} URLs using {args.workers} threads …\n")

    # ── resolve in parallel ───────────────────────────────────────────────────
    results   = [""] * total
    done      = 0
    start     = time.time()
    errors    = 0

    session = requests.Session()
    session.headers.update(HEADERS)
    session.max_redirects = 10

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        future_to_idx = {
            pool.submit(resolve, rows[i][url_col], args.timeout, session): i
            for i in range(total)
        }
        for future in as_completed(future_to_idx):
            idx            = future_to_idx[future]
            resolved       = future.result()
            results[idx]   = resolved
            done          += 1
            if resolved.startswith("ERROR"):
                errors += 1

            # progress every 100 rows
            if done % 100 == 0 or done == total:
                elapsed = time.time() - start
                rate    = done / elapsed if elapsed else 0
                eta     = (total - done) / rate if rate else 0
                print(
                    f"  {done:>6,}/{total:,}  "
                    f"errors: {errors}  "
                    f"rate: {rate:.0f}/s  "
                    f"ETA: {eta:.0f}s",
                    end="\r",
                )

    print(f"\n\nDone. {errors} errors out of {total:,} URLs.")

    # ── write output ──────────────────────────────────────────────────────────
    out_fields = list(fieldnames) + ["resolved_url"]
    try:
        with open(args.output, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=out_fields)
            writer.writeheader()
            for row, resolved in zip(rows, results):
                writer.writerow({**row, "resolved_url": resolved})
    except PermissionError:
        raise SystemExit(
            f"Permission denied writing to '{args.output}'. "
            "Close the file if it's open in Excel and try again."
        )

    print(f"Output saved to: {args.output}")


if __name__ == "__main__":
    main()
