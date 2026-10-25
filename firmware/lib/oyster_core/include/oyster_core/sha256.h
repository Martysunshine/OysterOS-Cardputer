// Portable streaming SHA-256 and HMAC-SHA256 (FIPS 180-4 / RFC 2104). Used to hash artifacts while
// they stream to SD and for the lab application-authorisation exchange. Verified against NIST and
// RFC 4231 vectors in test/test_core. A hash proves byte integrity only, not confidentiality or time.
#pragma once

#include <cstddef>
#include <cstdint>

namespace oyster {

class Sha256 {
 public:
  Sha256() { reset(); }
  void reset();
  void update(const uint8_t* data, size_t len);
  void finish(uint8_t out[32]);  // call reset() before reusing
  uint64_t bytes_hashed() const { return total_bytes_; }

  static void to_hex(const uint8_t digest[32], char out[65]);

 private:
  void compress(const uint8_t block[64]);
  uint32_t h_[8];
  uint8_t block_[64];
  size_t used_ = 0;
  uint64_t total_bytes_ = 0;
};

void hmac_sha256(const uint8_t* key, size_t key_len, const uint8_t* msg, size_t msg_len, uint8_t out[32]);

// Constant-time comparison for MACs and tokens.
bool equal_ct(const uint8_t* a, const uint8_t* b, size_t n);

}  // namespace oyster
