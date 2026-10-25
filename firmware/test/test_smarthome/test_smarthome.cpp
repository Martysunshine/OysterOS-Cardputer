// Host tests for the Smart Home logic: IR code table (oyster_core/ir_codes.h) and the bounded response
// parsers (oyster_core/home_json.h). Run: pio test -e native -f test_smarthome
//
// The IR tests check the table against MSB-first hex codes that public code lists print for the same keys:
// they prove the address/command convention (what the IRremote library takes), not that a real TV obeys.
#include <unity.h>

#include <cstdio>
#include <cstring>
#include <string>

#include "oyster_core/home_json.h"
#include "oyster_core/ir_codes.h"

using namespace oyster;
using namespace oyster::ircodes;
using namespace oyster::homejson;

void setUp() {}
void tearDown() {}

namespace {

const Brand& brand(const char* id) {
  const Brand* b = find_brand(id);
  TEST_ASSERT_NOT_NULL(b);
  return *b;
}

uint32_t nec_printed(const char* id, Func f) {
  const Brand& b = brand(id);
  return reverse_bits32(nec_wire(b.address, b.command[static_cast<size_t>(f)]));
}

uint32_t samsung_printed(Func f) {
  const Brand& b = brand("samsung");
  return reverse_bits32(samsung32_wire(b.address, b.command[static_cast<size_t>(f)]));
}

uint16_t sony_printed(Func f) {
  const Brand& b = brand("sony");
  return reverse_bits12(sirc12_wire(b.address, b.command[static_cast<size_t>(f)]));
}

bool terminated(const char* s, size_t cap) { return std::memchr(s, '\0', cap) != nullptr; }

}  // namespace

// ---- IR table -------------------------------------------------------------------------------------

void test_ir_table_integrity() {
  TEST_ASSERT_TRUE(brand_count() >= 4);
  bool seen[4] = {false, false, false, false};
  for (size_t i = 0; i < brand_count(); ++i) {
    const Brand* b = brand_at(i);
    TEST_ASSERT_NOT_NULL(b);
    TEST_ASSERT_TRUE(std::strlen(b->id) >= 1 && std::strlen(b->id) <= 11);
    TEST_ASSERT_TRUE(std::strlen(b->name) >= 1 && std::strlen(b->name) <= 24);
    TEST_ASSERT_TRUE(b->source != nullptr && std::strlen(b->source) > 10);
    TEST_ASSERT_TRUE(b->repeats <= 3);
    seen[static_cast<size_t>(b->protocol)] = true;
    for (size_t j = i + 1; j < brand_count(); ++j) {
      TEST_ASSERT_TRUE(std::strcmp(b->id, brand_at(j)->id) != 0);
      TEST_ASSERT_TRUE(std::strcmp(b->name, brand_at(j)->name) != 0);
    }
    // no two keys of one remote share a command code
    for (size_t a = 0; a < kFuncCount; ++a) {
      for (size_t c = a + 1; c < kFuncCount; ++c) TEST_ASSERT_TRUE(b->command[a] != b->command[c]);
    }
  }
  for (bool s : seen) TEST_ASSERT_TRUE(s);  // every protocol is used by at least one brand
}

void test_ir_field_widths() {
  for (size_t i = 0; i < brand_count(); ++i) {
    const Brand* b = brand_at(i);
    for (size_t f = 0; f < kFuncCount; ++f) {
      switch (b->protocol) {
        case Protocol::Sirc12:
          TEST_ASSERT_TRUE(b->address <= 0x1F);
          TEST_ASSERT_TRUE(b->command[f] <= 0x7F);
          TEST_ASSERT_EQUAL_UINT8(2, b->repeats);  // 3 frames in total, as the SIRC description asks
          break;
        case Protocol::Rc5:
          TEST_ASSERT_TRUE(b->address <= 0x1F);
          TEST_ASSERT_TRUE(b->command[f] <= 0x3F);  // 6-bit commands: the RC5X extension is not used
          break;
        case Protocol::Nec:
        case Protocol::Samsung32:
          break;  // 8-bit address and command: any uint8_t is valid
      }
    }
  }
}

