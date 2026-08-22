#!/usr/bin/env python3
"""The Tape Read — audio pipeline (deterministic outline-driven narration).

Standalone, runs AFTER a brief is manually published in Ghost. Triggered by
workflow_dispatch with the published post's Ghost ID.

Sequence:
  1. Fetch the published brief from Ghost via the ADMIN API (the Content API
     truncates paid/members-only bodies, so Admin is required for Tue-Fri).
  2. PARSE the structured brief HTML -> BriefModel (deterministic, no LLM). The
     structure decisions (which section, which watchlist name, which table) are made
     here in code, keyed off the brief's stable HTML anchors — the model never has to
     find structure. Fails loudly if a required anchor is missing.
  3. BUILD a beat-plan Outline (deterministic): a fixed set of beats, each with a word
     budget, filled by floor/target/priority so every watchlist name is guaranteed its
     own budgeted beat and the shape is identical every session.
  4. NARRATE the outline -> spoken script in ONE Anthropic call. The model does prose
     only: it narrates to the plan, hits the budgets, keeps the audio-native/compliance
     rules. Retries on truncation OR coverage failure (below).
  5. ASSERT coverage (code): every watchlist name spoken, verbatim open/close, no raw
     tickers/symbols/table debris, under the word cap, each always-present beat left a
     trace. On failure, re-roll the narration call. A cut-off or incomplete script never
     reaches a live post.
  6. Synthesize an MP3 via ElevenLabs TTS (single-request char cap enforced).
  7. Upload the MP3 to Ghost's media store -> public CDN URL, and patch the post with an
     audio player card. On PAID posts the card goes INSIDE the gated region (members-only,
     same access as the brief); on public posts it's prepended.

The audio lives only on the Ghost site.

Ghost-idempotent: if the post already carries the audio card (id="tape-read-audio"),
the run is a no-op — the player is already in place.

Stdlib only — no pip installs.
"""

import os, sys, re, json, time, hmac, hashlib, base64, html as htmllib, datetime, zoneinfo
from dataclasses import dataclass
import urllib.request, urllib.error

ET = zoneinfo.ZoneInfo("America/New_York")

# --- tunables -------------------------------------------------------------
ANTHROPIC_MODEL   = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5")
ANTHROPIC_VERSION = "2023-06-01"
ELEVENLABS_MODEL  = os.environ.get("ELEVENLABS_MODEL", "eleven_multilingual_v2")
ELEVEN_OUTPUT_FMT = "mp3_44100_128"
MAX_TTS_CHARS     = 9500          # eleven_multilingual_v2 caps ~10k chars/request
NARRATION_MAX_ATTEMPTS = 3        # re-rolls on truncation OR coverage-assertion failure
AUDIO_CARD_ID     = "tape-read-audio"   # idempotency sentinel

# Outline budgeting (words).
FILL_TARGET   = 1000              # fill toward ~1000; HARD_CAP is the assert ceiling
HARD_CAP      = 1050
CARD_FLOOR, CARD_TARGET = 90, 125
COMPRESSED_PER_NAME = 22          # overflow cards -> one rapid-fire beat, ~a line each

# Verbatim fixed lines — SINGLE source of truth, used both in the prompt and to reserve
# their exact word count in the budget. Preserve the shipped branding; do not reword
# without an explicit request.
OPEN_SENTENCE  = "This is The Tape Read. Pre-market intelligence brief for {date}."
CLOSE_SENTENCE = "That's the tape for {date}. Educational and observational only — not investment advice."

# ElevenLabs voice character; tune once a voice is chosen.
VOICE_SETTINGS = {"stability": 0.5, "similarity_boost": 0.75, "style": 0.0, "use_speaker_boost": True}

