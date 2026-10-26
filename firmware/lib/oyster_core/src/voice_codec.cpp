// Voice audio helpers (voice_codec.h). No Arduino headers: compiled for the device and for the host tests.
#include "oyster_core/voice_codec.h"

#include <cstring>

namespace oyster::voice {

namespace {

constexpr int kBias = 0x84;
constexpr int kClip = 32635;

}  // namespace

uint8_t ulaw_encode(int16_t pcm) {
  int s = pcm;
  uint8_t sign = 0;
  if (s < 0) {
    s = -s;  // -32768 becomes 32768 in int and is clipped below
    sign = 0x80;
  }
  if (s > kClip) s = kClip;
  s += kBias;
  int exponent = 7;
  for (int mask = 0x4000; (s & mask) == 0 && exponent > 0; mask >>= 1) --exponent;
  const int mantissa = (s >> (exponent + 3)) & 0x0F;
  return static_cast<uint8_t>(~(sign | (exponent << 4) | mantissa));
}

int16_t ulaw_decode(uint8_t u) {
  const uint8_t v = static_cast<uint8_t>(~u);
  const int exponent = (v >> 4) & 0x07;
  const int mantissa = v & 0x0F;
  const int s = (((mantissa << 3) + kBias) << exponent) - kBias;
  return static_cast<int16_t>((v & 0x80) ? -s : s);
}

size_t decimate2_ulaw(const int16_t* in, size_t n_in, uint8_t* out, uint16_t* peak) {
  uint16_t pk = 0;
  const size_t n_out = n_in / 2;
  for (size_t i = 0; i < n_out; ++i) {
    const int a = in[2 * i];
    const int b = in[2 * i + 1];
    const int aa = a < 0 ? -a : a;
    const int bb = b < 0 ? -b : b;
    if (aa > pk) pk = static_cast<uint16_t>(aa > 32767 ? 32767 : aa);
    if (bb > pk) pk = static_cast<uint16_t>(bb > 32767 ? 32767 : bb);
    out[i] = ulaw_encode(static_cast<int16_t>((a + b) / 2));
  }
  if (peak != nullptr) *peak = pk;
  return n_out;
}

uint8_t level_percent(uint16_t peak) {
  if (peak == 0) return 0;
  int bits = 0;
  for (unsigned v = peak; v != 0; v >>= 1) ++bits;  // 1..15 for 1..32767
  if (bits > 15) bits = 15;
  return static_cast<uint8_t>((bits * 100) / 15);
}

void ByteRing::reset(uint8_t* storage, size_t capacity) {
  buf_ = storage;
  cap_ = (storage != nullptr) ? capacity : 0;
  head_.store(0, std::memory_order_relaxed);
  tail_.store(0, std::memory_order_relaxed);
}

size_t ByteRing::available() const {
  if (cap_ == 0) return 0;
  const size_t h = head_.load(std::memory_order_acquire);
  const size_t t = tail_.load(std::memory_order_acquire);
  return h >= t ? h - t : cap_ - t + h;
}

size_t ByteRing::free_space() const {
  if (cap_ == 0) return 0;
  return cap_ - 1 - available();
}

bool ByteRing::push(const uint8_t* src, size_t n) {
  if (cap_ == 0 || n == 0) return n == 0;
  if (n > free_space()) return false;
  size_t h = head_.load(std::memory_order_relaxed);
  const size_t first = (h + n <= cap_) ? n : cap_ - h;
  std::memcpy(buf_ + h, src, first);
  if (first < n) std::memcpy(buf_, src + first, n - first);
  h = (h + n) % cap_;
  head_.store(h, std::memory_order_release);
  return true;
}

size_t ByteRing::pop(uint8_t* dst, size_t n) {
  if (cap_ == 0 || n == 0) return 0;
  const size_t avail = available();
  if (n > avail) n = avail;
  if (n == 0) return 0;
  size_t t = tail_.load(std::memory_order_relaxed);
  const size_t first = (t + n <= cap_) ? n : cap_ - t;
  std::memcpy(dst, buf_ + t, first);
  if (first < n) std::memcpy(dst + first, buf_, n - first);
  t = (t + n) % cap_;
  tail_.store(t, std::memory_order_release);
  return n;
}

}  // namespace oyster::voice
