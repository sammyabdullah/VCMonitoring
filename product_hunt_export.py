#!/usr/bin/env python3
"""
Product Hunt CSV Exporter
─────────────────────────
Paginates through every Product Hunt post via the GraphQL API and writes
product name, maker(s), website, and launch date to a CSV file.

Usage
-----
    # Set your API token first:
    export PRODUCT_HUNT_TOKEN="your_developer_token_here"

    python product_hunt_export.py
    python product_hunt_export.py --output my_export.csv
    python product_hunt_export.py --first 50 --delay 1.2

    # Run as 20 automatic date-range batches (recommended for full export):
    python product_hunt_export.py --batches 20

    # Or specify a single date window manually:
    python product_hunt_export.py --start-date 2020-01-01 --end-date 2022-12-31

How to get a token
------------------
1. Go to https://www.producthunt.com/v2/oauth/applications
2. Create a developer application.
3. Use the "Developer Token" (no OAuth flow required for read-only data).
"""

import argparse
import csv
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urljoin

import requests

# ── Constants ────────────────────────────────────────────────────────────────

API_URL = "https://api.producthunt.com/v2/api/graphql"
DEFAULT_OUTPUT = "product_hunt_posts.csv"
DEFAULT_PAGE_SIZE = 20   # Max the API reliably allows per page
DEFAULT_DELAY = 1.0      # Seconds between requests (be a good API citizen)
MAX_RETRIES = 5
PRODUCT_HUNT_EPOCH = date(2013, 11, 1)  # Product Hunt's founding date

CSV_HEADERS = ["name", "maker_first_name", "maker_last_name", "website", "launch_date", "tagline", "featured_at"]

_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36"
)
RESOLVE_WORKERS = 10
RESOLVE_TIMEOUT = 10

# ── GraphQL query ─────────────────────────────────────────────────────────────

POSTS_QUERY = """
query FetchPosts(
  $first: Int!
  $after: String
  $order: PostsOrder!
  $postedAfter: DateTime
  $postedBefore: DateTime
) {
  posts(
    first: $first
    after: $after
    order: $order
    postedAfter: $postedAfter
    postedBefore: $postedBefore
  ) {
    pageInfo {
      hasNextPage
      endCursor
    }
    edges {
      node {
        name
        tagline
        website
        createdAt
        featuredAt
        makers {
          name
        }
      }
    }
  }
}
"""

# ── Helpers ───────────────────────────────────────────────────────────────────


def _resolve_url(url: str) -> str:
    """Follow redirects on a PH tracking URL and return the final destination."""
    if not url or not url.strip():
        return url
    url = url.strip()
    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    if "producthunt.com/r/" in url:
        clean = requests.Session()
        clean.headers.update({"User-Agent": _BROWSER_UA})
        current = url
        for _ in range(10):
            try:
                resp = clean.get(current, timeout=RESOLVE_TIMEOUT, allow_redirects=False)
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
        # Fell through without escaping PH — return whatever we last landed on
        return current

    return url


def _resolve_rows(rows: list[dict]) -> list[dict]:
    """Resolve website URLs for a batch of rows in parallel."""
    urls = [r["website"] for r in rows]
    resolved = [None] * len(urls)
    with ThreadPoolExecutor(max_workers=RESOLVE_WORKERS) as pool:
        futures = {pool.submit(_resolve_url, url): i for i, url in enumerate(urls)}
        for future in as_completed(futures):
            resolved[futures[future]] = future.result()
    return [{**row, "website": resolved[i]} for i, row in enumerate(rows)]


def build_session(token: str) -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
    )
    return session


