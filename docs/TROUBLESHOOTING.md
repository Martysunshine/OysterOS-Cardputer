[OysterOS](../README.md) / [Docs](README.md) / Troubleshooting

# Troubleshooting

The fixes below are for Windows.

**Never "fix" a certificate error by switching verification off.** That includes `--trusted-host`,
`PYTHONHTTPSVERIFY=0`, `verify=False`, `-SkipCertificateCheck`, or a "skip verification" build of
the firmware. The project has no such path, by design.

---

## HTTPS certificate errors on the PC (TLS-inspecting software)

**Symptoms:** `pip`, PlatformIO (`pio pkg install`, `pio run` downloading packages) or the Groq provider
fail with `CERTIFICATE_VERIFY_FAILED`, `unable to get local issuer certificate`, or `self-signed certificate
in certificate chain`. A browser on the same PC works fine.

**Cause:** some hosts run TLS-inspecting security software (a "web shield" or "HTTPS scanning" feature)
that intercepts outbound HTTPS and re-signs it with its own root certificate. Windows trusts that root;
Python's bundled `certifi` list does not.

**Fix (verification stays on):**

1. The scripts build `.venv\oyster-ca-bundle.pem` = certifi + the Windows trusted roots
   (`scripts\make_ca_bundle.py`) and point `REQUESTS_CA_BUNDLE` and `SSL_CERT_FILE` at it. Every script
   does this by dot-sourcing `scripts\_env.ps1`. In your own window, run `. .\scripts\_env.ps1` before
   calling `pio` or `oyster` directly.
2. If that software updates its root certificate, rebuild the bundle:

   ```powershell
   .\.venv\Scripts\python.exe .\scripts\make_ca_bundle.py .\.venv\oyster-ca-bundle.pem
   ```

3. `pip` (24.2 and newer) and the backend's Groq client use the Windows trust store directly
   (`truststore`), so they need no bundle.
4. The bundle only affects tools on this PC. The Cardputer trusts only the private Oyster dev CA. Its
   connection goes to your PC over the LAN, not through any outbound TLS inspection.

## COM port, cable and driver

**No new port appears when you plug in the Cardputer**

- The cable may be charge-only. Try another USB-C cable that you know carries data.
- The power switch must be ON for normal mode (download mode is different, see below).
- Try another USB socket on the PC, directly rather than through a hub.
- Device Manager > Ports (COM & LPT): look for "USB Serial Device (COMx)" or "USB JTAG/serial debug
  unit". The ADV's USB-C goes to the ESP32-S3's built-in USB, which normally uses Windows' built-in driver.
  No CH340/CP210x driver should be needed. A yellow warning icon means a driver problem: right-click it,
  choose **Update driver**, then **Search automatically**.
- `.\.venv\Scripts\pio.exe device list` lists ports. Compare the list with the device unplugged and plugged in.

**"Access is denied" / "could not open port COMx"**

Only one program can use a COM port. Close the serial monitor (`firmware-monitor.ps1`), Arduino IDE,
M5Burner, other terminal programs, and any other `provision.ps1` before flashing or provisioning.

**The port number changed**

Windows may assign a different number in download mode or on another USB socket. Check again with
`pio device list`.

## The flash script refuses: no verified backup

`firmware-flash.ps1` reads the connected chip's MAC and only continues when
`_backups\<mac>\<yyyymmdd-hhmmss>\factory-8MB.bin` exists for that MAC, is exactly 8,388,608 bytes and has the
SHA-256 recorded in `factory-8MB.bin.sha256`. It prints `REFUSING TO FLASH` and the exact command to run:

```powershell
.\scripts\firmware-backup.ps1 -Port COM5
```

| Situation | What to do |
| --- | --- |
| No backup folder for this MAC | Run the backup command above, then flash again. Take the backup before the first Oyster flash |
| A backup exists but is refused | The file is truncated, was edited, or its `.sha256` file is missing or does not match. Take a new backup. If the device already runs Oyster, the new dump contains Oyster, not the original software |
| You use another Cardputer than before | Backups are matched to the chip's MAC. Each device needs its own |
| "Could not read the chip" | Close the serial monitor, check the data cable, or use download mode (next section). Nothing was flashed |
| You deliberately do not want the original software | `.\scripts\firmware-flash.ps1 -Port COM5 -SkipBackupCheck` prints a warning and waits 5 seconds. The factory firmware and original apps are then lost unless saved another way |

