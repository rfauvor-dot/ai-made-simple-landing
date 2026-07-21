import json
import logging
import os
import random
import sys
from datetime import datetime, timedelta, timezone

import requests
from dotenv import load_dotenv

load_dotenv()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
POSTS_FILE = os.path.join(BASE_DIR, "facebook_posts.json")
HISTORY_FILE = os.path.join(BASE_DIR, "facebook_post_history.json")
LOG_FILE = os.path.join(BASE_DIR, "facebook_poster_log.txt")

NO_REPEAT_WINDOW = timedelta(days=7)
GRAPH_API_VERSION = "v21.0"

FB_PAGE_ID = os.environ.get("FB_PAGE_ID")
FB_PAGE_ACCESS_TOKEN = os.environ.get("FB_PAGE_ACCESS_TOKEN")

logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)


def load_posts():
    with open(POSTS_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def load_history():
    if not os.path.exists(HISTORY_FILE):
        return {}
    with open(HISTORY_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_history(history):
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(history, f, indent=2)


def pick_post(posts, history):
    now = datetime.now(timezone.utc)
    enabled = [p for p in posts if p.get("enabled", True)]
    if not enabled:
        raise RuntimeError("No enabled posts in facebook_posts.json")

    eligible = []
    for p in enabled:
        last_used = history.get(str(p["id"]))
        if last_used is None or now - datetime.fromisoformat(last_used) >= NO_REPEAT_WINDOW:
            eligible.append(p)

    if not eligible:
        # Small library + frequent posting can exhaust the 7-day window --
        # fall back to the least-recently-used post rather than skip a run.
        logging.warning("All enabled posts used within 7 days; falling back to least-recently-used")
        eligible = [min(enabled, key=lambda p: history.get(str(p["id"]), ""))]

    return random.choice(eligible)


def post_to_facebook(message):
    url = f"https://graph.facebook.com/{GRAPH_API_VERSION}/{FB_PAGE_ID}/feed"
    resp = requests.post(
        url,
        data={"message": message, "access_token": FB_PAGE_ACCESS_TOKEN},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def main():
    if not FB_PAGE_ID or not FB_PAGE_ACCESS_TOKEN:
        logging.error("FB_PAGE_ID or FB_PAGE_ACCESS_TOKEN not set in environment")
        sys.exit(1)

    posts = load_posts()
    history = load_history()

    post = pick_post(posts, history)
    variation = random.choice(post["variations"])

    try:
        result = post_to_facebook(variation)
    except requests.RequestException as exc:
        logging.error("Post failed for id=%s: %s", post["id"], exc)
        sys.exit(1)

    history[str(post["id"])] = datetime.now(timezone.utc).isoformat()
    save_history(history)

    logging.info(
        "Posted id=%s category=%s fb_post_id=%s",
        post["id"], post.get("category"), result.get("id"),
    )
    print(f"Posted post #{post['id']} ({post.get('category')}) -- Facebook post id: {result.get('id')}")


if __name__ == "__main__":
    main()
