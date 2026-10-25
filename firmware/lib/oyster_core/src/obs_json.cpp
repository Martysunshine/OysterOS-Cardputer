#include "oyster_core/obs_json.h"

namespace oyster {

namespace {

const char* gatt_kind_str(GattKind k) {
  switch (k) {
    case GattKind::Service:        return "service";
    case GattKind::Characteristic: return "characteristic";
    case GattKind::Descriptor:     return "descriptor";
  }
  return "service";
}

}  // namespace

void write_ap_json(JsonWriter& w, const ApObservation& ap) {
  w.begin_object();
  w.hex("ssid_hex", ap.ssid, ap.ssid_len);
  char disp[cfg::kMaxSsidBytes + 1];
  if (ap.ssid_len > 0 && display_safe(ap.ssid, ap.ssid_len, disp, sizeof(disp))) {
    w.str("ssid_display", disp);
  } else {
    w.null("ssid_display");
  }
  char mac[18];
  format_mac(ap.bssid, mac, sizeof(mac));
  w.str("bssid", mac);
  w.num("channel", ap.channel);
  w.num("rssi_dbm", ap.rssi_dbm);
  w.str("auth_mode", to_string(ap.auth));
  w.str("wps", "unknown");
  w.str("pmf", "unknown");
  w.end_object();
}

void write_adv_json(JsonWriter& w, const BleAdvObservation& o) {
  w.begin_object();
  w.unum("obs_id", o.obs_id);
  char mac[18];
  format_mac(o.addr, mac, sizeof(mac));
  w.str("address", mac);
  w.str("address_type", to_string(o.addr_type));
  w.num("rssi_dbm", o.rssi_dbm);
  w.tribool("connectable", o.connectable);
  w.hex("name_hex", o.name, o.name_len);
  char disp[cfg::kMaxBleNameBytes + 1];
  if (o.name_len > 0 && display_safe(o.name, o.name_len, disp, sizeof(disp))) {
    w.str("name_display", disp);
  } else {
    w.null("name_display");
  }
  w.begin_array("service_uuids");
  for (uint8_t k = 0; k < o.svc_uuid_count && k < cfg::kMaxSvcUuidsPerAdv; ++k) {
    char u[cfg::kUuidStrLen + 1];
    format_uuid128(o.svc_uuid128[k], u, sizeof(u));
    w.str_value(u);
  }
  w.end_array();
  w.boolean("service_uuids_truncated", o.svc_uuids_truncated);
  w.hex("mfg_data_hex", o.mfg_data, o.mfg_len);
  w.boolean("mfg_data_truncated", o.mfg_truncated);
  w.boolean("malformed", o.malformed);
  w.unum("first_seen_ms", o.first_seen_ms);
  w.unum("last_seen_ms", o.last_seen_ms);
  w.unum("seen_count", o.seen_count);
  w.end_object();
}

void write_gatt_entry_json(JsonWriter& w, const GattEntry& e) {
  w.begin_object();
  w.str("kind", gatt_kind_str(e.kind));
  char u[cfg::kUuidStrLen + 1];
  format_uuid128(e.uuid128, u, sizeof(u));
  w.str("uuid", u);
  w.num("uuid_bits", e.uuid_bits);
  w.unum("handle", e.handle);
  w.num("properties", e.props);
  if (e.parent == 0xFF) {
    w.null("parent");
  } else {
    w.num("parent", e.parent);
  }
  w.end_object();
}

}  // namespace oyster