# Context beats: (key, floor, target, priority). Watchlist names are priority 1 and are
# added dynamically (one beat per name). No-card day swaps the per-name beats for the
# no-card explainer. This table is the whole beat-plan spec.
CONTEXT_BEATS = [
    ("tape_thesis",           40,  70, 3),
    ("macro_loop",            60, 150, 2),
    ("sector_gate",           35,  80, 3),
    ("rate_context",          40,  90, 3),
    ("flow_standouts",        60, 110, 2),
    ("earnings_iv",           50,  85, 3),
    ("macro_options_bridge",  45,  85, 3),
    ("scorecard",             40,  60, 3),
    ("next_session",          40,  65, 4),
]
NOCARD_BEAT = ("nocard_explainer", 60, 90, 2)

BEAT_LABEL = {
    "tape_thesis":          "Today's Tape — the one-line thesis and what's driving the session",
    "macro_loop":           "Macro backdrop and loop-closure — what data printed and the read-through",
    "sector_gate":          "Sector gate — the spoken verdict on whether conditions are risk-on",
    "rate_context":         "Rates and macro context",
    "flow_standouts":       "Standout options-flow sweeps, told as prose (never a table of strikes)",
    "earnings_iv":          "Key earnings this week and the implied-move reads",
    "macro_options_bridge": "How the macro read translates into options positioning",
    "scorecard":            "The scorecard — how recent calls graded out",
    "next_session":         "Setup into the next session",
    "nocard_explainer":     "Why there are no watchlist names today",
}

# Per-beat coverage backstop: an always-present beat passes if ANY of its anchor
# substrings appears in the narration. A whole dropped section therefore triggers a
# re-roll. Kept small/high-signal to avoid false re-rolls; only these six are checked.
BEAT_ANCHORS = {
    "sector_gate":          ["gate", "sector", "risk-on", "risk-off", "green light",
                             "constructive", "cautious", "defensive"],
    "rate_context":         ["yield", "rate", "basis point", "treasury", "fed",
                             "two-year", "ten-year", "bond"],
    "flow_standouts":       ["sweep", "premium", "flow", "calls", "puts", "block", "unusual"],
    "earnings_iv":          ["earnings", "report", "implied move", "after the close",
                             "before the open", "guidance"],
    "macro_options_bridge": ["positioning", "into the open", "translate", "lean",
                             "tilt", "setup"],
    "scorecard":            ["record", "hit rate", "graded", "closed", "called",
                             "batting", "went ", "scorecard", "track record"],
}


