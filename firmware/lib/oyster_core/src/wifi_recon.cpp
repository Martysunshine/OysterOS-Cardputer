// Implementation of oyster_core/wifi_recon.h. Pure logic, no Arduino headers; see the header for the
// contract. All loops are over small bounded collections (one scan is <= 32 APs, the table <= 128).
#include "oyster_core/wifi_recon.h"

#include <cstdio>
#include <cstring>

namespace oyster::wifi_recon {

namespace {

bool is_printable(uint8_t b) { return b >= 0x20 && b <= 0x7E; }

bool same_bssid(const uint8_t* a, const uint8_t* b) { return std::memcmp(a, b, 6) == 0; }

void copy_ssid(ApEntry& e, const ApObservation& ap) {
  uint8_t len = ap.ssid_len;
  if (len > kSsidBytes) len = kSsidBytes;
  std::memcpy(e.ssid, ap.ssid, len);
  e.ssid_len = len;
}

}  // namespace

void ApTable::clear() {
  count_ = 0;
  dropped_ = 0;
}

int ApTable::find(const uint8_t bssid[6]) const {
  for (size_t i = 0; i < count_; ++i) {
    if (same_bssid(entries_[i].bssid, bssid)) return static_cast<int>(i);
  }
  return -1;
}

size_t ApTable::weakest() const {
  size_t w = 0;
  for (size_t i = 1; i < count_; ++i) {
    const ApEntry& e = entries_[i];
    const ApEntry& b = entries_[w];
    // Weakest by max_rssi; on a tie the older entry (smaller first_seen_ms) is the eviction candidate.
    if (e.max_rssi < b.max_rssi || (e.max_rssi == b.max_rssi && e.first_seen_ms < b.first_seen_ms)) w = i;
  }
  return w;
}

Merge ApTable::merge(const ApObservation& ap, uint32_t now_ms) {
  const int idx = find(ap.bssid);
  if (idx >= 0) {
    ApEntry& e = entries_[static_cast<size_t>(idx)];
    e.last_rssi = ap.rssi_dbm;
    if (ap.rssi_dbm > e.max_rssi) e.max_rssi = ap.rssi_dbm;
    e.channel = ap.channel;
    e.auth = ap.auth;
    e.last_seen_ms = now_ms;
    if (e.times_seen < 0xFFFF) ++e.times_seen;
    if (e.ssid_len == 0 && ap.ssid_len > 0) copy_ssid(e, ap);  // a name arrived for a formerly hidden AP
    return Merge::Updated;
  }

  ApEntry fresh = {};
  std::memcpy(fresh.bssid, ap.bssid, 6);
  copy_ssid(fresh, ap);
  fresh.channel = ap.channel;
  fresh.auth = ap.auth;
  fresh.max_rssi = ap.rssi_dbm;
  fresh.last_rssi = ap.rssi_dbm;
  fresh.first_seen_ms = now_ms;
  fresh.last_seen_ms = now_ms;
  fresh.times_seen = 1;

  if (count_ < kMaxApEntries) {
    entries_[count_++] = fresh;
    return Merge::Added;
  }
  // Full: replace the weakest entry only if the newcomer is stronger, else drop the newcomer.
  const size_t w = weakest();
  ++dropped_;
  if (ap.rssi_dbm > entries_[w].max_rssi) {
    entries_[w] = fresh;
    return Merge::Added;
  }
  return Merge::Dropped;
}

void ApTable::sort_by_rssi() {
  for (size_t i = 1; i < count_; ++i) {
    ApEntry key = entries_[i];
    size_t j = i;
    while (j > 0 && entries_[j - 1].max_rssi < key.max_rssi) {
      entries_[j] = entries_[j - 1];
      --j;
    }
    entries_[j] = key;
  }
}

const char* auth_short(WifiAuth v) {
  switch (v) {
    case WifiAuth::Open: return "OPEN";
    case WifiAuth::Wep: return "WEP";
    case WifiAuth::WpaPsk: return "WPA";
    case WifiAuth::Wpa2Psk: return "WPA2";
    case WifiAuth::WpaWpa2Psk: return "WPA2";
    case WifiAuth::Wpa2Enterprise: return "WPA2E";
    case WifiAuth::Wpa3Psk: return "WPA3";
    case WifiAuth::Wpa2Wpa3Psk: return "WPA3";
    case WifiAuth::WapiPsk: return "WAPI";
    case WifiAuth::Unknown: return "?";
  }
  return "?";
}

void bssid_to_str(const uint8_t bssid[6], char* out, size_t n) {
  if (out == nullptr || n == 0) return;
  char buf[kBssidStrLen];
  std::snprintf(buf, sizeof(buf), "%02x:%02x:%02x:%02x:%02x:%02x", bssid[0], bssid[1], bssid[2], bssid[3],
                bssid[4], bssid[5]);
  std::snprintf(out, n, "%s", buf);
}

void display_ssid(const uint8_t* ssid, uint8_t ssid_len, char* out, size_t n) {
  if (out == nullptr || n == 0) return;
  if (ssid == nullptr || ssid_len == 0) {
    std::snprintf(out, n, "<hidden>");
    return;
  }
  size_t w = 0;
  for (size_t i = 0; i < ssid_len && w + 1 < n; ++i) {
    out[w++] = is_printable(ssid[i]) ? static_cast<char>(ssid[i]) : '?';
  }
  out[w] = '\0';
}

void format_row(const ApEntry& e, char* out, size_t n, size_t cols) {
  if (out == nullptr || n == 0) return;
  out[0] = '\0';
  size_t budget = cols;
  if (budget > n - 1) budget = n - 1;

  // Fixed prefix: "<rssi> ch<ch> <auth> ". Build it first, then the SSID fills the rest.
  char prefix[24];
  std::snprintf(prefix, sizeof(prefix), "%d ch%u %s ", static_cast<int>(e.max_rssi),
                static_cast<unsigned>(e.channel), auth_short(e.auth));
  size_t w = 0;
  for (size_t i = 0; prefix[i] != '\0' && w < budget; ++i) out[w++] = prefix[i];

  char name[kSsidBytes + 10];
  display_ssid(e.ssid, e.ssid_len, name, sizeof(name));
  for (size_t i = 0; name[i] != '\0' && w < budget; ++i) out[w++] = name[i];
  out[w] = '\0';
}

void format_relative(uint32_t delta_ms, char* out, size_t n) {
  if (out == nullptr || n == 0) return;
  const uint32_t s = delta_ms / 1000;
  if (s == 0) {
    std::snprintf(out, n, "now");
  } else if (s < 60) {
    std::snprintf(out, n, "%us ago", static_cast<unsigned>(s));
  } else if (s < 3600) {
    std::snprintf(out, n, "%um ago", static_cast<unsigned>(s / 60));
  } else if (s < 86400) {
    std::snprintf(out, n, "%uh ago", static_cast<unsigned>(s / 3600));
  } else {
    std::snprintf(out, n, "%ud ago", static_cast<unsigned>(s / 86400));
  }
}

const char* csv_header() { return "ts,bssid,channel,rssi,auth,hidden,ssid"; }

void csv_escape_ssid(const uint8_t* ssid, uint8_t ssid_len, char* out, size_t n) {
  if (out == nullptr || n == 0) return;
  out[0] = '\0';
  if (ssid == nullptr || ssid_len == 0) return;  // empty field

  // Sanitise into a scratch buffer (non-printable -> '?'), tracking whether quoting is needed.
  char raw[kSsidBytes + 1];
  size_t rn = 0;
  bool needs_quote = false;
  for (size_t i = 0; i < ssid_len && i < kSsidBytes; ++i) {
    char c = is_printable(ssid[i]) ? static_cast<char>(ssid[i]) : '?';
    if (c == ',' || c == '"') needs_quote = true;
    raw[rn++] = c;
  }
  raw[rn] = '\0';

  if (!needs_quote) {
    std::snprintf(out, n, "%s", raw);
    return;
  }
  size_t w = 0;
  if (w + 1 < n) out[w++] = '"';
  for (size_t i = 0; i < rn && w + 1 < n; ++i) {
    if (raw[i] == '"') {
      if (w + 2 < n) {  // room for the doubled quote plus the closing quote
        out[w++] = '"';
        out[w++] = '"';
      } else {
        break;
      }
    } else {
      out[w++] = raw[i];
    }
  }
  if (w + 1 < n) out[w++] = '"';
  out[w] = '\0';
}

void format_csv_line(const ApObservation& ap, uint32_t ts, char* out, size_t n) {
  if (out == nullptr || n == 0) return;
  char bssid[kBssidStrLen];
  bssid_to_str(ap.bssid, bssid, sizeof(bssid));
  char ssid[2 * kSsidBytes + 4];  // worst case: every byte a quote, doubled, plus surrounding quotes
  csv_escape_ssid(ap.ssid, ap.ssid_len, ssid, sizeof(ssid));
  std::snprintf(out, n, "%lu,%s,%u,%d,%s,%d,%s", static_cast<unsigned long>(ts), bssid,
                static_cast<unsigned>(ap.channel), static_cast<int>(ap.rssi_dbm), auth_short(ap.auth),
                ap.ssid_len == 0 ? 1 : 0, ssid);
}

}  // namespace oyster::wifi_recon