void test_ir_lookup_helpers() {
  TEST_ASSERT_NULL(find_brand(nullptr));
  TEST_ASSERT_NULL(find_brand("nope"));
  TEST_ASSERT_NULL(find_brand(""));
  TEST_ASSERT_NULL(brand_at(brand_count()));
  TEST_ASSERT_NULL(brand_at(9999));
  TEST_ASSERT_EQUAL_STRING("Power", func_label(Func::Power));
  TEST_ASSERT_EQUAL_STRING("Ch-", func_label(Func::ChDown));
  TEST_ASSERT_EQUAL_STRING("?", func_label(Func::Count));
  TEST_ASSERT_EQUAL_STRING("NEC", protocol_name(Protocol::Nec));
  TEST_ASSERT_EQUAL_STRING("SIRC-12", protocol_name(Protocol::Sirc12));

  Frame f;
  TEST_ASSERT_FALSE(frame_for(nullptr, Func::Power, &f));
  TEST_ASSERT_FALSE(frame_for(find_brand("lg"), Func::Power, nullptr));
  TEST_ASSERT_FALSE(frame_for(find_brand("lg"), Func::Count, &f));
  TEST_ASSERT_TRUE(frame_for(find_brand("lg"), Func::VolUp, &f));
  TEST_ASSERT_EQUAL_UINT8(static_cast<uint8_t>(Protocol::Nec), static_cast<uint8_t>(f.protocol));
  TEST_ASSERT_EQUAL_UINT16(0x04, f.address);
  TEST_ASSERT_EQUAL_UINT16(0x02, f.command);
  TEST_ASSERT_EQUAL_UINT8(0, f.bits);
  TEST_ASSERT_TRUE(frame_for(find_brand("sony"), Func::Power, &f));
  TEST_ASSERT_EQUAL_UINT8(12, f.bits);
  TEST_ASSERT_EQUAL_UINT8(2, f.repeats);
  TEST_ASSERT_EQUAL_UINT16(21, f.command);
}

void test_ir_bit_helpers() {
  TEST_ASSERT_EQUAL_HEX32(0x80000000u, reverse_bits32(1));
  TEST_ASSERT_EQUAL_HEX32(1u, reverse_bits32(0x80000000u));
  TEST_ASSERT_EQUAL_HEX32(0u, reverse_bits32(0));
  TEST_ASSERT_EQUAL_HEX32(0xFFFFFFFFu, reverse_bits32(0xFFFFFFFFu));
  TEST_ASSERT_EQUAL_HEX16(0x800, reverse_bits12(1));
  TEST_ASSERT_EQUAL_HEX16(1, reverse_bits12(0x800));
  for (uint32_t v : {0x12345678u, 0xDEADBEEFu, 0x0F0F0F0Fu}) TEST_ASSERT_EQUAL_HEX32(v, reverse_bits32(reverse_bits32(v)));
}

// NEC as the IRremote library documents it: address 0x04 command 0x08 has the wire word 0xF708FB04 (LSB first).
void test_ir_nec_convention() {
  TEST_ASSERT_EQUAL_HEX32(0xF708FB04u, nec_wire(0x04, 0x08));
  TEST_ASSERT_EQUAL_HEX32(0x20DF10EFu, reverse_bits32(nec_wire(0x04, 0x08)));
}

// LG TV remote codes as public lists print them (MSB first, 0x20DFxxxx).
void test_ir_lg_matches_printed_codes() {
  TEST_ASSERT_EQUAL_HEX32(0x20DF10EFu, nec_printed("lg", Func::Power));
  TEST_ASSERT_EQUAL_HEX32(0x20DF40BFu, nec_printed("lg", Func::VolUp));
  TEST_ASSERT_EQUAL_HEX32(0x20DFC03Fu, nec_printed("lg", Func::VolDown));
  TEST_ASSERT_EQUAL_HEX32(0x20DF906Fu, nec_printed("lg", Func::Mute));
  TEST_ASSERT_EQUAL_HEX32(0x20DF00FFu, nec_printed("lg", Func::ChUp));
  TEST_ASSERT_EQUAL_HEX32(0x20DF807Fu, nec_printed("lg", Func::ChDown));
}

