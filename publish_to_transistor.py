#!/usr/bin/env python3
"""Publish the generated Tape Read MP3 to Transistor.fm as a podcast episode.

Runs AFTER generate_audio.py has produced the MP3 and patched Ghost. Publishing
is four sequential Transistor API calls:

  1. GET   /v1/episodes/authorize_upload  -> a pre-signed S3 upload URL + audio_url key
  2. PUT   {upload_url}                    -> the raw MP3 bytes (URL is already authenticated)
  3. POST  /v1/episodes                    -> create the episode (created as a draft)
  4. PATCH /v1/episodes/{id}/publish       -> publish it (status=published)

CLI:
  --audio-file  path to the MP3 (required)
  --title       episode title, matches the Ghost post title (required)
  --date        brief date YYYY-MM-DD (required)
  --summary     one-sentence episode summary (required)
  --ghost-url   full URL of the published Ghost post (required)

Env:
  TRANSISTOR_API_KEY  (required)
  TRANSISTOR_SHOW_ID  (required)

Writes transistor_episode_id and transistor_share_url to $GITHUB_OUTPUT.

Dependencies: requests (everything else is stdlib).
"""

import os
import sys
import json
import argparse

import requests

API_BASE = "https://api.transistor.fm/v1"


def die(msg):
    """Print a clear error (also a GitHub Actions annotation) and exit non-zero."""
    print(f"::error::{msg}")
    sys.exit(1)


def check(resp, what):
    """Fail loudly on any non-2xx response, printing status code + full body."""
    if not (200 <= resp.status_code < 300):
        die(f"{what} returned HTTP {resp.status_code}: {resp.text}")


def main():
    ap = argparse.ArgumentParser(description="Publish an MP3 to Transistor.fm as a podcast episode.")
    ap.add_argument("--audio-file", required=True, help="path to the MP3 file")
    ap.add_argument("--title", required=True, help="episode title (matches the Ghost post title)")
    ap.add_argument("--date", required=True, help="brief date, YYYY-MM-DD")
    ap.add_argument("--summary", required=True, help="one-sentence episode summary")
    ap.add_argument("--ghost-url", required=True, help="full URL of the published Ghost post")
    args = ap.parse_args()

    api_key = os.environ.get("TRANSISTOR_API_KEY")
    if not api_key:
        die("TRANSISTOR_API_KEY is not set")
    show_id = os.environ.get("TRANSISTOR_SHOW_ID")
    if not show_id:
        die("TRANSISTOR_SHOW_ID is not set")

    audio_path = args.audio_file
    if not os.path.isfile(audio_path):
        die(f"audio file not found on disk: {audio_path}")

    filename = os.path.basename(audio_path)
    auth_headers = {"x-api-key": api_key}

    # --- Call 1: request a pre-signed upload URL -------------------------------
    # authorize_upload accepts ONLY `filename`. content_type goes on the Call-2 PUT header
    # and show_id on the Call-3 episode body; sending them here returns HTTP 400.
    r1 = requests.get(
        f"{API_BASE}/episodes/authorize_upload",
        headers=auth_headers,
        params={"filename": filename},
        timeout=60,
    )
    check(r1, "authorize_upload")
    attrs = r1.json()["data"]["attributes"]
    upload_url = attrs["upload_url"]
    audio_url = attrs["audio_url"]
    print(f"authorized upload for {filename}")

    # --- Call 2: PUT the MP3 to the pre-signed S3 URL (no x-api-key here) ------
    with open(audio_path, "rb") as fh:
        mp3_bytes = fh.read()
    r2 = requests.put(
        upload_url,
        headers={"Content-Type": "audio/mpeg"},
        data=mp3_bytes,
        timeout=300,
    )
    check(r2, "S3 upload (PUT)")
    print(f"uploaded {len(mp3_bytes)} bytes to Transistor storage")

    # --- Call 3: create the published episode record --------------------------
    description = (
        f"The Tape Read — Pre-Market Intelligence Brief for {args.date}. "
        f"Full written brief (paid subscribers): {args.ghost_url} | thetaperead.morganbranch.co"
    )
    # POST /v1/episodes creates a DRAFT — it does not accept `status`. Publishing is a
    # separate call (below).
    payload = {
        "episode": {
            "show_id": show_id,
            "title": args.title,
            "summary": args.summary,
            "description": description,
            "audio_url": audio_url,
            "explicit": "false",
        }
    }
    r3 = requests.post(
        f"{API_BASE}/episodes",
        headers={"x-api-key": api_key, "Content-Type": "application/json"},
        data=json.dumps(payload),
        timeout=60,
    )
    check(r3, "create episode")
    data = r3.json()["data"]
    episode_id = data["id"]
    print(f"created draft episode {episode_id}")

    # --- Call 4: publish the episode (status transitions live on a dedicated endpoint) --
    r4 = requests.patch(
        f"{API_BASE}/episodes/{episode_id}/publish",
        headers={"x-api-key": api_key, "Content-Type": "application/json"},
        data=json.dumps({"episode": {"status": "published"}}),
        timeout=60,
    )
    check(r4, "publish episode")
    share_url = r4.json()["data"]["attributes"].get("share_url") or data["attributes"].get("share_url", "")
    print(f"published episode {episode_id}: {share_url}")

    # --- expose results to the workflow --------------------------------------
    with open(os.environ.get("GITHUB_OUTPUT", "/dev/null"), "a") as fh:
        fh.write(f"transistor_episode_id={episode_id}\n")
        fh.write(f"transistor_share_url={share_url}\n")


if __name__ == "__main__":
    main()
