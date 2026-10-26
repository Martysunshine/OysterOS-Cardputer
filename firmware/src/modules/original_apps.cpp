// Home tile 6 "Original Apps": shows the original firmware kept in the ota_1 slot and how to run it.
//
// In-device booting of the original is NOT possible on this device: the original was built with a newer
// ESP-IDF (5.x) than Oyster's bootloader (Arduino core, ESP-IDF 4.x), and a 4.x bootloader cannot load a 5.x
// app, so a boot of ota_1 fails immediately and the bootloader rolls back to Oyster. Two firmwares built on
// different IDF major versions need different bootloaders, and the chip has only one bootloader slot. The
// original is therefore run by restoring the full factory image from the PC (scripts/original-restore.ps1),
// which brings its own bootloader; returning to Oyster is a re-flash. See docs/DUAL_BOOT.md, ADR-009.
//
// This module only reads the ota_1 descriptor (to show what is stored) and explains the PC steps. It never
// writes the boot selector and never restarts. All of it runs on the UI task; the slot is probed on open.
#include <esp_ota_ops.h>
#include <esp_partition.h>

#include <cstdio>
#include <cstring>

#include "modules.h"

namespace oyster::modules {
namespace {

enum class Slot : uint8_t {
  NoLayout,  // no ota_1 partition: the single-slot layout
  Empty,     // ota_1 exists and is blank (all 0xFF)
  Invalid,   // ota_1 holds something that is not an app image
  Running,   // Oyster itself runs from ota_1 (wrong flash)
  Ready,     // a valid image header and app descriptor
};

struct Probe {
  Slot slot = Slot::NoLayout;
  uint8_t first_byte = 0xFF;
  char project[32] = {};
  char version[32] = {};
  char idf[32] = {};
  char date[16] = {};
};

Probe s_probe;
bool s_probed = false;

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
  const esp_partition_t* p =
      esp_partition_find_first(ESP_PARTITION_TYPE_APP, ESP_PARTITION_SUBTYPE_APP_OTA_1, nullptr);
  if (p == nullptr) return;
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
  s_probe.slot = Slot::Ready;
}

bool available(const char** why) {
  (void)why;
  return true;
}

void open() { probe(); }

void view(PageView& v) {
  if (!s_probed) probe();
  v.kind = PageKind::Text;
  v.page_id = 0;
  std::snprintf(v.left, sizeof(v.left), "Del:Back ;.:Scroll");
  switch (s_probe.slot) {
    case Slot::Ready:
      std::snprintf(v.title, sizeof(v.title), "Original firmware");
      std::snprintf(v.body, sizeof(v.body),
                    "Kept in a backup slot:\n%s\n%s\nBuilt %s\nIDF %s\n\n"
                    "It cannot boot from here: it was built with a newer IDF than this device's bootloader, "
                    "which only loads one firmware.\n\n"
                    "To run it, on the PC:\n  scripts/original-restore.ps1\n(restores it from your verified "
                    "backup; it then runs with its own bootloader).\n"
                    "To return to Oyster:\n  scripts/firmware-flash.ps1",
                    s_probe.project, s_probe.version, s_probe.date, s_probe.idf);
      return;
    case Slot::Running:
      std::snprintf(v.title, sizeof(v.title), "Original firmware: n/a");
      std::snprintf(v.body, sizeof(v.body),
                    "Oyster itself runs from the backup slot, so there is nothing stored to restore here. "
                    "Your original firmware is in the PC backup (scripts/original-restore.ps1).");
      return;
    case Slot::Empty:
    case Slot::Invalid:
    case Slot::NoLayout:
    default:
      std::snprintf(v.title, sizeof(v.title), "Original firmware");
      std::snprintf(v.body, sizeof(v.body),
                    "Your original Cardputer firmware is kept in the PC backup, not on the device.\n\n"
                    "To run it, on the PC:\n  scripts/original-restore.ps1\n(restores it from your verified "
                    "backup).\nTo return to Oyster:\n  scripts/firmware-flash.ps1\n\nSee docs/DUAL_BOOT.md.");
      return;
  }
}

void select(uint16_t row) { (void)row; }

bool back() { return false; }

void tile_status(char* out, size_t n) {
  if (!s_probed) probe();
  std::snprintf(out, n, "%s", s_probe.slot == Slot::Ready ? "on PC" : "backup");
}

}  // namespace

const Ops kOriginalOps = {"Original Apps", available, open,    view,    select,      back,
                          nullptr,         nullptr,   nullptr, nullptr, tile_status, nullptr};

}  // namespace oyster::modules
