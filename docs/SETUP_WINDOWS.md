[OysterOS](../README.md) / [Docs](README.md) / Setup on Windows

# Setup on Windows (beginner path)

This guide assumes you are comfortable with software but new to microcontroller hardware. It follows
this order: preserve the original software first, tools next, simulation before hardware, and the AI after
the firmware boots.

Two kinds of step appear below:

- **PC only**: safe to run at any time; no device needed.
- **On the device**: you physically connect or operate the Cardputer. Write down what you observe in
  [`HARDWARE_VERIFICATION.md`](HARDWARE_VERIFICATION.md), including failures.

All commands run in **PowerShell** from the repository root (the folder that contains `README.md`).
PowerShell 7 (`pwsh`) is recommended; Windows PowerShell 5.1 also works. If scripts are blocked, run
this once in the same window:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
```

---

## 0. Preserve the original software (before the first flash)

The Oyster firmware is a **full-flash image**. It carries its own partition table without OTA
(`firmware/partitions_8mb_noota.csv`). Uploading it writes a new bootloader, partition table and application
into the device's 8 MB flash, so the M5Stack factory firmware, the launcher and any original apps (Compass,
voice tools, PikaScript and so on, whatever your unit has installed) can no longer start. Only a copy of the
whole flash brings them back: the original firmware cannot be started from within Oyster, it is restored
from the PC ([`DUAL_BOOT.md`](DUAL_BOOT.md) explains why). So, before the first flash:

1. **Preservation audit (by hand).** Complete [`HARDWARE_AUDIT_TEMPLATE.md`](HARDWARE_AUDIT_TEMPLATE.md):
   photograph the boot and launcher screens, note the firmware and launcher version and the installed apps,
   and list the settings you want to keep. Do not commit the photos if they show personal data. If you are
   not willing to lose the original software, stop here and keep using the simulator.
2. **Backup (scripted).** After step 2 below has installed the tools, run
   `.\scripts\firmware-backup.ps1 -Port COMx` (step 3). It saves the whole flash to
   `_backups\<mac>\<yyyymmdd-hhmmss>\` on this PC, and `firmware-flash.ps1` will not flash a chip that has no
   verified backup. Take the backup while the device still runs its original firmware: a dump made after the
   first Oyster flash contains Oyster, not the original software.

The restore route is in [`TROUBLESHOOTING.md`](TROUBLESHOOTING.md#restoring-your-own-backup-esptool).

## What you need

| Item | Notes |
| --- | --- |
| M5Stack Cardputer **ADV** | Step 1 explains how to make sure it is the ADV. |
| USB-C cable that carries **data** | Charge-only cables are common and look identical. |
| microSD card, 32 GB or smaller | Formatted FAT32 (step 4). Optional, but needed for durable evidence. |
| Windows 10/11 PC | Runs the backend, the simulator and the BLE lab fixture. |
| 2.4 GHz Wi-Fi network | The Cardputer and the PC must be on the same LAN. The ESP32-S3 cannot use 5 or 6 GHz. |
| Groq API key | Created by you at the Groq console. Usage may be charged by Groq. |
| Python 3.13 (3.12+ works) | From python.org. Tick "Add python.exe to PATH" or keep the `py` launcher. |

## 1. Confirm the model and the cable (on the device)

1. The device must be the **Cardputer ADV** (Stamp-S3A module). The original Cardputer has different
   hardware and its keyboard driver differs. Check the packaging or the product page you bought from.
   The Oyster firmware also checks this at boot: it expects the M5Unified board id
   `board_M5CardputerADV` and shows a board-mismatch error otherwise.
2. Optional baseline: switch the device on with its factory firmware and check the screen and keyboard
   work. That tells you the device itself is fine before you flash anything.
3. Data cable test: open **Device Manager** (Win+X, then Device Manager), expand **Ports (COM & LPT)**,
   then plug the Cardputer into the PC and switch it on. A new entry such as
   "USB Serial Device (COM5)" or "USB JTAG/serial debug unit" should appear. If nothing appears, try
   another cable first. The USB-C port is the ESP32-S3's own USB, which normally uses the driver built
   into Windows 10/11. No CH340 or CP210x driver should be needed.

## 2. Install the tools (PC only)

```powershell
.\scripts\setup.ps1
```

What it does (safe to re-run):

- creates `.venv` with Python 3.12+ and installs `backend[dev]`, `lab[fixture]`, `truststore` and
  PlatformIO, constrained by `constraints.txt` (versions in [`BUILD_MATRIX.md`](BUILD_MATRIX.md));
- builds `.venv\oyster-ca-bundle.pem` so tools trust what Windows trusts. This matters on PCs where
  security software inspects HTTPS. See [`TROUBLESHOOTING.md`](TROUBLESHOOTING.md#https-certificate-errors-on-the-pc-tls-inspecting-software).
  Certificate verification is never switched off;
- creates `backend\.env` from `.env.example` if it does not exist, with random `OYSTER_ADMIN_TOKEN` and
  `LAB_FIXTURE_AUTH_TOKEN` values (never printed). Your AI key is added in step 6;
- downloads the pinned ESP32-S3 platform, compiler and libraries (several hundred MB the first time).

Optional, for the firmware's host unit tests (`pio test -e native`):

```powershell
.\scripts\setup.ps1 -InstallWinLibs     # installs WinLibs GCC through winget --source winget
```

Then run the automated checks. Each step prints PASS, FAIL or SKIPPED, and SKIPPED always says why:

```powershell
.\scripts\check.ps1
```

Every script dot-sources `scripts\_env.ps1`, which sets the CA-bundle variables and puts the venv and
WinLibs on `PATH` for that run only. To use `oyster`, `pio` and friends directly in your own window, run
`. .\scripts\_env.ps1` once (note the leading dot and space).

### Finding the serial port

```powershell
.\.venv\Scripts\pio.exe device list
```

Run it once with the Cardputer unplugged and once plugged in; the new entry is your port (for example
`COM5`). Device Manager > Ports (COM & LPT) shows the same thing. The number can change if you use a
different USB socket or put the board in download mode.

## 3. Back up, build and flash the firmware (build: PC only; backup and flash: on the device)

Flash the firmware before configuring the AI, so you know the device side works first. The audit of step 0
comes first, and so does the backup:

```powershell
.\scripts\firmware-backup.ps1 -Port COM5 -DryRun   # shows the commands; touches nothing
.\scripts\firmware-backup.ps1 -Port COM5           # on the device; reads all 8 MB
.\scripts\firmware-build.ps1                       # compile + size + SHA-256 of firmware.bin
.\scripts\firmware-flash.ps1 -Port COM5            # on the device
.\scripts\firmware-monitor.ps1 -Port COM5          # on the device; Ctrl+C to exit
```

What the backup does (it uses the esptool that PlatformIO already installed; nothing is downloaded):

- reads the chip info and the MAC address, and refuses anything that is not an ESP32-S3 with 8 MB of flash;
- reads the whole flash (`0x0`, `0x800000` bytes) into `_backups\<mac>\<yyyymmdd-hhmmss>\factory-8MB.bin`, then
  checks the size, lets the chip compare its own flash with the file (`esptool verify_flash`), and writes
  `factory-8MB.bin.sha256` and `backup.json` (chip, MAC, flash size, date, esptool version). Add
  `-VerifyByReread` for a second full read that must give the same SHA-256;
- can take several minutes (8 MB over a serial link). Close any serial monitor first, and do not unplug the
  cable meanwhile.

The dump can contain Wi-Fi credentials or keys of the original firmware. Keep `_backups\` on this PC and never
commit or share it.

`firmware-flash.ps1` is the gate. It reads the connected chip's MAC and looks for a backup of exactly that chip
whose `factory-8MB.bin` is 8,388,608 bytes and whose SHA-256 matches its `.sha256` file. Without one it refuses
and prints the backup command. `-SkipBackupCheck` overrides the gate after a loud warning and a 5-second
pause: the factory firmware and original apps are then lost unless you saved them some other way.
`-DryRun` prints what it would do without touching the device.

What to expect: the screen shows a boot splash that waits for any key, then the Home screen. Until
the device has backend settings its heading reads `OYSTER OS (setup!)`; once provisioned it reads
`OYSTER OS`. The serial monitor shows
boot logs with the board id, flash size, reset reason and heap numbers. Write down what you actually see
in [`HARDWARE_VERIFICATION.md`](HARDWARE_VERIFICATION.md).

If flashing cannot connect: use **download mode** (power switch OFF, hold **G0**, plug in the USB-C data
cable, release G0), flash again, then switch the device off and on. Details, plus how to restore your backup
or the factory firmware with M5Burner, are in [`TROUBLESHOOTING.md`](TROUBLESHOOTING.md#flashing-fails-download-mode).

## 4. Prepare the microSD card (on the device)

1. Use a card of 32 GB or smaller. In File Explorer, right-click the card, choose **Format...**, pick
   **FAT32**, and run the format. (Windows' own dialog offers FAT32 only up to 32 GB; larger cards come
   as exFAT, which this firmware does not use.)
2. Switch the Cardputer **off** before inserting or removing the card.
3. The firmware creates `/oyster/sessions/<session-id>/` with `events.jsonl`, `manifest.json`,
   `wifi.csv` and `artifacts/`. Without a card it keeps a small volatile log in RAM and shows reduced
   persistence. Nothing secret (tokens, Wi-Fi password, AI key) is ever written to the card.
4. The SD card is **not encrypted**. A hash proves the bytes did not change. It does not keep them
   confidential.

## 5. Start the backend (PC only)

```powershell
.\scripts\backend.ps1
```

This runs `oyster init-db` and `oyster serve`. It listens on:

| Listener | Address | For |
| --- | --- | --- |
| HTTP | `127.0.0.1:8000` (this PC only) | CLI, simulator, tests. The owner/admin API is served **only** here |
| HTTPS | `OYSTER_BIND_HOST:8443` | the Cardputer (needs the dev CA from step 7). Device routes only; the owner token gets 403 |

`oyster serve` refuses to start when `OYSTER_ADMIN_TOKEN` is empty, a placeholder, shorter than 24
characters or too uniform. `setup.ps1` already wrote a random one; to make a new one, run
`python -c "import secrets; print(secrets.token_urlsafe(32))"`. Repeated authentication failures from one
address are blocked for 60 s (HTTP 429).

Check it from a second window: `Invoke-RestMethod http://127.0.0.1:8000/v1/health` should return
`status: ok`.