def fetch_page(
    session: requests.Session,
    page_size: int,
    cursor: str | None,
    order: str,
    posted_after: str | None = None,
    posted_before: str | None = None,
) -> dict:
    """Execute one GraphQL request with retry/back-off on transient errors.

    429 rate-limit responses are retried indefinitely with exponential backoff
    and do NOT count against MAX_RETRIES.  Only network errors and other HTTP
    failures consume retry attempts.
    """
    variables: dict = {"first": page_size, "after": cursor, "order": order}
    if posted_after:
        variables["postedAfter"] = posted_after
    if posted_before:
        variables["postedBefore"] = posted_before

    attempt = 0
    rate_limit_count = 0

    while True:
        try:
            resp = session.post(
                API_URL,
                json={"query": POSTS_QUERY, "variables": variables},
                timeout=30,
            )
        except requests.RequestException as exc:
            attempt += 1
            if attempt >= MAX_RETRIES:
                raise SystemExit(f"Network error after {MAX_RETRIES} attempts: {exc}") from exc
            wait = 2 ** attempt
            print(f"  [retry {attempt}/{MAX_RETRIES}] network error, waiting {wait}s …", file=sys.stderr)
            time.sleep(wait)
            continue

        if resp.status_code == 429:
            rate_limit_count += 1
            base_wait = int(resp.headers.get("Retry-After", 60))
            wait = min(base_wait * (2 ** (rate_limit_count - 1)), 900)  # cap at 15 min
            print(
                f"  [rate-limited #{rate_limit_count}] waiting {wait}s …",
                file=sys.stderr,
            )
            time.sleep(wait)
            continue

        if resp.status_code == 401:
            raise SystemExit(
                "Authentication failed (HTTP 401). "
                "Check that PRODUCT_HUNT_TOKEN is correct and not expired."
            )

        if not resp.ok:
            attempt += 1
            if attempt >= MAX_RETRIES:
                raise SystemExit(
                    f"API returned HTTP {resp.status_code} after {MAX_RETRIES} attempts.\n"
                    f"Body: {resp.text[:500]}"
                )
            wait = 2 ** attempt
            print(
                f"  [retry {attempt}/{MAX_RETRIES}] HTTP {resp.status_code}, waiting {wait}s …",
                file=sys.stderr,
            )
            time.sleep(wait)
            continue

        data = resp.json()
        if "errors" in data:
            errors = "; ".join(e.get("message", str(e)) for e in data["errors"])
            raise SystemExit(f"GraphQL error(s): {errors}")

        return data


def format_date(iso: str | None) -> str:
    if not iso:
        return ""
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return dt.strftime("%Y-%m-%d")
    except ValueError:
        return iso


def row_from_node(node: dict) -> dict:
    makers = node.get("makers") or []
    first_maker_name = makers[0]["name"] if makers else ""
    name_parts = first_maker_name.split(" ", 1)
    maker_first = name_parts[0] if name_parts else ""
    maker_last = name_parts[1] if len(name_parts) > 1 else ""

    launch_date = format_date(node.get("featuredAt") or node.get("createdAt"))
    featured_at = format_date(node.get("featuredAt"))

    return {
        "name": node.get("name", ""),
        "maker_first_name": maker_first,
        "maker_last_name": maker_last,
        "website": node.get("website", ""),
        "launch_date": launch_date,
        "tagline": node.get("tagline", ""),
        "featured_at": featured_at,
    }


def date_to_iso(d: date) -> str:
    """Convert a date to the ISO 8601 UTC string the API expects."""
    return datetime(d.year, d.month, d.day, tzinfo=timezone.utc).isoformat()


def make_batches(n: int, end: date) -> list[tuple[date, date]]:
    """Split [PRODUCT_HUNT_EPOCH, end] into n equal-width date windows."""
    total_days = (end - PRODUCT_HUNT_EPOCH).days
    batches = []
    for i in range(n):
        start = PRODUCT_HUNT_EPOCH + timedelta(days=round(i * total_days / n))
        stop  = PRODUCT_HUNT_EPOCH + timedelta(days=round((i + 1) * total_days / n))
        if i == n - 1:
            stop = end
        batches.append((start, stop))
    return batches


