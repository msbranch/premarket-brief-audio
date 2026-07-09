#!/usr/bin/env python3
"""Update an existing Transistor episode in place to the current show-notes template.

Retrofits already-published episodes: rebuilds the description (written date + Ghost URL),
blanks the summary, and sets keywords — via PATCH, so no duplicate episode is created.

CLI:
  --episode-id  Transistor episode id to update (required)
  --title       episode title (required)
  --date        brief date, e.g. "July 9, 2026" (required)
  --ghost-url   full URL of the published Ghost post (required)
  --keywords    comma-separated keywords (optional)

Env: TRANSISTOR_API_KEY (required). Dependencies: requests.
"""

import os
import sys
import json
import argparse

import requests

from episode_description import build_description

API_BASE = "https://api.transistor.fm/v1"


def die(msg):
    print(f"::error::{msg}")
    sys.exit(1)


def main():
    ap = argparse.ArgumentParser(description="Update a Transistor episode in place.")
    ap.add_argument("--episode-id", required=True, help="Transistor episode id")
    ap.add_argument("--title", required=True, help="episode title")
    ap.add_argument("--date", required=True, help="brief date, e.g. 'July 9, 2026'")
    ap.add_argument("--ghost-url", required=True, help="Ghost post URL (goes in alternate_url)")
    ap.add_argument("--keywords", required=False, default="", help="comma-separated keywords")
    ap.add_argument("--excerpt", required=False, default="", help="post excerpt for the description")
    args = ap.parse_args()

    api_key = (os.environ.get("TRANSISTOR_API_KEY") or "").strip()
    if not api_key:
        die("TRANSISTOR_API_KEY is not set")

    episode = {
        "title": args.title,
        "description": build_description(args.date, args.excerpt),
        "summary": "",                       # explicitly blank the summary
        "alternate_url": args.ghost_url,     # Ghost post URL lives here, not in the description
    }
    if args.keywords.strip():
        episode["keywords"] = args.keywords.strip()

    r = requests.patch(
        f"{API_BASE}/episodes/{args.episode_id}",
        headers={"x-api-key": api_key, "Content-Type": "application/json"},
        data=json.dumps({"episode": episode}),
        timeout=60,
    )
    if not (200 <= r.status_code < 300):
        die(f"update episode {args.episode_id} returned HTTP {r.status_code}: {r.text}")

    attrs = r.json()["data"].get("attributes", {})
    print(f"updated episode {args.episode_id}: status={attrs.get('status')!r} "
          f"summary={attrs.get('summary')!r} keywords={attrs.get('keywords')!r}")
    print(f"  alternate_url={attrs.get('alternate_url')!r}")
    print(f"  description={attrs.get('description')!r}")


if __name__ == "__main__":
    main()
