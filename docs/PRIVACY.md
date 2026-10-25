# Privacy: what leaves your PC

This page covers the data the backend sends to the AI provider and a few related local files. Secrets
handling in general is in the [README](../README.md#secrets).

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

## Local files worth knowing about

- The simulator's `*.withheld.sealed` file (written next to the session file for the `withheld` scenario)
  is **plain text**. It only keeps the blind choice off the console; it is not secret or encrypted.
- The microSD card is not encrypted. A SHA-256 shows that bytes did not change; it does not keep them
  confidential. Tokens, the Wi-Fi password and the AI key are never written to it.
