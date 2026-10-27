# Scripts (Home tile 3, "Red Team")

Oyster is a recon and defensive companion. It does not include tools that disrupt or deceive devices and
people the owner does not own: no deauth, no beacon spam, no Karma, no credential-harvesting captive
portal. Full offensive Wi-Fi firmware for this board is a separate project the owner can flash
themselves - [Evil-M5Project by 7h30th3r0n3](https://github.com/7h30th3r0n3/Evil-M5Project) - and is not
part of Oyster.

What tile 3 *does* carry is a small Python interpreter, so the owner can script Oyster's own, already
built-in functions. This page documents that feature. **Unverified on hardware** until it has been run on
a real device; see HV-38 in [`HARDWARE_VERIFICATION.md`](HARDWARE_VERIFICATION.md).

## What runs

[PikaPython](https://github.com/pikastech/pikapython) (also called PikaScript), MIT licensed, vendored
into `firmware/lib/pikascript` (the interpreter core) plus a small generated binding layer for one
Oyster-specific module. No network access, no file access beyond the one script file it was asked to run,
no device actions beyond the three calls below: this is a sandbox for scripting *logic*, not a new way to
drive the radio or storage.

The core is vendored as-is with one exception: `PikaVM.c`'s `_get_n_input_with_unpack` had a label
directly followed by a variable declaration, which this project's xtensa-gcc rejects in C (C++ allows it;
C only gained it in C23). The fix is a single no-op `;` after the label - same behaviour, just satisfies
the grammar - marked inline with a comment so it is easy to drop if a later PikaPython release already
fixes it upstream.

Opening Home tile 3 always shows two rows first, so the tile is useful with no SD card:

- `About / Evil-M5 reference` - what the tile is, the bindings, and the pointer to the separate offensive
  firmware project.
- `Run example (built-in)` - a short script compiled into the firmware; it logs a greeting, reads
  `wifi_ap_count()` and sleeps briefly.

A script of your own is any `.py` file placed in `/oyster/scripts/` on the SD card; those files are listed
after the two built-in rows. Enter on one runs it from the start; a file of 4096 bytes or more is refused,
never half-run. Output from `print()` and from `Oyster.log()`
calls lands in the same on-screen result log (bounded; the oldest lines drop first if a script is chatty).
Del cancels a running script from the device (stops the interpreter and any `sleep_ms()` it is waiting
in); the status LED and footer behave like any other worker-task operation.

## The `Oyster` module

```python
import Oyster
o = Oyster.Oyster()

o.log("hello from SD")          # -> the on-screen result log
o.sleep_ms(250)                  # cooperative delay, capped per call, cancel-aware
n = o.wifi_ap_count()            # access points in the LAST COMPLETED Wi-Fi survey (0 if none yet)
```

- `log(text: str)` - writes to the result log shown after the run.
- `sleep_ms(ms: int)` - delays in milliseconds; each call is capped (`kSleepMaxMsPerCall`,
  `firmware/src/scripts_bridge.cpp`) regardless of what the script asked for, and the on-device cancel key
  interrupts it immediately.
- `wifi_ap_count() -> int` - read-only. Reports the count from the most recent completed Wi-Fi survey
  (Security > Wi-Fi Survey). It never starts a new survey itself.

Everything else PikaPython ships (`print`, `range`, `len`, lists, dicts, strings, basic control flow) is
available, since the generated bindings include `builtins`, `PikaStdLib` and `PikaStdData` alongside
`Oyster`. A script can only `import` what was bound at generation time - there is no `os`, no arbitrary
native module, and no way to script Wi-Fi/BLE/IR/the display beyond what `Oyster` exposes above.

## Extending it

The whole surface is generated from one interface file, `firmware/lib/pikascript/Oyster.pyi` (kept next to
the vendored sources, not consumed by the Arduino build itself - it is the *input* to the step below).
Adding a method:

1. Add it to `Oyster.pyi` with a Python type signature.
2. Run PikaPython's own compiler (built from its own Rust source, `tools/pikaCompiler` in the
   [PikaPython repo](https://github.com/pikasTech/PikaPython); never the project's prebuilt `.exe`) against
   a project containing that `.pyi` plus `main.py`/`requestment.txt`/the stdlib `.pyi` files - see the
   PikaPython docs linked above for the exact layout. It writes `pikascript-api/Oyster_Oyster.h` with the
   new C signature.
3. Implement the new function in `firmware/lib/pikascript/src/Oyster_Oyster.c` (one line, matching the
   generated signature) and its real body as a plain-C function declared in
   `firmware/lib/pikascript/include/oyster_script_bridge.h`, implemented in C++ in
   `firmware/src/scripts_bridge.cpp` where it can reach the rest of the firmware.
4. Re-copy the regenerated `pikascript-api/*.h` and `__pikaBinding.c` over the vendored copies and rebuild.

Nothing about this requires re-running the generator on every firmware build - only when the bound surface
(the set of classes/methods a script can call) changes.

## What this firmware build leaves out, on purpose

- No file-system bridge: every `pika_platform_f*`, path and directory hook is overridden in
  `firmware/src/scripts_bridge.cpp` to fail closed (the interpreter's weak defaults would spin forever and
  freeze the device). Scripts cannot open arbitrary files, only the one they were started from (loaded by the Scripts module itself, through the SD API the rest of this firmware already
  uses, not through the interpreter's own file layer).
- No REPL, no serial script upload: PikaPython's `pika_platform_getchar`/`repl_recv` are left at their
  do-nothing weak defaults. A script is always a complete file run once, start to finish.
- No radio-triggering calls (no new Wi-Fi/BLE scan, no IR send, no Control Deck action) from a script in
  this build. `wifi_ap_count()` only reads what a prior, owner-started survey already found. Extending the
  `Oyster` module to trigger operations is possible (see above) but is a deliberate, separate decision, not
  something this version does.