There is **no Docker route**: the repository has no Dockerfile. The native route above is the
supported one. A Docker container could host the backend and simulator, but it would not give the
live BLE fixture access to the PC's Bluetooth adapter.

## 6. Configure the AI provider (PC only)

1. Create an API key in your Groq account.
2. Open `backend\.env` in an editor and set `AI_API_KEY=<your key>`. Keep `AI_PROVIDER=groq` and
   `AI_BASE_URL=https://api.groq.com/openai/v1`.
3. Pick a model that is available to you **today** and that supports tool calls:

   ```powershell
   . .\scripts\_env.ps1
   oyster ai-check --model openai/gpt-oss-120b     # test a candidate before you configure it
   ```

   `ai-check` lists the provider's models, runs one tool-call round trip with `--model` (or with
   `AI_MODEL` when `--model` is omitted), and records the result in `backend\data\provider_check.json`.
   Put a model id that passed into `AI_MODEL=` and run `oyster ai-check` once more without `--model`.
   Candidates noted in [ADR-002](adr/ADR-002-ai-provider.md) include `openai/gpt-oss-120b`,
   `openai/gpt-oss-20b` and `llama-3.3-70b-versatile`. Models change, so trust what `ai-check` reports,
   not this list. `ai-check` proves one tool call works, not that a whole question completes: in the lab demo
   (`scripts\demo-m1.ps1`) `qwen/qwen3.8-27b` finished end to end on a free Groq key, while `openai/gpt-oss-120b`
   passed `ai-check` but lost its final answer (see [Troubleshooting](TROUBLESHOOTING.md)). Run the demo once
   with the model you pick.
