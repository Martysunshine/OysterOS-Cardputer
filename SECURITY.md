# Security

## Responsible use

OysterOS is built for learning and for testing **equipment you own or are authorised to test**. It does passive
Wi-Fi and Bluetooth reconnaissance, inspection of devices you have enrolled yourself, and automation of systems
you control.

It deliberately does not contain tools that disrupt or deceive other people's devices: no deauthentication,
beacon spam, Karma attacks or credential-harvesting portals. In radio range the effect of such tools cannot be
limited to your own gear, so they are out of scope by design and pull requests adding them are declined.

Radio regulations and computer-misuse laws differ between countries. You are responsible for how you use this
software.

## How the project protects you

| Area | Safeguard |
|---|---|
| Secrets | The AI key lives in `backend/.env` on the PC and, for standalone mode, in the device's internal NVS storage. It is never written to source files, logs, the SD card or the screen. `.gitignore` keeps `.env`, keys, certificates, databases and flash backups out of commits. |
| Transport | The device talks TLS to the backend with a per-device token, and TLS with a pinned certificate chain to the AI provider. Certificate verification is never switched off. |
| AI actions | The model cannot reach the radio or the PC directly. It can only request typed tools; every request is validated against a schema, and anything that changes state needs a physical key press on the device. |
| PC and smart-home control | Only allowlisted actions and entities can be triggered; impactful ones ask for confirmation on the device, and every action is written to an audit log. |
| On-device scripts | The Python sandbox has no network access and no file access beyond its own script; sleeps are capped and cancellable, and all file hooks fail closed. |
| Flashing | `scripts/firmware-flash.ps1` refuses to write a chip that has no verified full backup of that exact chip. |
| Privacy | What is sent to the AI provider is listed in [`docs/PRIVACY.md`](docs/PRIVACY.md). |

## Reporting a vulnerability

Please report security problems **privately**: open the repository's **Security** tab and choose
**Report a vulnerability**. Include the affected component (firmware, backend, scripts), the steps to
reproduce and the impact you expect.

Do not put secrets, tokens, Wi-Fi credentials or captures of other people's networks into issues, pull
requests or reports.

The supported version is the latest commit on `main`.

---

Created by martin.matysek