// Samsung32 TV codes as printed by public lists (0xE0E0xxxx).
void test_ir_samsung_matches_printed_codes() {
  TEST_ASSERT_EQUAL_HEX32(0xE0E040BFu, samsung_printed(Func::Power));
  TEST_ASSERT_EQUAL_HEX32(0xE0E0E01Fu, samsung_printed(Func::VolUp));
  TEST_ASSERT_EQUAL_HEX32(0xE0E0D02Fu, samsung_printed(Func::VolDown));
  TEST_ASSERT_EQUAL_HEX32(0xE0E0F00Fu, samsung_printed(Func::Mute));
  TEST_ASSERT_EQUAL_HEX32(0xE0E048B7u, samsung_printed(Func::ChUp));
  TEST_ASSERT_EQUAL_HEX32(0xE0E008F7u, samsung_printed(Func::ChDown));
}

// Sony SIRC 12-bit codes as printed by public lists (MSB first, 12 bits).
void test_ir_sony_matches_printed_codes() {
  TEST_ASSERT_EQUAL_HEX16(0xA90, sony_printed(Func::Power));
  TEST_ASSERT_EQUAL_HEX16(0x490, sony_printed(Func::VolUp));
  TEST_ASSERT_EQUAL_HEX16(0xC90, sony_printed(Func::VolDown));
  TEST_ASSERT_EQUAL_HEX16(0x290, sony_printed(Func::Mute));
  TEST_ASSERT_EQUAL_HEX16(0x090, sony_printed(Func::ChUp));
  TEST_ASSERT_EQUAL_HEX16(0x890, sony_printed(Func::ChDown));
  // the library builds the same word: command in bits 0..6, address from bit 7
  TEST_ASSERT_EQUAL_HEX16(0x095, sirc12_wire(1, 21));
}

// NEC address 0x40 (Toshiba-type sets): the commonly printed 0x02FDxxxx family.
void test_ir_nec_toshiba_type_matches_printed_codes() {
  TEST_ASSERT_EQUAL_HEX32(0x02FD48B7u, nec_printed("nec", Func::Power));
  TEST_ASSERT_EQUAL_HEX32(0x02FD58A7u, nec_printed("nec", Func::VolUp));
  TEST_ASSERT_EQUAL_HEX32(0x02FD7887u, nec_printed("nec", Func::VolDown));
  TEST_ASSERT_EQUAL_HEX32(0x02FD08F7u, nec_printed("nec", Func::Mute));
}

// RC5: S1 S2 T A4..A0 C5..C0, MSB first; S2 is 1 for commands below 64.
void test_ir_rc5_word() {
  TEST_ASSERT_EQUAL_HEX16(0x300C, rc5_word(0, 0x0C, false));
  TEST_ASSERT_EQUAL_HEX16(0x380C, rc5_word(0, 0x0C, true));
  TEST_ASSERT_EQUAL_HEX16(0x3010, rc5_word(0, 0x10, false));
  TEST_ASSERT_EQUAL_HEX16(0x3000 | (5 << 6) | 1, rc5_word(5, 1, false));
  TEST_ASSERT_EQUAL_HEX16(0x2000 | 0x01, rc5_word(0, 0x41, false));  // extended command: S2 = 0
}

// ---- parsers: rooms ---------------------------------------------------------------------------------

void test_rooms_ok() {
  const char* j =
      "{\"configured\":true,\"rooms\":[{\"id\":\"living\",\"name\":\"Living room\",\"count\":3},"
      "{\"id\":\"bed-2\",\"name\":\"Bedroom\",\"count\":1}],\"next_after\":null}";
  RoomList r;
  TEST_ASSERT_EQUAL_UINT8(static_cast<uint8_t>(Parse::Ok), static_cast<uint8_t>(parse_rooms(j, std::strlen(j), &r)));
  TEST_ASSERT_TRUE(r.configured);
  TEST_ASSERT_EQUAL_UINT8(2, r.count);
  TEST_ASSERT_FALSE(r.more);
  TEST_ASSERT_EQUAL_STRING("living", r.rooms[0].id);
  TEST_ASSERT_EQUAL_STRING("Living room", r.rooms[0].name);
  TEST_ASSERT_EQUAL_UINT8(3, r.rooms[0].count);
  TEST_ASSERT_EQUAL_STRING("bed-2", r.rooms[1].id);
}

