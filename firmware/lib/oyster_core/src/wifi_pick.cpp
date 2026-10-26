// Implementation of oyster_core/wifi_pick.h. Pure logic, no Arduino headers; see the header for the
// contract. O(n^2) over a small bounded input (n is the driver's AP count, <= kMaxApObservations).
#include "oyster_core/wifi_pick.h"

#include <cstring>

namespace oyster::wifi_pick {

namespace {

bool is_printable(uint8_t b) { return b >= 0x20 && b <= 0x7E; }

// Index of the stored pick with the weakest RSSI (caller guarantees count > 0).
size_t weakest(const PickList& l) {
  size_t w = 0;
  for (size_t i = 1; i < l.count; ++i) {
    if (l.items[i].rssi_dbm < l.items[w].rssi_dbm) w = i;
  }
  return w;
}

void fill_pick(Pick& p, const ApObservation& ap) {
  uint8_t len = ap.ssid_len;
  if (len > kSsidBytes) len = kSsidBytes;
  std::memcpy(p.ssid, ap.ssid, len);
  p.ssid[len] = '\0';
  p.ssid_len = len;
  p.rssi_dbm = ap.rssi_dbm;
  p.auth = ap.auth;
  p.open = ap.auth == WifiAuth::Open;
}

}  // namespace

bool same_ssid(const uint8_t* a, uint8_t a_len, const uint8_t* b, uint8_t b_len) {
  return a_len == b_len && std::memcmp(a, b, a_len) == 0;
}

void build(const ApObservation* aps, size_t n, PickList& out) {
  out = PickList{};
  if (aps == nullptr) return;
  for (size_t i = 0; i < n; ++i) {
    const ApObservation& ap = aps[i];
    uint8_t len = ap.ssid_len;
    if (len > kSsidBytes) len = kSsidBytes;
    if (len == 0) {  // hidden or empty SSID: cannot be joined by name from a list
      ++out.skipped_hidden;
      continue;
    }
    // Already have this SSID? Keep the strongest sighting (and its auth).
    int found = -1;
    for (size_t j = 0; j < out.count; ++j) {
      if (same_ssid(out.items[j].ssid, out.items[j].ssid_len, ap.ssid, len)) {
        found = static_cast<int>(j);
        break;
      }
    }
    if (found >= 0) {
      ++out.merged;
      if (ap.rssi_dbm > out.items[found].rssi_dbm) {
        out.items[found].rssi_dbm = ap.rssi_dbm;
        out.items[found].auth = ap.auth;
        out.items[found].open = ap.auth == WifiAuth::Open;
      }
      continue;
    }
    if (out.count < kMaxPicks) {
      fill_pick(out.items[out.count], ap);
      ++out.count;
    } else {  // more distinct SSIDs than we can hold: keep the strongest
      out.overflow = true;
      const size_t w = weakest(out);
      if (ap.rssi_dbm > out.items[w].rssi_dbm) fill_pick(out.items[w], ap);
    }
  }
  // Sort by RSSI descending; stable (equal RSSI keeps earlier position).
  for (size_t i = 1; i < out.count; ++i) {
    Pick key = out.items[i];
    size_t j = i;
    while (j > 0 && out.items[j - 1].rssi_dbm < key.rssi_dbm) {
      out.items[j] = out.items[j - 1];
      --j;
    }
    out.items[j] = key;
  }
}

void display_ssid(const uint8_t* ssid, uint8_t ssid_len, char* out, size_t n) {
  if (out == nullptr || n == 0) return;
  if (ssid == nullptr || ssid_len == 0) {
    std::strncpy(out, "(hidden)", n);
    out[n - 1] = '\0';
    return;
  }
  size_t w = 0;
  for (size_t i = 0; i < ssid_len && w + 1 < n; ++i) {
    out[w++] = is_printable(ssid[i]) ? static_cast<char>(ssid[i]) : '?';
  }
  out[w] = '\0';
}

void format_row(const Pick& p, char* out, size_t n, size_t cols) {
  if (out == nullptr || n == 0) return;
  out[0] = '\0';
  // Visible width is the smaller of the caller's column budget and the buffer.
  size_t budget = cols;
  if (budget > n - 1) budget = n - 1;
  static const char kOpen[] = " (open)";
  const size_t open_len = sizeof(kOpen) - 1;
  // Reserve room for the " (open)" marker when the network is open and the marker fits.
  size_t ssid_budget = budget;
  const bool show_open = p.open && budget > open_len;
  if (show_open) ssid_budget = budget - open_len;

  char name[kSsidBufLen + 1];
  display_ssid(p.ssid, p.ssid_len, name, sizeof(name));
  size_t w = 0;
  for (size_t i = 0; name[i] != '\0' && w < ssid_budget; ++i) out[w++] = name[i];
  if (show_open) {
    for (size_t i = 0; i < open_len && w < budget; ++i) out[w++] = kOpen[i];
  }
  out[w] = '\0';
}

}  // namespace oyster::wifi_pick
