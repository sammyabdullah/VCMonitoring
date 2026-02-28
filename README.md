# VCMonitoring — Product Hunt CSV Exporter

Paginates every post on Product Hunt via the GraphQL API and writes a CSV with:

| Column | Description |
|---|---|
| `name` | Product name |
| `makers` | Semicolon-separated maker names |
| `website` | Product website URL |
| `launch_date` | Featured date (falls back to created date) |
| `tagline` | Short tagline |
| `featured_at` | Date the post was featured on PH |

## Setup

```bash
pip install -r requirements.txt
export PRODUCT_HUNT_TOKEN="<your_developer_token>"
```

Get a token at <https://www.producthunt.com/v2/oauth/applications> → create an app → copy the **Developer Token**.

## Usage

```bash
# Export all posts (can take a while — ~700k+ posts exist)
python product_hunt_export.py

# Custom output file
python product_hunt_export.py --output ph_export.csv

# Export only the first 1000 posts
python product_hunt_export.py --limit 1000

# Tune page size and request delay
python product_hunt_export.py --first 50 --delay 0.5

# Change sort order (NEWEST | FEATURED_AT | RANKING | VOTES)
python product_hunt_export.py --order FEATURED_AT
```

### All options

```
--output / -o   Output CSV path         (default: product_hunt_posts.csv)
--token  / -t   API token               (default: $PRODUCT_HUNT_TOKEN)
--first         Posts per page (1-50)   (default: 20)
--delay         Seconds between pages   (default: 1.0)
--order         Sort order              (default: NEWEST)
--limit         Max posts to fetch      (default: 0 = unlimited)
```