void test_rooms_unconfigured() {
  const char* j = "{\"configured\":false,\"reason\":\"no_token\",\"rooms\":[]}";
  RoomList r;
  TEST_ASSERT_EQUAL_UINT8(static_cast<uint8_t>(Parse::Ok), static_cast<uint8_t>(parse_rooms(j, std::strlen(j), &r)));
  TEST_ASSERT_FALSE(r.configured);
  TEST_ASSERT_EQUAL_STRING("no_token", r.reason);
  TEST_ASSERT_EQUAL_UINT8(0, r.count);
  // a backend that omits the rooms key while unconfigured is accepted; while configured it is not
  const char* k = "{\"configured\":false}";
  TEST_ASSERT_EQUAL_UINT8(static_cast<uint8_t>(Parse::Ok), static_cast<uint8_t>(parse_rooms(k, std::strlen(k), &r)));
  const char* m = "{\"configured\":true}";
  TEST_ASSERT_EQUAL_UINT8(static_cast<uint8_t>(Parse::WrongShape), static_cast<uint8_t>(parse_rooms(m, std::strlen(m), &r)));
}

void test_rooms_rejects_bad_input() {
  RoomList r;
  TEST_ASSERT_EQUAL_UINT8(static_cast<uint8_t>(Parse::Malformed), static_cast<uint8_t>(parse_rooms(nullptr, 0, &r)));
  TEST_ASSERT_EQUAL_UINT8(static_cast<uint8_t>(Parse::Malformed), static_cast<uint8_t>(parse_rooms("", 0, &r)));
  TEST_ASSERT_EQUAL_UINT8(static_cast<uint8_t>(Parse::Malformed), static_cast<uint8_t>(parse_rooms("{", 1, &r)));
  TEST_ASSERT_EQUAL_UINT8(static_cast<uint8_t>(Parse::Malformed), static_cast<uint8_t>(parse_rooms("not json", 8, &r)));
  TEST_ASSERT_EQUAL_UINT8(static_cast<uint8_t>(Parse::WrongShape), static_cast<uint8_t>(parse_rooms("[1,2]", 5, &r)));
  const char* a = "{\"configured\":\"yes\",\"rooms\":[]}";
  TEST_ASSERT_EQUAL_UINT8(static_cast<uint8_t>(Parse::WrongShape), static_cast<uint8_t>(parse_rooms(a, std::strlen(a), &r)));
  const char* b = "{\"configured\":true,\"rooms\":{\"id\":\"x\"}}";
  TEST_ASSERT_EQUAL_UINT8(static_cast<uint8_t>(Parse::WrongShape), static_cast<uint8_t>(parse_rooms(b, std::strlen(b), &r)));
  TEST_ASSERT_EQUAL_UINT8(static_cast<uint8_t>(Parse::WrongShape), static_cast<uint8_t>(parse_rooms("{}", 2, nullptr)));
  // larger than the client buffer
  const std::string big(homejson::kMaxInput + 1, ' ');
  TEST_ASSERT_EQUAL_UINT8(static_cast<uint8_t>(Parse::Malformed), static_cast<uint8_t>(parse_rooms(big.c_str(), big.size(), &r)));
}

void test_rooms_nesting_limit() {
  std::string deep = "{\"configured\":true,\"rooms\":";
  for (int i = 0; i < 40; ++i) deep += "[";
  for (int i = 0; i < 40; ++i) deep += "]";
  deep += "}";
  RoomList r;
  TEST_ASSERT_EQUAL_UINT8(static_cast<uint8_t>(Parse::Malformed), static_cast<uint8_t>(parse_rooms(deep.c_str(), deep.size(), &r)));
}

