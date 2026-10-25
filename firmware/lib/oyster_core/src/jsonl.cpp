// JSONL recovery scan and minimal field extraction. Lines are processed in a caller-provided scratch
// buffer; nothing is allocated.
#include "oyster_core/jsonl.h"
#include "oyster_core/jsonl_tail.h"

#include <climits>
#include <cstring>

namespace oyster {

size_t MemoryByteSource::read_at(size_t offset, uint8_t* buf, size_t n) {
  if (buf == nullptr || data_ == nullptr || offset >= len_) return 0;
  const size_t avail = len_ - offset;
  const size_t take = n < avail ? n : avail;
  std::memcpy(buf, data_ + offset, take);
  return take;
}

namespace {

bool is_ws(char c) { return c == ' ' || c == '\t' || c == '\r' || c == '\n'; }

// Locates `"key":` outside string literals. When the key occurs more than once, the shallowest
// occurrence wins (the first one among equals), so envelope fields beat same-named keys in
// "details". value_pos is the first non-whitespace byte after the colon.
bool find_key(const char* line, size_t len, const char* key, size_t& value_pos) {
  if (line == nullptr || key == nullptr) return false;
  const size_t klen = std::strlen(key);
  bool found = false;
  int best_depth = INT_MAX;
  int depth = 0;
  size_t i = 0;
  while (i < len) {
    const char c = line[i];
    if (c == '"') {
      const size_t start = i + 1;
      size_t j = start;
      bool escaped = false;
      while (j < len && line[j] != '"') {
        if (line[j] == '\\') {
          escaped = true;
          j += 2;
        } else {
          ++j;
        }
      }
      if (j >= len) break;  // unterminated string: stop scanning
      const size_t end = j;
      i = j + 1;
      size_t k = i;
      while (k < len && is_ws(line[k])) ++k;
      if (k < len && line[k] == ':') {
        if (!escaped && end - start == klen && std::memcmp(line + start, key, klen) == 0 && depth < best_depth) {
          size_t v = k + 1;
          while (v < len && is_ws(line[v])) ++v;
          best_depth = depth;
          value_pos = v;
          found = true;
        }
        i = k + 1;
      }
      continue;
    }
    if (c == '{' || c == '[') {
      ++depth;
    } else if (c == '}' || c == ']') {
      --depth;
    }
    ++i;
  }
  return found;
}

// Reads up to n bytes at offset, tolerating short reads. Returns bytes read.
size_t read_full(ByteSource& src, size_t offset, uint8_t* buf, size_t n) {
  size_t got = 0;
  while (got < n) {
    const size_t r = src.read_at(offset + got, buf + got, n - got);
    if (r == 0) break;
    got += r;
  }
  return got;
}

const uint8_t* find_newline(const uint8_t* p, size_t n) {
  return static_cast<const uint8_t*>(std::memchr(p, '\n', n));
}

bool is_valid_record(const uint8_t* line, size_t len, uint32_t& seq) {
  if (len < 2 || line[0] != '{' || line[len - 1] != '}') return false;
  uint64_t v = 0;
  if (!extract_uint_field(reinterpret_cast<const char*>(line), len, "sequence", v)) return false;
  if (v > UINT32_MAX) return false;
  seq = static_cast<uint32_t>(v);
  return true;
}

}  // namespace

// True when the bytes are exactly one balanced JSON object: braces and brackets outside strings pair up and
// the outermost '}' is the last byte. Tells a record that only lacks its '\n' from one cut off at a nested '}'.
bool is_balanced_json_object(const uint8_t* b, size_t n) {
  if (n < 2 || b[0] != '{' || b[n - 1] != '}') return false;
  int depth = 0;
  bool in_string = false;
  bool escaped = false;
  for (size_t i = 0; i < n; ++i) {
    const uint8_t c = b[i];
    if (in_string) {
      if (escaped) {
        escaped = false;
      } else if (c == '\\') {
        escaped = true;
      } else if (c == '"') {
        in_string = false;
      }
      continue;
    }
    if (c == '"') {
      in_string = true;
    } else if (c == '{' || c == '[') {
      ++depth;
    } else if (c == '}' || c == ']') {
      --depth;
      if (depth < 0 || (depth == 0 && i != n - 1)) return false;
    }
  }
  return depth == 0 && !in_string;
}

// longest_line counts the bytes of a complete line including its newline (the unit of
// cfg::kMaxEventBytes). Bytes that cannot be read are reported in truncated_tail.
// A final record that is complete JSON but lacks its '\n' (power lost between the record and the newline)
// is counted as valid, so its sequence number is never reused. Its bytes stay in truncated_tail and
// last_good_end stays before it: the repair step still appends the '\n'.
JsonlScanResult scan_jsonl(ByteSource& src, uint8_t* scratch, size_t scratch_len) {
  JsonlScanResult r;
  if (scratch == nullptr || scratch_len == 0) return r;
  const size_t total = src.size();
  size_t pos = 0;
  while (pos < total) {
    const size_t want = (total - pos) < scratch_len ? (total - pos) : scratch_len;
    const size_t got = read_full(src, pos, scratch, want);
    if (got == 0) break;
    const uint8_t* nl = find_newline(scratch, got);
    if (nl != nullptr) {
      const size_t line_len = static_cast<size_t>(nl - scratch);
      uint32_t seq = 0;
      if (is_valid_record(scratch, line_len, seq)) {
        ++r.valid_records;
        if (!r.has_sequence || seq > r.last_sequence) r.last_sequence = seq;
        r.has_sequence = true;
      } else {
        ++r.invalid_lines;
      }
      if (line_len + 1 > r.longest_line) r.longest_line = line_len + 1;
      pos += line_len + 1;
      r.last_good_end = pos;
      continue;
    }
    if (got < scratch_len) {
      // End of data (or a short read) without a newline: the tail. Count it only when all of it was read.
      uint32_t seq = 0;
      if (pos + got == total && is_balanced_json_object(scratch, got) && is_valid_record(scratch, got, seq)) {
        ++r.valid_records;
        if (!r.has_sequence || seq > r.last_sequence) r.last_sequence = seq;
        r.has_sequence = true;
      }
      break;
    }
    // The line does not fit in scratch: skip to its newline and count it as invalid.
    size_t skip = pos + got;
    bool terminated = false;
    while (skip < total) {
      const size_t w = (total - skip) < scratch_len ? (total - skip) : scratch_len;
      const size_t g = read_full(src, skip, scratch, w);
      if (g == 0) break;
      const uint8_t* n2 = find_newline(scratch, g);
      if (n2 != nullptr) {
        skip += static_cast<size_t>(n2 - scratch) + 1;
        terminated = true;
        break;
      }
      skip += g;
    }
    if (!terminated) break;
    ++r.invalid_lines;
    r.line_too_long = true;
    if (skip - pos > r.longest_line) r.longest_line = skip - pos;
    pos = skip;
    r.last_good_end = pos;
  }
  r.truncated_tail = total - r.last_good_end;
  return r;
}

bool extract_uint_field(const char* line, size_t len, const char* key, uint64_t& out) {
  size_t p = 0;
  if (!find_key(line, len, key, p)) return false;
  if (p >= len || line[p] < '0' || line[p] > '9') return false;
  uint64_t v = 0;
  while (p < len && line[p] >= '0' && line[p] <= '9') {
    const uint64_t d = static_cast<uint64_t>(line[p] - '0');
    if (v > (UINT64_MAX - d) / 10) return false;  // overflow
    v = v * 10 + d;
    ++p;
  }
  if (p < len) {
    const char c = line[p];
    if (!(c == ',' || c == '}' || c == ']' || is_ws(c))) return false;  // e.g. 1.5, 1e3
  }
  out = v;
  return true;
}

bool extract_str_field(const char* line, size_t len, const char* key, char* out, size_t out_len) {
  if (out == nullptr || out_len == 0) return false;
  out[0] = '\0';
  size_t p = 0;
  if (!find_key(line, len, key, p)) return false;
  if (p >= len || line[p] != '"') return false;
  const size_t start = p + 1;
  size_t q = start;
  while (q < len && line[q] != '"') {
    if (line[q] == '\\') return false;  // escapes are not decoded
    ++q;
  }
  if (q >= len) return false;
  const size_t n = q - start;
  if (n + 1 > out_len) return false;
  std::memcpy(out, line + start, n);
  out[n] = '\0';
  return true;
}

}  // namespace oyster
