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
from datetime import datetime, timezone

import requests

# ── Constants ────────────────────────────────────────────────────────────────

API_URL = "https://api.producthunt.com/v2/api/graphql"
DEFAULT_OUTPUT = "product_hunt_posts.csv"
DEFAULT_PAGE_SIZE = 20   # Max the API reliably allows per page
DEFAULT_DELAY = 1.0      # Seconds between requests (be a good API citizen)
MAX_RETRIES = 5

CSV_HEADERS = ["name", "makers", "website", "launch_date", "tagline", "featured_at"]

# ── GraphQL query ─────────────────────────────────────────────────────────────

POSTS_QUERY = """
query FetchPosts($first: Int!, $after: String, $order: PostsOrder!) {
  posts(first: $first, after: $after, order: $order) {
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
) -> dict:
    """Execute one GraphQL request with retry/back-off on transient errors.

    429 rate-limit responses are retried indefinitely with exponential backoff
    and do NOT count against MAX_RETRIES.  Only network errors and other HTTP
    failures consume retry attempts.
    """
    variables = {"first": page_size, "after": cursor, "order": order}
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
            # Rate-limited: exponential backoff, does not consume attempt budget.
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
    """Return a human-readable UTC date string, or empty string if None."""
    if not iso:
        return ""
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return dt.strftime("%Y-%m-%d")
    except ValueError:
        return iso


def row_from_node(node: dict) -> dict:
    makers = "; ".join(m["name"] for m in node.get("makers") or [])
    launch_date = format_date(node.get("featuredAt") or node.get("createdAt"))
    featured_at = format_date(node.get("featuredAt"))

    return {
        "name": node.get("name", ""),
        "makers": makers,
        "website": node.get("website", ""),
        "launch_date": launch_date,
        "tagline": node.get("tagline", ""),
        "featured_at": featured_at,
    }


# ── Main ──────────────────────────────────────────────────────────────────────


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Export all Product Hunt posts to a CSV file.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--output", "-o",
        default=DEFAULT_OUTPUT,
        help="Path for the output CSV file.",
    )
    parser.add_argument(
        "--token", "-t",
        default=os.environ.get("PRODUCT_HUNT_TOKEN", ""),
        help="Product Hunt API developer token. Falls back to $PRODUCT_HUNT_TOKEN.",
    )
    parser.add_argument(
        "--first",
        type=int,
        default=DEFAULT_PAGE_SIZE,
        choices=range(1, 51),
        metavar="1-50",
        help="Posts fetched per API request (max 50).",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=DEFAULT_DELAY,
        help="Seconds to wait between requests.",
    )
    parser.add_argument(
        "--order",
        default="NEWEST",
        choices=["NEWEST", "FEATURED_AT", "RANKING", "VOTES"],
        help="Sort order for iterating posts.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Stop after this many posts (0 = no limit, fetch all).",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    if not args.token:
        raise SystemExit(
            "No API token found.\n"
            "Set the PRODUCT_HUNT_TOKEN environment variable or use --token."
        )

    session = build_session(args.token)
    cursor: str | None = None
    total_written = 0
    page_num = 0

    print(f"Output file : {args.output}")
    print(f"Page size   : {args.first}")
    print(f"Order       : {args.order}")
    print(f"Limit       : {'unlimited' if args.limit == 0 else args.limit}")
    print()

    with open(args.output, "w", newline="", encoding="utf-8") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=CSV_HEADERS)
        writer.writeheader()

        while True:
            page_num += 1
            print(f"Page {page_num:>5}  (cursor={cursor!r}) … ", end="", flush=True)

            data = fetch_page(session, args.first, cursor, args.order)
            posts_conn = data["data"]["posts"]
            edges = posts_conn["edges"]
            page_info = posts_conn["pageInfo"]

            if not edges:
                print("no results, done.")
                break

            rows = [row_from_node(e["node"]) for e in edges]

            # Honour --limit
            if args.limit > 0:
                remaining = args.limit - total_written
                rows = rows[:remaining]

            writer.writerows(rows)
            csv_file.flush()

            total_written += len(rows)
            print(f"fetched {len(rows):>3} posts  (total: {total_written})")

            if not page_info["hasNextPage"]:
                print("Last page reached.")
                break

            if args.limit > 0 and total_written >= args.limit:
                print(f"Limit of {args.limit} posts reached.")
                break

            cursor = page_info["endCursor"]
            time.sleep(args.delay)

    print(f"\nDone. {total_written} posts written to '{args.output}'.")


if __name__ == "__main__":
    main()
