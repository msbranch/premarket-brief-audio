#!/usr/bin/env python3
"""The Tape Read — audio pipeline.

Standalone, runs AFTER a brief is manually published in Ghost. Triggered by
workflow_dispatch with the published post's Ghost ID.

Sequence:
  1. Fetch the published brief from Ghost via the ADMIN API (the Content API
     truncates paid/members-only bodies, so Admin is required for Tue-Fri).
  2. Convert the written brief -> spoken-word narration via the Anthropic API.
  3. Synthesize an MP3 via ElevenLabs TTS.
  4. Upload the MP3 to Ghost's own media store (/media/upload/) -> public CDN URL.
  5. Patch the post with an audio player card. On PAID/members posts the card goes
     INSIDE the gated region (just after the post's paywall divider, or at the top of
     an already-fully-gated body) so the audio is members-only — same access as the
     brief. On public posts it's simply prepended.

Ghost-idempotent: if the post already carries the audio card (id="tape-read-audio"),
the Ghost upload/patch is skipped. The MP3 Ghost is already hosting is reused (downloaded
and written to out/ with episode metadata) so the downstream Transistor step can still
publish it. Note: Transistor itself has no idempotency guard — re-running a post that is
already on Transistor will create a duplicate episode.

Stdlib only — no pip installs.
"""

import os, sys, re, json, time, hmac, hashlib, base64, html as htmllib, datetime, zoneinfo
import urllib.request, urllib.error

ET = zoneinfo.ZoneInfo("America/New_York")

# --- tunables -------------------------------------------------------------
ANTHROPIC_MODEL   = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5")
ANTHROPIC_VERSION = "2023-06-01"
ELEVENLABS_MODEL  = os.environ.get("ELEVENLABS_MODEL", "eleven_multilingual_v2")
ELEVEN_OUTPUT_FMT = "mp3_44100_128"
MAX_TTS_CHARS     = 9500          # eleven_multilingual_v2 caps ~10k chars/request
AUDIO_CARD_ID     = "tape-read-audio"   # idempotency sentinel

# ElevenLabs voice character; tune once a voice is chosen.
VOICE_SETTINGS = {"stability": 0.5, "similarity_boost": 0.75, "style": 0.0, "use_speaker_boost": True}

SCRIPT_SYSTEM = """You convert a written pre-market options-trading brief into a spoken-word \
narration for an audio version subscribers listen to on the go. This is "The Tape Read."

Output ONLY the narration text to be read aloud — no preamble, no markdown, no headings, no \
stage directions, no quotation marks around the whole thing.

HARD RULES:
- Open with EXACTLY this sentence, verbatim: "This is The Tape Read. Pre-market intelligence brief for {date}."
- Audio-native prose: NO tables, NO symbols, NO HTML, NO bullet points, NO ticker symbols in raw form. \
Say company names ("Robinhood," "Nvidia," "the semiconductor complex"), and speak numbers the way a person \
would ("up about four and a quarter percent," "a put/call ratio near point four," "roughly twenty-three \
million dollars of net call premium"). Spell out abbreviations.
- Spell EVERY ticker as its company name, EVERYWHERE including the scorecard and flow sections — "SpaceX" not \
"SPCX," "Robinhood" not "HOOD." Never voice a raw ticker as letters.
- LENGTH IS A HARD CAP: 1050 words MAXIMUM, about six to seven minutes spoken. This cap is absolute and \
OVERRIDES completeness — when the brief is long or dense, be more selective and compress; never run over. \
Within that budget make it the FULLER companion to the written brief, not a teaser.
- COVERAGE: give EACH watchlist name its full reasoning — the flow read, what the skew says, the technical \
level, the catalyst status, and why it's capped where it is. Also carry: the macro loop-closure (what data \
printed and the read-through), the sector-gate logic with the skew read, the standout options-flow sweeps as \
PROSE (e.g. "a large long-dated call sweep in Bank of America" — never a table of strikes), the week's key \
earnings, and the full scorecard. Narrate the numbers that carry the story; still NEVER read tables, raw \
strikes, or every figure — summarize density, but do not drop whole sections. If the brief is dense (many \
names), do NOT give every name equal airtime: cover the standouts in full and compress the rest to a line \
each, so the whole narration stays within the word cap above.
- EDUCATIONAL AND OBSERVATIONAL ONLY. Never say buy, sell, enter, take, add, or recommend. Describe what the \
flow, the skew, and the setup show; never direct the listener to act. This is a hard compliance rule.
- Confident, plain, conversational — a desk analyst walking someone through the open, not a robot reading a report.
- Close with: "That's the tape for {date}. Educational and observational only — not investment advice."
- Never mention HTML, tables, the website, the production process, or that you are an AI."""


