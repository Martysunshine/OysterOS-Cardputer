// IR remote code table for the Smart Home tile (host-testable, no Arduino headers).
//
// The Cardputer has an IR LED and no receiver, so there is no learn mode: the codes below are fixed tables of
// well-known TV remote families. Every code family comes from public protocol documentation and public
// community code collections (the NEC, Samsung32, SIRC and RC5 protocol descriptions and open "TV universal
// remote" code lists); address and command are stored the way the IRremote library takes them (the values it
// prints when it decodes a remote), NOT as the MSB-first hex strings some code lists print.
//
// UNVERIFIED on the owner's devices: none of these codes has been tried on a real TV yet, and the IR LED pin
// (GPIO44) is the M5Stack example's pin, not confirmed on this unit. IR is one-way: the device cannot know
// whether the TV obeyed.
#pragma once

#include <cstddef>
#include <cstdint>

namespace oyster::ircodes {

enum class Protocol : uint8_t {
  Nec,        // NEC1, 38 kHz, 8-bit address and 8-bit command (each sent with its complement)
  Samsung32,  // Samsung32, 38 kHz, 8-bit address (sent twice) and 8-bit command (sent with its complement)
  Sirc12,     // Sony SIRC 12 bit, 40 kHz, 5-bit address and 7-bit command, frame sent 3 times
  Rc5,        // Philips RC5, 36 kHz, 5-bit address and 6-bit command, toggle bit flips on every key press
};

enum class Func : uint8_t { Power, VolUp, VolDown, Mute, ChUp, ChDown, Count };
constexpr size_t kFuncCount = static_cast<size_t>(Func::Count);

struct Brand {
  const char* id;        // stable short id ("samsung"); unique, at most 11 characters
  const char* name;      // list row text, at most 24 characters
  Protocol protocol;
  uint8_t address;       // IRremote convention: Nec/Samsung32/Rc5 device byte, Sirc12 5-bit address
  uint8_t repeats;       // frames sent after the first one (SIRC wants 3 frames in total)
  uint8_t command[kFuncCount];
  const char* source;    // where the code family is documented (shown in docs, not on a screen)
};

// What the sender needs for one key press.
struct Frame {
  Protocol protocol = Protocol::Nec;
  uint16_t address = 0;
  uint16_t command = 0;
  uint8_t bits = 0;     // Sirc12: 12, others 0 (fixed by the protocol)
  uint8_t repeats = 0;
};

size_t brand_count();
const Brand* brand_at(size_t index);        // nullptr when out of range
const Brand* find_brand(const char* id);    // nullptr when unknown
const char* func_label(Func f);             // "Power", "Vol+", "Vol-", "Mute", "Ch+", "Ch-"
const char* protocol_name(Protocol p);      // "NEC", "Samsung32", "SIRC-12", "RC5"
bool frame_for(const Brand* brand, Func f, Frame* out);

// Wire words as the IRremote library builds them (transmitted least significant bit first). They exist so the
// host tests can compare the table with the MSB-first hex codes that public code lists print; the sender
// itself hands address and command to the library and does not use them.
uint32_t nec_wire(uint8_t address, uint8_t command);      // addr, ~addr, cmd, ~cmd (low byte first)
uint32_t samsung32_wire(uint8_t address, uint8_t command);  // addr, addr, cmd, ~cmd (low byte first)
uint16_t sirc12_wire(uint8_t address, uint8_t command);   // command bits 0..6, address bits 7..11
uint16_t rc5_word(uint8_t address, uint8_t command, bool toggle);  // 14 bits, sent MSB first: S1 S2 T A4..A0 C5..C0
uint32_t reverse_bits32(uint32_t v);                      // LSB-first wire word <-> MSB-first printed code
uint16_t reverse_bits12(uint16_t v);

}  // namespace oyster::ircodes