NARRATION_SYSTEM = """You are the voice of "The Tape Read," a pre-market options-flow brief.
You will receive an OUTLINE: an ordered list of beats, each with a word budget and the facts to cover.
Narrate the whole outline as ONE flowing spoken piece — a desk analyst walking someone through the open.

FOLLOW THE PLAN:
- Cover the beats in the given order. Aim for each beat's word budget (within about 15%); the totals fit the runtime.
- Use ONLY the facts provided in each beat. Do not invent numbers, names, or catalysts.
- Do NOT announce structure ("Section 4", "next beat", "the scorecard section"); just speak, with natural transitions.

AUDIO-NATIVE:
- No tables, symbols, HTML, bullet points, or raw ticker symbols. Say company names ("Robinhood," "Nvidia,"
  "the semiconductor complex"), NEVER the ticker letters. Speak numbers the way a person would
  ("up about eleven and a half percent," "a put/call ratio near one point four," "an IV rank around five").
- Spell out abbreviations. Never read strikes or tables verbatim — narrate the story the numbers tell.

FIXED LINES (verbatim, exactly):
- FIRST sentence: "{open}"
- LAST sentence: "{close}"

COMPLIANCE — this is a hard rule:
- EDUCATIONAL AND OBSERVATIONAL ONLY. Never say buy, sell, enter, add, take, or recommend. Describe what the
  flow, the skew, and the setup show; never direct the listener to act.
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
    url = f"{api_url}/ghost/api/admin/posts/{post_id}/?formats=html"
    hdr = {"Authorization": f"Ghost {make_jwt(admin_key)}", "Accept-Version": "v5.0"}
    return req_json("GET", url, hdr)["posts"][0]


# --- step 2: parse the structured brief -> BriefModel ---------------------
class BriefParseError(RuntimeError):
    """Raised when a required structural anchor is missing — never narrate a malformed brief."""


@dataclass
class Card:
    company: str
    ticker: str = ""
    tier: str = ""            # "Strong" / "Moderate" / "Weak" (from "Signal Confluence: X")
    direction: str = ""       # derived from thesis/flow ("call setup" / "put setup" / "")
    price_note: str = ""      # the gap/price context that precedes the Flow Read label
    flow_read: str = ""
    catalyst: str = ""
    technical_level: str = ""
    skew: str = ""
    thesis: str = ""


@dataclass
class BriefModel:
    date_iso: str
    thesis: str
    snapshot: list                    # header "snap" rows as "label: value" strings
    sections: dict                    # {section_title: raw_text} in document order
    cards: list                       # [Card, ...]  (empty on a no-card day)
    nocard_note: str                  # <div class="nocard"> text when cards == []
    tables: dict                      # {"gate"|"flow"|"net_premium"|"earnings": {"headers","rows"}}


_TAG = re.compile(r"<[^>]+>")


def _txt(fragment: str) -> str:
    return re.sub(r"\s+", " ", htmllib.unescape(_TAG.sub(" ", fragment))).strip()


def _require(cond, msg):
    if not cond:
        raise BriefParseError(msg)


def _blocks(html: str, cls: str):
    """Depth-aware grab of every <div class="... cls ...">…</div> block (handles nesting)."""
    out = []
    pat = re.compile(r'<div[^>]*class="[^"]*\b' + re.escape(cls) + r'\b[^"]*"[^>]*>', re.I)
    for m in pat.finditer(html):
        depth, end = 1, m.end()
        for mm in re.finditer(r"</?div\b", html[m.end():], re.I):
            depth += 1 if mm.group()[1] != "/" else -1
            if depth == 0:
                end = m.end() + mm.start()
                break
        out.append(html[m.end():end])
    return out


def _parse_card(card_html: str) -> Card:
    ps = re.findall(r"<p\b[^>]*>(.*?)</p>", card_html, re.I | re.S)
    _require(ps, "an <div class='ncard'> has no <p> lines")

    # line 1: identity ("TICKER · Company") + the .tier span ("Signal Confluence: X")
    first = ps[0]
    tier = ""
    mt = re.search(r'<span[^>]*class="[^"]*\btier\b[^"]*"[^>]*>(.*?)</span>', first, re.I | re.S)
    if mt:
        tier = re.sub(r"(?i)^.*signal\s+confluence:\s*", "", _txt(mt.group(1))).strip()
        identity = _txt(first[:mt.start()] + first[mt.end():])
    else:
        identity = _txt(first)
    ticker, company = "", identity
    if "·" in identity:                                   # middot separator
        ticker, company = (x.strip() for x in identity.split("·", 1))
    _require(company, "an <div class='ncard'> has no company name — cannot guarantee coverage")

    fields = {"flow_read": "", "catalyst": "", "technical_level": "", "skew": "", "thesis": ""}
    label_map = [("flow read", "flow_read"), ("catalyst", "catalyst"),
                 ("technical", "technical_level"), ("skew", "skew"), ("thesis", "thesis")]
    price_note = ""
    for p in ps[1:]:
        ms = re.search(r"<strong\b[^>]*>(.*?)</strong>(.*)", p, re.I | re.S)
        if not ms:
            continue
        label, val = _txt(ms.group(1)).lower(), _txt(ms.group(2))
        for key, fld in label_map:
            if label.startswith(key):
                fields[fld] = val
                if fld == "flow_read":
                    pre = _txt(p[:ms.start()])                  # gap/price context before "Flow Read:"
                    if pre:
                        price_note = pre
                break

    hay = (fields["flow_read"] + " " + fields["thesis"]).lower()
    direction = "call setup" if "call setup" in hay else "put setup" if "put setup" in hay else ""
    return Card(company=company, ticker=ticker, tier=tier, direction=direction,
                price_note=price_note, **fields)


def _classify_dg(headers):
    H = " ".join(headers).lower()
    if "gate result" in H or ("etf" in H and "pcr" in H):
        return "gate"
    if "contract" in H and "premium" in H:
        return "flow"
    if "net call" in H and "net put" in H:
        return "net_premium"
    if "eps" in H:
        return "earnings"
    return None


def _parse_dg(dg_html):
    headers = [_txt(m.group(1)) for m in
               re.finditer(r'<div[^>]*class="[^"]*\bh\b[^"]*"[^>]*>(.*?)</div>', dg_html, re.I | re.S)]
    cells = [_txt(m.group(1)) for m in
             re.finditer(r'<div[^>]*class="[^"]*\bc\b[^"]*"[^>]*>(.*?)</div>', dg_html, re.I | re.S)]
    n = len(headers)
    rows = [cells[i:i + n] for i in range(0, len(cells), n)] if n else []
    return headers, rows


def parse_brief(html: str, date_iso: str) -> BriefModel:
    root = _blocks(html, "trd")
    _require(root, "missing <div class='trd'> brief card — is this the structured layout?")
    body = root[0]

    # thesis / Today's Tape
    tape = _blocks(body, "tape")
    _require(tape, "missing <div class='tape'> (Today's Tape / thesis)")
    mth = re.search(r'<p[^>]*class="[^"]*\bthesis\b[^"]*"[^>]*>(.*?)</p>', tape[0], re.I | re.S)
    _require(mth, "missing <p class='thesis'> inside .tape")
    thesis = _txt(mth.group(1))

    # header snapshot rows: .snap > .row > (.t label, .v value)
    snapshot = []
    snap = _blocks(body, "snap")
    if snap:
        for row in _blocks(snap[0], "row"):
            t = re.search(r'class="[^"]*\bt\b[^"]*"[^>]*>(.*?)<', row, re.I | re.S)
            v = re.search(r'class="[^"]*\bv\b[^"]*"[^>]*>(.*?)<', row, re.I | re.S)
            label, value = (_txt(t.group(1)) if t else ""), (_txt(v.group(1)) if v else "")
            if label or value:
                snapshot.append(f"{label}: {value}".strip(": ").strip())

    # sections, sliced by successive .sh anchors (8 numbered + Next-Session = 9)
    anchors = [m.start() for m in re.finditer(r'<div[^>]*class="[^"]*\bsh\b', body, re.I)]
    _require(len(anchors) >= 8, f"expected >=8 section headers (.sh), found {len(anchors)} — layout drift")
    bounds = anchors + [len(body)]
    sections = {}
    for i in range(len(anchors)):
        seg = body[anchors[i]:bounds[i + 1]]
        mh = re.search(r"<h2[^>]*>(.*?)</h2>", seg, re.I | re.S)
        title = _txt(mh.group(1)) if mh else f"section_{i + 1}"
        # strip the leading section-header div (number + h2) so facts are body-only, not "02 Macro …"
        seg_body = re.sub(r"^.*?</div>", "", seg, count=1, flags=re.S)
        sections[title] = _txt(seg_body)

    # watchlist cards OR the no-card note
    cards, nocard_note = [], ""
    ncards = _blocks(body, "ncard")
    if ncards:
        cards = [_parse_card(c) for c in ncards]
    else:
        nc = _blocks(body, "nocard")
        _require(nc, "no <div class='ncard'> cards and no <div class='nocard'> note — ambiguous brief")
        nocard_note = _txt(nc[0])

    # data grids: classified by header labels (robust across `dg gate` and bare `dg`)
    tables = {}
    for dg in _blocks(body, "dg"):
        headers, rows = _parse_dg(dg)
        key = _classify_dg(headers)
        if key and key not in tables:
            tables[key] = {"headers": headers, "rows": rows}

    return BriefModel(date_iso=date_iso, thesis=thesis, snapshot=snapshot, sections=sections,
                      cards=cards, nocard_note=nocard_note, tables=tables)


# --- step 3: build the beat-plan -> Outline -------------------------------
@dataclass
class Beat:
    key: str
    label: str
    facts: str
    floor: int
    target: int
    priority: int
    budget: int = 0
    flexible: bool = True


@dataclass
class Outline:
    date_str: str
    open_sentence: str
    close_sentence: str
    beats: list
    total: int


TIER_RANK = {"strong": 0, "moderate": 1, "weak": 2}

_CORP_SUFFIX = re.compile(
    r"(?i)\s*\b(corporation|corp|incorporated|inc|company|co|ltd|limited|plc|holdings|"
    r"group|n\.?v|s\.?a|a\.?g|se)\.?$")


def core_name(name: str) -> str:
    """Reduce a legal name to what the narration actually says: 'Reddit, Inc.' -> 'Reddit',
    'NVIDIA Corporation' -> 'NVIDIA'. Used for the spoken-name coverage check."""
    core = name.split(",")[0].strip()
    prev = None
    while core and core != prev:            # strip stacked suffixes ("… Group Inc")
        prev = core
        core = _CORP_SUFFIX.sub("", core).strip()
    return core or name.strip()


def _section(sections, *keys):
    for title, text in sections.items():
        low = title.lower()
        if any(k in low for k in keys):
            return text
    return ""


def _table_facts(tables, key):
    tbl = tables.get(key)
    if not tbl:
        return ""
    rows = ["; ".join(f"{h}={c}" for h, c in zip(tbl["headers"], row)) for row in tbl["rows"]]
    return " | ".join(rows)


def _card_facts(card: Card) -> str:
    bits = []
    if card.price_note:
        bits.append(card.price_note)
    if card.direction:
        bits.append(f"Direction: {card.direction}")
    for lbl, val in (("Flow read", card.flow_read), ("Catalyst", card.catalyst),
                     ("Technical", card.technical_level), ("Skew", card.skew), ("Thesis", card.thesis)):
        if val:
            bits.append(f"{lbl}: {val}")
    return "\n".join(bits)


def _context_facts(model: BriefModel) -> dict:
    s, t = model.sections, model.tables
    return {
        "tape_thesis":          model.thesis,
        "macro_loop":           _section(s, "macro") or " ".join(model.snapshot),
        "sector_gate":          "\n".join(x for x in (_section(s, "gate", "sector"),
                                                      _table_facts(t, "gate")) if x),
        "rate_context":         _section(s, "rate", "yield"),
        "flow_standouts":       "\n".join(x for x in (_section(s, "flow"),
                                                      _table_facts(t, "flow"),
                                                      _table_facts(t, "net_premium")) if x),
        "earnings_iv":          "\n".join(x for x in (_section(s, "earning"),
                                                      _table_facts(t, "earnings")) if x),
        "macro_options_bridge": _section(s, "bridge", "positioning") or model.thesis,
        "scorecard":            _section(s, "scorecard", "record", "grade"),
        "next_session":         _section(s, "next"),
        "nocard_explainer":     model.nocard_note,
    }


def build_outline(model: BriefModel, date_str: str, open_words: int, close_words: int) -> Outline:
    facts = _context_facts(model)
    no_card = not model.cards
    remaining = FILL_TARGET - open_words - close_words

    # assemble present context beats (drop any whose source facts are empty)
    spec = list(CONTEXT_BEATS)
    if no_card:
        spec = spec[:1] + [NOCARD_BEAT] + spec[1:]          # thesis, no-card explainer, then rest
    beats = []
    for key, fl, tg, pr in spec:
        f = (facts.get(key) or "").strip()
        if not f:
            continue
        beats.append(Beat(key, BEAT_LABEL[key], f, fl, tg, pr))

    # watchlist cards -> one full beat each, ranked Strong > Moderate > Weak then order.
    # As many full cards as fit alongside context floors; overflow -> one rapid-fire beat.
    if not no_card:
        ranked = sorted(range(len(model.cards)),
                        key=lambda i: (TIER_RANK.get(model.cards[i].tier.lower(), 3), i))
        ordered = [model.cards[i] for i in ranked]
        ctx_floor = sum(b.floor for b in beats)
        n = len(ordered)
        full_n = n
        while full_n > 0:
            need = ctx_floor + full_n * CARD_FLOOR + (n - full_n) * COMPRESSED_PER_NAME
            if need <= remaining:
                break
            full_n -= 1
        for c in ordered[:full_n]:
            label = f"Watchlist name: {c.company} — flow read, catalyst, technical level, skew, ranked {c.tier or 'n/a'}"
            beats.append(Beat(f"card::{c.company}", label, _card_facts(c), CARD_FLOOR, CARD_TARGET, 1))
        overflow = ordered[full_n:]
        if overflow:
            names = ", ".join(c.company for c in overflow)
            facts_txt = "\n".join(f"{c.company}: {c.direction or 'setup'} — "
                                  f"{(c.thesis or c.flow_read or '')[:160]}" for c in overflow)
            w = len(overflow) * COMPRESSED_PER_NAME
            beats.append(Beat("cards_rapidfire",
                              f"Rapid-fire — one line each for the remaining names: {names}",
                              facts_txt, w, w, 1, flexible=False))

    # fill: floors first, then distribute slack by priority (1->4), watchlist first,
    # within a tier proportional to headroom, capping at target.
    for b in beats:
        b.budget = b.floor
    slack = remaining - sum(b.floor for b in beats)
    if slack > 0:
        for pri in (1, 2, 3, 4):
            while slack > 0:
                head = [b for b in beats if b.priority == pri and b.flexible and b.budget < b.target]
                if not head:
                    break
                room = sum(b.target - b.budget for b in head)
                give = min(slack, room)
                alloc = 0
                for b in head:                       # proportional, floor-division
                    add = min((give * (b.target - b.budget)) // room, b.target - b.budget)
                    b.budget += add
                    slack -= add
                    alloc += add
                if alloc == 0:                       # rounding stalled — guarantee progress
                    b = max(head, key=lambda x: x.target - x.budget)
                    b.budget += 1
                    slack -= 1

    total = open_words + close_words + sum(b.budget for b in beats)
    return Outline(date_str=date_str,
                   open_sentence=OPEN_SENTENCE.format(date=date_str),
                   close_sentence=CLOSE_SENTENCE.format(date=date_str),
                   beats=beats, total=total)


def render_outline_for_model(outline: Outline) -> str:
    lines = [f"OUTLINE for {outline.date_str}. Narrate to these beats and word budgets, in order.",
             f'Open with exactly: "{outline.open_sentence}"', ""]
    for b in outline.beats:
        lines.append(f"[{b.label} — about {b.budget} words]")
        lines.append(b.facts.strip())
        lines.append("")
    lines.append(f'Close with exactly: "{outline.close_sentence}"')
    return "\n".join(lines)


# --- step 4: narrate the outline (Anthropic), with coverage re-roll --------
def generate_narration(anthropic_key, outline: Outline, model: BriefModel, date_str: str):
    system = (NARRATION_SYSTEM
              .replace("{open}", outline.open_sentence)
              .replace("{close}", outline.close_sentence))
    body = {
        "model": ANTHROPIC_MODEL,
        "max_tokens": 4096,
        "system": system,
        "messages": [{"role": "user", "content": render_outline_for_model(outline)}],
    }
    hdr = {"x-api-key": anthropic_key, "anthropic-version": ANTHROPIC_VERSION, "content-type": "application/json"}

    # The model can overrun max_tokens (truncated, mid-sentence) or drop a beat/name. Output
    # is non-deterministic, so a fresh attempt almost always fixes it — re-roll a few times,
    # then fail loudly rather than ship a cut-off or incomplete script to a live post.
    last = "unknown error"
    for attempt in range(1, NARRATION_MAX_ATTEMPTS + 1):
        data = req_json("POST", "https://api.anthropic.com/v1/messages", hdr, body)
        parts = [b.get("text", "") for b in data.get("content", []) if b.get("type") == "text"]
        script = "".join(parts).strip()
        if not script:
            last = "Anthropic returned an empty script"
        elif data.get("stop_reason") == "max_tokens":
            last = "hit max_tokens — script truncated (model overran the length cap)"
        else:
            ok, reasons = assert_coverage(script, outline, model)
            if ok:
                return script
            last = "coverage failed — " + "; ".join(reasons)
        if attempt < NARRATION_MAX_ATTEMPTS:
            print(f"::warning::narration attempt {attempt}/{NARRATION_MAX_ATTEMPTS}: {last}; retrying ...")

    raise RuntimeError(f"{last}; exhausted {NARRATION_MAX_ATTEMPTS} attempts")


# --- step 5: coverage assertion -------------------------------------------
def assert_coverage(script: str, outline: Outline, model: BriefModel):
    """Return (ok, reasons). Any reason is a hard fail -> re-roll."""
    reasons = []
    s = script.strip()
    low = s.lower()

    if not s.startswith(outline.open_sentence):
        reasons.append("open line not verbatim")
    if not s.endswith(outline.close_sentence):
        reasons.append("close line missing or not final (script may be truncated)")

    # every watchlist name must be spoken — a dropped name is a hard fail. Match the spoken
    # core name ("Reddit"), not the legal name ("Reddit, Inc."), which narration never says.
    for c in model.cards:
        core = core_name(c.company).lower()
        if core and core not in low and c.company.lower() not in low:
            reasons.append(f"watchlist name dropped: {c.company}")

    # no raw symbols or table debris in spoken prose
    if "$" in s or "%" in s:
        reasons.append("raw $ or % symbol present (numbers must be spoken as words)")
    if re.search(r"\|\s*-{2,}|\bhttps?://", s):
        reasons.append("table debris or URL present")

    # no raw ticker letters voiced (card tickers + every table's first-column tk ticker)
    raw = {c.ticker for c in model.cards if c.ticker}
    for tbl in model.tables.values():
        for row in tbl["rows"]:
            if row and re.fullmatch(r"[A-Z]{1,6}", row[0]):
                raw.add(row[0])
    for t in sorted(raw):
        if re.search(rf"\b{re.escape(t)}\b", s):               # case-sensitive: matches "SPY", not "spy"
            reasons.append(f"raw ticker voiced: {t}")

    # per-beat backstop: a whole dropped section (of the always-present six) triggers re-roll
    present = {b.key for b in outline.beats}
    for key, anchors in BEAT_ANCHORS.items():
        if key in present and not any(a in low for a in anchors):
            reasons.append(f"beat likely dropped: {key}")

    wc = len(s.split())
    if wc > HARD_CAP:
        reasons.append(f"over word cap: {wc} > {HARD_CAP}")

    return (not reasons), reasons


# --- step 6: spoken script -> MP3 (ElevenLabs) ----------------------------
def synthesize(eleven_key, voice_id, script):
    if len(script) > MAX_TTS_CHARS:
        raise RuntimeError(f"script is {len(script)} chars, over the {MAX_TTS_CHARS} single-request cap")
    url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}?output_format={ELEVEN_OUTPUT_FMT}"
    body = json.dumps({"text": script, "model_id": ELEVENLABS_MODEL, "voice_settings": VOICE_SETTINGS}).encode()
    r = urllib.request.Request(url, data=body, method="POST", headers={
        "xi-api-key": eleven_key, "Content-Type": "application/json", "Accept": "audio/mpeg"})
    try:
        with urllib.request.urlopen(r, timeout=300) as resp:
            return resp.read()
    except urllib.error.HTTPError as e:
        # ElevenLabs returns the real reason in the JSON body (invalid_api_key, quota_exceeded,
        # detected_unusual_activity). The bare "HTTP 401" hides it — surface the body.
        detail = ""
        try:
            detail = e.read().decode("utf-8", "replace")[:800]
        except Exception:
            pass
        raise RuntimeError(f"ElevenLabs TTS failed: HTTP {e.code} {e.reason} — {detail}") from None


# --- step 7: MP3 -> Ghost media store -> public URL -----------------------
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
        return card + "\n" + original_html
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


# --- main -----------------------------------------------------------------
def main() -> int:
    # .strip() every credential: a trailing newline pasted into a GitHub secret would otherwise
    # ride along in the auth header and surface as a spurious HTTP 401.
    api_url     = (os.environ.get("GHOST_ADMIN_API_URL") or "").strip().rstrip("/")
    admin_key   = (os.environ.get("GHOST_ADMIN_API_KEY") or "").strip()
    anthropic   = (os.environ.get("ANTHROPIC_API_KEY") or "").strip()
    eleven_key  = (os.environ.get("ELEVENLABS_API_KEY") or "").strip()
    voice_id    = (os.environ.get("ELEVENLABS_VOICE_ID") or "").strip()
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

    # dump raw brief HTML as an artifact so any parse failure is debuggable
    with open("out/brief_raw.html", "w", encoding="utf-8") as f:
        f.write(html)

    # already carries the Ghost audio card: player in place, nothing to do
    if f'id="{AUDIO_CARD_ID}"' in html:
        print("audio card already present on Ghost — nothing to do.")
        return 0

    if status != "published":
        print(f"::warning::post status is {status!r}, not 'published' — proceeding, but you normally trigger this after publishing.")

    # date (ET) — narration + MP3 filename
    pub = post.get("published_at") or datetime.datetime.now(ET).isoformat()
    dt = datetime.datetime.fromisoformat(pub.replace("Z", "+00:00")).astimezone(ET)
    date_str = dt.strftime("%A, %B %-d, %Y")
    fname = f"tape-read-{dt.strftime('%Y%m%d')}.mp3"

    # parse -> model (fail loud on malformed brief)
    try:
        model = parse_brief(html, dt.date().isoformat())
    except BriefParseError as e:
        print(f"::error::brief parse failed: {e}  (raw HTML saved to out/brief_raw.html)")
        return 1
    print(f"parsed: {len(model.cards)} watchlist card(s), {len(model.sections)} sections, "
          f"tables={sorted(model.tables)}")
    print(f"section titles: {list(model.sections)}")

    # build outline (reserve open/close at their exact word count)
    open_words = len(OPEN_SENTENCE.format(date=date_str).split())
    close_words = len(CLOSE_SENTENCE.format(date=date_str).split())
    outline = build_outline(model, date_str, open_words, close_words)
    print(f"outline (~{outline.total} words, open {open_words} / close {close_words}):")
    print(f"  {'open (verbatim)':32} {open_words:>4}")
    for b in outline.beats:
        print(f"  {b.key:32} {b.budget:>4}  (floor {b.floor}/tgt {b.target}, p{b.priority})")
    print(f"  {'close (verbatim)':32} {close_words:>4}")

    script = generate_narration(anthropic, outline, model, date_str)
    with open("out/script.txt", "w", encoding="utf-8") as f:
        f.write(script)
    print(f"script: {len(script)} chars / ~{len(script.split())} words (coverage OK)")

    print(f"synthesizing MP3 with ElevenLabs ({ELEVENLABS_MODEL}) ...")
    mp3 = synthesize(eleven_key, voice_id, script)
    with open(f"out/{fname}", "wb") as f:
        f.write(mp3)
    print(f"MP3: {len(mp3)} bytes -> out/{fname}")

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