# --- Ghost Admin auth -----------------------------------------------------
def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def make_jwt(admin_key: str) -> str:
    kid, secret = admin_key.split(":", 1)
    header  = {"alg": "HS256", "typ": "JWT", "kid": kid}
    iat = int(time.time())
    payload = {"iat": iat, "exp": iat + 300, "aud": "/admin/"}
    segs = [b64url(json.dumps(header, separators=(",", ":")).encode()),
            b64url(json.dumps(payload, separators=(",", ":")).encode())]
    sig = hmac.new(bytes.fromhex(secret), ".".join(segs).encode(), hashlib.sha256).digest()
    segs.append(b64url(sig))
    return ".".join(segs)


def req_json(method, url, headers, body=None, timeout=120):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(url, data=data, method=method, headers=headers)
    with urllib.request.urlopen(r, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


# --- step 1: fetch the published brief (Admin API) ------------------------
def get_post(api_url, admin_key, post_id):
    url = f"{api_url}/ghost/api/admin/posts/{post_id}/?formats=html&include=tags"
    hdr = {"Authorization": f"Ghost {make_jwt(admin_key)}", "Accept-Version": "v5.0"}
    return req_json("GET", url, hdr)["posts"][0]


def html_to_text(html: str) -> str:
    html = re.sub(r"<!--.*?-->", " ", html, flags=re.DOTALL)          # drop kg-card comments
    html = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html, flags=re.DOTALL | re.IGNORECASE)
    html = re.sub(r"</(p|div|tr|li|h[1-6]|table)>", "\n", html, flags=re.IGNORECASE)
    html = re.sub(r"<[^>]+>", " ", html)
    text = htmllib.unescape(html)
    text = "\n".join(re.sub(r"[ \t]+", " ", ln).strip() for ln in text.splitlines())
    return re.sub(r"\n{3,}", "\n\n", text).strip()


# --- step 2: written brief -> spoken script (Anthropic) -------------------
def generate_script(anthropic_key, brief_text, date_str):
    body = {
        "model": ANTHROPIC_MODEL,
        "max_tokens": 4096,   # headroom for the ~1100-word target (spelling every ticker as a company
                              # name runs longer) + the mandatory closing line. A completed script still
                              # lands under the 9500-char TTS cap; the guard below backstops the rest.
        "system": SCRIPT_SYSTEM.replace("{date}", date_str),
        "messages": [{"role": "user", "content":
            f"Here is today's written brief. Convert it into the spoken narration per the rules.\n\n{brief_text}"}],
    }
    hdr = {"x-api-key": anthropic_key, "anthropic-version": ANTHROPIC_VERSION, "content-type": "application/json"}
    data = req_json("POST", "https://api.anthropic.com/v1/messages", hdr, body)
    parts = [b.get("text", "") for b in data.get("content", []) if b.get("type") == "text"]
    script = "".join(parts).strip()
    if not script:
        raise RuntimeError("Anthropic returned an empty script")
    if data.get("stop_reason") == "max_tokens":
        # Hit the ceiling -> the script is truncated mid-sentence and the closing line is missing.
        # Fail loudly rather than synthesize a cut-off MP3 (and never patch a live post with it).
        raise RuntimeError("Anthropic hit max_tokens — script was truncated; raise max_tokens or tighten the length target")
    return script


