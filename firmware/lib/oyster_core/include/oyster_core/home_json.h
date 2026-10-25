// Bounded parsers for the Smart Home responses of the backend (docs/HOME_ASSISTANT.md):
//   GET  /v1/home/rooms                      -> RoomList
//   GET  /v1/home/rooms/{room_id}/devices    -> DeviceList
//   POST /v1/home/actions                    -> ActionResult
//   any error body {"error": ..., "detail": ...} -> ErrorBody
// The input is untrusted: at most kMaxInput bytes, nesting limited, every field type-checked, every string cut
// to its buffer and reduced to printable ASCII, identifiers checked against the allowlist grammar. Items that
// do not fit or do not validate are dropped, never half-copied. Host-testable (ArduinoJson only).
#pragma once

#include <cstddef>
#include <cstdint>

namespace oyster::homejson {

constexpr size_t kMaxInput = 5120;   // the backend client buffer; larger input is rejected as malformed
constexpr size_t kMaxRooms = 16;
constexpr size_t kMaxDevices = 16;
constexpr size_t kNameLen = 25;      // room id / room name / device label: 24 characters + NUL
constexpr size_t kEntityLen = 65;    // entity id: 64 characters + NUL
constexpr size_t kStateLen = 17;     // state word: 16 characters + NUL
constexpr size_t kReasonLen = 16;
constexpr size_t kCursorLen = 12;

enum ActionBit : uint8_t { kTurnOn = 1, kTurnOff = 2, kToggle = 4 };

struct Room {
  char id[kNameLen];
  char name[kNameLen];
  uint8_t count;  // devices in the room
};

struct RoomList {
  bool configured = false;
  char reason[kReasonLen] = {};  // when not configured: no_url, no_token, no_allowlist, bad_allowlist
  uint8_t count = 0;
  bool more = false;             // the backend has more rooms than were taken
  Room rooms[kMaxRooms] = {};
};

struct Device {
  char entity_id[kEntityLen];
  char label[kNameLen];
  char state[kStateLen];
  bool impactful;
  uint8_t actions;  // ActionBit mask
};

struct DeviceList {
  char room_id[kNameLen] = {};
  char room_name[kNameLen] = {};
  uint8_t count = 0;
  bool more = false;                // next_after was present
  char next_after[kCursorLen] = {};
  Device devices[kMaxDevices] = {};
};

struct ActionResult {
  char entity_id[kEntityLen] = {};
  char state[kStateLen] = {};
  bool duplicate = false;
};

struct ErrorBody {
  char code[32] = {};
  char detail[64] = {};
};

enum class Parse : uint8_t { Ok, Malformed, WrongShape };

Parse parse_rooms(const char* json, size_t len, RoomList* out);
// append = true adds the devices of a following page to `out` (same room), false starts a new list.
Parse parse_devices(const char* json, size_t len, DeviceList* out, bool append);
Parse parse_action(const char* json, size_t len, ActionResult* out);
bool parse_error(const char* json, size_t len, ErrorBody* out);

// Identifier grammar shared with the backend allowlist.
bool valid_entity_id(const char* s);  // domain.object_id, lowercase letters, digits, '_' ; at most 64 characters
bool valid_room_id(const char* s);    // 1..24 of a-z 0-9 _ -
const char* action_name(uint8_t bit);  // "turn_on", "turn_off", "toggle"

}  // namespace oyster::homejson
