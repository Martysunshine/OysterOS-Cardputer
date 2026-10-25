// Bounded, allocation-free JSON writer used for evidence events and API bodies on the device and
// in host tests, so the encoding is identical everywhere. On overflow or misuse ok() turns false
// and the output must be discarded; it never writes past the buffer.
#pragma once

#include <cstddef>
#include <cstdint>

#include "oyster_core/types.h"

namespace oyster {

class JsonWriter {
 public:
  JsonWriter(char* buf, size_t cap);

  // Containers. The keyed forms are for use inside an object, the unkeyed ones inside an array or at top level.
  void begin_object();
  void begin_object(const char* key);
  void end_object();
  void begin_array();
  void begin_array(const char* key);
  void end_array();

  // Keyed values (inside an object).
  void str(const char* key, const char* v);                 // nullptr -> null. Escapes quotes, backslash, control and non-ASCII (\u00XX)
  void str_bytes(const char* key, const uint8_t* v, size_t n);  // raw bytes escaped byte-by-byte (\u00XX for >= 0x80)
  void hex(const char* key, const uint8_t* v, size_t n);    // lowercase hex string
  void num(const char* key, int64_t v);
  void unum(const char* key, uint64_t v);
  void boolean(const char* key, bool v);
  void tribool(const char* key, TriBool v);                 // true / false / null
  void null(const char* key);
  void raw(const char* key, const char* json);              // trusted, already-serialised JSON value

  // Unkeyed values (inside an array).
  void str_value(const char* v);
  void hex_value(const uint8_t* v, size_t n);
  void num_value(int64_t v);
  void raw_value(const char* json);

  bool ok() const { return ok_; }
  size_t size() const { return len_; }
  const char* c_str() const { return buf_; }

 private:
  void put(char c);
  void puts(const char* s);
  void comma();
  void write_key(const char* key);
  void write_escaped(const uint8_t* v, size_t n);

  char* buf_;
  size_t cap_;
  size_t len_ = 0;
  bool ok_ = true;
  uint8_t depth_ = 0;
  bool need_comma_[16] = {};
};

// Printable-ASCII display form of untrusted bytes (SSIDs, BLE names). Returns true and writes the
// text when every byte is 0x20..0x7E; otherwise returns false and writes an empty string. Callers
// record the hex form always and the display form only when this returns true.
bool display_safe(const uint8_t* in, size_t n, char* out, size_t out_len);

// "aa:bb:cc:dd:ee:ff" (lowercase). out_len >= 18.
void format_mac(const uint8_t mac[6], char* out, size_t out_len);

// Canonical lowercase UUID string from 16 big-endian bytes. out_len >= 37.
void format_uuid128(const uint8_t uuid[16], char* out, size_t out_len);

// Parse "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx" (case-insensitive) into 16 big-endian bytes.
bool parse_uuid128(const char* s, uint8_t out[16]);

}  // namespace oyster