# --- step 3: spoken script -> MP3 (ElevenLabs) ----------------------------
def synthesize(eleven_key, voice_id, script):
    if len(script) > MAX_TTS_CHARS:
        # Single-request cap. Keep the narration tight (the prompt targets ~600 words);
        # if you move to long-form, chunk on sentence boundaries and concatenate the MP3s.
        raise RuntimeError(f"script is {len(script)} chars, over the {MAX_TTS_CHARS} single-request cap")
    url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}?output_format={ELEVEN_OUTPUT_FMT}"
    body = json.dumps({"text": script, "model_id": ELEVENLABS_MODEL, "voice_settings": VOICE_SETTINGS}).encode()
    r = urllib.request.Request(url, data=body, method="POST", headers={
        "xi-api-key": eleven_key, "Content-Type": "application/json", "Accept": "audio/mpeg"})
    with urllib.request.urlopen(r, timeout=300) as resp:
        return resp.read()


# --- step 4: MP3 -> Ghost media store -> public URL -----------------------
def upload_media(api_url, admin_key, mp3_bytes, filename):
    boundary = "----TapeRead" + base64.urlsafe_b64encode(os.urandom(12)).decode().rstrip("=")
    b = boundary.encode()
    body = b"".join([
        b"--" + b + b"\r\n",
        b'Content-Disposition: form-data; name="file"; filename="' + filename.encode() + b'"\r\n',
        b"Content-Type: audio/mpeg\r\n\r\n", mp3_bytes, b"\r\n",
        b"--" + b + b"--\r\n",
    ])
    r = urllib.request.Request(f"{api_url}/ghost/api/admin/media/upload/", data=body, method="POST", headers={
        "Authorization": f"Ghost {make_jwt(admin_key)}",
        "Content-Type": f"multipart/form-data; boundary={boundary}",
        "Accept-Version": "v5.0"})
    with urllib.request.urlopen(r, timeout=180) as resp:
        return json.loads(resp.read().decode("utf-8"))["media"][0]["url"]


# --- step 5: patch the post with the audio player -------------------------
def audio_card(mp3_url: str) -> str:
    return (
        '<!--kg-card-begin: html-->\n'
        f'<div id="{AUDIO_CARD_ID}" style="margin:0 0 20px;padding:14px 16px;background:#DBECFF;'
        'border:1px solid #b9d4f0;border-radius:10px;font-family:-apple-system,Segoe UI,Arial,sans-serif;">'
        '<div style="font-size:13px;font-weight:700;color:#16161a;margin:0 0 8px;">🎧 Listen to today\'s brief</div>'
        f'<audio controls preload="none" style="width:100%;"><source src="{mp3_url}" type="audio/mpeg">'
        'Your browser does not support the audio element.</audio></div>\n'
        '<!--kg-card-end: html-->'
    )


def build_new_html(original_html, mp3_url, visibility):
    card = audio_card(mp3_url)
    if visibility == "public":
        return card + "\n" + original_html                    # Monday: everything already public
    # Paid/members: the audio is paid content too — same access as the brief, never in the public
    # preview. If the post has a public-preview (paywall) divider, drop the card just AFTER it so it
    # sits at the top of the gated region. Otherwise the whole paid body is already gated, so the card
    # goes at the top and inherits that gating.
    marker = "<!--members-only-->"
    idx = original_html.find(marker)
    if idx != -1:
        cut = idx + len(marker)
        return original_html[:cut] + "\n" + card + "\n" + original_html[cut:]
    return card + "\n" + original_html


def patch_post(api_url, admin_key, post_id, updated_at, new_html):
    url = f"{api_url}/ghost/api/admin/posts/{post_id}/?source=html"
    hdr = {"Authorization": f"Ghost {make_jwt(admin_key)}", "Content-Type": "application/json", "Accept-Version": "v5.0"}
    body = {"posts": [{"updated_at": updated_at, "html": new_html}]}
    return req_json("PUT", url, hdr, body)


