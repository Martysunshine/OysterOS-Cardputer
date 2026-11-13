[OysterOS](../README.md) / [Docs](README.md) / Privacy

# Privacy: what leaves your PC

This page covers the data the backend sends to the AI provider and a few related local files. Secrets
handling in general is in the [README](../README.md#scope-and-responsible-use).

## What the AI provider receives

During an AI run the backend sends the configured provider (Groq by default):

- the owner's question;
- a bounded session overview;
- the redacted evidence the run reads: SSIDs with channel/RSSI/authentication mode, BLE advertised names,
  enrolled target ids and display names, evaluator finding text and the session room label.

## Masked by default

Neighbouring identifiers are masked by default: the last three octets of every Wi-Fi BSSID and BLE
address are replaced, for example `aa:bb:cc:xx:xx:xx#ap-1f3a9`.

- The vendor prefix (first three octets) stays visible.
- The alias after `#` is derived from the session id and the address, so the model can tell devices apart
  without learning them.
- `AI_SEND_FULL_IDENTIFIERS=true` in `backend\.env` turns masking off.

**SSIDs are not masked**, because they are needed for explanation. An SSID can identify a household or
business: treat SSIDs as disclosed to the provider, and do not run AI analysis on surveys whose SSIDs are
sensitive.

## Never sent

- `AI_API_KEY`, `OYSTER_ADMIN_TOKEN`, device tokens, Wi-Fi passwords, `LAB_FIXTURE_AUTH_TOKEN` and any
  other `*_TOKEN` / `*_KEY` / `*_SECRET` / `*_PASSWORD` environment value;
- values of non-public characteristics (only their length and SHA-256);
- raw advertisement payloads;
- `app_auth` challenge material.

## How the provider is contacted and what is stored

- Requests use TLS verified against the OS trust store, and redirects are never followed.
- The model never classifies a result. Classifications come from the deterministic evaluator and are
  stored next to the model's text (`answer.findings`).
- Model-written prose is stored with links, images and HTML removed, and is labelled as a model
  explanation.
- `oyster ask` sends the admin token only to a loopback backend unless `--allow-remote-admin` is given.

## Voice questions (speech)

When the owner speaks a question instead of typing it (docs/VOICE_INPUT.md):

- The recorded **audio** leaves the device only to the PC backend, and from the backend only to the
  configured speech provider (Groq by default) for transcription, over TLS. The spoken words become a normal
  question, so treat what you say as disclosed to that provider exactly like a typed question.
- The audio is **never stored**: it is not written to the device, the SD card or the backend disk. Uploads
  live in the backend's memory with a 60-second limit and are deleted as soon as they are transcribed or
  cancelled. Only the returned text (clipped to 240 characters) is kept, and only once the owner sends it.
- `AI_API_KEY` is sent only in the `Authorization` header to the speech endpoint and is scrubbed from error
  messages; it never reaches the device. Leave the key or `AI_SPEECH_MODEL` empty to disable voice input.

## When web access is on (off by default)

`AI_WEB_ACCESS=on` in `backend\.env` lets the assistant call two more backend tools; details and the exact
limits are in [`WEB_ACCESS.md`](WEB_ACCESS.md). With the line absent or `off`, nothing below happens.

- **To the search provider** (Brave Search over HTTPS, key in a request header): the model's search query
  after redaction (configured secrets and credential-looking strings, e-mail addresses, IPv4 addresses, MAC
  addresses and every SSID observed in the session are replaced by `[redacted]`) and the result count. The
  query is written by the model from the conversation, so treat it as derived from your question. The backend
  logs the query's length only. The provider's own data handling applies to what it receives.
- **To fetched sites** (http/https): one GET for the URL the model chose, usually a search result, with a fixed
  User-Agent and no cookies or credentials. The site learns your public IP address and the URL, like any browser
  visit. Private, loopback and link-local addresses are refused, so the backend, the Cardputer, Home Assistant
  and anything else on your LAN cannot be reached this way.
- **Back to the AI provider**: the search results (title, domain, URL, snippet) and the fetched page text (bounded)
  become tool results of the run, so they are sent to the AI provider like other evidence, labelled as untrusted
  internet content. Run records store the result summaries (domain, lengths, error codes), never the key.
- **Never sent**: `WEB_SEARCH_API_KEY` (scrubbed like every `*_KEY` value), session evidence, identifiers or
  the AI key to a search provider or a web site.

## Local files worth knowing about

- The simulator's `*.withheld.sealed` file (written next to the session file for the `withheld` scenario)
  is **plain text**. It only keeps the blind choice off the console; it is not secret or encrypted.
- The microSD card is not encrypted. A SHA-256 shows that bytes did not change; it does not keep them
  confidential. Tokens, the Wi-Fi password and the AI key are never written to it.