void test_rooms_hostile_fields() {
  // invalid ids are dropped, long names cut, control characters replaced, counts clamped
  std::string j =
      "{\"configured\":true,\"rooms\":["
      "{\"id\":\"UPPER\",\"name\":\"x\",\"count\":1},"
      "{\"id\":\"has space\",\"name\":\"x\",\"count\":1},"
      "{\"id\":\"../etc\",\"name\":\"x\",\"count\":1},"
      "{\"id\":12,\"name\":\"x\"},"
      "{\"name\":\"no id\"},"
      "{\"id\":\"ok\",\"name\":\"0123456789012345678901234567890123456789\",\"count\":99999},"
      "{\"id\":\"ok2\",\"name\":\"tab\\there\\u00e9\\u0001\",\"count\":-5}]}";
  RoomList r;
  TEST_ASSERT_EQUAL_UINT8(static_cast<uint8_t>(Parse::Ok), static_cast<uint8_t>(parse_rooms(j.c_str(), j.size(), &r)));
  TEST_ASSERT_EQUAL_UINT8(2, r.count);
  TEST_ASSERT_EQUAL_STRING("ok", r.rooms[0].id);
  TEST_ASSERT_EQUAL_UINT(24, std::strlen(r.rooms[0].name));
  TEST_ASSERT_EQUAL_UINT8(255, r.rooms[0].count);  // clamped
  TEST_ASSERT_EQUAL_UINT8(0, r.rooms[1].count);    // negative -> 0
  for (const char* p = r.rooms[1].name; *p != '\0'; ++p) TEST_ASSERT_TRUE(*p >= 0x20 && *p <= 0x7E);
}

void test_rooms_capacity() {
  std::string j = "{\"configured\":true,\"rooms\":[";
  for (int i = 0; i < 40; ++i) {
    char one[64];
    std::snprintf(one, sizeof(one), "%s{\"id\":\"r%d\",\"name\":\"R%d\",\"count\":1}", i ? "," : "", i, i);
    j += one;
  }
  j += "]}";
  RoomList r;
  TEST_ASSERT_EQUAL_UINT8(static_cast<uint8_t>(Parse::Ok), static_cast<uint8_t>(parse_rooms(j.c_str(), j.size(), &r)));
  TEST_ASSERT_EQUAL_UINT8(kMaxRooms, r.count);
  TEST_ASSERT_TRUE(r.more);
}

// ---- parsers: devices -------------------------------------------------------------------------------

const char* kDevicesPage1 =
    "{\"room\":{\"id\":\"living\",\"name\":\"Living room\"},\"devices\":["
    "{\"entity_id\":\"light.living_ceiling\",\"label\":\"Ceiling light\",\"state\":\"on\",\"impactful\":false,"
    "\"actions\":[\"turn_on\",\"turn_off\",\"toggle\"]},"
    "{\"entity_id\":\"lock.front_door\",\"label\":\"Front door\",\"state\":\"locked\",\"impactful\":true,"
    "\"actions\":[\"turn_on\",\"turn_off\"]}],\"next_after\":\"2\"}";

const char* kDevicesPage2 =
    "{\"room\":{\"id\":\"living\",\"name\":\"Living room\"},\"devices\":["
    "{\"entity_id\":\"switch.fan\",\"label\":\"Fan\",\"state\":\"off\",\"impactful\":false,\"actions\":[\"toggle\"]}],"
    "\"next_after\":null}";

void test_devices_ok_and_paging() {
  DeviceList d;
  TEST_ASSERT_EQUAL_UINT8(static_cast<uint8_t>(Parse::Ok),
                          static_cast<uint8_t>(parse_devices(kDevicesPage1, std::strlen(kDevicesPage1), &d, false)));
  TEST_ASSERT_EQUAL_STRING("living", d.room_id);
  TEST_ASSERT_EQUAL_STRING("Living room", d.room_name);
  TEST_ASSERT_EQUAL_UINT8(2, d.count);
  TEST_ASSERT_TRUE(d.more);
  TEST_ASSERT_EQUAL_STRING("2", d.next_after);
  TEST_ASSERT_EQUAL_STRING("light.living_ceiling", d.devices[0].entity_id);
  TEST_ASSERT_EQUAL_STRING("on", d.devices[0].state);
  TEST_ASSERT_FALSE(d.devices[0].impactful);
  TEST_ASSERT_EQUAL_UINT8(kTurnOn | kTurnOff | kToggle, d.devices[0].actions);
  TEST_ASSERT_TRUE(d.devices[1].impactful);
  TEST_ASSERT_EQUAL_UINT8(kTurnOn | kTurnOff, d.devices[1].actions);

  TEST_ASSERT_EQUAL_UINT8(static_cast<uint8_t>(Parse::Ok),
                          static_cast<uint8_t>(parse_devices(kDevicesPage2, std::strlen(kDevicesPage2), &d, true)));
  TEST_ASSERT_EQUAL_UINT8(3, d.count);
  TEST_ASSERT_FALSE(d.more);
  TEST_ASSERT_EQUAL_STRING("", d.next_after);
  TEST_ASSERT_EQUAL_STRING("switch.fan", d.devices[2].entity_id);
  TEST_ASSERT_EQUAL_UINT8(kToggle, d.devices[2].actions);
}