# --- episode metadata + MP3 reuse (for the Transistor publish step) --------
# Base brand keywords applied to every episode; the post's public Ghost tags are appended.
KEYWORDS_BASE = ["pre-market", "options flow", "unusual options activity", "options trading",
                 "stock market", "day trading", "market analysis"]
# Structural/internal tags that should never become podcast keywords.
KEYWORDS_EXCLUDE_SLUGS = {"the-full-read"}


def build_keywords(post):
    """Base brand keywords + the post's public Ghost tags (minus internal/structural ones)."""
    kws, seen = list(KEYWORDS_BASE), {k.lower() for k in KEYWORDS_BASE}
    for tag in (post.get("tags") or []):
        if (tag.get("visibility") or "public") == "internal":
            continue
        if (tag.get("slug") or "") in KEYWORDS_EXCLUDE_SLUGS:
            continue
        name = (tag.get("name") or "").strip()
        if name and name.lower() not in seen:
            kws.append(name)
            seen.add(name.lower())
    return ", ".join(kws)


def write_episode_meta(post, dt, date_str, fname, api_url):
    """Write out/episode_*.txt from data already fetched from Ghost, for the workflow's
    Transistor step to read (title / written date / public URL / keywords + the MP3 path).
    No summary file — episodes intentionally carry no summary."""
    meta = {
        "episode_title.txt":    post.get("title") or "The Tape Read",
        "episode_date.txt":     dt.strftime("%B %-d, %Y"),   # e.g. "July 9, 2026"
        "episode_url.txt":      post.get("url") or f"{api_url}/{post.get('slug', '')}/",
        "episode_keywords.txt": build_keywords(post),
        "episode_mp3_path.txt": f"out/{fname}",
    }
    for name, val in meta.items():
        with open(f"out/{name}", "w", encoding="utf-8") as f:
            f.write(val)
    print(f"episode metadata -> out/ (date={meta['episode_date.txt']}, keywords={meta['episode_keywords.txt']!r})")


def existing_card_mp3_url(html):
    """Pull the hosted MP3 URL out of an already-present audio card's <source src=...>."""
    m = re.search(r'id="' + re.escape(AUDIO_CARD_ID) + r'".*?<source[^>]+src="([^"]+)"',
                  html, re.DOTALL | re.IGNORECASE)
    return htmllib.unescape(m.group(1)) if m else None


def download_mp3(url):
    req = urllib.request.Request(url, headers={"User-Agent": "tape-read-audio/1.0"})
    with urllib.request.urlopen(req, timeout=180) as resp:
        return resp.read()