`firmware-backup.ps1 -Port COM5 -DryRun` prints the esptool commands and touches nothing. The backup refuses a
chip that is not an ESP32-S3 with 8 MB of flash, and deletes its partial file if the size or a verification
check fails. Both scripts take the esptool that PlatformIO installed (package `tool-esptoolpy`); if it is
missing, run `.\scripts\setup.ps1`. A backup can only be made on a device that connects: if it cannot, use
download mode as described next, then run the backup again.

## Flashing fails (download mode)

**Symptoms:** `Failed to connect to ESP32-S3`, `No serial data received`, `Timed out waiting for packet
header`, or the port disappears during upload.

Put the board into **download mode** manually:

1. Switch the Cardputer's power switch **OFF**.
2. Press and **hold the G0 button**. The M5Stack Cardputer ADV page shows where it is.
3. While holding G0, **plug in the USB-C data cable**.
4. **Release G0.** The screen stays dark. That is normal in download mode.
5. Run `.\scripts\firmware-flash.ps1 -Port COMx` again (check the port number first).
6. After a successful upload the script restarts the chip with a watchdog reset. If the screen is still dark,
   unplug the USB cable, switch the Cardputer **off**, plug it in and switch it **on** (the power switch alone
   does not cut USB power).

If it still fails, try a different cable or USB socket and make sure nothing else has the port open. As a
last resort, change `upload_speed` in `firmware\platformio.ini` (pinned at 115200, which is the most tolerant:
the native USB port ignores the UART rate, and a switch to a higher rate such as 460800 can stall a flash read).

## Restoring your own backup (esptool)

This puts back exactly what `firmware-backup.ps1` saved, using the esptool that PlatformIO installed. It
overwrites the **whole** flash, so Oyster and everything it stored in flash (provisioned settings, the CA
certificate) are gone afterwards; provision again when you come back to Oyster. If you can, test the restore
once and record it in [`HARDWARE_VERIFICATION.md`](HARDWARE_VERIFICATION.md) (HV-27).

1. Close every program that uses the COM port. If the board does not connect, use download mode (above).
2. Pick the backup folder of **this** chip: the folder name is the MAC that esptool prints as `MAC:`. Check the
   file against its hash, then write and verify (adjust the port and the folder):

   ```powershell
   . .\scripts\_env.ps1
   $bin = '.\_backups\aa-bb-cc-dd-ee-ff\20261024-221500\factory-8MB.bin'
   (Get-FileHash $bin -Algorithm SHA256).Hash.ToLower()     # must equal the first field of the .sha256 file
   $esptool = Find-OysterEsptool
   & $OysterPython $esptool --chip esp32s3 --port COM5 --baud 460800 write_flash --flash_mode keep --flash_freq keep --flash_size keep 0x0 $bin
   & $OysterPython $esptool --chip esp32s3 --port COM5 --baud 460800 verify_flash --flash_mode keep --flash_freq keep --flash_size keep 0x0 $bin
   ```

   Keep the three `keep` options: esptool keeps the flash settings by default, but environment variables such
   as `ESPTOOL_FM` can change that, and esptool then rewrites the image header at address `0x0`, so the
   restore would no longer be byte-for-byte.
3. Switch the device off and on. It should start the firmware that was on it when you took the backup. If it
   does not, use the M5Burner route below, and tell the project what you saw.

## Restoring the factory firmware (M5Burner)

You can go back to M5Stack's own firmware without a backup of your own:

1. Download **M5Burner** from M5Stack (linked from the Cardputer ADV page at
   `docs.m5stack.com/en/core/Cardputer-Adv`). Follow that page for the current procedure; it is the
   authority here.
2. In M5Burner, choose the **Cardputer ADV** factory firmware. Do not choose the original Cardputer.
3. Close every other program that uses the COM port, select the port, and burn. If it cannot connect, use
   download mode as above.
4. To come back to Oyster, run `.\scripts\firmware-flash.ps1 -Port COMx`. It still finds the backups you made
   for this chip. Provisioned settings may need to be sent again, because a full erase clears NVS.

## Certificate and clock errors on the Cardputer

The Settings screen and the status bar show the backend state in short form; opening Settings > Backend
shows the full state (for example `cert_error`). The serial log (`firmware-monitor.ps1`) adds the reason.

