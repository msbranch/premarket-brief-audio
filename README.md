# The Tape Read — Audio

Standalone pipeline that adds a "listen" audio player to a published Tape Read brief.
Runs on a manual trigger, AFTER the brief is published in Ghost. It does not touch the
brief pipeline or its timing.

## How to run
Actions → **The Tape Read — Audio** → **Run workflow** → paste the Ghost **post ID** *or*
**slug** → Run. Tick `dry_run` to generate the script + MP3 as a downloadable artifact without
patching Ghost (use this to audition the voice before going live). Tick `regenerate` to rebuild
the audio on a post that already has a player — it replaces the old card rather than no-op'ing.

## What it does
1. Fetches the published brief from Ghost via the **Admin API** (the Content API truncates
   paid/members-only bodies, so Admin is required).
2. **Parses** the structured brief HTML into a model (deterministic, no LLM) — sections,
   watchlist cards, and data tables, keyed off the brief's stable anchors. Fails loudly on a
   malformed brief rather than narrating it.
3. **Builds a beat-plan** (deterministic): a fixed set of beats, each with a word budget set
   by floor/target/priority, so every watchlist name is guaranteed its own budgeted beat and
   the shape is identical every session (~1,150 words, hard cap 1,250).
4. **Narrates** the plan in one Anthropic call (Sonnet 5) — the model writes prose only; the
   structure and coverage are already decided in code.
5. **Asserts coverage** (code): verbatim open/close, every watchlist name spoken, no raw
   tickers/symbols, under the word cap, no dropped section. On failure it re-rolls the call;
   a cut-off or incomplete script never reaches a live post.
6. Synthesizes an MP3 (ElevenLabs, with retry/backoff on the 429 concurrency cap), uploads it
   to Ghost's media store, and **fills the player's `src` in place**. The brief already ships
   its own editorial player — the scoped `.trd .audio.reveal` block with an empty `src=""` — so
   this run only fills that src; it never injects its own player and never restyles anything.
   That keeps the audio post visually identical to the written brief, with exactly one player.
   If the template player isn't found, the run **fails loudly** rather than injecting a fallback
   (it means the post wasn't built from the current template, and we want to know).

The audio lives only on the Ghost site.

Idempotent — if the player's `src` is already filled, the run is a no-op, so re-running is safe;
pass `regenerate` to refresh it with a new MP3. Every run also saves the raw brief HTML to the
`out/` artifact for debugging.

## Required secrets (Settings → Secrets and variables → Actions)
| Secret | Value |
|---|---|
| `GHOST_ADMIN_API_URL` | e.g. `https://premarket-intelligence.ghost.io` |
| `GHOST_ADMIN_API_KEY` | Ghost Admin API key, `id:secret` |
| `ANTHROPIC_API_KEY` | Anthropic API key |
| `ELEVENLABS_API_KEY` | ElevenLabs API key |
| `ELEVENLABS_VOICE_ID` | ElevenLabs voice id (placeholder until a voice is chosen) |

## Getting the post ID
Ghost admin → open the published post → the 24-hex ID is in the editor URL
(`/ghost/#/editor/post/<ID>`).