# --- main -----------------------------------------------------------------
def main() -> int:
    api_url     = (os.environ.get("GHOST_ADMIN_API_URL") or "").rstrip("/")
    admin_key   = os.environ.get("GHOST_ADMIN_API_KEY") or ""
    anthropic   = os.environ.get("ANTHROPIC_API_KEY") or ""
    eleven_key  = os.environ.get("ELEVENLABS_API_KEY") or ""
    voice_id    = os.environ.get("ELEVENLABS_VOICE_ID") or ""
    post_id     = (os.environ.get("POST_ID") or "").strip()
    dry_run     = (os.environ.get("DRY_RUN") or "false").lower() == "true"

    missing = [k for k, v in {
        "GHOST_ADMIN_API_URL": api_url, "GHOST_ADMIN_API_KEY": admin_key,
        "ANTHROPIC_API_KEY": anthropic, "ELEVENLABS_API_KEY": eleven_key,
        "ELEVENLABS_VOICE_ID": voice_id, "POST_ID": post_id}.items() if not v]
    if missing:
        print(f"::error::missing required env/inputs: {', '.join(missing)}")
        return 1

    os.makedirs("out", exist_ok=True)

    try:
        post = get_post(api_url, admin_key, post_id)
    except urllib.error.HTTPError as e:
        print(f"::error::could not fetch post {post_id} (HTTP {e.code}): {e.read().decode('utf-8','replace')[:400]}")
        return 1

    html = post.get("html") or ""
    visibility = post.get("visibility") or "public"
    status = post.get("status") or "unknown"
    print(f"post {post_id}: status={status}, visibility={visibility}, title={post.get('title')!r}")

    # date (ET) — used for the narration, the episode metadata, and the MP3 filename
    pub = post.get("published_at") or datetime.datetime.now(ET).isoformat()
    dt = datetime.datetime.fromisoformat(pub.replace("Z", "+00:00")).astimezone(ET)
    date_str = dt.strftime("%A, %B %-d, %Y")
    fname = f"tape-read-{dt.strftime('%Y%m%d')}.mp3"

    # Already carries the Ghost audio card: don't re-patch Ghost or re-run TTS. Reuse the MP3
    # that Ghost is already hosting so the downstream Transistor step can still publish it.
    if f'id="{AUDIO_CARD_ID}"' in html:
        print("audio card already present on Ghost — skipping Ghost patch; reusing the hosted MP3.")
        mp3_url = existing_card_mp3_url(html)
        if not mp3_url:
            print("::error::audio card present but no MP3 <source> URL found; cannot reuse.")
            return 1
        try:
            mp3 = download_mp3(mp3_url)
        except urllib.error.HTTPError as e:
            print(f"::error::could not download existing MP3 ({mp3_url}) (HTTP {e.code}).")
            return 1
        with open(f"out/{fname}", "wb") as f:
            f.write(mp3)
        print(f"reused {len(mp3)} bytes from {mp3_url} -> out/{fname}")
        write_episode_meta(post, dt, date_str, fname, api_url)
        return 0

    if status != "published":
        print(f"::warning::post status is {status!r}, not 'published' — proceeding, but you normally trigger this after publishing.")

    brief_text = html_to_text(html)
    print(f"brief text: {len(brief_text)} chars -> generating script with {ANTHROPIC_MODEL} ...")
    script = generate_script(anthropic, brief_text, date_str)
    with open("out/script.txt", "w", encoding="utf-8") as f:
        f.write(script)
    print(f"script: {len(script)} chars / ~{len(script.split())} words")

    print(f"synthesizing MP3 with ElevenLabs ({ELEVENLABS_MODEL}) ...")
    mp3 = synthesize(eleven_key, voice_id, script)
    with open(f"out/{fname}", "wb") as f:
        f.write(mp3)
    print(f"MP3: {len(mp3)} bytes -> out/{fname}")

    write_episode_meta(post, dt, date_str, fname, api_url)

    if dry_run:
        print("DRY_RUN=true — script + MP3 written to out/ as artifacts; NOT uploading or patching Ghost.")
        return 0

    print("uploading MP3 to Ghost media store ...")
    mp3_url = upload_media(api_url, admin_key, mp3, fname)
    print(f"hosted at: {mp3_url}")

    new_html = build_new_html(html, mp3_url, visibility)
    try:
        patch_post(api_url, admin_key, post_id, post["updated_at"], new_html)
    except urllib.error.HTTPError as e:
        if e.code == 409:   # someone edited the post since our fetch — re-read and retry once
            print("::warning::update collision (409) — re-fetching and retrying once ...")
            fresh = get_post(api_url, admin_key, post_id)
            if f'id="{AUDIO_CARD_ID}"' in (fresh.get("html") or ""):
                print("audio card appeared in the meantime — skip."); return 0
            new_html = build_new_html(fresh.get("html") or "", mp3_url, fresh.get("visibility") or "public")
            patch_post(api_url, admin_key, post_id, fresh["updated_at"], new_html)
        else:
            print(f"::error::patch failed (HTTP {e.code}): {e.read().decode('utf-8','replace')[:400]}")
            return 1

    print(f"OK — audio player attached to post {post_id} ({visibility}).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
