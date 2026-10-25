// Bounded plain-data types shared by every firmware module and by the host tests.
// Wire names (to_string) must match schemas/*.schema.json exactly.
#pragma once

#include <cstddef>
#include <cstdint>

#include "oyster_core/config.h"

namespace oyster {

enum class SourceMode : uint8_t { Live, Offline, Simulated };
enum class Transport : uint8_t { Wifi, Ble, Backend, Local, None };
enum class ClockQuality : uint8_t { Ntp, Serial, BackendHint, Unknown };
enum class EventStatus : uint8_t {
  Success, Denied, Timeout, Unsupported, Error, Cancelled, Partial, UnknownOutcome, Inconclusive
};

// Job operations (schemas/job.schema.json) followed by device-local event kinds.
enum class Operation : uint8_t {
  WifiSurvey, BleDiscover, BleInspectGatt, BleReadProfileValue, BleObserveNotifications,
  BleCheckAccessPolicy, BleWriteLabValue, BleCollectDiagnostics,
  Boot, Telemetry, SessionOpen, SessionClose, LocalConfirmation, Recovery,
  Unknown
};
constexpr uint8_t kJobOperationCount = 8;  // WifiSurvey .. BleCollectDiagnostics

enum class JobOutcome : uint8_t { Succeeded, Failed, Cancelled, Expired, UnknownOutcome, Unsupported };

// State of a non-blocking bounded operation (scan, discovery, transfer).
enum class PollState : uint8_t { Running, Done, Failed, Cancelled };

const char* to_string(SourceMode v);
const char* to_string(Transport v);
const char* to_string(ClockQuality v);
const char* to_string(EventStatus v);
const char* to_string(Operation v);   // e.g. "wifi_survey", "boot"
const char* to_string(JobOutcome v);
// Accepts only the 8 job operation wire names; false for anything else.
bool parse_operation(const char* s, Operation& out);
bool is_job_operation(Operation op);

// Nullable boolean serialised as true/false/null. Default is unknown: never invent a value.
struct TriBool {
  int8_t v = -1;
  static TriBool unknown() { return TriBool{}; }
  static TriBool of(bool b) { TriBool t; t.v = b ? 1 : 0; return t; }
  bool known() const { return v >= 0; }
  bool is_true() const { return v == 1; }
  bool is_false() const { return v == 0; }
  bool operator==(const TriBool& o) const { return v == o.v; }
  bool operator!=(const TriBool& o) const { return v != o.v; }
};

// Pairing, encryption, peer authentication and application authorisation are separate facts.
struct SecurityState {
  TriBool encrypted;
  TriBool authenticated_pairing;
  TriBool application_authorized;
};

enum class WifiAuth : uint8_t {
  Open, Wep, WpaPsk, Wpa2Psk, WpaWpa2Psk, Wpa2Enterprise, Wpa3Psk, Wpa2Wpa3Psk, WapiPsk, Unknown
};
const char* to_string(WifiAuth v);  // "OPEN", "WEP", "WPA_PSK", "WPA2_PSK", ... "UNKNOWN"

struct ApObservation {
  uint8_t ssid[cfg::kMaxSsidBytes];  // raw bytes; not assumed to be UTF-8
  uint8_t ssid_len;                  // 0 = hidden or empty SSID
  uint8_t bssid[6];
  uint8_t channel;
  int8_t rssi_dbm;
  WifiAuth auth;
};

enum class BleAddrType : uint8_t { Public, Random, Unknown };
const char* to_string(BleAddrType v);  // "public", "random", "unknown"

// One advertising observation. obs_id is session-local and is NOT an identity claim:
// random/private addresses rotate and names are not unique.
struct BleAdvObservation {
  uint16_t obs_id;
  uint8_t addr[6];  // most significant byte first, as printed
  BleAddrType addr_type;
  int8_t rssi_dbm;  // last seen
  TriBool connectable;
  uint8_t name[cfg::kMaxBleNameBytes];
  uint8_t name_len;
  uint8_t svc_uuid_count;                         // stored entries
  bool svc_uuids_truncated;                       // more were advertised than stored
  uint8_t svc_uuid128[cfg::kMaxSvcUuidsPerAdv][16];  // big-endian (string order); 16/32-bit UUIDs expanded with the Bluetooth base UUID
  uint8_t mfg_data[cfg::kMaxMfgDataBytes];
  uint8_t mfg_len;
  bool mfg_truncated;
  bool malformed;  // payload could not be parsed cleanly; preserved as observed
  uint32_t first_seen_ms;
  uint32_t last_seen_ms;
  uint16_t seen_count;
};

enum class GattKind : uint8_t { Service, Characteristic, Descriptor };

struct GattEntry {
  GattKind kind;
  uint8_t uuid128[16];  // big-endian (string order)
  uint8_t uuid_bits;    // 16, 32 or 128 as reported by the peer
  uint16_t handle;
  uint8_t props;        // advertised property bits (0x02 read, 0x04 write-no-rsp, 0x08 write, 0x10 notify, 0x20 indicate). Not a permission guarantee.
  uint8_t parent;       // index of the parent entry in this page, 0xFF for services
};

struct GattPage {
  GattEntry entries[cfg::kMaxGattEntriesPerPage];
  uint16_t count;
  uint16_t page_index;
  bool complete;    // false when discovery stopped early or failed part-way
  bool more_pages;  // more entries exist beyond this page
  uint16_t total_seen;
};

struct JobLimits {
  uint16_t max_results;     // 0 = not specified
  uint32_t max_duration_ms; // 0 = not specified
  uint32_t max_bytes;       // 0 = not specified
};

// A job as received from the backend and parsed by BackendClient. LocalPolicy validates it.
struct JobSpec {
  char job_id[cfg::kIdBufLen];
  char session_id[cfg::kIdBufLen];
  char device_id[cfg::kIdBufLen];  // empty when absent
  char target_id[cfg::kIdBufLen];  // empty when null
  Operation op;
  char op_wire[32];  // operation string as received (reported back when unsupported)
  uint32_t scope_version;
  uint32_t deadline_ms;    // budget relative to received_ms
  uint64_t received_ms;    // device uptime when the job was received
  JobLimits limits;
  bool server_requires_confirmation;  // informational only: the device derives its own requirement and never lowers it
  char nonce[cfg::kIdBufLen];
  char args_json[cfg::kMaxJobArgsBytes];
  uint16_t args_len;
};

struct LocalConfirmation {
  char job_id[cfg::kIdBufLen];
  char nonce[cfg::kIdBufLen];
  bool approved;
  uint64_t decided_ms;
};

}  // namespace oyster