4. Restart the backend. If the key or model is missing, AI runs end with status `ai_unavailable`. The
   system never makes up an answer, and it never switches to another provider without telling you.

The key lives only in `backend\.env`, which must never be committed. It is never sent to the Cardputer or the SD card.

Before the first AI run, read [`PRIVACY.md`](PRIVACY.md): SSIDs, BLE names and the room label are sent
to the provider; BSSIDs and BLE addresses are masked by default.

## 7. Provision trust and device authentication (on the device)

The Cardputer talks to the backend over HTTPS. It trusts a private development CA that you create, and
it identifies itself with a per-device token. These are loaded once over the USB cable.

1. Find the PC's LAN IP: run `ipconfig` and read the **IPv4 Address** of the Wi-Fi or Ethernet adapter on
   the same network as the Cardputer (for example `192.168.1.50`). The server certificate names this
   IP, so a fixed IP (a DHCP reservation in your router) saves trouble later.
2. Create the dev CA and server certificate, and start the backend:

   ```powershell
   .\scripts\backend.ps1 -MakeDevCaIp 192.168.1.50
   ```

   Windows Firewall may ask about Python accepting connections. Allow **Private networks** only, and
   make sure your Wi-Fi network profile is set to Private (see
   [`TROUBLESHOOTING.md`](TROUBLESHOOTING.md#windows-firewall-and-port-8443)).
3. Enrol the lab target on the backend once (the device copies the backend's enrolled targets during
   provisioning):

   ```powershell
   . .\scripts\_env.ps1
   oyster enroll-target --file .\schemas\examples\target.lab-01.json
   ```

   **Recommended: enrol the target's address.** The example file has `"address": null`. Without an address
   the Cardputer treats every discovered device with the lab service UUID or the exact name `OysterLab` as
   a candidate, and refuses to connect when there are two or more ("ambiguous target ... enrol an
   address"). To enrol it, copy the example file, set `identity.address` (and `address_type`) and enrol the
   copy. Take the address and its type from the detail view of the `OysterLab` row in a discovery result
   on the Cardputer (`Address: ... (public|random)`); Windows Bluetooth
   settings also show the PC adapter's address, but the fixture may advertise from a different one, so
   prefer the discovery result. Provision again afterwards so the device gets the updated target.

4. Keep the backend running, close any serial monitor, then provision over USB:

   ```powershell
   .\scripts\provision.ps1 -Port COM5 --help                          # all options
   .\scripts\provision.ps1 -Port COM5 --id cardputer-01 --ssid "YourNetwork"   # on the device
   ```

   `oyster provision-device` enrols the device on the backend, which issues its token (re-running it
   rotates the token; `--token-file` reuses an existing one). It then sends everything over the serial
   protocol ([`PROVISIONING.md`](PROVISIONING.md)): device id, backend IP (taken from the server certificate
   unless you pass `--backend-host`) and port, 2.4 GHz Wi-Fi credentials, the token, the CA certificate, the
   current time (applied when `DONE` is accepted, so the device can send authenticated requests before
   network time is available), and the enrolled targets. **The Wi-Fi password is always asked for interactively.** It is never a command-line
   argument, so it does not end up in your PowerShell history. The device confirms each field by name only
   and never echoes a secret.
5. If the PC's IP changes, re-run step 2 with the new IP and provision again.

## 8. Connect over Wi-Fi and check status (on the device)

- On the Cardputer, open **Settings** (tile 7 on the Home screen). The `Backend:` row shows `offline`, `connect`, `no-clock`,
  `cert-err`, `auth-err` or `online` (states `Offline`, `Connecting`, `ClockNotSet`, `CertError`,
  `AuthError`, `Connected`; also `unprov`, `cfg-err`, `init-err` before the client can run). Opening that
  row shows the full state, for example `cert_error`.
- The status bar shows one mode banner, the first that applies: `LIVE RADIO` (a radio phase is active),
  `START FAILED` (a module failed at boot; see Settings > Diagnostics), `AI CONNECTED` (the latest AI run
  actually used a provider), `BACKEND OK` (backend reachable and authenticated), or `OFFLINE`. The firmware
  never shows `SIMULATED`.
- On the PC: `Invoke-RestMethod http://127.0.0.1:8000/v1/health` and `oyster ai-check`.
- **Settings > Keyboard** lists the key bindings (including Home and Help) and **Settings > Key tester**
  shows the physical keys and the action each one resolves to. Record what you see under HV-03 in
  [`HARDWARE_VERIFICATION.md`](HARDWARE_VERIFICATION.md); the screen structure is in
  [`UI_SCREEN_MAP.md`](UI_SCREEN_MAP.md).
- Use a 2.4 GHz network. Most dual-band routers that share one name across bands work, but if joining
  fails, try a 2.4 GHz-only network name.
- Errors such as `cert-err` and `no-clock` are explained in [`TROUBLESHOOTING.md`](TROUBLESHOOTING.md#certificate-and-clock-errors-on-the-cardputer).

## 9. Simulation first, then the live fixture check (PC only)

The simulator is a virtual Cardputer that uses the same API and job contracts. Everything it produces
is labelled `simulated`. It checks orchestration, policy evaluation and reports. It cannot tell you
anything about radios, pairing or SD timing.

```powershell
# window 1
.\scripts\backend.ps1
# window 2
.\scripts\sim.ps1 -List                        # available scenarios
.\scripts\sim.ps1 -Scenario success -AutoConfirm
```

The one-command M1 demonstration (real model, simulated device, exported report):

```powershell
.\scripts\demo-m1.ps1
```

It stops with a clear message if `AI_API_KEY` or `AI_MODEL` is missing, and does not fake an answer.

Before any live BLE work, check the PC's Bluetooth adapter (read-only):

```powershell
.\scripts\fixture.ps1 check
```

The adapter must support the LE **peripheral** role. If it does not, keep using the simulator; live
BLE assessment is then blocked by that specific prerequisite (see
[`TROUBLESHOOTING.md`](TROUBLESHOOTING.md#bluetooth-peripheral-role-check)).

## 10. Full demonstration, report, and recovery

Reference demonstration of the project. **Steps 5-9 need the access-policy checks and the diagnostic transfer
(the M4 tests).** The Cardputer firmware lists them under Security > Assess as "not in build" and answers
those operations with `unsupported`; the simulated device of step 9 runs them (scenarios `vulnerable_target`
and `corrected_target`).

1. Start the backend (`.\scripts\backend.ps1`) and the fixture (`.\scripts\fixture.ps1 run -Mode vulnerable`).
2. Switch on the Cardputer. The firmware records live data only and opens a session at start-up
   (**Settings > New session** starts another).
3. Run a Wi-Fi survey and save a room label (**Security > Wi-Fi Survey**, then the **Room** row).
4. Discover the lab peripheral (advertised name `OysterLab`; **Security > Bluetooth Explorer**) and select
   the enrolled target (**Security > Enrolled Targets**). After connecting, the device first reads the
   public `device_info` value and disconnects unless it is exactly
   `oyster-lab-v1;1` (a consistency check, not authentication; see
   [`BLE_LAB_PROFILE.md`](BLE_LAB_PROFILE.md#8-target-identity-and-remote-reads-on-the-cardputer)).
5. Ask the AI to assess restricted diagnostic access (**Oyster AI > Ask a question**).
6. In vulnerable mode, the diagnostic object is returned before application authorisation (contrary to
   the documented policy).
7. Save the synthetic file and check its SHA-256 against the fixture manifest.
8. Stop the fixture, run `.\scripts\fixture.ps1 reset-state`, start it with `-Mode corrected`, and repeat
   under the same conditions.
9. Check that access is refused before authorisation and works after a legitimate authorisation.
10. Export a report that links both runs:

    ```powershell
    . .\scripts\_env.ps1
    oyster sessions
    oyster export-report --session <session-id> --format md --out .\backend\data\reports\demo.md
    ```

**Recovery:** put your own backup back with esptool ([`TROUBLESHOOTING.md`](TROUBLESHOOTING.md#restoring-your-own-backup-esptool)),
or the original M5Stack firmware with M5Burner, following the Cardputer ADV page at docs.m5stack.com. See
[`TROUBLESHOOTING.md`](TROUBLESHOOTING.md#restoring-the-factory-firmware-m5burner).