void test_devices_drops_bad_entities() {
  std::string j =
      "{\"room\":{\"id\":\"r\",\"name\":\"R\"},\"devices\":["
      "{\"entity_id\":\"LIGHT.x\",\"label\":\"a\",\"state\":\"on\"},"
      "{\"entity_id\":\"light.x/../y\",\"label\":\"b\",\"state\":\"on\"},"
      "{\"entity_id\":\"nodot\",\"label\":\"c\",\"state\":\"on\"},"
      "{\"entity_id\":\"a.b.c\",\"label\":\"d\",\"state\":\"on\"},"
      "{\"entity_id\":\".x\",\"label\":\"e\",\"state\":\"on\"},"
      "{\"entity_id\":\"x.\",\"label\":\"f\",\"state\":\"on\"},"
      "{\"entity_id\":42,\"label\":\"g\"},"
      "{\"label\":\"h\"},"
      "{\"entity_id\":\"light.";
  j += std::string(60, 'a');  // 6 + 60 = 66 characters: one too many
  j += "\",\"label\":\"long\"},{\"entity_id\":\"light.ok\",\"label\":\"fine\",\"state\":\"on\"}]}";
  DeviceList d;
  TEST_ASSERT_EQUAL_UINT8(static_cast<uint8_t>(Parse::Ok), static_cast<uint8_t>(parse_devices(j.c_str(), j.size(), &d, false)));
  TEST_ASSERT_EQUAL_UINT8(1, d.count);
  TEST_ASSERT_EQUAL_STRING("light.ok", d.devices[0].entity_id);
}

void test_devices_field_bounds() {
  // maximum-length entity id (64) is kept; state and label are cut; unknown and non-string actions ignored;
  // impactful must be a real boolean
  std::string eid = "light." + std::string(58, 'b');
  std::string j = "{\"room\":{\"id\":\"r\",\"name\":\"R\"},\"devices\":[{\"entity_id\":\"" + eid +
                  "\",\"label\":\"0123456789012345678901234567890123456789\","
                  "\"state\":\"0123456789012345678901234567890\",\"impactful\":\"true\","
                  "\"actions\":[\"turn_on\",\"format_disk\",7,null,\"toggle\"]}]}";
  DeviceList d;
  TEST_ASSERT_EQUAL_UINT8(static_cast<uint8_t>(Parse::Ok), static_cast<uint8_t>(parse_devices(j.c_str(), j.size(), &d, false)));
  TEST_ASSERT_EQUAL_UINT8(1, d.count);
  TEST_ASSERT_EQUAL_UINT(64, std::strlen(d.devices[0].entity_id));
  TEST_ASSERT_EQUAL_UINT(24, std::strlen(d.devices[0].label));
  TEST_ASSERT_EQUAL_UINT(16, std::strlen(d.devices[0].state));
  TEST_ASSERT_FALSE(d.devices[0].impactful);
  TEST_ASSERT_EQUAL_UINT8(kTurnOn | kToggle, d.devices[0].actions);
}

void test_devices_wrong_shape_and_capacity() {
  DeviceList d;
  const char* a = "{\"room\":{\"id\":\"r\"},\"devices\":{}}";
  TEST_ASSERT_EQUAL_UINT8(static_cast<uint8_t>(Parse::WrongShape), static_cast<uint8_t>(parse_devices(a, std::strlen(a), &d, false)));
  const char* b = "{\"room\":{\"id\":\"BAD ID\"},\"devices\":[]}";
  TEST_ASSERT_EQUAL_UINT8(static_cast<uint8_t>(Parse::WrongShape), static_cast<uint8_t>(parse_devices(b, std::strlen(b), &d, false)));
  const char* c = "{\"devices\":[]}";
  TEST_ASSERT_EQUAL_UINT8(static_cast<uint8_t>(Parse::WrongShape), static_cast<uint8_t>(parse_devices(c, std::strlen(c), &d, false)));
  TEST_ASSERT_EQUAL_UINT8(static_cast<uint8_t>(Parse::Malformed), static_cast<uint8_t>(parse_devices("[[[", 3, &d, false)));

  std::string j = "{\"room\":{\"id\":\"r\",\"name\":\"R\"},\"devices\":[";
  for (int i = 0; i < 30; ++i) {
    char one[96];
    std::snprintf(one, sizeof(one), "%s{\"entity_id\":\"light.l%d\",\"label\":\"L%d\",\"state\":\"on\"}", i ? "," : "", i, i);
    j += one;
  }
  j += "],\"next_after\":\"30\"}";
  TEST_ASSERT_EQUAL_UINT8(static_cast<uint8_t>(Parse::Ok), static_cast<uint8_t>(parse_devices(j.c_str(), j.size(), &d, false)));
  TEST_ASSERT_EQUAL_UINT8(kMaxDevices, d.count);
  TEST_ASSERT_TRUE(d.more);
  TEST_ASSERT_EQUAL_STRING("", d.next_after);  // the cursor no longer matches what was taken
}

