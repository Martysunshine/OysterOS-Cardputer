// BleExplorer: NimBLE-Arduino 2.x observer/central. Discovery never connects; connections only to an
// enrolled target whose address the caller resolved. Values the stack does not expose are recorded as
// unknown/null. All module state is file-local (the header declares no data members).
//
// Identity (fail closed): after the link is up the FIRST and only thing done is a Read-By-Type of the
// profile's public device_info value (ATT, whole handle range). That needs no service enumeration and
// cannot trigger NimBLE's automatic pairing retry. Only an exact "<profile_id>;<version>" match lets
// connect() succeed; every other outcome closes the link before anything else is read. Bonding is off
// until then. The value is public, so a match is an application-level consistency check, not
// authentication.
//
// Cancel: connect is asynchronous and polled so a cancel interrupts it. The GATT enumeration and read
// loops check for cancel / deadline between steps. A single in-flight ATT request (one service, one
// characteristic or descriptor discovery, one readValue) cannot be interrupted by this module's own
// task; cancel() called from another task tears the link down, which aborts that step.
#include "ble_explorer.h"

#include <Arduino.h>
#include <NimBLEDevice.h>
#include <freertos/FreeRTOS.h>
#include <freertos/semphr.h>

#include <atomic>
#include <cstdio>
#include <cstring>
#include <string>

#include "oyster_core/ids.h"
#include "oyster_core/json_writer.h"
#include "oyster_core/lab_identity.h"
#include "oyster_core/lab_profile_gen.h"
#include "radio_scheduler.h"

