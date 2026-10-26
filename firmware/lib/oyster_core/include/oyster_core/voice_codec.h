// Voice question audio path, platform independent (host-tested in test/test_voice): G.711 mu-law, the
// 16 kHz -> 8 kHz decimation that feeds it, a coarse level for the on-screen meter and the single-producer /
// single-consumer byte ring between the microphone task and the uploading worker. The backend decodes
// mu-law with the same formula (backend/oyster/voice/audio.py). Implementation: src/voice_codec.cpp.
#pragma once

#include <atomic>
#include <cstddef>
#include <cstdint>

namespace oyster::voice {

// 16-bit linear PCM <-> 8-bit mu-law (bias 0x84, clip 32635: the classic G.711 tables).
uint8_t ulaw_encode(int16_t pcm);
int16_t ulaw_decode(uint8_t u);

// Averages sample pairs (a crude 2:1 low-pass, enough for speech) and mu-law encodes the result. `n_in`
// 16-bit samples produce n_in / 2 bytes (an odd trailing sample is dropped). `peak` receives the largest
// |sample| of the input (0..32767). Returns the number of bytes written.
size_t decimate2_ulaw(const int16_t* in, size_t n_in, uint8_t* out, uint16_t* peak);

// 0..100 in 16 coarse steps of about 6 dB (the bit width of the peak), for a text meter.
uint8_t level_percent(uint16_t peak);

// Lock-free ring over caller-provided storage: one producer (push) and one consumer (pop) on different
// tasks. One slot stays empty, so `capacity - 1` bytes fit.
class ByteRing {
 public:
  void reset(uint8_t* storage, size_t capacity);
  size_t capacity() const { return cap_; }
  size_t available() const;  // bytes stored
  size_t free_space() const;
  // Producer: copies all `n` bytes or nothing (false when they do not fit).
  bool push(const uint8_t* src, size_t n);
  // Consumer: copies up to `n` bytes; returns how many.
  size_t pop(uint8_t* dst, size_t n);

 private:
  uint8_t* buf_ = nullptr;
  size_t cap_ = 0;
  std::atomic<size_t> head_{0};  // next write index (producer)
  std::atomic<size_t> tail_{0};  // next read index (consumer)
};

}  // namespace oyster::voice