// ---- parsers: action result and error ------------------------------------------------------------------

void test_action_result() {
  ActionResult a;
  const char* ok = "{\"entity_id\":\"light.living_ceiling\",\"action\":\"toggle\",\"result\":\"ok\",\"state\":\"off\",\"duplicate\":true}";
  TEST_ASSERT_EQUAL_UINT8(static_cast<uint8_t>(Parse::Ok), static_cast<uint8_t>(parse_action(ok, std::strlen(ok), &a)));
  TEST_ASSERT_EQUAL_STRING("light.living_ceiling", a.entity_id);
  TEST_ASSERT_EQUAL_STRING("off", a.state);
  TEST_ASSERT_TRUE(a.duplicate);

  const char* no = "{\"entity_id\":\"light.x\",\"result\":\"failed\",\"state\":\"off\"}";
  TEST_ASSERT_EQUAL_UINT8(static_cast<uint8_t>(Parse::WrongShape), static_cast<uint8_t>(parse_action(no, std::strlen(no), &a)));
  const char* bad = "{\"entity_id\":\"../x\",\"result\":\"ok\"}";
  TEST_ASSERT_EQUAL_UINT8(static_cast<uint8_t>(Parse::WrongShape), static_cast<uint8_t>(parse_action(bad, std::strlen(bad), &a)));
  TEST_ASSERT_EQUAL_UINT8(static_cast<uint8_t>(Parse::Malformed), static_cast<uint8_t>(parse_action("{\"result\":", 10, &a)));
}

void test_error_body() {
  ErrorBody e;
  const char* j = "{\"error\":\"home_unreachable\",\"detail\":\"Home Assistant did not answer\"}";
  TEST_ASSERT_TRUE(parse_error(j, std::strlen(j), &e));
  TEST_ASSERT_EQUAL_STRING("home_unreachable", e.code);
  TEST_ASSERT_EQUAL_STRING("Home Assistant did not answer", e.detail);
  const char* longd = "{\"error\":\"x\",\"detail\":\"0123456789012345678901234567890123456789012345678901234567890123456789\"}";
  TEST_ASSERT_TRUE(parse_error(longd, std::strlen(longd), &e));
  TEST_ASSERT_EQUAL_UINT(63, std::strlen(e.detail));
  TEST_ASSERT_FALSE(parse_error("{\"detail\":\"no code\"}", 20, &e));
  TEST_ASSERT_FALSE(parse_error("{\"error\":5}", 11, &e));
  TEST_ASSERT_FALSE(parse_error("<html>502</html>", 16, &e));
  TEST_ASSERT_FALSE(parse_error(nullptr, 0, &e));
}

// ---- hostile input: every prefix and byte-flip of valid documents ---------------------------------------