def fetch_window(
    session: requests.Session,
    args: argparse.Namespace,
    start: date,
    end: date,
    writer: csv.DictWriter,
    csv_file,
    resolve_urls: bool = True,
) -> int:
    """Paginate through one date window and write rows. Returns count written."""
    posted_after  = date_to_iso(start)
    posted_before = date_to_iso(end)
    cursor: str | None = None
    window_written = 0
    page_num = 0

    while True:
        page_num += 1
        print(f"    Page {page_num:>4}  (cursor={cursor!r}) … ", end="", flush=True)

        data = fetch_page(
            session, args.first, cursor, args.order,
            posted_after=posted_after, posted_before=posted_before,
        )
        posts_conn = data["data"]["posts"]
        edges      = posts_conn["edges"]
        page_info  = posts_conn["pageInfo"]

        if not edges:
            print("no results.")
            break

        rows = [row_from_node(e["node"]) for e in edges]

        if args.limit > 0:
            remaining = args.limit - window_written
            rows = rows[:remaining]

        if resolve_urls:
            rows = _resolve_rows(rows)

        writer.writerows(rows)
        csv_file.flush()

        window_written += len(rows)
        print(f"fetched {len(rows):>3}  (window total: {window_written})")

        if not page_info["hasNextPage"]:
            print("    Last page reached.")
            break

        if args.limit > 0 and window_written >= args.limit:
            break

        cursor = page_info["endCursor"]
        time.sleep(args.delay)

    return window_written


# ── Main ──────────────────────────────────────────────────────────────────────


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Export all Product Hunt posts to a CSV file.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--output", "-o", default=DEFAULT_OUTPUT,
                        help="Path for the output CSV file.")
    parser.add_argument("--token", "-t", default=os.environ.get("PRODUCT_HUNT_TOKEN", ""),
                        help="Product Hunt API developer token. Falls back to $PRODUCT_HUNT_TOKEN.")
    parser.add_argument("--first", type=int, default=DEFAULT_PAGE_SIZE,
                        choices=range(1, 51), metavar="1-50",
                        help="Posts fetched per API request (max 50).")
    parser.add_argument("--delay", type=float, default=DEFAULT_DELAY,
                        help="Seconds to wait between requests.")
    parser.add_argument("--order", default="NEWEST",
                        choices=["NEWEST", "FEATURED_AT", "RANKING", "VOTES"],
                        help="Sort order for iterating posts.")
    parser.add_argument("--limit", type=int, default=0,
                        help="Stop after this many posts per window (0 = no limit).")
    parser.add_argument("--no-resolve-urls", action="store_true", default=False,
                        help="Skip resolving PH redirect URLs; write raw API URLs instead.")

    # Date range options
    date_group = parser.add_argument_group("date range (pick one approach)")
    date_group.add_argument("--batches", type=int, default=0, metavar="N",
                            help="Auto-split the full Product Hunt history into N date-range batches "
                                 "and run them all into one output file (e.g. --batches 20).")
    date_group.add_argument("--start-date", default=None, metavar="YYYY-MM-DD",
                            help="Fetch posts on or after this date (single window).")
    date_group.add_argument("--end-date", default=None, metavar="YYYY-MM-DD",
                            help="Fetch posts before this date (single window).")

    return parser.parse_args()


def main() -> None:
    args = parse_args()

    if not args.token:
        raise SystemExit(
            "No API token found.\n"
            "Set the PRODUCT_HUNT_TOKEN environment variable or use --token."
        )

    today = date.today()

    # Build list of (start, end) windows to process
    if args.batches > 0:
        windows = make_batches(args.batches, today)
        print(f"Running {args.batches} batches from {PRODUCT_HUNT_EPOCH} to {today}")
    else:
        start = date.fromisoformat(args.start_date) if args.start_date else PRODUCT_HUNT_EPOCH
        end   = date.fromisoformat(args.end_date)   if args.end_date   else today
        windows = [(start, end)]

    resolve_urls = not args.no_resolve_urls

    print(f"Output file   : {args.output}")
    print(f"Page size     : {args.first}")
    print(f"Order         : {args.order}")
    print(f"Resolve URLs  : {resolve_urls}")
    print()

    session = build_session(args.token)
    grand_total = 0

    with open(args.output, "w", newline="", encoding="utf-8") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=CSV_HEADERS)
        writer.writeheader()

        for i, (start, end) in enumerate(windows, 1):
            label = f"Batch {i}/{len(windows)}" if len(windows) > 1 else "Fetching"
            print(f"── {label}: {start} → {end} ──")
            count = fetch_window(session, args, start, end, writer, csv_file, resolve_urls=resolve_urls)
            grand_total += count
            print(f"   Batch done: {count:,} posts  (running total: {grand_total:,})\n")

    print(f"Done. {grand_total:,} posts written to '{args.output}'.")


if __name__ == "__main__":
    main()
