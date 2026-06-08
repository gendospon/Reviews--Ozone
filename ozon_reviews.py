"""OZON review responder — fetches reviews and posts replies.

This script handles OZON API interactions only.
Response generation is done manually in conversation.

Usage:
    python ozon_reviews.py fetch [--limit N]          # fetch unprocessed reviews
    python ozon_reviews.py post <review_id> <reply>   # post a reply (dry-run)
    python ozon_reviews.py post --live <review_id> <reply>  # post a reply (live)
    python ozon_reviews.py mark-processed <review_id>  # mark review as processed
"""

import os
import sys
import json
import argparse
import logging
import time

# Fix Windows console encoding for Russian text and emojis
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import requests
from dotenv import load_dotenv

BASE_URL = "https://api-seller.ozon.ru"
REQUEST_DELAY = 1.0

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("ozon_reviews")


def load_config():
    load_dotenv()
    ozon_client_id = os.environ.get("OZON_CLIENT_ID", "").strip()
    ozon_api_key = os.environ.get("OZON_API_KEY", "").strip()

    missing = []
    if not ozon_client_id:
        missing.append("OZON_CLIENT_ID")
    if not ozon_api_key:
        missing.append("OZON_API_KEY")

    if missing:
        log.critical("Missing environment variables: %s", ", ".join(missing))
        sys.exit(1)

    headers = {
        "Client-Id": ozon_client_id,
        "Api-Key": ozon_api_key,
        "Content-Type": "application/json",
    }

    return {"headers": headers}


def ozon_post(path, payload, headers, retries=1):
    url = BASE_URL + path
    for attempt in range(1 + retries):
        try:
            resp = requests.post(url, json=payload, headers=headers, timeout=30)
        except requests.ConnectionError:
            log.critical("Network error connecting to %s", url)
            sys.exit(1)

        if resp.status_code in (401, 403):
            log.critical("OZON API HTTP %d — check Client-Id and Api-Key", resp.status_code)
            sys.exit(1)

        if resp.status_code == 429:
            if attempt < retries:
                log.warning("Rate limited (429), retrying in 5s...")
                time.sleep(5)
                continue
            log.error("Rate limited (429) after retry")
            sys.exit(1)

        if resp.status_code >= 500:
            if attempt < retries:
                log.warning("Server error (%d), retrying in 3s...", resp.status_code)
                time.sleep(3)
                continue
            log.error("Server error (%d) after retry", resp.status_code)
            sys.exit(1)

        if resp.status_code >= 400:
            log.error("OZON API HTTP %d: %s", resp.status_code, resp.text[:500])
            return None

        return resp.json()

    return None


def cmd_fetch(config, limit):
    all_reviews = []
    last_id = None
    has_next = True

    while has_next:
        remaining = limit - len(all_reviews) if limit else 100
        page_limit = max(20, min(remaining, 100))
        payload = {
            "limit": page_limit,
            "status": "UNPROCESSED",
            "sort_dir": "ASC",
        }
        if last_id is not None:
            payload["last_id"] = last_id

        data = ozon_post("/v1/review/list", payload, config["headers"])
        if data is None:
            break

        reviews = data.get("reviews", [])
        all_reviews.extend(reviews)
        last_id = data.get("last_id")
        has_next = data.get("has_next", False)

        if limit and len(all_reviews) >= limit:
            all_reviews = all_reviews[:limit]
            break

        if not reviews:
            break

        time.sleep(REQUEST_DELAY)

    if not all_reviews:
        print("Новых не отвеченных отзывов не найдено")
        return

    print("=" * 70)
    print("НЕОБРАБОТАННЫЕ ОТЗЫВЫ ({})".format(len(all_reviews)))
    print("=" * 70)

    for i, r in enumerate(all_reviews, 1):
        rating = r.get("rating", "?")
        text = r.get("text", "").strip() or "(без текста)"
        sku = r.get("sku", "?")
        rid = r.get("id", "?")
        photos = r.get("photos_amount", 0)
        videos = r.get("videos_amount", 0)
        comments = r.get("comments_amount", 0)

        print()
        print("--- Review {}/{} ---".format(i, len(all_reviews)))
        print("ID:      {}".format(rid))
        print("Rating:  {}/5".format(rating))
        print("SKU:     {}".format(sku))
        print("Text:    {}".format(text))
        print("Photos:  {} | Videos: {} | Comments: {}".format(photos, videos, comments))

    print()
    print("=" * 70)
    print("Всего: {} необработанных отзывов".format(len(all_reviews)))


def cmd_post(config, review_id, reply_text, live=False):
    mode = "LIVE" if live else "DRY-RUN"
    log.info("[%s] Review %s: %s", mode, review_id, reply_text)

    if not live:
        print("[DRY-RUN] Would post reply to review {}:".format(review_id))
        print('  "{}"'.format(reply_text))
        print()
        print("To actually post, run with --live flag.")
        return True

    payload = {
        "review_id": review_id,
        "text": reply_text,
        "mark_review_as_processed": True,
    }
    result = ozon_post("/v1/review/comment/create", payload, config["headers"])
    if result is None:
        print("ERROR: Failed to post reply to review {}".format(review_id))
        return False

    status_payload = {
        "review_ids": [review_id],
        "status": "PROCESSED",
    }
    ozon_post("/v1/review/change-status", status_payload, config["headers"])

    print("[LIVE] Posted reply to review {}".format(review_id))
    return True


def cmd_mark_processed(config, review_id):
    payload = {
        "review_ids": [review_id],
        "status": "PROCESSED",
    }
    result = ozon_post("/v1/review/change-status", payload, config["headers"])
    if result is None:
        print("ERROR: Failed to mark review {} as processed".format(review_id))
        return False

    print("Marked review {} as PROCESSED".format(review_id))
    return True


def main():
    parser = argparse.ArgumentParser(
        description="OZON review responder — fetch reviews and post replies"
    )
    subparsers = parser.add_subparsers(dest="command")

    # fetch
    fetch_parser = subparsers.add_parser("fetch", help="Fetch unprocessed reviews")
    fetch_parser.add_argument("--limit", type=int, default=50, help="Max reviews to fetch (default: 50)")

    # post
    post_parser = subparsers.add_parser("post", help="Post a reply to a review")
    post_parser.add_argument("--live", action="store_true", help="Actually post (default: dry-run)")
    post_parser.add_argument("review_id", help="Review ID")
    post_parser.add_argument("reply", help="Reply text")

    # mark-processed
    mark_parser = subparsers.add_parser("mark-processed", help="Mark review as processed")
    mark_parser.add_argument("review_id", help="Review ID")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    config = load_config()

    if args.command == "fetch":
        cmd_fetch(config, args.limit)
    elif args.command == "post":
        cmd_post(config, args.review_id, args.reply, live=args.live)
    elif args.command == "mark-processed":
        cmd_mark_processed(config, args.review_id)


if __name__ == "__main__":
    main()