void test_prefixes_and_mutations_never_overrun() {
  const char* docs[] = {kDevicesPage1, kDevicesPage2,
                        "{\"configured\":true,\"rooms\":[{\"id\":\"a\",\"name\":\"A\",\"count\":2}]}",
                        "{\"entity_id\":\"light.x\",\"result\":\"ok\",\"state\":\"on\"}"};
  for (const char* doc : docs) {
    const size_t n = std::strlen(doc);
    for (size_t cut = 0; cut <= n; ++cut) {
      DeviceList d;
      RoomList r;
      ActionResult a;
      ErrorBody e;
      parse_devices(doc, cut, &d, false);
      parse_rooms(doc, cut, &r);
      parse_action(doc, cut, &a);
      parse_error(doc, cut, &e);
      TEST_ASSERT_TRUE(terminated(d.room_id, sizeof(d.room_id)));
      for (uint8_t i = 0; i < d.count; ++i) {
        TEST_ASSERT_TRUE(terminated(d.devices[i].entity_id, sizeof(d.devices[i].entity_id)));
        TEST_ASSERT_TRUE(terminated(d.devices[i].label, sizeof(d.devices[i].label)));
        TEST_ASSERT_TRUE(terminated(d.devices[i].state, sizeof(d.devices[i].state)));
      }
      TEST_ASSERT_TRUE(r.count <= kMaxRooms);
      TEST_ASSERT_TRUE(d.count <= kMaxDevices);
    }
    std::string copy(doc);
    for (size_t i = 0; i < copy.size(); i += 3) {
      const char saved = copy[i];
      copy[i] = static_cast<char>(0xFF ^ saved);
      DeviceList d;
      RoomList r;
      parse_devices(copy.c_str(), copy.size(), &d, false);
      parse_rooms(copy.c_str(), copy.size(), &r);
      TEST_ASSERT_TRUE(d.count <= kMaxDevices && r.count <= kMaxRooms);
      copy[i] = saved;
    }
  }
}

void test_identifier_grammar() {
  TEST_ASSERT_TRUE(valid_entity_id("light.kitchen_1"));
  TEST_ASSERT_TRUE(valid_entity_id("alarm_control_panel.home"));
  TEST_ASSERT_FALSE(valid_entity_id(""));
  TEST_ASSERT_FALSE(valid_entity_id(nullptr));
  TEST_ASSERT_FALSE(valid_entity_id("light"));
  TEST_ASSERT_FALSE(valid_entity_id("light.Kitchen"));
  TEST_ASSERT_FALSE(valid_entity_id("light.kitchen "));
  TEST_ASSERT_FALSE(valid_entity_id("light..x"));
  TEST_ASSERT_TRUE(valid_room_id("living-room_2"));
  TEST_ASSERT_FALSE(valid_room_id(""));
  TEST_ASSERT_FALSE(valid_room_id("Living"));
  TEST_ASSERT_FALSE(valid_room_id("0123456789012345678901234"));  // 25 characters
  TEST_ASSERT_TRUE(valid_room_id("012345678901234567890123"));    // 24 characters
  TEST_ASSERT_EQUAL_STRING("turn_on", action_name(kTurnOn));
  TEST_ASSERT_EQUAL_STRING("toggle", action_name(kToggle));
  TEST_ASSERT_EQUAL_STRING("", action_name(0));
}

int main(int, char**) {
  UNITY_BEGIN();
  RUN_TEST(test_ir_table_integrity);
  RUN_TEST(test_ir_field_widths);
  RUN_TEST(test_ir_lookup_helpers);
  RUN_TEST(test_ir_bit_helpers);
  RUN_TEST(test_ir_nec_convention);
  RUN_TEST(test_ir_lg_matches_printed_codes);
  RUN_TEST(test_ir_samsung_matches_printed_codes);
  RUN_TEST(test_ir_sony_matches_printed_codes);
  RUN_TEST(test_ir_nec_toshiba_type_matches_printed_codes);
  RUN_TEST(test_ir_rc5_word);
  RUN_TEST(test_rooms_ok);
  RUN_TEST(test_rooms_unconfigured);
  RUN_TEST(test_rooms_rejects_bad_input);
  RUN_TEST(test_rooms_nesting_limit);
  RUN_TEST(test_rooms_hostile_fields);
  RUN_TEST(test_rooms_capacity);
  RUN_TEST(test_devices_ok_and_paging);
  RUN_TEST(test_devices_drops_bad_entities);
  RUN_TEST(test_devices_field_bounds);
  RUN_TEST(test_devices_wrong_shape_and_capacity);
  RUN_TEST(test_action_result);
  RUN_TEST(test_error_body);
  RUN_TEST(test_prefixes_and_mutations_never_overrun);
  RUN_TEST(test_identifier_grammar);
  return UNITY_END();
}
