# Voice questions (push-to-talk)

On the Oyster AI screen the owner can **speak** a question instead of typing it. The Cardputer ADV records
a short clip on its microphone, streams it to the PC backend while still recording, the backend transcribes
it with the configured provider's speech model, and the text lands in the normal question entry for review.
Pressing Enter then sends it as the usual AI question.

Code: `firmware/include/voice_capture.h`, `firmware/src/voice_capture.cpp`, the audio codec in
`firmware/lib/oyster_core/{include/oyster_core/voice_codec.h,src/voice_codec.cpp}` (device); the backend
package `backend/oyster/voice/`. Tests: `firmware/test/test_voice/`, `tests/voice/`.

## Status, stated plainly

- **The microphone has not been exercised on a real Cardputer ADV in this build.** M5Unified 0.2.27 drives
  the ADV microphone through the ES8311 codec on I2S, and this code initialises it the way the library's own
  board table expects, but no audio has been captured on hardware. If `M5.Mic.begin()` fails on the device,
  the screen says "Microphone did not start" — it never fakes a recording.
- **The streamed upload timing has not been measured on hardware.** The ring is sized for about one second;
  if the uplink falls behind, whole audio blocks are dropped and the screen shows `gaps!`.
- **Transcription has not been run against the real provider.** The backend is tested only against a fake
  speech endpoint (`httpx.MockTransport`). The request shape follows the provider's OpenAI-compatible audio
  API; the model (`whisper-large-v3-turbo`) is on the account's list but has not been called from here.

## How it works

1. **Record.** The owner picks "Speak a question (v)" (or presses `v`) on the Oyster AI list. The microphone
   is started only now (`M5.Mic.begin()`) and released when the recording ends. Capture is 16 kHz, 16-bit
   mono; each 64 ms block is decimated 2:1 to **8 kHz** and encoded as **8-bit G.711 mu-law** (8 KB/s). That
   is the smallest format that stays intelligible for speech; the backend decodes it to 16-bit PCM before it
   builds the WAV. Recording stops at **12 s**, or on Enter (stop early), or on Back / the emergency cancel
   chord (discard).
2. **Stream.** The device opens an upload (`POST .../voice`), then uploads the mu-law in 4 KiB chunks
   (`POST .../voice/{upload_id}/chunks`, raw `application/octet-stream`, sequence in the `X-Chunk-Seq` header
   or the `seq` query) over the same keep-alive TLS connection the rest of the API uses, while the next block
   is still being recorded. The radio uplink is leased per request, so a Wi-Fi survey or BLE session cannot
   cut in mid-chunk.
3. **Transcribe.** `POST .../voice/{upload_id}/finish` makes the backend assemble the chunks, build a 16-bit
   PCM WAV and call the provider's `POST {AI_BASE_URL}/audio/transcriptions` with `AI_API_KEY`. The transcript
   is clipped to **240 characters** (the firmware's question limit), cut at a word boundary with `...`.
4. **Review and send.** The transcript opens pre-filled in the question entry. The owner edits if needed and
   presses Enter to send it as a normal question. Nothing is sent to the AI run automatically.

## What is sent where

- The **audio** goes only to the PC backend, and from there only to the configured speech provider (Groq by
  default), over TLS verified against the OS trust store. It is **never written to the device, the SD card or
  the backend disk**: uploads live in memory with a 60-second TTL and are deleted the moment they are
  transcribed or cancelled.
- The provider receives the audio and returns text. Treat anything you say as disclosed to that provider,
  exactly like a typed question. See [PRIVACY.md](PRIVACY.md).
- `AI_API_KEY` is sent only in the `Authorization` header and is scrubbed from every error message; it is
  never logged and never reaches the device.

## Formats and limits

| Thing | Value |
| --- | --- |
| Capture | 16 kHz, 16-bit mono (M5Unified ES8311 path) |
| On the wire | `ulaw_8k` (8 kHz G.711 mu-law, 8 KB/s); `pcm16_8k` also accepted by the backend |
| Max duration | 12 s (bounded on both sides) |
| Chunk size | ≤ 4096 bytes, raw `application/octet-stream` |
| Uploads in flight | 2 per device; each bounded to its `max_seconds` of audio |
| Upload TTL | 60 s in memory, then dropped |
| Transcript | ≤ 240 characters, clipped at a word with `...` |
| Rate limits | 20 uploads and 12 transcriptions per minute per device (`429 rate_limited`) |

## Errors (shown as plain on-screen toasts)

| Backend code | On screen |
| --- | --- |
| `transcription_unavailable` | "voice input is not set up on the backend" |
| `too_short` / `no_speech` | "too short, speak a little longer" / "no speech recognised" |
| `upload_incomplete` | "upload incomplete, try again" |
| `provider_error` | "speech service error, try again" |
| `rate_limited` | "rate limited, wait a moment" |
| `not_found` | "upload expired or unknown, try again" |
| transport failure | "backend offline" |

## How to disable

Leave `AI_API_KEY` empty, or set `AI_SPEECH_MODEL=` (empty) in `backend/.env`. "Speak a question" then fails
cleanly with `transcription_unavailable` and the screen says voice input is not set up. Typing a question
still works regardless. `AI_SPEECH_LANGUAGE` is an optional ISO-639-1 hint; empty lets the provider detect
the language.
