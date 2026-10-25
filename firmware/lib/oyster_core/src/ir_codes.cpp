// IR code table. See include/oyster_core/ir_codes.h: all families are UNVERIFIED on the owner's devices.
#include "oyster_core/ir_codes.h"

#include <cstring>

namespace oyster::ircodes {
namespace {

// Order of the commands: Power, Vol+, Vol-, Mute, Ch+, Ch-.
// Each row says which public description the family comes from. Values are in the IRremote convention
// (what the library prints when it decodes the remote), e.g. the LG power key prints as NEC address 0x04
// command 0x08, which is the MSB-first code 0x20DF10EF in lists that print the wire word the other way round.
const Brand kBrands[] = {
    {"samsung", "TV Samsung", Protocol::Samsung32, 0x07, 0, {0x02, 0x07, 0x0B, 0x0F, 0x12, 0x10},
     "Samsung32 TV remotes, address 0x07 (E0E0xxxx in MSB-first lists); Samsung32 protocol description"},
    {"lg", "TV LG", Protocol::Nec, 0x04, 0, {0x08, 0x02, 0x03, 0x09, 0x00, 0x01},
     "NEC1 address 0x04 (20DFxxxx in MSB-first lists), the LG TV remote family; NEC protocol description"},
    {"sony", "TV Sony", Protocol::Sirc12, 0x01, 2, {0x15, 0x12, 0x13, 0x14, 0x10, 0x11},
     "Sony SIRC 12 bit, device 1 (TV): commands 21 power, 18/19 volume, 20 mute, 16/17 channel; SIRC description"},
    {"nec", "TV NEC (Toshiba type)", Protocol::Nec, 0x40, 0, {0x12, 0x1A, 0x1E, 0x10, 0x1B, 0x1F},
     "NEC1 address 0x40, the Toshiba-built TV family and many relabelled sets; NEC protocol description"},
    {"philips", "TV Philips (RC5)", Protocol::Rc5, 0x00, 0, {0x0C, 0x10, 0x11, 0x0D, 0x20, 0x21},
     "Philips RC5, system 0 (TV): commands 12 standby, 16/17 volume, 13 mute, 32/33 programme; RC5 description"},
};
constexpr size_t kBrandCount = sizeof(kBrands) / sizeof(kBrands[0]);

const char* const kFuncLabels[kFuncCount] = {"Power", "Vol+", "Vol-", "Mute", "Ch+", "Ch-"};

}  // namespace

size_t brand_count() { return kBrandCount; }

const Brand* brand_at(size_t index) { return index < kBrandCount ? &kBrands[index] : nullptr; }

const Brand* find_brand(const char* id) {
  if (id == nullptr) return nullptr;
  for (const Brand& b : kBrands) {
    if (std::strcmp(b.id, id) == 0) return &b;
  }
  return nullptr;
}

const char* func_label(Func f) {
  const size_t i = static_cast<size_t>(f);
  return i < kFuncCount ? kFuncLabels[i] : "?";
}

const char* protocol_name(Protocol p) {
  switch (p) {
    case Protocol::Nec: return "NEC";
    case Protocol::Samsung32: return "Samsung32";
    case Protocol::Sirc12: return "SIRC-12";
    case Protocol::Rc5: return "RC5";
  }
  return "?";
}

bool frame_for(const Brand* brand, Func f, Frame* out) {
  if (brand == nullptr || out == nullptr || static_cast<size_t>(f) >= kFuncCount) return false;
  out->protocol = brand->protocol;
  out->address = brand->address;
  out->command = brand->command[static_cast<size_t>(f)];
  out->bits = brand->protocol == Protocol::Sirc12 ? 12 : 0;
  out->repeats = brand->repeats;
  return true;
}

uint32_t nec_wire(uint8_t address, uint8_t command) {
  return static_cast<uint32_t>(address) | static_cast<uint32_t>(static_cast<uint8_t>(~address)) << 8 |
         static_cast<uint32_t>(command) << 16 | static_cast<uint32_t>(static_cast<uint8_t>(~command)) << 24;
}

uint32_t samsung32_wire(uint8_t address, uint8_t command) {
  return static_cast<uint32_t>(address) | static_cast<uint32_t>(address) << 8 | static_cast<uint32_t>(command) << 16 |
         static_cast<uint32_t>(static_cast<uint8_t>(~command)) << 24;
}

uint16_t sirc12_wire(uint8_t address, uint8_t command) {
  return static_cast<uint16_t>(((address & 0x1F) << 7) | (command & 0x7F));
}

uint16_t rc5_word(uint8_t address, uint8_t command, bool toggle) {
  const uint16_t field = (command & 0x40) ? 0 : 1;  // S2 is the inverted 7th command bit (RC5X extends the command)
  return static_cast<uint16_t>(1u << 13 | field << 12 | (toggle ? 1u : 0u) << 11 | (address & 0x1F) << 6 |
                               (command & 0x3F));
}

uint32_t reverse_bits32(uint32_t v) {
  uint32_t r = 0;
  for (int i = 0; i < 32; ++i) {
    r = (r << 1) | (v & 1u);
    v >>= 1;
  }
  return r;
}

uint16_t reverse_bits12(uint16_t v) {
  uint16_t r = 0;
  for (int i = 0; i < 12; ++i) {
    r = static_cast<uint16_t>((r << 1) | (v & 1u));
    v >>= 1;
  }
  return r;
}

}  // namespace oyster::ircodes