| Short / state | Likely cause | Fix |
| --- | --- | --- |
| `offline` / `Offline` | Wi-Fi not joined, wrong SSID/password, 5 GHz-only network, backend not running, or a TLS error that is not a certificate rejection ("backend unreachable: TLS error") | Check the 2.4 GHz network; re-provision Wi-Fi; start `.\scripts\backend.ps1` |
| `connect` / `Connecting` (stays there) | Firewall blocks 8443, wrong backend IP/port | See the firewall section; check `ipconfig` against the provisioned IP |
| `no-clock` / `ClockNotSet` | "clock not set (firmware policy)": the firmware has no plausible time from SNTP, the serial seed or the backend's `/v1/time` hint, so it sends no authenticated request. This is a firmware policy, not a TLS result | Re-provision (sends `SET-TIME`, applied at `DONE`), or allow outbound NTP (UDP 123) |
| `cert-err` / `CertError` | The TLS handshake rejected the certificate: it does not name the IP the device connects to (the PC's IP changed), or the CA on the device is from another `make-dev-ca` run or could not be parsed. A certificate failure is always shown as `cert-err`; it is never caused by the clock | `.\scripts\backend.ps1 -MakeDevCaIp <current IP>`, then provision the CA again. A DHCP reservation keeps the IP stable |
| `auth-err` / `AuthError` | Device token revoked (device re-enrolled) or the backend database was reset | Run `.\scripts\provision.ps1 -Port COMx --id <device-id> ...` again. It re-enrols the device and sends the new token |

**Certificate dates are not checked by the device.** The Arduino-ESP32 2.0.17 TLS library is built
without date support, so the Cardputer checks the chain and the IP/host name but not notBefore/notAfter
(see the amendment in [ADR-003](adr/ADR-003-transport-tls.md)). An expired dev certificate therefore does
not produce `cert-err`; renew it on the PC anyway. On the PC side, an expired or missing server certificate
shows up in the backend log when it starts.

**Banner `START FAILED`:** a firmware module did not start at boot. Settings > Diagnostics lists which one
and what is disabled; the dependent actions are refused with a message. Reboot to retry, and record it in
[`HARDWARE_VERIFICATION.md`](HARDWARE_VERIFICATION.md).

## Screens and keys on the Cardputer

The structure of the screens is in [`UI_SCREEN_MAP.md`](UI_SCREEN_MAP.md).

| Symptom | Meaning / fix |
| --- | --- |
| A module tile (Control, SmartHome) says `offline`, `no profile` or `IR only` | Intended until the device is provisioned and the backend is running (`scripts/oyster-up.ps1`), a profile is seeded (`oyster seed-demo --id <device>`) and Home Assistant is set up ([`HOME_ASSISTANT.md`](HOME_ASSISTANT.md)). The Original tile says `backup` or `on PC` and explains how the original firmware is run from the PC ([`DUAL_BOOT.md`](DUAL_BOOT.md)) |
| Entries in Security > Assess are muted and say "not in build" | Intended. They are the M4 tests (access-policy check, notifications, lab write, diagnostic transfer): the firmware does not run them and answers a backend job for one with `unsupported`. The simulated device runs them (`.\scripts\sim.ps1`) |
| Home (`Fn`+`Del`, press `Fn` first) or Help (`?`) does nothing | Open **Settings > Key tester** to see which physical keys arrive and which action they resolve to. Meanwhile use Back (`` ` `` or `Del`) one level at a time and the Help tile (8) on the Home screen. Record the result under HV-03 in [`HARDWARE_VERIFICATION.md`](HARDWARE_VERIFICATION.md) |
| The serial log says `display sprites unavailable (out of memory)` | The 7,680 B drawing sprite (one 240x16 band) could not be allocated, so screen output is switched off while the rest keeps running. Reboot; if it repeats, record the heap numbers of the boot event under HV-05 |

## Windows Firewall and port 8443

The first time the backend opens its HTTPS listener (`OYSTER_BIND_HOST:8443`), Windows may ask whether
Python may accept incoming connections.

- Allow **Private networks** only. Do not allow Public networks.
- Your Wi-Fi network profile must be **Private**: Settings > Network & internet > Wi-Fi > (your
  network) > Network profile type > Private.
- If you dismissed the prompt, add a rule in an **administrator** PowerShell:

  ```powershell
  New-NetFirewallRule -DisplayName "Oyster backend 8443 (private)" -Direction Inbound -Protocol TCP `
      -LocalPort 8443 -Profile Private -Action Allow
  ```

- Port 8000 is bound to `127.0.0.1` only and needs no firewall rule.
- Third-party security suites with their own firewall may need the same exception in their own settings.

## Bluetooth peripheral-role check

Run the read-only check before any live BLE work:

```powershell
.\scripts\fixture.ps1 check
```

| Row in the output | If it is not "supported" |
| --- | --- |
| Bluetooth adapter / Bluetooth LE | No usable adapter. Enable Bluetooth in Device Manager or use a USB BLE adapter |
| **BLE peripheral role** | This adapter or driver cannot host a GATT server. Try another adapter (some USB dongles support it), or stay with the simulator. Live BLE assessment is then **blocked by this prerequisite**, which is a valid, honest outcome |
| **Radio state: radio reports off** | Turn Bluetooth on: Settings > Bluetooth & devices > Bluetooth **On**; switch airplane mode off. Run `check` again |
| WinRT GATT server API | Run `.\scripts\setup.ps1` (installs `lab[fixture]`) |
| Authenticated pairing "unverified" | Expected: Windows offers the protection level. This read-only check of the PC cannot confirm a pairing with the Cardputer, so it reports the row that way |

Other notes:

- The fixture uses the native Windows API ([ADR-008](adr/ADR-008-native-winrt-fixture.md)). It does not
  run on Linux/macOS or inside Docker, and WSL is not supported either.
- While a central reads `restricted_enc` / `restricted_mitm`, Windows may show its own pairing
  notification or prompt on the PC. Record what happens in
  [`HARDWARE_VERIFICATION.md`](HARDWARE_VERIFICATION.md).
- `fixture.ps1 run` refuses to start without `LAB_FIXTURE_AUTH_TOKEN` (at least 32 characters) in
  `backend\.env`.
- `fixture.ps1 reset-state` reports "no running fixture acknowledged" when no fixture is running. A freshly
  started fixture is always unauthorised.
- **"ambiguous target: N devices match; enrol an address"** (on the Cardputer): the target has no enrolled
  address and more than one discovered device advertises the lab service UUID or the exact name. Enrol the
  target's address and provision again (see
  [`BLE_LAB_PROFILE.md`](BLE_LAB_PROFILE.md#8-target-identity-and-remote-reads-on-the-cardputer)).
- **Connection closed with "device_info mismatch"** (or "not exposed", "served more than once"): the peer
  did not serve exactly `oyster-lab-v1;1`, so the device disconnected before reading anything else. Check
  that the right fixture is running and that the enrolled address belongs to it.
- **"characteristic not readable by remote job (policy is not public)"**: a backend/AI job asked to read a
  protected characteristic or `app_auth`. That is refused by design; protected access is checked only
  through the access-policy assertions (the M4 tests, which the firmware answers with `unsupported`).

## Backend and simulator

| Symptom | Fix |
| --- | --- |
| `backend\.env not found` | `.\scripts\setup.ps1` creates it from `.env.example` |
| `OYSTER_ADMIN_TOKEN ... placeholder` / `is shorter than 24 characters` / `has too little variety` | `oyster serve` refuses such a token. Put the output of `python -c "import secrets; print(secrets.token_urlsafe(32))"` into `backend\.env` |
| Owner command gets 403 `the owner token is not accepted on the device listener` | The owner/admin API is served only on `http://127.0.0.1:8000`. Do not point owner tools at the LAN HTTPS port 8443 |
| HTTP 429 `rate_limited` on authentication | More than 10 failed authentications from one address within a minute; that address is blocked for 60 s. Fix the token, then wait for `Retry-After` |
| `oyster ask` refuses a non-loopback backend | Intended: the admin token goes only to a loopback backend unless you pass `--allow-remote-admin` |
| `Backend not reachable at http://127.0.0.1:8000` | Start `.\scripts\backend.ps1` in another window. Check that nothing else uses port 8000 (`Get-NetTCPConnection -LocalPort 8000`) |
| Runs end `ai_unavailable` | `AI_API_KEY` or `AI_MODEL` missing or rejected. Run `oyster ai-check` |
| Simulator gets 401 | The token file no longer matches (database reset, or device re-enrolled). Delete `backend\data\sim-cardputer-01.token` (or `<DeviceId>.token`) and run `.\scripts\sim.ps1` again |
| `demo-m1.ps1` stops at "Not configured" | Intended: it does not run without a real model. Configure the provider (SETUP step 6) |
| `demo-m1.ps1` stops with "oyster-sim exited ... right after registering" | The simulator stopped before any job was queued, so nothing could execute the AI's device jobs. The AI was deliberately not called. The script starts it with `--serve` (keep polling); if it still exits, read `sim.err.log` in the run's log folder |
| Logs of a demo run | `backend\data\logs\demo-m1-<timestamp>\` (`backend.*.log`, `sim.*.log`, `session.txt`) |

## Host unit tests (`pio test -e native`) are SKIPPED

`check.ps1` skips them when no host `g++` is on `PATH`. Install WinLibs with
`.\scripts\setup.ps1 -InstallWinLibs`, or set `OYSTER_GCC_BIN` to a folder that contains `g++.exe`.

## PowerShell refuses to run the scripts

`running scripts is disabled on this system`: run
`Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass` in that window (affects only that window).

## AI run ends with "provider rate limit (HTTP 429)"

Free API tiers cap tokens per minute (for example 8,000 tokens per minute for `openai/gpt-oss-120b` on Groq's
free tier). A multi-step question sends several requests, each carrying the tool list and the evidence so far,
so it can hit that cap.

- The backend waits and retries by itself: at most 2 retries per request, each only if the provider asks for 45 s
  or less (it honours the provider's `Retry-After` / "try again in Ns" hint). The wait counts against the run's time
  budget (`RUN_MAX_DURATION_S`, default 180 s).
- If the provider asks for a longer wait, or the retries are used up, the run ends as `ai_unavailable` with the
  rate-limit message. Nothing is invented: ask again in a minute.
- To raise the limits, use a paid tier or another model whose limit is higher (run `oyster ai-check` to see which
  tool-capable models your key can use), or raise `RUN_MAX_DURATION_S` in `backend/.env` so a slow run can finish.
- Organization identifiers and keys are removed from the stored error text.
- "Request too large ... please reduce your message size" (HTTP 413) is the same cap hit by one request alone
  (a question that ran a survey and a discovery carries their results). The backend calibrates its token
  estimate from the message, shrinks the context and retries once. Set `AI_MAX_INPUT_TOKENS` in `backend/.env`
  below the tier's per-minute limit (6000 for a 7,000 tokens per minute model) so requests are trimmed before
  they are sent.

## AI run ends with "no tool call in the response (empty reply)"

Seen with `openai/gpt-oss-120b` and `openai/gpt-oss-20b` on Groq: the model plans a `final_answer` call, but the
provider's tool-call parser drops it (the reply has reasoning text but no content and no tool call; the 20b model
reports a tool name with a stray `<|channel|>commentary` suffix). The backend retries once with a repair prompt
and then ends the run `failed`; it never invents an answer.

- Set `AI_MODEL` to another tool-capable model of your key (`oyster ai-check` lists and probes them).
  `qwen/qwen3.8-27b` completed tool calls reliably in the lab demo.
- The free tier of that model has its own input-token-per-minute cap (7,000), so a run may pause 30-40 s between
  requests (see the rate-limit section above).

## Screen stays dark after flashing, serial port silent, chip answers as a bootloader

Symptom: the upload reports success and every hash verified, but the screen is dark and no log appears.
`esptool --before no_reset --after no_reset read_mac` succeeds, which only a chip sitting in its ROM bootloader does.

Cause: on Windows the USB-Serial/JTAG port decodes the DTR/RTS lines as reset and boot-mode signals. A reset that
leaves GPIO0 low, or a terminal that opens the port with DTR asserted, puts the chip into download mode. The power
switch does not help while USB power stays connected.

- `firmware-flash.ps1` finishes with a watchdog reset (`--after watchdog_reset`), which does not depend on the
  line state. By hand: `python esptool.py --chip esp32s3 --port COMx --before default_reset --after watchdog_reset read_mac`.
- `monitor_dtr = 0` and `monitor_rts = 0` are set for the PlatformIO monitor. In your own serial tools open the port
  with DTR and RTS off.
- Last resort: unplug the cable, switch the device off, plug it in, switch it on.
