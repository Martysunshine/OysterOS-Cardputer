// Identifier validation and formatting. Scans are bounded so an unterminated buffer is never over-read
// by more than max_len + 1 bytes.
#include "oyster_core/ids.h"

#include <cstring>

namespace oyster {

namespace {

constexpr size_t kObjectIdMaxLen = 16;  // xfer::kObjectIdBytes (manifest field width)

bool is_safe_char(char c) {
  return (c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') || (c >= '0' && c <= '9') || c == '_' || c == '-';
}

// Writes 8 lowercase hex digits of v (most significant first), no NUL.
void put_hex32(uint32_t v, char* out) {
  static const char kHex[] = "0123456789abcdef";
  for (int i = 7; i >= 0; --i) {
    out[i] = kHex[v & 0xF];
    v >>= 4;
  }
}

}  // namespace

bool is_safe_id_chars(const char* s, size_t max_len) {
  if (s == nullptr || max_len == 0) return false;
  size_t i = 0;
  for (; i < max_len; ++i) {
    if (s[i] == '\0') break;
    if (!is_safe_char(s[i])) return false;
  }
  if (i == 0) return false;                     // empty
  if (i == max_len && s[i] != '\0') return false;  // longer than max_len
  return true;
}

bool is_prefixed_id(const char* s, const char* prefix) {
  if (s == nullptr || prefix == nullptr) return false;
  const size_t plen = std::strlen(prefix);
  if (plen == 0 || std::strncmp(s, prefix, plen) != 0) return false;
  return is_safe_id_chars(s + plen, cfg::kMaxIdLen);
}

bool is_valid_session_id(const char* s) { return is_prefixed_id(s, "sess-"); }
bool is_valid_job_id(const char* s) { return is_prefixed_id(s, "job-"); }
bool is_valid_target_id(const char* s) { return is_prefixed_id(s, "target-"); }
bool is_valid_device_id(const char* s) { return is_safe_id_chars(s, cfg::kMaxIdLen); }
bool is_valid_nonce(const char* s) { return is_safe_id_chars(s, cfg::kMaxIdLen); }

// The safe character set already excludes '/', '\\', '.', ':' and NUL, so "..", absolute paths and
// drive prefixes cannot pass.
bool is_valid_object_id(const char* s) { return is_safe_id_chars(s, kObjectIdMaxLen); }

bool format_event_id(uint32_t sequence, char* out, size_t n) {
  if (out == nullptr || n == 0) return false;
  char digits[10];
  size_t nd = 0;
  uint32_t v = sequence;
  do {
    digits[nd++] = static_cast<char>('0' + (v % 10));
    v /= 10;
  } while (v != 0);
  const size_t width = nd < 6 ? 6 : nd;
  const size_t need = 4 + width + 1;
  if (n < need) {
    out[0] = '\0';
    return false;
  }
  std::memcpy(out, "evt-", 4);
  size_t p = 4;
  for (size_t i = nd; i < width; ++i) out[p++] = '0';
  while (nd > 0) out[p++] = digits[--nd];
  out[p] = '\0';
  return true;
}

bool format_boot_id(uint32_t r1, uint32_t r2, char* out, size_t n) {
  if (out == nullptr || n == 0) return false;
  if (n < 5 + 16 + 1) {
    out[0] = '\0';
    return false;
  }
  std::memcpy(out, "boot-", 5);
  put_hex32(r1, out + 5);
  put_hex32(r2, out + 13);
  out[21] = '\0';
  return true;
}

// nullptr src is treated as an empty string.
bool copy_bounded(char* dst, size_t dst_len, const char* src) {
  if (dst == nullptr || dst_len == 0) return false;
  if (src == nullptr) {
    dst[0] = '\0';
    return true;
  }
  size_t i = 0;
  for (; i + 1 < dst_len && src[i] != '\0'; ++i) dst[i] = src[i];
  dst[i] = '\0';
  return src[i] == '\0';
}

}  // namespace oyster
