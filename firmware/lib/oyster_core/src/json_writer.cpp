// Bounded JSON writer and display helpers. One byte of the buffer is always reserved for the NUL
// terminator; a write that does not fit clears ok() and is dropped.
#include "oyster_core/json_writer.h"

#include <cstring>

namespace oyster {

namespace {

constexpr char kHexDigits[] = "0123456789abcdef";
constexpr uint8_t kMaxDepth = 15;  // need_comma_[0] is the top level

// Decimal digits of v into tmp (no NUL), most significant first. Returns the digit count.
size_t format_u64(uint64_t v, char tmp[20]) {
  char rev[20];
  size_t n = 0;
  do {
    rev[n++] = static_cast<char>('0' + (v % 10));
    v /= 10;
  } while (v != 0);
  for (size_t i = 0; i < n; ++i) tmp[i] = rev[n - 1 - i];
  return n;
}

int hex_nibble(char c) {
  if (c >= '0' && c <= '9') return c - '0';
  if (c >= 'a' && c <= 'f') return c - 'a' + 10;
  if (c >= 'A' && c <= 'F') return c - 'A' + 10;
  return -1;
}

}  // namespace

JsonWriter::JsonWriter(char* buf, size_t cap) : buf_(buf), cap_(cap) {
  if (buf_ == nullptr || cap_ == 0) {
    ok_ = false;
    return;
  }
  buf_[0] = '\0';
}

void JsonWriter::put(char c) {
  if (!ok_) return;
  if (len_ + 1 >= cap_) {  // keep room for the terminator
    ok_ = false;
    return;
  }
  buf_[len_++] = c;
  buf_[len_] = '\0';
}

void JsonWriter::puts(const char* s) {
  if (s == nullptr) return;
  while (*s != '\0' && ok_) put(*s++);
}

void JsonWriter::comma() {
  if (need_comma_[depth_]) put(',');
  need_comma_[depth_] = true;
}

void JsonWriter::write_key(const char* key) {
  if (key == nullptr) {
    ok_ = false;
    return;
  }
  comma();
  put('"');
  write_escaped(reinterpret_cast<const uint8_t*>(key), std::strlen(key));
  put('"');
  put(':');
}

void JsonWriter::write_escaped(const uint8_t* v, size_t n) {
  for (size_t i = 0; i < n && ok_; ++i) {
    const uint8_t b = v[i];
    switch (b) {
      case '"': put('\\'); put('"'); break;
      case '\\': put('\\'); put('\\'); break;
      case '\n': put('\\'); put('n'); break;
      case '\r': put('\\'); put('r'); break;
      case '\t': put('\\'); put('t'); break;
      case '\b': put('\\'); put('b'); break;
      case '\f': put('\\'); put('f'); break;
      default:
        if (b < 0x20 || b >= 0x7F) {
          // Control bytes, DEL and every non-ASCII byte: \u00XX of the raw byte value.
          put('\\'); put('u'); put('0'); put('0');
          put(kHexDigits[b >> 4]);
          put(kHexDigits[b & 0x0F]);
        } else {
          put(static_cast<char>(b));
        }
        break;
    }
  }
}

void JsonWriter::begin_object() {
  comma();
  put('{');
  if (depth_ >= kMaxDepth) {
    ok_ = false;
    return;
  }
  need_comma_[++depth_] = false;
}

void JsonWriter::begin_object(const char* key) {
  write_key(key);
  put('{');
  if (depth_ >= kMaxDepth) {
    ok_ = false;
    return;
  }
  need_comma_[++depth_] = false;
}

void JsonWriter::end_object() {
  if (depth_ == 0) {
    ok_ = false;
    return;
  }
  put('}');
  --depth_;
}

void JsonWriter::begin_array() {
  comma();
  put('[');
  if (depth_ >= kMaxDepth) {
    ok_ = false;
    return;
  }
  need_comma_[++depth_] = false;
}

void JsonWriter::begin_array(const char* key) {
  write_key(key);
  put('[');
  if (depth_ >= kMaxDepth) {
    ok_ = false;
    return;
  }
  need_comma_[++depth_] = false;
}

void JsonWriter::end_array() {
  if (depth_ == 0) {
    ok_ = false;
    return;
  }
  put(']');
  --depth_;
}

void JsonWriter::str(const char* key, const char* v) {
  write_key(key);
  if (v == nullptr) {
    puts("null");
    return;
  }
  put('"');
  write_escaped(reinterpret_cast<const uint8_t*>(v), std::strlen(v));
  put('"');
}

void JsonWriter::str_bytes(const char* key, const uint8_t* v, size_t n) {
  write_key(key);
  if (v == nullptr && n > 0) {
    puts("null");
    return;
  }
  put('"');
  if (v != nullptr) write_escaped(v, n);
  put('"');
}

void JsonWriter::hex(const char* key, const uint8_t* v, size_t n) {
  write_key(key);
  if (v == nullptr && n > 0) {
    puts("null");
    return;
  }
  put('"');
  for (size_t i = 0; i < n && ok_; ++i) {
    put(kHexDigits[v[i] >> 4]);
    put(kHexDigits[v[i] & 0x0F]);
  }
  put('"');
}

void JsonWriter::num(const char* key, int64_t v) {
  write_key(key);
  char tmp[20];
  uint64_t mag;
  if (v < 0) {
    put('-');
    mag = static_cast<uint64_t>(-(v + 1)) + 1;  // safe for INT64_MIN
  } else {
    mag = static_cast<uint64_t>(v);
  }
  const size_t n = format_u64(mag, tmp);
  for (size_t i = 0; i < n; ++i) put(tmp[i]);
}

void JsonWriter::unum(const char* key, uint64_t v) {
  write_key(key);
  char tmp[20];
  const size_t n = format_u64(v, tmp);
  for (size_t i = 0; i < n; ++i) put(tmp[i]);
}

void JsonWriter::boolean(const char* key, bool v) {
  write_key(key);
  puts(v ? "true" : "false");
}

void JsonWriter::tribool(const char* key, TriBool v) {
  write_key(key);
  puts(!v.known() ? "null" : (v.is_true() ? "true" : "false"));
}

void JsonWriter::null(const char* key) {
  write_key(key);
  puts("null");
}

void JsonWriter::raw(const char* key, const char* json) {
  write_key(key);
  puts(json == nullptr ? "null" : json);
}

void JsonWriter::str_value(const char* v) {
  comma();
  if (v == nullptr) {
    puts("null");
    return;
  }
  put('"');
  write_escaped(reinterpret_cast<const uint8_t*>(v), std::strlen(v));
  put('"');
}

void JsonWriter::hex_value(const uint8_t* v, size_t n) {
  comma();
  if (v == nullptr && n > 0) {
    puts("null");
    return;
  }
  put('"');
  for (size_t i = 0; i < n && ok_; ++i) {
    put(kHexDigits[v[i] >> 4]);
    put(kHexDigits[v[i] & 0x0F]);
  }
  put('"');
}

void JsonWriter::num_value(int64_t v) {
  comma();
  char tmp[20];
  uint64_t mag;
  if (v < 0) {
    put('-');
    mag = static_cast<uint64_t>(-(v + 1)) + 1;
  } else {
    mag = static_cast<uint64_t>(v);
  }
  const size_t n = format_u64(mag, tmp);
  for (size_t i = 0; i < n; ++i) put(tmp[i]);
}

void JsonWriter::raw_value(const char* json) {
  comma();
  puts(json == nullptr ? "null" : json);
}

// An empty input has no display form (e.g. a hidden SSID is shown as null, not "").
bool display_safe(const uint8_t* in, size_t n, char* out, size_t out_len) {
  if (out == nullptr || out_len == 0) return false;
  out[0] = '\0';
  if (in == nullptr || n == 0 || n + 1 > out_len) return false;
  for (size_t i = 0; i < n; ++i) {
    if (in[i] < 0x20 || in[i] > 0x7E) return false;
  }
  std::memcpy(out, in, n);
  out[n] = '\0';
  return true;
}

void format_mac(const uint8_t mac[6], char* out, size_t out_len) {
  if (out == nullptr || out_len == 0) return;
  out[0] = '\0';
  if (mac == nullptr || out_len < 18) return;
  size_t p = 0;
  for (int i = 0; i < 6; ++i) {
    if (i > 0) out[p++] = ':';
    out[p++] = kHexDigits[mac[i] >> 4];
    out[p++] = kHexDigits[mac[i] & 0x0F];
  }
  out[p] = '\0';
}

void format_uuid128(const uint8_t uuid[16], char* out, size_t out_len) {
  if (out == nullptr || out_len == 0) return;
  out[0] = '\0';
  if (uuid == nullptr || out_len < 37) return;
  size_t p = 0;
  for (int i = 0; i < 16; ++i) {
    if (i == 4 || i == 6 || i == 8 || i == 10) out[p++] = '-';
    out[p++] = kHexDigits[uuid[i] >> 4];
    out[p++] = kHexDigits[uuid[i] & 0x0F];
  }
  out[p] = '\0';
}

bool parse_uuid128(const char* s, uint8_t out[16]) {
  if (s == nullptr || out == nullptr) return false;
  uint8_t tmp[16];
  size_t byte = 0;
  size_t i = 0;
  for (; i < 36; ++i) {
    const char c = s[i];
    if (c == '\0') return false;
    if (i == 8 || i == 13 || i == 18 || i == 23) {
      if (c != '-') return false;
      continue;
    }
    const int hi = hex_nibble(c);
    const int lo = hex_nibble(s[i + 1]);  // s[i] != NUL, so s[i + 1] is readable
    if (hi < 0 || lo < 0) return false;
    tmp[byte++] = static_cast<uint8_t>((hi << 4) | lo);
    ++i;
  }
  if (s[36] != '\0' || byte != 16) return false;
  std::memcpy(out, tmp, 16);
  return true;
}

}  // namespace oyster
