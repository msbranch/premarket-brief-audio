# The Tape Read — Audio

Standalone pipeline that adds a "listen" audio player to a published Tape Read brief.
Runs on a manual trigger, AFTER the brief is published in Ghost. It does not touch the
brief pipeline or its timing.

## How to run
Actions → **The Tape Read — Audio** → **Run workflow** → paste the Ghost **post ID** → Run.
Tick `dry_run` to generate the script + MP3 as a downloadable artifact without patching Ghost
(use this to audition the voice before going live).

## What it does
1. Fetches the published brief from Ghost via the **Admin API** (the Content API truncates
   paid/members-only bodies, so Admin is required).
2. Converts the written brief into a ~3–4 minute spoken narration (Anthropic, Sonnet 5).
3. Synthesizes an MP3 (ElevenLabs).
4. Uploads the MP3 to Ghost's own media store → public CDN URL.
5. Patches the post with the audio player. On paid posts the player sits **inside** the
   paywalled region (same access as the brief), so it's members-only. On public posts it's
   at the top for everyone.

The audio lives only on the Ghost site.

Ghost-idempotent — if the post already has the player, the Ghost patch is skipped and the
run is a no-op, so re-running a post is safe.

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
