// Scripts module (Red Team tile): a reference to the separate Evil-M5Project firmware plus on-device Python
// scripting through the vendored PikaScript interpreter (lib/pikascript), bound to a small, read-only-leaning
// Oyster module (Oyster.log, Oyster.sleep_ms, Oyster.wifi_ap_count - see docs/SCRIPTS.md). Works with the
// laptop off: a built-in example runs with no SD card, and any .py files under /oyster/scripts on a microSD
// card are listed and run too. No backend, no session - just the interpreter.
//
// Pages: List (the .py files found under /oyster/scripts) -> Result (Text: the script's print()/log output).
// Threads: open/view/select/back/key/tick/cancel run on the UI task; call() runs on the worker task, which
// is also the only task that ever touches the interpreter (one script at a time, start to finish, before
// the next UI request is drained - the same rule every other worker-task operation in this firmware follows).
#include <Arduino.h>
#include <SD.h>

#include <cstdio>
#include <cstring>

#include "PikaObj.h"  // wraps its own declarations in extern "C"
#include "PikaVM.h"   // ditto
// PikaMain.h is generated per-project (rust-msc) and, unlike the vendored core headers above, does not
// wrap New_PikaMain in extern "C" itself - without this, a C++ translation unit would look for a
// C++-mangled symbol that only exists in C (__pikaBinding.c defines it with C linkage).
extern "C" {
#include "PikaMain.h"
}
#include "modules.h"
#include "oyster_script_bridge.h"  // oyster_script_log: the same sink pika_platform_printf writes to
#include "scripts_bridge.h"

