// Append-only JSONL evidence: crash/power-loss recovery scan and minimal field extraction.
// FAT rename/flush is not power-loss atomic, so recovery must tolerate a truncated final record
// WITHOUT discarding the valid records before it. Recovery policy (implemented by EvidenceStore):
// if truncated_tail > 0, append a single '\n' so the partial record becomes a complete but invalid
// line; readers skip invalid lines; a "recovery" event records what was found.
#pragma once

#include <cstddef>
#include <cstdint>

namespace oyster {

// Random-access byte source, so the same scan runs over an SD file on the device and over memory in tests.
class ByteSource {
 public:
  virtual ~ByteSource() = default;
  virtual size_t size() const = 0;
  virtual size_t read_at(size_t offset, uint8_t* buf, size_t n) = 0;  // returns bytes read
};

class MemoryByteSource : public ByteSource {
 public:
  MemoryByteSource(const uint8_t* data, size_t len) : data_(data), len_(len) {}
  size_t size() const override { return len_; }
  size_t read_at(size_t offset, uint8_t* buf, size_t n) override;

 private:
  const uint8_t* data_;
  size_t len_;
};

struct JsonlScanResult {
  size_t valid_records = 0;
  size_t invalid_lines = 0;      // complete lines that are not a valid record (skipped, never deleted)
  size_t last_good_end = 0;      // offset just after the last newline of a complete line
  size_t truncated_tail = 0;     // bytes after the final newline (partial last record)
  bool has_sequence = false;
  uint32_t last_sequence = 0;    // highest "sequence" among valid records
  size_t longest_line = 0;
  bool line_too_long = false;    // a line exceeded the scratch buffer; counted as invalid
};

// A line is a valid record when it starts with '{', ends with '}' before the newline, and has an
// unsigned integer "sequence" field. scratch must be at least kMaxEventBytes.
JsonlScanResult scan_jsonl(ByteSource& src, uint8_t* scratch, size_t scratch_len);

// Minimal extraction for lines this firmware wrote (no general JSON parser):
// finds "key": followed by an unsigned integer / a JSON string without escapes.
bool extract_uint_field(const char* line, size_t len, const char* key, uint64_t& out);
bool extract_str_field(const char* line, size_t len, const char* key, char* out, size_t out_len);

}  // namespace oyster
