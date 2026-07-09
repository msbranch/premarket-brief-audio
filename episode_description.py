"""Shared show-notes description builder for Tape Read podcast episodes.

Used by both publish_to_transistor.py (new episodes) and update_transistor_episode.py
(retrofits) so the description stays identical in both. The Ghost post URL is NOT put in
the description — it goes in the episode's `alternate_url` field instead.
"""


def build_description(date, excerpt=""):
    parts = [f"The Tape Read — Pre-Market Intelligence Brief for {date}."]
    excerpt = (excerpt or "").strip()
    if excerpt:
        parts.append(excerpt)
    parts.append(
        "Full written brief for paid subscribers at thetaperead.morganbranch.co. "
        "Educational and observational only — not investment advice."
    )
    return "\n\n".join(parts)