namespace oyster::modules {
namespace {

constexpr uint8_t kCallRun = 1;
constexpr size_t kMaxScripts = 12;
constexpr size_t kNameLen = 28;
constexpr size_t kSourceCap = 4096;  // a script larger than this is refused, not truncated and half-run
const char* const kScriptDir = "/oyster/scripts";

constexpr uint8_t kBuiltinRows = 2;      // row 0 = About/reference, row 1 = the built-in example
constexpr uint8_t kCallAbout = 2;        // show the reference text (no interpreter, no SD)
constexpr uint8_t kCallRunBuiltin = 3;   // run the compiled-in example (no SD)

// Restored Red Team reference plus a runnable example compiled into the firmware, so the tile is useful
// with no SD card. Any .py files on a microSD card under /oyster/scripts are listed after these two rows.
const char* const kAboutText =
    "Scripting runs on the device with the laptop off (PikaScript).\n"
    "Bindings: o = Oyster.Oyster(), then o.log(text), o.sleep_ms(ms), o.wifi_ap_count().\n"
    "Add your own .py files under /oyster/scripts on a microSD card and they appear here.\n\n"
    "Full offensive Wi-Fi firmware for this board is a separate project you flash yourself:\n"
    "Evil-M5Project by 7h30th3r0n3\n"
    "github.com/7h30th3r0n3/Evil-M5Project\n\n"
    "Its attacks (deauth, captive portals, Karma, beacon spam) are not built into Oyster.";

const char* const kExampleSource =
    "import Oyster\n"
    "o = Oyster.Oyster()\n"
    "o.log(\"Hello from Oyster scripting.\\n\")\n"
    "o.log(\"Counting Wi-Fi APs...\\n\")\n"
    "n = o.wifi_ap_count()\n"
    "print(n)\n"
    "o.sleep_ms(200)\n"
    "o.log(\"Done.\\n\")\n";

enum class Level : uint8_t { List, Result };

struct State {
  Level level = Level::List;
  char names[kMaxScripts][kNameLen] = {};
  size_t count = 0;
  bool scanned = false;
  bool sd_present = false;
  char ran_name[kNameLen] = "";
  bool last_ok = false;
  bool showing_about = false;
  uint32_t runs = 0;
};

State s_state;

void scan_dir() {
  s_state.count = 0;
  s_state.sd_present = SD.cardType() != CARD_NONE;
  if (!s_state.sd_present) return;
  if (!SD.exists(kScriptDir)) SD.mkdir(kScriptDir);
  File dir = SD.open(kScriptDir);
  if (!dir || !dir.isDirectory()) return;
  for (File f = dir.openNextFile(); f; f = dir.openNextFile()) {
    if (f.isDirectory()) continue;
    const char* n = f.name();
    const char* base = std::strrchr(n, '/');
    base = base != nullptr ? base + 1 : n;
    const size_t len = std::strlen(base);
    if (len < 4 || std::strcmp(base + len - 3, ".py") != 0) continue;  // only *.py
    if (s_state.count < kMaxScripts) {
      std::snprintf(s_state.names[s_state.count], kNameLen, "%s", base);
      ++s_state.count;
    }
  }
  s_state.scanned = true;
}

bool full_path(const char* name, char* out, size_t n) {
  if (name == nullptr || name[0] == '\0') return false;
  std::snprintf(out, n, "%s/%s", kScriptDir, name);
  return true;
}

void view_list(PageView& v) {
  v.kind = PageKind::List;
  v.page_id = 0;
  std::snprintf(v.title, sizeof(v.title), "Red Team");
  v.count = static_cast<uint16_t>(kBuiltinRows + s_state.count);
  v.row = [](uint16_t i, char* out, size_t n) {
    Guard g;
    if (i == 0) {
      std::snprintf(out, n, "About / Evil-M5 reference");
    } else if (i == 1) {
      std::snprintf(out, n, "Run example (built-in)");
    } else if (i - kBuiltinRows < s_state.count) {
      std::snprintf(out, n, "%s", s_state.names[i - kBuiltinRows]);
    } else {
      out[0] = '\0';
    }
  };
  // About + the built-in example are always present, so the tile never dead-ends on a missing SD card.
  v.empty = "";
  std::snprintf(v.left, sizeof(v.left), "Ent:Open Del:Back");
}

void view_result(PageView& v) {
  v.kind = PageKind::Text;
  v.page_id = 1;
  if (s_state.showing_about) {
    std::snprintf(v.title, sizeof(v.title), "Red Team");
    std::snprintf(v.body, sizeof(v.body), "%s", kAboutText);
    std::snprintf(v.left, sizeof(v.left), "Del:Back");
    return;
  }
  std::snprintf(v.title, sizeof(v.title), "%s%s", s_state.last_ok ? "" : "!", s_state.ran_name);
  char log[sizeof(v.body)];
  scripts::read_log(log, sizeof(log));
  if (log[0] != '\0') {
    std::snprintf(v.body, sizeof(v.body), "%s", log);
  } else {
    std::snprintf(v.body, sizeof(v.body), "(no output)");
  }
  std::snprintf(v.left, sizeof(v.left), "Del:Back");
}

// ---- Ops ------------------------------------------------------------------------------------------------

bool mod_available(const char** why) {
  (void)why;
  return true;
}

void mod_open() {
  Guard g;
  s_state = State{};
  scan_dir();
}

void mod_view(PageView& v) {
  Guard g;
  switch (s_state.level) {
    case Level::List: view_list(v); break;
    case Level::Result: view_result(v); break;
  }
}

void mod_select(uint16_t row) {
  Guard g;
  if (s_state.level != Level::List) return;
  if (row == 0) {
    request(Id::Scripts, kCallAbout, nullptr);
    return;
  }
  if (row == 1) {
    request(Id::Scripts, kCallRunBuiltin, "example");
    return;
  }
  const uint16_t idx = static_cast<uint16_t>(row - kBuiltinRows);
  if (idx < s_state.count) request(Id::Scripts, kCallRun, s_state.names[idx]);
}

bool mod_back() {
  Guard g;
  if (s_state.level == Level::Result) {
    s_state.level = Level::List;
    return true;
  }
  return false;  // List: let Home pop the module as usual
}

bool mod_key(NavEvent e, char c) {
  (void)e;
  (void)c;
  return false;
}

bool mod_tick(uint32_t now_ms) {
  (void)now_ms;
  return false;
}

void mod_cancel() { scripts::request_cancel(); }

void mod_call(uint8_t call_id, const char* arg) {
  // About: no interpreter and no SD - just show the restored reference text on the Result page.
  if (call_id == kCallAbout) {
    {
      Guard g;
      s_state.showing_about = true;
      std::snprintf(s_state.ran_name, sizeof(s_state.ran_name), "Red Team");
      s_state.last_ok = true;
      s_state.level = Level::Result;
    }
    changed();
    return;
  }
  if (call_id != kCallRun && call_id != kCallRunBuiltin) return;

  scripts::clear_cancel();
  scripts::reset_log();
  bool ok = false;
  const char* name = "example";
  // Heap, not a resident 4 KiB static: the buffer exists only while a script runs (never during a TLS
  // connection), so that RAM stays free for the mbedTLS handshake the rest of the time.
  char* source = static_cast<char*>(malloc(kSourceCap));
  if (source == nullptr) {
    oyster_script_log("Out of memory to load the script\n");
  } else {
    source[0] = '\0';
    if (call_id == kCallRunBuiltin) {
      std::snprintf(source, kSourceCap, "%s", kExampleSource);  // compiled-in, runs with no SD card
    } else {
      name = arg;
      char path[64];  // "/oyster/scripts/" (16) + a name up to kNameLen-1 + NUL comfortably fits
      if (full_path(arg, path, sizeof(path))) {
        File f = SD.open(path, FILE_READ);
        if (!f) {
          oyster_script_log("Could not open the script file\n");
        } else {
          const size_t size = static_cast<size_t>(f.size());
          if (size >= kSourceCap) {
            char msg[96];
            std::snprintf(msg, sizeof(msg), "Script too large: %u bytes (max %u)\n",
                          static_cast<unsigned>(size), static_cast<unsigned>(kSourceCap - 1));
            oyster_script_log(msg);
          } else {
            const size_t rd = f.read(reinterpret_cast<uint8_t*>(source), size);
            source[rd] = '\0';
          }
          f.close();
        }
      }
    }
    if (source[0] != '\0') {
      PikaObj* root = newRootObj(const_cast<char*>("pikaMain"), New_PikaMain);
      if (root == nullptr) {
        oyster_script_log("Could not start the interpreter (out of memory)\n");
      } else {
        pikaVM_run(root, source);
        ok = !scripts::cancel_requested();
        obj_deinit(root);
      }
    }
    free(source);
  }

  {
    Guard g;
    s_state.showing_about = false;
    std::snprintf(s_state.ran_name, sizeof(s_state.ran_name), "%s", name);
    s_state.last_ok = ok;
    s_state.level = Level::Result;
    ++s_state.runs;
  }
  changed();
}

void mod_tile_status(char* out, size_t n) {
  Guard g;
  // The tile always has About + the built-in example; SD scripts (if any) add to that, so never "no SD".
  if (s_state.count == 0) {
    std::snprintf(out, n, "scripts");
  } else {
    std::snprintf(out, n, "%u script%s", static_cast<unsigned>(s_state.count), s_state.count == 1 ? "" : "s");
  }
}

}  // namespace

const Ops kScriptsOps = {"Scripts", mod_available, mod_open,   mod_view,        mod_select, mod_back,
                          mod_key,  mod_tick,      mod_cancel, mod_call,        mod_tile_status,
                          nullptr};

}  // namespace oyster::modules
