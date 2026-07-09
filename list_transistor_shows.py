#!/usr/bin/env python3
"""Diagnostic: list the Transistor shows visible to TRANSISTOR_API_KEY.

Prints each show's id + title + slug so you can confirm the correct value for the
TRANSISTOR_SHOW_ID secret. (A show id that does NOT match the current secret won't be
masked in the log, so the correct id will be visible.)

Stdlib only. Safe to delete this file and its workflow once the show id is confirmed.
"""
import os
import sys
import json
import urllib.request
import urllib.error


def main():
    api_key = (os.environ.get("TRANSISTOR_API_KEY") or "").strip()
    if not api_key:
        print("::error::TRANSISTOR_API_KEY is not set")
        return 1

    req = urllib.request.Request("https://api.transistor.fm/v1/shows", headers={"x-api-key": api_key})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        print(f"::error::GET /v1/shows returned HTTP {e.code}: {e.read().decode('utf-8', 'replace')[:400]}")
        return 1

    shows = data.get("data", [])
    print(f"shows visible to this API key: {len(shows)}")
    for s in shows:
        attrs = s.get("attributes", {})
        print(f"  id={s.get('id')}  title={attrs.get('title')!r}  slug={attrs.get('slug')!r}")
    if data.get("meta"):
        print(f"meta: {data['meta']}")
    if not shows:
        print("::warning::this API key sees ZERO shows — it is likely for a different Transistor account than the show.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
