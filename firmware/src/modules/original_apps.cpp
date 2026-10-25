// Home tile 6 "Original Apps": boots the original firmware that scripts/dualboot-setup.ps1 copied into the ota_1
// slot (docs/DUAL_BOOT.md, ADR-009). Local only: it reads the partition table and the app descriptor in ota_1 and,
// after a confirmation, points the boot selector (otadata) at ota_1 and restarts. No network, no worker call.
//
// All of it runs on the UI task. The slot is probed when the module opens (and once for the Home tile), never
// inside view(); nothing is written before the user approves the confirmation page.
#include <Arduino.h>
#include <esp_err.h>
#include <esp_ota_ops.h>
#include <esp_partition.h>
#include <esp_system.h>

#include <cstdio>
#include <cstring>

#include "modules.h"

namespace oyster::modules {
namespace {

enum class Slot : uint8_t {
  NoLayout,  // no ota_1 partition: the single-slot layout
  Empty,     // ota_1 exists and is blank (all 0xFF)
  Invalid,   // ota_1 holds something that is not an app image
  Running,   // Oyster itself runs from ota_1 (wrong flash): nothing to switch to
  Ready,     // a valid image header and app descriptor
};

enum class Stage : uint8_t {
  Info,       // the text page
  Confirm,    // "Boot original firmware" confirmation
  Switching,  // approved: the page is painted, then the boot partition is set and the device restarts
};

constexpr uint32_t kRestartDelayMs = 300;  // lets the "Restarting" page stay on screen

struct Probe {
  Slot slot = Slot::NoLayout;
  const esp_partition_t* part = nullptr;
  uint8_t first_byte = 0xFF;
  char project[32] = {};
  char version[32] = {};
  char idf[32] = {};
  char date[16] = {};
  char time[16] = {};
};

Probe s_probe;
bool s_probed = false;
Stage s_stage = Stage::Info;
bool s_painted = false;  // the Switching page has been drawn once
char s_error[48] = {};   // why the last attempt did not switch

// Copies descriptor text for the screen: printable ASCII only, so image contents cannot inject line breaks.
void copy_text(char* dst, size_t n, const char* src, size_t src_n) {
  size_t i = 0;
  for (; i + 1 < n && i < src_n && src[i] != '\0'; ++i) {
    const unsigned char c = static_cast<unsigned char>(src[i]);
    dst[i] = (c >= 0x20 && c <= 0x7E) ? static_cast<char>(c) : '?';
  }
  dst[i] = '\0';
}

void probe() {
  s_probe = Probe{};
  s_probed = true;
  const esp_partition_t* p = esp_partition_find_first(ESP_PARTITION_TYPE_APP, ESP_PARTITION_SUBTYPE_APP_OTA_1, nullptr);
  if (p == nullptr) return;
  s_probe.part = p;
  const esp_partition_t* run = esp_ota_get_running_partition();
  if (run != nullptr && run->address == p->address) {
    s_probe.slot = Slot::Running;
    return;
  }
  uint8_t first = 0xFF;
  if (esp_partition_read(p, 0, &first, 1) != ESP_OK) {
    s_probe.slot = Slot::Invalid;
    return;
  }
  s_probe.first_byte = first;
  if (first == 0xFF) {
    s_probe.slot = Slot::Empty;
    return;
  }
  esp_app_desc_t desc;
  if (first != 0xE9 || esp_ota_get_partition_description(p, &desc) != ESP_OK) {
    s_probe.slot = Slot::Invalid;
    return;
  }
  copy_text(s_probe.project, sizeof(s_probe.project), desc.project_name, sizeof(desc.project_name));
  copy_text(s_probe.version, sizeof(s_probe.version), desc.version, sizeof(desc.version));
  copy_text(s_probe.idf, sizeof(s_probe.idf), desc.idf_ver, sizeof(desc.idf_ver));
  copy_text(s_probe.date, sizeof(s_probe.date), desc.date, sizeof(desc.date));
  copy_text(s_probe.time, sizeof(s_probe.time), desc.time, sizeof(desc.time));
  s_probe.slot = Slot::Ready;
}

// Runs on the UI task after the approval and after the "switching" page was painted. The image check inside
// esp_ota_set_boot_partition reads the whole slot, so this blocks for a moment; there is no network involved.
void apply() {
  const esp_err_t err = esp_ota_set_boot_partition(s_probe.part);
  if (err != ESP_OK) {
    s_stage = Stage::Info;
    std::snprintf(s_error, sizeof(s_error), "Not switched: %s",
                  err == ESP_ERR_OTA_VALIDATE_FAILED ? "image check failed" : esp_err_to_name(err));
    toast("Switch failed");
    return;
  }
  toast("Restarting");
  delay(kRestartDelayMs);
  esp_restart();
}

bool available(const char** why) {
  (void)why;
  return true;
}

void open() {
  probe();
  s_stage = Stage::Info;
  s_painted = false;
  s_error[0] = '\0';
}

void build_info(PageView& v) {
  v.kind = PageKind::Text;
  v.page_id = 0;
  const char* err_sep = s_error[0] != '\0' ? "\n\n" : "";
  switch (s_probe.slot) {
    case Slot::Ready:
      std::snprintf(v.title, sizeof(v.title), "Original firmware");
      std::snprintf(v.body, sizeof(v.body),
                    "%s%sIn slot ota_1:\n%s\n%s\nBuilt %s %s\nIDF %s\n\nEnter boots it, after a confirmation. "
                    "A reset should bring Oyster back; if it does not, run scripts/oyster-return.ps1 on the PC.",
                    s_error, err_sep, s_probe.project, s_probe.version, s_probe.date, s_probe.time, s_probe.idf);
      std::snprintf(v.left, sizeof(v.left), "Ent:Boot Del:Back ;.:Scroll");
      return;
    case Slot::Empty:
      std::snprintf(v.title, sizeof(v.title), "Original firmware: not set up");
      std::snprintf(v.body, sizeof(v.body),
                    "%s%sThe dual-boot layout is installed, but slot ota_1 is empty. "
                    "On the PC run scripts/dualboot-setup.ps1 -Apply (docs/DUAL_BOOT.md).",
                    s_error, err_sep);
      break;
    case Slot::Invalid:
      std::snprintf(v.title, sizeof(v.title), "Original firmware: invalid");
      std::snprintf(v.body, sizeof(v.body),
                    "%s%sSlot ota_1 holds no valid app image (first byte 0x%02X). "
                    "On the PC run scripts/dualboot-setup.ps1 -Apply to copy it again (docs/DUAL_BOOT.md).",
                    s_error, err_sep, static_cast<unsigned>(s_probe.first_byte));
      break;
    case Slot::Running:
      std::snprintf(v.title, sizeof(v.title), "Original firmware: n/a");
      std::snprintf(v.body, sizeof(v.body),
                    "%s%sOyster itself runs from slot ota_1, so there is nothing to switch to. "
                    "Flash the dual-boot build again (docs/DUAL_BOOT.md).",
                    s_error, err_sep);
      break;
    case Slot::NoLayout:
      std::snprintf(v.title, sizeof(v.title), "Original firmware: not set up");
      std::snprintf(v.body, sizeof(v.body),
                    "%s%sThis build has a single app slot, so the original firmware is not kept next to Oyster. "
                    "On the PC run scripts/dualboot-setup.ps1 and flash the dual-boot build (docs/DUAL_BOOT.md).",
                    s_error, err_sep);
      break;
  }
  std::snprintf(v.left, sizeof(v.left), "Del:Back ;.:Scroll");
}

void view(PageView& v) {
  if (!s_probed) probe();
  switch (s_stage) {
    case Stage::Confirm: {
      v.kind = PageKind::Confirm;
      v.page_id = 1;
      std::snprintf(v.title, sizeof(v.title), "Original firmware");
      std::snprintf(v.confirm_operation, sizeof(v.confirm_operation), "Boot original firmware");
      // The target row shows 20 characters: the version alone when "ota_1 " plus the version does not fit.
      if (std::strlen(s_probe.version) + 6 <= 20) {
        std::snprintf(v.confirm_target, sizeof(v.confirm_target), "ota_1 %s", s_probe.version);
      } else {
        std::snprintf(v.confirm_target, sizeof(v.confirm_target), "%s", s_probe.version);
      }
      std::snprintf(v.confirm_effect, sizeof(v.confirm_effect),
                    "Restarts into ota_1. A reset should bring Oyster back, else run oyster-return.ps1");
      std::snprintf(v.left, sizeof(v.left), "Ent:Boot Del:Cancel");
      return;
    }
    case Stage::Switching:
      v.kind = PageKind::Text;
      v.page_id = 2;
      std::snprintf(v.title, sizeof(v.title), "Original firmware");
      std::snprintf(v.body, sizeof(v.body),
                    "Restarting into the original firmware.\n\nTo come back: reset or power-cycle. If Oyster does not "
                    "return, run scripts/oyster-return.ps1 on the PC.");
      std::snprintf(v.left, sizeof(v.left), "Please wait");
      return;
    case Stage::Info:
      break;
  }
  build_info(v);
}

void select(uint16_t row) {
  (void)row;
  if (s_stage == Stage::Info) {
    probe();  // the confirmation names what is in the slot now
    s_error[0] = '\0';
    if (s_probe.slot == Slot::Ready) s_stage = Stage::Confirm;
  } else if (s_stage == Stage::Confirm) {
    s_stage = Stage::Switching;
    s_painted = false;
  }
}

bool back() {
  if (s_stage == Stage::Confirm) {  // Back rejects
    s_stage = Stage::Info;
    return true;
  }
  return s_stage == Stage::Switching;  // no leaving between the approval and the restart
}

bool tick(uint32_t now_ms) {
  (void)now_ms;
  if (s_stage != Stage::Switching) return false;
  if (!s_painted) {  // the page is drawn after this tick; the blocking work happens on the next one
    s_painted = true;
    return true;
  }
  apply();
  return true;
}

// Emergency cancel drops a pending confirmation or an approval that has not run yet. Once apply() runs it blocks the
// UI task until the restart, so there is nothing left to cancel.
void cancel() { s_stage = Stage::Info; }

void tile_status(char* out, size_t n) {
  if (!s_probed) probe();
  std::snprintf(out, n, "%s", s_probe.slot == Slot::Ready ? "ready" : "not set up");
}

}  // namespace

const Ops kOriginalOps = {"Original Apps", available, open, view, select, back, nullptr, tick, cancel, nullptr, tile_status};

}  // namespace oyster::modules