namespace oyster {

BleExplorer g_ble;

namespace {

constexpr uint16_t kGattMax = 128;            // flattened services + characteristics + descriptors
constexpr uint32_t kScanEndGraceMs = 400;     // isScanning() drops slightly before onScanEnd() runs
constexpr uint32_t kConnectPollMs = 50;       // cancel latency while a connect is pending
constexpr uint32_t kConnectSlackMs = 500;     // over the stack's own connect timeout before we cancel
constexpr uint32_t kTeardownWaitMs = 2000;    // wait for a cancel / disconnect to complete
constexpr uint32_t kIdentityReadMs = 5000;    // public device_info read
constexpr size_t kIdentityBuf = sizeof(ConnectedTargetInfo::identity_value);

SemaphoreHandle_t s_buf_mtx = nullptr;
SemaphoreHandle_t s_gatt_mtx = nullptr;  // guards s_gatt / s_gatt_total (written by the worker, read by the UI)

// Discovery accumulation buffer. onResult() runs on the NimBLE host task; poll on the worker task.
BleAdvObservation s_adv[cfg::kMaxBleAdverts];
uint8_t s_adv_count = 0;
uint16_t s_overflow = 0;
uint16_t s_malformed = 0;
uint32_t s_callbacks = 0;
uint32_t s_dropped = 0;
uint16_t s_obs_seq = 0;
bool s_frozen = true;       // set at a terminal state: late callbacks are ignored (guarded by s_buf_mtx)
bool s_limit_hit = false;   // guarded by s_buf_mtx

std::atomic<bool> s_scan_ended{false};
std::atomic<int> s_scan_end_reason{-1};
std::atomic<bool> s_cancel{false};
uint32_t s_scan_started_ms = 0;
uint32_t s_idle_since_ms = 0;  // first poll that saw the scan not running without an end event
BleDiscoveryConfig s_scan_cfg;
uint32_t s_final_duration_ms = 0;
bool s_final_complete = false;
bool s_final_cancelled = false;
PollState s_final_state = PollState::Done;

enum class ConnState : uint8_t { Idle, Connecting, Connected, Failed };

NimBLEClient* s_client = nullptr;  // created once and reused: no create/delete race with the host task
std::atomic<ConnState> s_conn_state{ConnState::Idle};
std::atomic<int> s_conn_fail_reason{0};
std::atomic<bool> s_blocking{false};  // a blocking GATT step is in flight
ConnectedTargetInfo s_conn_info;
SecurityState s_sec;
std::atomic<int> s_disc_reason{-1};
bool s_verified = false;  // identity check passed on the current link

// Limits of the current session (set_limits()).
bool s_has_deadline = false;
uint32_t s_deadline_ms = 0;
uint16_t s_max_entries = 0;

// Flattened GATT snapshot (built once on page 0 of a discovery).
enum class GattStop : uint8_t { None, Cancelled, Deadline, Capacity, EntryLimit, NoServices, LinkLost };
GattEntry s_gatt[kGattMax];
uint16_t s_gatt_total = 0;
bool s_gatt_complete = false;
GattStop s_gatt_stop = GattStop::None;

bool cancel_pending() { return s_cancel.load() || g_radio.cancel_requested(); }
bool deadline_hit() { return s_has_deadline && static_cast<int32_t>(millis() - s_deadline_ms) >= 0; }

BleAddrType map_addr_type(uint8_t t) {
  if (t == 0 || t == 2) return BleAddrType::Public;   // public / public identity
  if (t == 1 || t == 3) return BleAddrType::Random;   // random / random identity
  return BleAddrType::Unknown;
}

uint8_t to_nimble_type(BleAddrType t) { return (t == BleAddrType::Random) ? 1 : 0; }

// Canonical big-endian (string order) 16 bytes from any UUID width.
void uuid_to_be(NimBLEUUID u, uint8_t out[16], uint8_t& bits) {
  bits = u.bitSize();
  u.to128();
  const uint8_t* v = u.getValue();  // little-endian
  if (v == nullptr) {
    std::memset(out, 0, 16);
    return;
  }
  for (int i = 0; i < 16; ++i) out[i] = v[15 - i];
}

bool sec_equal(const SecurityState& a, const SecurityState& b) {
  return a.encrypted == b.encrypted && a.authenticated_pairing == b.authenticated_pairing &&
         a.application_authorized == b.application_authorized;
}

SecurityState read_sec() {
  SecurityState s;  // all unknown by default
  if (s_client != nullptr && s_client->isConnected()) {
    NimBLEConnInfo ci = s_client->getConnInfo();
    s.encrypted = TriBool::of(ci.isEncrypted());
    s.authenticated_pairing = TriBool::of(ci.isAuthenticated());
    // Application-level authorisation is not a link property the stack can report.
    s.application_authorized = TriBool::unknown();
  }
  return s;
}

BleOpStatus new_status() {
  BleOpStatus st{};
  st.status = EventStatus::Error;
  st.nimble_rc = 0;
  st.disconnect_reason = -1;
  st.att_status_known = false;
  st.att_status = 0;
  st.state_changed = false;
  st.detail[0] = '\0';
  return st;
}

// Pairing and bonding stay off until the peer's identity is verified (see connect()).
void set_pairing_policy(bool verified_target) {
  NimBLEDevice::setSecurityAuth(/*bonding=*/verified_target, /*mitm=*/false, /*sc=*/true);
}

class DiscoveryCallbacks : public NimBLEScanCallbacks {
  void onResult(const NimBLEAdvertisedDevice* d) override {
    if (s_buf_mtx == nullptr) return;
    if (xSemaphoreTake(s_buf_mtx, pdMS_TO_TICKS(10)) != pdTRUE) {
      ++s_dropped;
      return;
    }
    if (s_frozen) {  // the discovery already reached a terminal state
      xSemaphoreGive(s_buf_mtx);
      return;
    }
    ++s_callbacks;

    const uint8_t* val = d->getAddress().getVal();  // little-endian
    uint8_t addr[6];
    for (int i = 0; i < 6; ++i) addr[i] = val[5 - i];
    BleAddrType at = map_addr_type(d->getAddressType());

    int idx = -1;
    for (int i = 0; i < s_adv_count; ++i) {
      if (s_adv[i].addr_type == at && std::memcmp(s_adv[i].addr, addr, 6) == 0) {
        idx = i;
        break;
      }
    }
    BleAdvObservation* o;
    if (idx < 0) {
      const bool at_limit = s_scan_cfg.max_results != 0 && s_adv_count >= s_scan_cfg.max_results;
      if (s_adv_count >= cfg::kMaxBleAdverts || at_limit) {
        ++s_overflow;
        if (at_limit) s_limit_hit = true;
        xSemaphoreGive(s_buf_mtx);
        return;
      }
      o = &s_adv[s_adv_count++];
      std::memset(o, 0, sizeof(*o));
      o->obs_id = ++s_obs_seq;
      std::memcpy(o->addr, addr, 6);
      o->addr_type = at;
      o->first_seen_ms = millis();
      o->seen_count = 0;
    } else {
      o = &s_adv[idx];
    }
    o->last_seen_ms = millis();
    if (o->seen_count < 0xFFFF) ++o->seen_count;
    o->rssi_dbm = d->getRSSI();
    o->connectable = TriBool::of(d->isConnectable());

    bool malformed = false;
    if (d->haveName()) {
      std::string nm = d->getName();
      size_t nl = nm.size();
      if (nl > cfg::kMaxBleNameBytes) {
        nl = cfg::kMaxBleNameBytes;
        malformed = true;
      }
      std::memcpy(o->name, nm.data(), nl);
      o->name_len = static_cast<uint8_t>(nl);
    }

    uint8_t sc = d->getServiceUUIDCount();
    uint8_t keep = (sc > cfg::kMaxSvcUuidsPerAdv) ? cfg::kMaxSvcUuidsPerAdv : sc;
    o->svc_uuids_truncated = (sc > cfg::kMaxSvcUuidsPerAdv);
    o->svc_uuid_count = keep;
    for (uint8_t i = 0; i < keep; ++i) {
      uint8_t bits;
      uuid_to_be(d->getServiceUUID(i), o->svc_uuid128[i], bits);
    }

    if (d->haveManufacturerData()) {
      std::string md = d->getManufacturerData(0);
      size_t ml = md.size();
      if (ml > cfg::kMaxMfgDataBytes) {
        ml = cfg::kMaxMfgDataBytes;
        o->mfg_truncated = true;
      } else {
        o->mfg_truncated = false;
      }
      std::memcpy(o->mfg_data, md.data(), ml);
      o->mfg_len = static_cast<uint8_t>(ml);
    }

    if (malformed && !o->malformed) {
      o->malformed = true;
      ++s_malformed;
    }
    xSemaphoreGive(s_buf_mtx);
  }

