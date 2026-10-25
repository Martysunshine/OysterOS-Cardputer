// Bounded parsers for the Smart Home responses. See include/oyster_core/home_json.h.
#include "oyster_core/home_json.h"

#include <ArduinoJson.h>

#include <cstring>

namespace oyster::homejson {
namespace {

constexpr uint8_t kNestingLimit = 6;

// Copies printable ASCII only, cut to the buffer; anything else becomes '?'.
void copy_text(char* dst, size_t n, const char* src) {
  size_t o = 0;
  if (src != nullptr && n > 0) {
    for (size_t i = 0; src[i] != '\0' && o + 1 < n; ++i) {
      const unsigned char c = static_cast<unsigned char>(src[i]);
      dst[o++] = (c >= 0x20 && c <= 0x7E) ? static_cast<char>(c) : '?';
    }
  }
  if (n > 0) dst[o] = '\0';
}

const char* str_or_null(JsonVariantConst v) { return v.is<const char*>() ? v.as<const char*>() : nullptr; }

bool read_doc(const char* json, size_t len, const JsonDocument& filter, JsonDocument& doc) {
  if (json == nullptr || len == 0 || len > kMaxInput) return false;
  const DeserializationError err = deserializeJson(doc, json, len, DeserializationOption::Filter(filter),
                                                   DeserializationOption::NestingLimit(kNestingLimit));
  return !err;
}

uint8_t action_mask(JsonVariantConst v) {
  uint8_t mask = 0;
  if (!v.is<JsonArrayConst>()) return 0;
  for (JsonVariantConst a : v.as<JsonArrayConst>()) {
    const char* s = str_or_null(a);
    if (s == nullptr) continue;
    if (std::strcmp(s, "turn_on") == 0) mask |= kTurnOn;
    else if (std::strcmp(s, "turn_off") == 0) mask |= kTurnOff;
    else if (std::strcmp(s, "toggle") == 0) mask |= kToggle;
  }
  return mask;
}

}  // namespace

bool valid_entity_id(const char* s) {
  if (s == nullptr) return false;
  const size_t n = std::strlen(s);
  if (n < 3 || n > kEntityLen - 1) return false;
  size_t dots = 0;
  for (size_t i = 0; i < n; ++i) {
    const char c = s[i];
    if (c == '.') {
      if (i == 0 || i == n - 1) return false;
      ++dots;
    } else if (!((c >= 'a' && c <= 'z') || (c >= '0' && c <= '9') || c == '_')) {
      return false;
    }
  }
  return dots == 1;
}

bool valid_room_id(const char* s) {
  if (s == nullptr) return false;
  const size_t n = std::strlen(s);
  if (n < 1 || n > kNameLen - 1) return false;
  for (size_t i = 0; i < n; ++i) {
    const char c = s[i];
    if (!((c >= 'a' && c <= 'z') || (c >= '0' && c <= '9') || c == '_' || c == '-')) return false;
  }
  return true;
}

const char* action_name(uint8_t bit) {
  switch (bit) {
    case kTurnOn: return "turn_on";
    case kTurnOff: return "turn_off";
    case kToggle: return "toggle";
    default: return "";
  }
}

Parse parse_rooms(const char* json, size_t len, RoomList* out) {
  if (out == nullptr) return Parse::WrongShape;
  *out = RoomList{};
  JsonDocument filter;
  filter["configured"] = true;
  filter["reason"] = true;
  filter["next_after"] = true;
  filter["rooms"][0]["id"] = true;
  filter["rooms"][0]["name"] = true;
  filter["rooms"][0]["count"] = true;
  JsonDocument doc;
  if (!read_doc(json, len, filter, doc)) return Parse::Malformed;

  if (!doc["configured"].is<bool>()) return Parse::WrongShape;
  out->configured = doc["configured"].as<bool>();
  copy_text(out->reason, sizeof(out->reason), str_or_null(doc["reason"]));
  if (!doc["next_after"].isNull()) out->more = true;

  JsonVariantConst rooms = doc["rooms"];
  if (rooms.isNull()) return out->configured ? Parse::WrongShape : Parse::Ok;
  if (!rooms.is<JsonArrayConst>()) return Parse::WrongShape;
  for (JsonVariantConst r : rooms.as<JsonArrayConst>()) {
    const char* id = str_or_null(r["id"]);
    if (!valid_room_id(id)) continue;
    if (out->count >= kMaxRooms) {
      out->more = true;
      break;
    }
    Room& room = out->rooms[out->count];
    copy_text(room.id, sizeof(room.id), id);
    const char* name = str_or_null(r["name"]);
    copy_text(room.name, sizeof(room.name), name != nullptr ? name : id);
    const int count = r["count"].is<int>() ? r["count"].as<int>() : 0;
    room.count = static_cast<uint8_t>(count < 0 ? 0 : (count > 255 ? 255 : count));
    ++out->count;
  }
  return Parse::Ok;
}

Parse parse_devices(const char* json, size_t len, DeviceList* out, bool append) {
  if (out == nullptr) return Parse::WrongShape;
  if (!append) *out = DeviceList{};
  JsonDocument filter;
  filter["room"]["id"] = true;
  filter["room"]["name"] = true;
  filter["next_after"] = true;
  filter["devices"][0]["entity_id"] = true;
  filter["devices"][0]["label"] = true;
  filter["devices"][0]["state"] = true;
  filter["devices"][0]["impactful"] = true;
  filter["devices"][0]["actions"] = true;
  JsonDocument doc;
  if (!read_doc(json, len, filter, doc)) return Parse::Malformed;

  JsonVariantConst devices = doc["devices"];
  if (!devices.is<JsonArrayConst>()) return Parse::WrongShape;

  if (!append || out->room_id[0] == '\0') {
    const char* rid = str_or_null(doc["room"]["id"]);
    if (!valid_room_id(rid)) return Parse::WrongShape;
    copy_text(out->room_id, sizeof(out->room_id), rid);
    const char* rname = str_or_null(doc["room"]["name"]);
    copy_text(out->room_name, sizeof(out->room_name), rname != nullptr ? rname : rid);
  }

  out->more = false;
  out->next_after[0] = '\0';
  const char* next = str_or_null(doc["next_after"]);
  if (next != nullptr && next[0] != '\0') {
    out->more = true;
    copy_text(out->next_after, sizeof(out->next_after), next);
  }

  for (JsonVariantConst d : devices.as<JsonArrayConst>()) {
    const char* eid = str_or_null(d["entity_id"]);
    if (!valid_entity_id(eid)) continue;
    if (out->count >= kMaxDevices) {
      out->more = true;
      out->next_after[0] = '\0';  // the cursor no longer matches what was taken: nothing more is fetched
      break;
    }
    Device& dev = out->devices[out->count];
    copy_text(dev.entity_id, sizeof(dev.entity_id), eid);
    const char* label = str_or_null(d["label"]);
    copy_text(dev.label, sizeof(dev.label), label != nullptr ? label : eid);
    const char* state = str_or_null(d["state"]);
    copy_text(dev.state, sizeof(dev.state), state != nullptr ? state : "unknown");
    dev.impactful = d["impactful"].is<bool>() && d["impactful"].as<bool>();
    dev.actions = action_mask(d["actions"]);
    ++out->count;
  }
  return Parse::Ok;
}

Parse parse_action(const char* json, size_t len, ActionResult* out) {
  if (out == nullptr) return Parse::WrongShape;
  *out = ActionResult{};
  JsonDocument filter;
  filter["entity_id"] = true;
  filter["state"] = true;
  filter["duplicate"] = true;
  filter["result"] = true;
  JsonDocument doc;
  if (!read_doc(json, len, filter, doc)) return Parse::Malformed;

  const char* result = str_or_null(doc["result"]);
  if (result == nullptr || std::strcmp(result, "ok") != 0) return Parse::WrongShape;
  const char* eid = str_or_null(doc["entity_id"]);
  if (!valid_entity_id(eid)) return Parse::WrongShape;
  copy_text(out->entity_id, sizeof(out->entity_id), eid);
  const char* state = str_or_null(doc["state"]);
  copy_text(out->state, sizeof(out->state), state != nullptr ? state : "");
  out->duplicate = doc["duplicate"].is<bool>() && doc["duplicate"].as<bool>();
  return Parse::Ok;
}

bool parse_error(const char* json, size_t len, ErrorBody* out) {
  if (out == nullptr) return false;
  *out = ErrorBody{};
  JsonDocument filter;
  filter["error"] = true;
  filter["detail"] = true;
  JsonDocument doc;
  if (!read_doc(json, len, filter, doc)) return false;
  const char* code = str_or_null(doc["error"]);
  if (code == nullptr) return false;
  copy_text(out->code, sizeof(out->code), code);
  copy_text(out->detail, sizeof(out->detail), str_or_null(doc["detail"]));
  return true;
}

}  // namespace oyster::homejson
