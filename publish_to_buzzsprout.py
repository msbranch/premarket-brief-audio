#!/usr/bin/env python3
"""Publish the generated Tape Read MP3 to Buzzsprout as a podcast episode.

Runs AFTER generate_audio.py has produced the MP3 and patched Ghost. Publishing is a
single multipart POST:

  POST https://www.buzzsprout.com/api/{BUZZSPROUT_PODCAST_ID}/episodes.json
  Authorization: Token token={BUZZSPROUT_API_TOKEN}
  multipart/form-data with the MP3 as the `audio_file` field.

CLI:
  --audio-file  path to the MP3 (required)
  --title       episode title, matches the Ghost post title (required)
  --date        brief date (required)
  --summary     one-sentence episode summary (required)
  --ghost-url   full URL of the published Ghost post (required)

Env:
  BUZZSPROUT_PODCAST_ID  (required)
  BUZZSPROUT_API_TOKEN   (required)

Writes buzzsprout_episode_id and buzzsprout_audio_url to $GITHUB_OUTPUT.

Dependencies: requests (everything else is stdlib).
"""

import os
import sys
import argparse

import requests


def die(msg):
    """Print a clear error (also a GitHub Actions annotation) and exit non-zero."""
    print(f"::error::{msg}")
    sys.exit(1)


def main():
    ap = argparse.ArgumentParser(description="Publish an MP3 to Buzzsprout as a podcast episode.")
    ap.add_argument("--audio-file", required=True, help="path to the MP3 file")
    ap.add_argument("--title", required=True, help="episode title (matches the Ghost post title)")
    ap.add_argument("--date", required=True, help="brief date")
    ap.add_argument("--summary", required=True, help="one-sentence episode summary")
    ap.add_argument("--ghost-url", required=True, help="full URL of the published Ghost post")
    args = ap.parse_args()

    podcast_id = (os.environ.get("BUZZSPROUT_PODCAST_ID") or "").strip()
    if not podcast_id:
        die("BUZZSPROUT_PODCAST_ID is not set")
    api_token = (os.environ.get("BUZZSPROUT_API_TOKEN") or "").strip()
    if not api_token:
        die("BUZZSPROUT_API_TOKEN is not set")

    audio_path = args.audio_file
    if not os.path.isfile(audio_path):
        die(f"audio file not found on disk: {audio_path}")

    description = (
        f"The Tape Read — Pre-Market Intelligence Brief for {args.date}. "
        f"Full written brief: {args.ghost_url}"
    )

    url = f"https://www.buzzsprout.com/api/{podcast_id}/episodes.json"
    headers = {
        "Authorization": f"Token token={api_token}",
        "User-Agent": "TheTapeRead-AudioPipeline/1.0",
    }
    data = {
        "title": args.title,
        "description": description,
        "summary": args.summary,
        "private": "0",                       # public episode
        "email_after_audio_processed": "0",   # no notification email
    }

    # multipart/form-data — the MP3 goes in the `audio_file` field. requests sets the
    # multipart Content-Type + boundary automatically when `files=` is used; do NOT use json=.
    with open(audio_path, "rb") as fh:
        files = {"audio_file": (os.path.basename(audio_path), fh, "audio/mpeg")}
        resp = requests.post(url, headers=headers, data=data, files=files, timeout=300)

    if not (200 <= resp.status_code < 300):
        die(f"create episode returned HTTP {resp.status_code}: {resp.text}")

    body = resp.json()
    episode_id = body["id"]
    audio_url = body["audio_url"]
    print(f"published Buzzsprout episode {episode_id}: {audio_url}")

    with open(os.environ.get("GITHUB_OUTPUT", "/dev/null"), "a") as fh:
        fh.write(f"buzzsprout_episode_id={episode_id}\n")
        fh.write(f"buzzsprout_audio_url={audio_url}\n")


if __name__ == "__main__":
    main()