  // Fires only when the scan runs to its scheduled end (reason 0) or the host aborts it; an application
  // stop() does not report here.
  void onScanEnd(const NimBLEScanResults&, int reason) override {
    s_scan_end_reason.store(reason);
    s_scan_ended.store(true);
  }
};

class ClientCallbacks : public NimBLEClientCallbacks {
  void onConnect(NimBLEClient*) override { s_conn_state.store(ConnState::Connected); }
  void onConnectFail(NimBLEClient*, int reason) override {
    s_conn_fail_reason.store(reason);
    s_conn_state.store(ConnState::Failed);
  }
  void onDisconnect(NimBLEClient*, int reason) override {
    s_disc_reason.store(reason);
    if (s_conn_state.load() == ConnState::Connected) s_conn_state.store(ConnState::Idle);
  }
};

DiscoveryCallbacks s_scan_cb;
ClientCallbacks s_client_cb;

// Counts and flags without the rows. Called with the buffer lock NOT held.
void fill_counts(BleDiscoverySummary& out) {
  std::memset(&out, 0, sizeof(out));
  if (s_buf_mtx != nullptr) xSemaphoreTake(s_buf_mtx, portMAX_DELAY);
  out.count = (s_adv_count > cfg::kMaxBleAdverts) ? cfg::kMaxBleAdverts : s_adv_count;
  out.overflow = s_overflow;
  out.malformed = s_malformed;
  out.callbacks = s_callbacks;
  out.dropped_in_callback = s_dropped;
  out.limit_hit = s_limit_hit;
  if (s_buf_mtx != nullptr) xSemaphoreGive(s_buf_mtx);
  out.active_scan = s_scan_cfg.active;
  out.interval_ms = s_scan_cfg.interval_ms;
  out.window_ms = s_scan_cfg.window_ms;
  out.started_ms = s_scan_started_ms;
  out.duration_ms = millis() - s_scan_started_ms;
}

// Freezes the working set (late callbacks are ignored from here on) and records the verdict.
PollState finalize_discovery(BleDiscoverySummary& out, PollState state, bool ran_to_end, bool cancelled) {
  if (s_buf_mtx != nullptr) xSemaphoreTake(s_buf_mtx, portMAX_DELAY);
  s_frozen = true;
  if (s_buf_mtx != nullptr) xSemaphoreGive(s_buf_mtx);
  fill_counts(out);
  out.cancelled = cancelled;
  // Complete only when the scan ran to its scheduled end and nothing was lost on the way.
  out.complete = ran_to_end && !cancelled && out.overflow == 0 && out.dropped_in_callback == 0 && !out.limit_hit;
  s_final_duration_ms = out.duration_ms;
  s_final_complete = out.complete;
  s_final_cancelled = cancelled;
  s_final_state = state;
  return state;
}

void gatt_lock() {
  if (s_gatt_mtx != nullptr) xSemaphoreTake(s_gatt_mtx, portMAX_DELAY);
}
void gatt_unlock() {
  if (s_gatt_mtx != nullptr) xSemaphoreGive(s_gatt_mtx);
}

void add_entry(GattKind kind, const NimBLEUUID& uuid, uint16_t handle, uint8_t props, uint8_t parent) {
  GattEntry e{};
  e.kind = kind;
  e.handle = handle;
  e.props = props;
  e.parent = parent;
  uint8_t bits = 0;
  uuid_to_be(uuid, e.uuid128, bits);
  e.uuid_bits = bits;
  gatt_lock();
  if (s_gatt_total < kGattMax) s_gatt[s_gatt_total++] = e;
  gatt_unlock();
}

uint8_t char_props(NimBLERemoteCharacteristic* ch) {
  uint8_t p = 0;
  if (ch->canBroadcast()) p |= 0x01;
  if (ch->canRead()) p |= 0x02;
  if (ch->canWriteNoResponse()) p |= 0x04;
  if (ch->canWrite()) p |= 0x08;
  if (ch->canNotify()) p |= 0x10;
  if (ch->canIndicate()) p |= 0x20;
  if (ch->canWriteSigned()) p |= 0x40;
  if (ch->hasExtendedProps()) p |= 0x80;
  return p;
}

// True (and s_gatt_stop set) when the enumeration must stop before the next ATT request.
bool gatt_step_blocked() {
  if (cancel_pending()) {
    s_gatt_stop = GattStop::Cancelled;
  } else if (deadline_hit()) {
    s_gatt_stop = GattStop::Deadline;
  } else if (s_client == nullptr || !s_client->isConnected()) {
    s_gatt_stop = GattStop::LinkLost;
  } else {
    return false;
  }
  s_gatt_complete = false;
  return true;
}

bool gatt_full() {
  const uint16_t cap = (s_max_entries != 0 && s_max_entries < kGattMax) ? s_max_entries : kGattMax;
  if (s_gatt_total < cap) return false;
  s_gatt_stop = (cap < kGattMax) ? GattStop::EntryLimit : GattStop::Capacity;
  s_gatt_complete = false;
  return true;
}

// Each getServices/getCharacteristics/getDescriptors call is one blocking request sequence.
void build_gatt() {
  gatt_lock();
  s_gatt_total = 0;
  gatt_unlock();
  s_gatt_complete = true;
  s_gatt_stop = GattStop::None;
  if (gatt_step_blocked()) return;
  s_blocking.store(true);
  const std::vector<NimBLERemoteService*>& svcs = s_client->getServices(true);
  s_blocking.store(false);
  if (svcs.empty()) {
    // A failed discovery and a server without services look the same here: never report it complete.
    s_gatt_complete = false;
    s_gatt_stop = (s_client != nullptr && s_client->isConnected()) ? GattStop::NoServices : GattStop::LinkLost;
    return;
  }
  for (NimBLERemoteService* svc : svcs) {
    if (gatt_step_blocked() || gatt_full()) return;
    uint8_t svc_idx = static_cast<uint8_t>(s_gatt_total);
    add_entry(GattKind::Service, svc->getUUID(), svc->getHandle(), 0, 0xFF);
    s_blocking.store(true);
    const std::vector<NimBLERemoteCharacteristic*>& chars = svc->getCharacteristics(true);
    s_blocking.store(false);
    for (NimBLERemoteCharacteristic* ch : chars) {
      if (gatt_step_blocked() || gatt_full()) return;
      uint8_t ch_idx = static_cast<uint8_t>(s_gatt_total);
      add_entry(GattKind::Characteristic, ch->getUUID(), ch->getHandle(), char_props(ch), svc_idx);
      s_blocking.store(true);
      const std::vector<NimBLERemoteDescriptor*>& dscs = ch->getDescriptors(true);
      s_blocking.store(false);
      for (NimBLERemoteDescriptor* d : dscs) {
        if (gatt_step_blocked() || gatt_full()) return;
        add_entry(GattKind::Descriptor, d->getUUID(), d->getHandle(), 0, ch_idx);
      }
    }
  }
}

// ---- identity read ----------------------------------------------------------------------------------

// State of the one Read-By-Type request used for the identity check. Written by the NimBLE host task,
// read by the worker; static so a late callback after a timeout never touches a dead stack frame.
struct IdentityRead {
  std::atomic<bool> done{false};
  int status = 0;           // terminal host status (0 / BLE_HS_EDONE = finished normally)
  uint8_t count = 0;        // attributes returned
  uint16_t value_len = 0;   // length of the first value as served (its first bytes go to s_conn_info)
};
IdentityRead s_idr;

int identity_read_cb(uint16_t, const struct ble_gatt_error* error, struct ble_gatt_attr* attr, void*) {
  if (error->status == 0) {
    if (attr != nullptr && attr->om != nullptr) {
      if (s_idr.count < 255) ++s_idr.count;
      if (s_idr.count == 1) {
        const uint16_t n = OS_MBUF_PKTLEN(attr->om);
        s_idr.value_len = n;
        const uint16_t c = n < kIdentityBuf ? n : static_cast<uint16_t>(kIdentityBuf);
        os_mbuf_copydata(attr->om, 0, c, s_conn_info.identity_value);
      }
    }
    return 0;
  }
  s_idr.status = error->status;
  s_idr.done.store(true);
  return 0;
}

enum class IdentityResult : uint8_t { Verified, Mismatch, Unreadable, Cancelled, Deadline };

void set_note(const char* s) { copy_bounded(s_conn_info.identity_note, sizeof(s_conn_info.identity_note), s); }

// Waits for a pending single request; on cancel/deadline/timeout drops the link so the request returns.
IdentityResult read_identity() {
  s_conn_info.identity_ok = TriBool::unknown();
  s_conn_info.identity_len = 0;
  s_conn_info.identity_status = -1;
  std::memset(s_conn_info.identity_value, 0, sizeof(s_conn_info.identity_value));

  const lab::CharDef* di = lab::find_char("device_info");
  uint8_t chr_be[16];
  if (di == nullptr || !parse_uuid128(di->uuid, chr_be)) {
    set_note("profile device_info unknown");
    return IdentityResult::Unreadable;
  }
  char cs[cfg::kUuidStrLen + 1];
  format_uuid128(chr_be, cs, sizeof(cs));
  const NimBLEUUID uuid{std::string(cs)};

  s_idr.done.store(false);
  s_idr.status = 0;
  s_idr.count = 0;
  s_idr.value_len = 0;

  const int rc = ble_gattc_read_by_uuid(s_client->getConnHandle(), 1, 0xFFFF, uuid.getBase(), identity_read_cb, nullptr);
  if (rc != 0) {
    s_conn_info.identity_status = rc;
    std::snprintf(s_conn_info.identity_note, sizeof(s_conn_info.identity_note), "device_info request failed rc=%d", rc);
    return IdentityResult::Unreadable;
  }

  const uint32_t t0 = millis();
  bool aborted = false;
  uint32_t abort_ms = 0;
  IdentityResult why = IdentityResult::Unreadable;
  while (!s_idr.done.load()) {
    const uint32_t now = millis();
    if (!aborted) {
      if (cancel_pending()) {
        why = IdentityResult::Cancelled;
        aborted = true;
      } else if (deadline_hit()) {
        why = IdentityResult::Deadline;
        aborted = true;
      } else if (now - t0 > kIdentityReadMs) {
        why = IdentityResult::Unreadable;
        aborted = true;
      }
      if (aborted) {
        s_client->disconnect();
        abort_ms = now;
      }
    } else if (now - abort_ms > kTeardownWaitMs) {
      break;
    }
    vTaskDelay(pdMS_TO_TICKS(20));
  }

  if (aborted) {
    s_conn_info.identity_status = (why == IdentityResult::Unreadable) ? BLE_HS_ETIMEOUT : BLE_HS_EAPP;  // attempted, not completed
    set_note(why == IdentityResult::Cancelled ? "identity check cancelled"
             : why == IdentityResult::Deadline ? "identity check hit the time limit"
                                               : "device_info read timed out");
    return why;
  }

  const int status = s_idr.status;
  s_conn_info.identity_status = (status == BLE_HS_EDONE) ? 0 : status;
  s_conn_info.identity_len = s_idr.value_len > 255 ? 255 : static_cast<uint8_t>(s_idr.value_len);

  const bool finished = (status == 0 || status == BLE_HS_EDONE);
  const bool not_found = (status == BLE_HS_ATT_ERR(BLE_ATT_ERR_ATTR_NOT_FOUND));
  if (!finished && !not_found) {
    std::snprintf(s_conn_info.identity_note, sizeof(s_conn_info.identity_note), "device_info unreadable rc=%d", status);
    return IdentityResult::Unreadable;
  }
  if (s_idr.count == 0) {
    set_note("device_info not exposed by peer");
    return IdentityResult::Mismatch;
  }
  if (s_idr.count > 1) {
    set_note("device_info served more than once");
    return IdentityResult::Mismatch;
  }
  if (s_idr.value_len <= kIdentityBuf && lab_device_info_matches(s_conn_info.identity_value, s_idr.value_len)) {
    s_conn_info.identity_ok = TriBool::of(true);
    set_note("device_info matches profile id;version");
    return IdentityResult::Verified;
  }
  set_note("device_info mismatch");
  return IdentityResult::Mismatch;
}

// Waits until the link is down (disconnect() is asynchronous). Returns false on timeout.
bool wait_link_down(uint32_t timeout_ms) {
  const uint32_t t0 = millis();
  while (s_client != nullptr && s_client->isConnected()) {
    if (millis() - t0 > timeout_ms) return false;
    vTaskDelay(pdMS_TO_TICKS(20));
  }
  return true;
}

}  // namespace

bool BleExplorer::begin() {
  if (s_buf_mtx == nullptr) s_buf_mtx = xSemaphoreCreateMutex();
  if (s_gatt_mtx == nullptr) s_gatt_mtx = xSemaphoreCreateMutex();
  if (s_buf_mtx == nullptr || s_gatt_mtx == nullptr) return false;
  if (!NimBLEDevice::isInitialized()) {
    if (!NimBLEDevice::init("")) return false;
  }
  // No pairing/bonding until a peer's identity is verified (connect() enables it for that link only).
  set_pairing_policy(false);
  NimBLEDevice::setSecurityIOCap(BLE_HS_IO_NO_INPUT_OUTPUT);
  NimBLEScan* s = NimBLEDevice::getScan();
  if (s == nullptr) return false;
  s->setScanCallbacks(&s_scan_cb, false);
  std::memset(&s_conn_info, 0, sizeof(s_conn_info));
  s_conn_info.identity_ok = TriBool::unknown();
  s_conn_info.identity_status = -1;
  return true;
}

bool BleExplorer::start_discovery(const BleDiscoveryConfig& c) {
  s_scan_cfg = c;
  if (s_buf_mtx != nullptr) xSemaphoreTake(s_buf_mtx, portMAX_DELAY);
  s_adv_count = 0;
  s_overflow = 0;
  s_malformed = 0;
  s_callbacks = 0;
  s_dropped = 0;
  s_limit_hit = false;
  s_frozen = false;
  if (s_buf_mtx != nullptr) xSemaphoreGive(s_buf_mtx);
  s_cancel.store(false);  // cancel is per operation
  s_scan_ended.store(false);
  s_scan_end_reason.store(-1);
  s_idle_since_ms = 0;
  s_scan_started_ms = millis();

  NimBLEScan* s = NimBLEDevice::getScan();
  if (s == nullptr) return false;
  s->setScanCallbacks(&s_scan_cb, false);
  s->setActiveScan(c.active);
  s->setInterval(c.interval_ms);
  s->setWindow(c.window_ms);
  s->setMaxResults(0);  // store nothing in the library; our callback owns the buffer
  bool ok = s->start(c.duration_ms, /*isContinue=*/false, /*restart=*/true);
  if (!ok) {
    if (s_buf_mtx != nullptr) xSemaphoreTake(s_buf_mtx, portMAX_DELAY);
    s_frozen = true;
    if (s_buf_mtx != nullptr) xSemaphoreGive(s_buf_mtx);
  }
  return ok;
}

PollState BleExplorer::poll_discovery_summary(BleDiscoverySummary& out) {
  if (s_frozen) {  // terminal already: repeat the recorded verdict
    fill_counts(out);
    out.duration_ms = s_final_duration_ms;
    out.complete = s_final_complete;
    out.cancelled = s_final_cancelled;
    return s_final_state;
  }
  NimBLEScan* s = NimBLEDevice::getScan();
  const uint32_t now = millis();

  if (cancel_pending()) {
    if (s != nullptr) s->stop();
    return finalize_discovery(out, PollState::Cancelled, false, true);
  }
  if (s_scan_ended.load()) {
    // The scan reached its scheduled end (reason 0); any other reason means the host aborted it.
    return finalize_discovery(out, PollState::Done, s_scan_end_reason.load() == 0, false);
  }
  const bool running = (s != nullptr) && s->isScanning();
  if (!running) {
    // isScanning() turns false just before onScanEnd() runs on the host task. Give the callback a short
    // grace instead of reporting a finished scan as incomplete; if it never comes the scan ended without
    // a completion event and is reported incomplete.
    if (s_idle_since_ms == 0) s_idle_since_ms = (now != 0) ? now : 1;
    if (now - s_idle_since_ms < kScanEndGraceMs) {
      fill_counts(out);
      return PollState::Running;
    }
    return finalize_discovery(out, PollState::Done, false, false);
  }
  s_idle_since_ms = 0;

  bool limit = false;
  if (s_buf_mtx != nullptr) xSemaphoreTake(s_buf_mtx, portMAX_DELAY);
  limit = s_limit_hit;
  if (s_buf_mtx != nullptr) xSemaphoreGive(s_buf_mtx);
  if (limit) {  // max_results reached: stop collecting
    if (s != nullptr) s->stop();
    return finalize_discovery(out, PollState::Done, false, false);
  }
  if ((now - s_scan_started_ms) >= (s_scan_cfg.duration_ms + 2000)) {
    if (s != nullptr) s->stop();  // the scheduled end never came
    return finalize_discovery(out, PollState::Done, false, false);
  }
  fill_counts(out);
  return PollState::Running;
}

size_t BleExplorer::discovery_rows(size_t start, BleAdvObservation* out, size_t n) const {
  size_t copied = 0;
  if (s_buf_mtx != nullptr) xSemaphoreTake(s_buf_mtx, portMAX_DELAY);
  for (size_t i = start; i < s_adv_count && copied < n; ++i) out[copied++] = s_adv[i];
  if (s_buf_mtx != nullptr) xSemaphoreGive(s_buf_mtx);
  return copied;
}

PollState BleExplorer::poll_discovery(BleDiscoveryResult& out) {
  BleDiscoverySummary sum;
  const PollState st = poll_discovery_summary(sum);
  std::memset(&out, 0, sizeof(out));
  out.count = sum.count;
  out.overflow = sum.overflow;
  out.malformed = sum.malformed;
  out.callbacks = sum.callbacks;
  out.dropped_in_callback = sum.dropped_in_callback;
  out.complete = sum.complete;
  out.cancelled = sum.cancelled;
  out.active_scan = sum.active_scan;
  out.started_ms = sum.started_ms;
  out.duration_ms = sum.duration_ms;
  out.interval_ms = sum.interval_ms;
  out.window_ms = sum.window_ms;
  out.limit_hit = sum.limit_hit;
  discovery_rows(0, out.adv, out.count);
  return st;
}

void BleExplorer::stop_discovery() {
  NimBLEScan* s = NimBLEDevice::getScan();
  if (s != nullptr) s->stop();
}

void BleExplorer::set_limits(const BleOpLimits& l) {
  s_max_entries = l.max_entries;
  s_has_deadline = l.max_duration_ms != 0;
  s_deadline_ms = millis() + l.max_duration_ms;
}

BleOpStatus BleExplorer::connect(const EnrolledTarget& t, uint32_t timeout_ms) {
  BleOpStatus st = new_status();
  s_cancel.store(false);  // cancel is per operation; the radio-wide flag is still honoured
  s_verified = false;

  std::memset(&s_conn_info, 0, sizeof(s_conn_info));
  copy_bounded(s_conn_info.target_id, sizeof(s_conn_info.target_id), t.target_id);
  std::memcpy(s_conn_info.addr, t.address, 6);
  s_conn_info.addr_type = t.address_type;
  s_conn_info.identity_ok = TriBool::unknown();
  s_conn_info.identity_status = -1;
  set_note("identity not checked");

  if (!t.has_address) {
    // Resolution (enrolled address, or exactly one discovered candidate) happens before this call.
    std::snprintf(st.detail, sizeof(st.detail), "no resolved address - refusing to connect");
    return st;
  }
  if (connected() || s_conn_state.load() == ConnState::Connecting) disconnect();
  if (cancel_pending()) {
    st.status = EventStatus::Cancelled;
    std::snprintf(st.detail, sizeof(st.detail), "cancelled before connect");
    return st;
  }
  uint32_t to = timeout_ms;
  if (s_has_deadline) {
    const int32_t left = static_cast<int32_t>(s_deadline_ms - millis());
    if (left < 200) {
      st.status = EventStatus::Timeout;
      std::snprintf(st.detail, sizeof(st.detail), "time limit reached before connect");
      return st;
    }
    if (static_cast<uint32_t>(left) < to) to = static_cast<uint32_t>(left);
  }

  // Nothing may pair or bond before the identity is verified (a peer-initiated pairing would otherwise
  // store keys for an unverified device).
  set_pairing_policy(false);

  if (s_client == nullptr) s_client = NimBLEDevice::createClient();
  if (s_client == nullptr) {
    std::snprintf(st.detail, sizeof(st.detail), "no client");
    return st;
  }
  s_client->setClientCallbacks(&s_client_cb, false);
  s_client->setConnectTimeout(to);
  s_client->setConnectRetries(0);  // one attempt: the poll loop below owns timing and cancel

  s_disc_reason.store(-1);
  s_conn_fail_reason.store(0);
  s_conn_state.store(ConnState::Connecting);
  const NimBLEAddress addr(t.address, to_nimble_type(t.address_type));  // t.address is MSB first
  if (!s_client->connect(addr, /*deleteAttributes=*/true, /*asyncConnect=*/true, /*exchangeMTU=*/true)) {
    s_conn_state.store(ConnState::Idle);
    st.nimble_rc = s_client->getLastError();
    std::snprintf(st.detail, sizeof(st.detail), "connect not started rc=%d", st.nimble_rc);
    return st;
  }

  // Poll instead of blocking inside NimBLE so a cancel (or the time limit) interrupts the attempt.
  const uint32_t t0 = millis();
  bool cancelled = false;
  bool timed_out = false;
  bool abort_sent = false;
  uint32_t abort_ms = 0;
  ConnState cs;
  for (;;) {
    cs = s_conn_state.load();
    if (cs == ConnState::Connected || cs == ConnState::Failed) break;
    const uint32_t now = millis();
    if (!abort_sent) {
      if (cancel_pending()) {
        cancelled = true;
        abort_sent = true;
      } else if ((now - t0) > to + kConnectSlackMs || deadline_hit()) {
        timed_out = true;
        abort_sent = true;
      }
      if (abort_sent) {
        s_client->cancelConnect();
        abort_ms = now;
      }
    } else if ((now - abort_ms) > kTeardownWaitMs) {
      break;  // the attempt did not stop; reported below, retried by the next connect()
    }
    vTaskDelay(pdMS_TO_TICKS(kConnectPollMs));
  }

  if (cs != ConnState::Connected) {
    const int reason = s_conn_fail_reason.load();
    st.nimble_rc = reason;
    if (cs == ConnState::Failed) s_conn_state.store(ConnState::Idle);
    if (cancelled) {
      st.status = EventStatus::Cancelled;
      std::snprintf(st.detail, sizeof(st.detail), "connect cancelled");
    } else if (timed_out || reason == BLE_HS_ETIMEOUT) {
      st.status = EventStatus::Timeout;
      std::snprintf(st.detail, sizeof(st.detail), "connect timed out");
    } else if (cs == ConnState::Failed) {
      st.status = EventStatus::Error;
      std::snprintf(st.detail, sizeof(st.detail), "connect failed rc=%d", reason);
    } else {
      st.status = EventStatus::Error;
      std::snprintf(st.detail, sizeof(st.detail), "connect attempt did not stop");
    }
    return st;
  }
  if (cancelled || timed_out) {  // the link came up while we were cancelling: close it again
    disconnect();
    st.status = cancelled ? EventStatus::Cancelled : EventStatus::Timeout;
    std::snprintf(st.detail, sizeof(st.detail), cancelled ? "connect cancelled" : "connect timed out");
    return st;
  }

  s_conn_info.mtu = s_client->getMTU();
  const SecurityState before;  // unknown before a link exists
  s_sec = read_sec();
  st.before = before;

  // 1) identity, and nothing else, before any enumeration or other read
  const IdentityResult idr = read_identity();
  s_sec = read_sec();
  st.after = s_sec;
  st.state_changed = !sec_equal(before, s_sec);
  switch (idr) {
    case IdentityResult::Verified:
      s_verified = true;
      set_pairing_policy(true);  // what the profile's restricted characteristics need, for this link only
      st.status = EventStatus::Success;
      std::snprintf(st.detail, sizeof(st.detail), "connected mtu=%u identity verified",
                    static_cast<unsigned>(s_conn_info.mtu));
      return st;
    case IdentityResult::Mismatch:
      s_conn_info.identity_ok = TriBool::of(false);
      st.status = EventStatus::Denied;
      break;
    case IdentityResult::Unreadable:
      st.status = EventStatus::Inconclusive;
      break;
    case IdentityResult::Cancelled:
      st.status = EventStatus::Cancelled;
      break;
    case IdentityResult::Deadline:
      st.status = EventStatus::Timeout;
      break;
  }
  disconnect();  // unverified peer: nothing else is enumerated or read
  std::snprintf(st.detail, sizeof(st.detail), "%.40s; disconnected", s_conn_info.identity_note);
  st.disconnect_reason = s_disc_reason.load();
  return st;
}

BleOpStatus BleExplorer::discover_gatt(uint16_t page_index, GattPage& out) {
  BleOpStatus st = new_status();
  st.disconnect_reason = s_disc_reason.load();
  st.before = s_sec;
  st.after = s_sec;
  std::memset(&out, 0, sizeof(out));

  if (!connected()) {
    std::snprintf(st.detail, sizeof(st.detail), "not connected");
    return st;
  }
  if (!s_verified) {
    std::snprintf(st.detail, sizeof(st.detail), "identity not verified - not enumerating");
    return st;
  }

  if (page_index == 0) {
    s_cancel.store(false);  // cancel is per operation
    build_gatt();
  }

  const uint16_t per = cfg::kMaxGattEntriesPerPage;
  const uint32_t begin = static_cast<uint32_t>(page_index) * per;
  uint16_t cnt = 0;
  gatt_lock();
  const uint16_t total = s_gatt_total;
  for (uint16_t i = 0; i < per && (begin + i) < total; ++i) {
    GattEntry e = s_gatt[begin + i];
    // Re-base parent index to this page; 0xFF for services and cross-page parents.
    if (e.parent != 0xFF) {
      if (e.parent >= begin && e.parent < begin + per) {
        e.parent = static_cast<uint8_t>(e.parent - begin);
      } else {
        e.parent = 0xFF;
      }
    }
    out.entries[i] = e;
    ++cnt;
  }
  gatt_unlock();
  out.count = cnt;
  out.page_index = page_index;
  out.total_seen = total;
  out.complete = s_gatt_complete;
  out.more_pages = (begin + cnt) < total;

  switch (s_gatt_stop) {
    case GattStop::None:
      st.status = EventStatus::Success;
      std::snprintf(st.detail, sizeof(st.detail), "gatt entries=%u", static_cast<unsigned>(s_gatt_total));
      break;
    case GattStop::Cancelled:
      st.status = EventStatus::Cancelled;
      std::snprintf(st.detail, sizeof(st.detail), "cancelled; gatt entries=%u", static_cast<unsigned>(s_gatt_total));
      break;
    case GattStop::Deadline:
      st.status = EventStatus::Timeout;
      std::snprintf(st.detail, sizeof(st.detail), "time limit; gatt entries=%u", static_cast<unsigned>(s_gatt_total));
      break;
    case GattStop::Capacity:
      st.status = EventStatus::Partial;
      std::snprintf(st.detail, sizeof(st.detail), "gatt table full at %u entries", static_cast<unsigned>(s_gatt_total));
      break;
    case GattStop::EntryLimit:
      st.status = EventStatus::Partial;
      std::snprintf(st.detail, sizeof(st.detail), "max_results limit at %u entries", static_cast<unsigned>(s_gatt_total));
      break;
    case GattStop::NoServices:
      st.status = EventStatus::Partial;
      std::snprintf(st.detail, sizeof(st.detail), "no services returned; ATT status not exposed");
      break;
    case GattStop::LinkLost:
      st.status = EventStatus::Partial;
      std::snprintf(st.detail, sizeof(st.detail), "link lost; gatt entries=%u", static_cast<unsigned>(s_gatt_total));
      break;
  }
  return st;
}

BleOpStatus BleExplorer::read_by_uuid(const uint8_t service_uuid[16], const uint8_t char_uuid[16],
                                      uint8_t* out, size_t max, size_t& out_len, size_t* full_len) {
  BleOpStatus st = new_status();
  st.disconnect_reason = s_disc_reason.load();
  out_len = 0;
  if (full_len != nullptr) *full_len = 0;
  s_cancel.store(false);  // cancel is per operation

  const SecurityState before = read_sec();
  st.before = before;
  st.after = before;

  if (!connected()) {
    std::snprintf(st.detail, sizeof(st.detail), "not connected");
    return st;
  }
  if (!s_verified) {
    std::snprintf(st.detail, sizeof(st.detail), "identity not verified - not reading");
    return st;
  }
  if (cancel_pending()) {
    st.status = EventStatus::Cancelled;
    std::snprintf(st.detail, sizeof(st.detail), "cancelled before read");
    return st;
  }
  if (deadline_hit()) {
    st.status = EventStatus::Timeout;
    std::snprintf(st.detail, sizeof(st.detail), "time limit reached before read");
    return st;
  }

  char ss[cfg::kUuidStrLen + 1];
  char cs[cfg::kUuidStrLen + 1];
  format_uuid128(service_uuid, ss, sizeof(ss));
  format_uuid128(char_uuid, cs, sizeof(cs));

  s_blocking.store(true);
  NimBLERemoteService* svc = s_client->getService(NimBLEUUID(std::string(ss)));
  s_blocking.store(false);
  if (svc == nullptr) {
    std::snprintf(st.detail, sizeof(st.detail), cancel_pending() ? "cancelled" : "service not found");
    if (cancel_pending()) st.status = EventStatus::Cancelled;
    return st;
  }
  if (cancel_pending() || deadline_hit()) {
    st.status = cancel_pending() ? EventStatus::Cancelled : EventStatus::Timeout;
    std::snprintf(st.detail, sizeof(st.detail), "stopped before read");
    return st;
  }
  s_blocking.store(true);
  NimBLERemoteCharacteristic* ch = svc->getCharacteristic(NimBLEUUID(std::string(cs)));
  s_blocking.store(false);
  if (ch == nullptr) {
    std::snprintf(st.detail, sizeof(st.detail), cancel_pending() ? "cancelled" : "characteristic not found");
    if (cancel_pending()) st.status = EventStatus::Cancelled;
    return st;
  }
  if (!ch->canRead()) {
    st.after = read_sec();
    s_sec = st.after;
    st.state_changed = !sec_equal(before, st.after);
    st.status = EventStatus::Unsupported;
    std::snprintf(st.detail, sizeof(st.detail), "characteristic not readable");
    return st;
  }
  if (cancel_pending() || deadline_hit()) {
    st.status = cancel_pending() ? EventStatus::Cancelled : EventStatus::Timeout;
    std::snprintf(st.detail, sizeof(st.detail), "stopped before read");
    return st;
  }

  // One in-flight readValue cannot be interrupted by this task (cancel() from another task drops the link).
  s_blocking.store(true);
  NimBLEAttValue v = ch->readValue();
  s_blocking.store(false);
  const SecurityState after = read_sec();
  s_sec = after;
  st.after = after;
  st.state_changed = !sec_equal(before, after);

  const size_t served = v.size();
  size_t n = served;
  if (n > max) n = max;
  if (n > 0 && out != nullptr) std::memcpy(out, v.data(), n);
  out_len = n;
  if (full_len != nullptr) *full_len = served;

  if (served > 0) {
    st.status = EventStatus::Success;
    std::snprintf(st.detail, sizeof(st.detail), "read %u bytes", static_cast<unsigned>(served));
  } else if (cancel_pending() && !connected()) {
    st.status = EventStatus::Cancelled;
    std::snprintf(st.detail, sizeof(st.detail), "cancelled during read");
  } else {
    // NimBLE returns an empty value on failure and does not expose the ATT status: a genuinely empty
    // value cannot be told apart from a denied or failed read.
    st.status = EventStatus::Inconclusive;
    std::snprintf(st.detail, sizeof(st.detail), "value empty; the stack does not expose the ATT status");
  }
  return st;
}

size_t BleExplorer::gatt_rows(size_t start, GattEntry* out, size_t n, uint16_t& total) const {
  gatt_lock();
  total = s_gatt_total;
  size_t copied = 0;
  for (size_t i = start; i < s_gatt_total && copied < n; ++i) out[copied++] = s_gatt[i];
  gatt_unlock();
  return copied;
}

SecurityState BleExplorer::security_state() const { return read_sec(); }

const ConnectedTargetInfo& BleExplorer::connected_info() const { return s_conn_info; }

bool BleExplorer::connected() const { return s_client != nullptr && s_client->isConnected(); }

void BleExplorer::disconnect() {
  if (s_client != nullptr) {
    if (s_client->isConnected()) {
      s_client->disconnect();
      wait_link_down(kTeardownWaitMs);
    } else if (s_conn_state.load() == ConnState::Connecting) {
      s_client->cancelConnect();
      const uint32_t t0 = millis();
      while (s_conn_state.load() == ConnState::Connecting && (millis() - t0) < kTeardownWaitMs) {
        vTaskDelay(pdMS_TO_TICKS(20));
      }
    }
    if (!s_client->isConnected()) {
      s_client->deleteServices();  // free the attribute tree; the client object itself is reused
      if (s_conn_state.load() != ConnState::Connecting) s_conn_state.store(ConnState::Idle);
    }
  }
  s_verified = false;
  s_sec = SecurityState{};
  set_pairing_policy(false);
}

void BleExplorer::cancel() {
  s_cancel.store(true);
  g_radio.request_cancel();
  NimBLEScan* s = NimBLEDevice::getScan();
  if (s != nullptr) s->stop();
  if (s_client == nullptr) return;
  if (s_conn_state.load() == ConnState::Connecting) {
    s_client->cancelConnect();
  } else if (s_blocking.load() && s_client->isConnected()) {
    s_client->disconnect();  // aborts the blocking discovery/read in the worker task
  }
}

}  // namespace oyster
