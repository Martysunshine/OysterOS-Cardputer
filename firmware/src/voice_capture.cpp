// Voice question capture and upload (voice_capture.h).
// Audio path: ES8311 codec -> I2S -> M5Unified capture task (16 kHz, 16-bit mono, 64 ms blocks) -> block
// callback: 2:1 decimation + mu-law into a byte ring -> worker: 4 KiB chunks (512 ms of audio each) ->
// POST .../voice/{upload_id}/chunks?seq=N over the existing TLS connection -> POST .../finish -> transcript.
// The ring holds about one second, so an upload slower than that drops whole blocks (counted and shown;
// never silently). All buffers (17 KiB) are one heap block that exists only while a recording runs.
// The microphone pins and the codec power sequence come from M5Unified's board table and are installed by
// M5.begin() only when config.internal_mic is true (board_support.cpp); otherwise Mic.begin() has no data
// pin and this module reports "Microphone not enabled in this build" instead of pretending.
#include "voice_capture.h"

#include <Arduino.h>
#include <ArduinoJson.h>
#include <M5Unified.h>
#include <freertos/FreeRTOS.h>
#include <freertos/task.h>

#include <atomic>
#include <cstdarg>
#include <cstdio>
#include <cstdlib>
#include <cstring>

#include "app_events.h"
#include "assessment_runner.h"
#include "backend_client.h"
#include "modules.h"
#include "oyster_core/config.h"
#include "oyster_core/ids.h"
#include "oyster_core/voice_codec.h"
#include "radio_scheduler.h"

namespace oyster::voice {

namespace {

constexpr uint32_t kCaptureRate = 16000;             // the library default for this codec; decimated 2:1
constexpr size_t kBlockSamples = 1024;               // 64 ms per captured block
constexpr size_t kUlawBlockBytes = kBlockSamples / 2;
constexpr size_t kRingBytes = 8192;                  // about one second of 8 kHz mu-law
constexpr size_t kChunkBytes = 4096;                 // the backend's chunk limit (device bodies stay < 4 KiB)
constexpr size_t kRespBytes = 768;                   // start/finish answers are small JSON objects
constexpr uint32_t kBytesPerSecond = 8000;           // 8 kHz, one byte per sample
constexpr uint32_t kMaxBytes = kMaxSeconds * kBytesPerSecond;
constexpr uint32_t kMinBytes = kBytesPerSecond / 2;  // under half a second is not worth a transcription
constexpr uint32_t kStatusEventMs = 500;
constexpr uint32_t kPollMs = 20;
constexpr uint32_t kRetryDelayMs = 300;

struct Buffers {
  int16_t pcm[2][kBlockSamples];
  uint8_t ulaw_block[kUlawBlockBytes];
  uint8_t ring[kRingBytes];
  uint8_t chunk[kChunkBytes];
  char resp[kRespBytes];
};

std::atomic<uint8_t> s_state{static_cast<uint8_t>(State::Idle)};
std::atomic<bool> s_stop{false};
std::atomic<bool> s_cancel{false};
std::atomic<bool> s_capture_stop{false};  // the block callback stops queueing buffers
std::atomic<uint32_t> s_produced{0};      // mu-law bytes the microphone path put into the ring
std::atomic<uint32_t> s_sent{0};
std::atomic<uint8_t> s_level{0};
std::atomic<uint16_t> s_dropped{0};
char s_text[cfg::kMaxQuestionLen + 1] = "";  // transcript or error text; written by the worker before Ready/Failed
State s_outcome = State::Failed;             // worker only
size_t s_chunk_limit = kChunkBytes;          // worker only
Buffers* s_buf = nullptr;
ByteRing s_ring;

State state() { return static_cast<State>(s_state.load(std::memory_order_acquire)); }
void set_state(State s) { s_state.store(static_cast<uint8_t>(s), std::memory_order_release); }

// The UI redraws the Oyster AI screen on this event (it carries the real backend state).
void notify_ui() { app_post(AppEventType::BackendStatusChanged, static_cast<uint32_t>(g_backend.state())); }

// Holds RadioPhase::UplinkSync for one request so a survey cannot start in the middle of it.
class Lease {
 public:
  Lease() : held_(g_radio.acquire(RadioPhase::UplinkSync, 0)) {}
  ~Lease() {
    if (held_) g_radio.release(RadioPhase::UplinkSync);
  }
  bool held() const { return held_; }
  Lease(const Lease&) = delete;
  Lease& operator=(const Lease&) = delete;

 private:
  bool held_;
};

void fail(const char* fmt, ...) __attribute__((format(printf, 1, 2)));
void fail(const char* fmt, ...) {
  va_list ap;
  va_start(ap, fmt);
  std::vsnprintf(s_text, sizeof(s_text), fmt, ap);
  va_end(ap);
  s_outcome = State::Failed;
}

// ---- microphone task side -------------------------------------------------------------------------------
// Runs on M5Unified's capture task (about 2.5 KiB of stack): encode the block, queue it, hand the buffer back.
// Nothing here blocks or allocates.
void on_block(void*, void* data, size_t len) {
  Buffers* b = s_buf;
  if (b == nullptr) return;
  uint16_t peak = 0;
  const size_t n = decimate2_ulaw(static_cast<const int16_t*>(data), len, b->ulaw_block, &peak);
  s_level.store(level_percent(peak), std::memory_order_relaxed);
  if (s_capture_stop.load(std::memory_order_acquire)) return;
  if (s_ring.push(b->ulaw_block, n)) {
    s_produced.fetch_add(static_cast<uint32_t>(n), std::memory_order_acq_rel);
  } else {
    s_dropped.fetch_add(1, std::memory_order_relaxed);  // the upload fell behind by more than the ring
  }
  if (s_produced.load(std::memory_order_acquire) < kMaxBytes) {
    M5.Mic.record(static_cast<int16_t*>(data), len);  // false only while the port is being stopped
  }
}

// ---- worker side --------------------------------------------------------------------------------------------

int http(const char* method, const char* path, const void* body, size_t len, bool idempotent) {
  size_t rl = 0;
  return modules::http(method, path, static_cast<const char*>(body), len, s_buf->resp, kRespBytes, &rl, idempotent);
}

// "error" code of the last JSON error body, "" when none.
void error_code(char* out, size_t n) {
  out[0] = '\0';
  JsonDocument doc;
  if (deserializeJson(doc, s_buf->resp) != DeserializationError::Ok) return;
  const char* code = doc["error"].as<const char*>();
  if (code != nullptr) copy_bounded(out, n, code);
}

void describe_failure(const char* what, int st) {
  if (st == modules::kHttpRadioBusy) {
    fail("%.40s: radio busy, try again", what);
    return;
  }
  if (st < 0) {
    fail("%.40s: backend offline (%.90s)", what, modules::last_error());
    return;
  }
  char code[40];
  error_code(code, sizeof(code));
  const char* text = nullptr;
  if (std::strcmp(code, "transcription_unavailable") == 0) text = "voice input is not set up on the backend";
  else if (std::strcmp(code, "too_short") == 0) text = "too short, speak a little longer";
  else if (std::strcmp(code, "no_speech") == 0) text = "no speech recognised";
  else if (std::strcmp(code, "upload_incomplete") == 0) text = "upload incomplete, try again";
  else if (std::strcmp(code, "provider_error") == 0) text = "speech service error, try again";
  else if (std::strcmp(code, "rate_limited") == 0) text = "rate limited, wait a moment";
  else if (std::strcmp(code, "not_found") == 0) text = "upload expired or unknown, try again";
  if (text != nullptr) {
    fail("%.40s: %s", what, text);
  } else {
    fail("%.40s: backend refused (HTTP %d%s%.30s)", what, st, code[0] ? " " : "", code);
  }
}

bool open_upload(const char* sid, char* upload_id) {
  {
    Lease l;
    if (!l.held()) {
      fail("Radio busy (survey or BLE running); try again");
      return false;
    }
    if (!g_backend.register_session(sid)) {
      fail("Backend offline: %.100s", modules::last_error());
      return false;
    }
  }
  char path[160];
  std::snprintf(path, sizeof(path), "/v1/sessions/%.47s/voice", sid);
  char body[64];
  const int n = std::snprintf(body, sizeof(body), "{\"format\":\"ulaw_8k\",\"max_seconds\":%u}",
                              static_cast<unsigned>(kMaxSeconds));
  const int st = http("POST", path, body, static_cast<size_t>(n), false);
  if (st != 200 && st != 201) {
    describe_failure("Voice upload refused", st);
    return false;
  }
  JsonDocument doc;
  if (deserializeJson(doc, s_buf->resp) != DeserializationError::Ok) {
    fail("Backend answer could not be parsed");
    return false;
  }
  const char* id = doc["upload_id"].as<const char*>();
  if (id == nullptr || !is_prefixed_id(id, "vup-")) {
    fail("Backend answer had no upload id");
    return false;
  }
  copy_bounded(upload_id, cfg::kIdBufLen, id);
  const uint32_t cmax = doc["chunk_bytes_max"] | 0u;
  s_chunk_limit = (cmax >= 512 && cmax < kChunkBytes) ? cmax : kChunkBytes;
  return true;
}

bool start_mic() {
  auto mc = M5.Mic.config();
  if (mc.pin_data_in < 0) {
    fail("Microphone not enabled in this build (internal_mic is off)");
    return false;
  }
  mc.sample_rate = kCaptureRate;
  M5.Mic.config(mc);
  M5.Mic.setBufferReleaseCallback(nullptr, on_block);
  if (!M5.Mic.begin()) {
    M5.Mic.setBufferReleaseCallback(nullptr, nullptr);
    fail("Microphone did not start (codec or I2S)");
    return false;
  }
  if (!M5.Mic.record(s_buf->pcm[0], kBlockSamples) || !M5.Mic.record(s_buf->pcm[1], kBlockSamples)) {
    M5.Mic.end();
    M5.Mic.setBufferReleaseCallback(nullptr, nullptr);
    fail("Microphone did not accept a buffer");
    return false;
  }
  return true;
}

// end() waits for the capture task to finish and deletes it: no callback runs after this returns.
void stop_mic() {
  s_capture_stop.store(true, std::memory_order_release);
  M5.Mic.end();
  M5.Mic.setBufferReleaseCallback(nullptr, nullptr);
}

// Uploads chunks while the microphone fills the ring; returns true when everything recorded was uploaded.
// On return the microphone is stopped. False: cancelled (s_cancel) or failed (s_text set).
bool stream(const char* sid, const char* upload_id) {
  char path[160];
  uint32_t seq = 0;
  uint32_t last_event = millis();
  bool ended = false;
  for (;;) {
    if (g_radio.cancel_requested()) s_cancel.store(true, std::memory_order_release);  // emergency chord
    if (s_cancel.load(std::memory_order_acquire)) {
      if (!ended) stop_mic();
      return false;
    }
    const bool stop = s_stop.load(std::memory_order_acquire) || s_produced.load(std::memory_order_acquire) >= kMaxBytes;
    if (stop && !ended) {
      stop_mic();
      ended = true;
      set_state(State::Finishing);
      notify_ui();
    }
    const size_t avail = s_ring.available();
    if (avail >= s_chunk_limit || (ended && avail > 0)) {
      const size_t n = s_ring.pop(s_buf->chunk, s_chunk_limit);
      std::snprintf(path, sizeof(path), "/v1/sessions/%.47s/voice/%.47s/chunks?seq=%lu", sid, upload_id,
                    static_cast<unsigned long>(seq));
      int st = -1;
      for (int attempt = 0; attempt < 2; ++attempt) {
        st = http("POST", path, s_buf->chunk, n, true);  // the backend ignores a repeated seq
        if (st > 0) break;  // answered: a retry would not change it
        vTaskDelay(pdMS_TO_TICKS(kRetryDelayMs));
      }
      if (st != 200) {
        if (!ended) stop_mic();
        describe_failure("Upload failed", st);
        return false;
      }
      ++seq;
      s_sent.fetch_add(static_cast<uint32_t>(n), std::memory_order_acq_rel);
      continue;  // drain further without waiting
    }
    if (ended) return true;
    const uint32_t now = millis();
    if (now - last_event >= kStatusEventMs) {
      last_event = now;
      notify_ui();
    }
    vTaskDelay(pdMS_TO_TICKS(kPollMs));
  }
}

bool finish(const char* sid, const char* upload_id) {
  if (s_sent.load(std::memory_order_acquire) < kMinBytes) {
    fail("Too short: speak for at least half a second");
    return false;
  }
  char path[160];
  std::snprintf(path, sizeof(path), "/v1/sessions/%.47s/voice/%.47s/finish", sid, upload_id);
  int st = -1;
  for (int attempt = 0; attempt < 2; ++attempt) {
    st = http("POST", path, nullptr, 0, true);  // repeatable: the backend keeps the transcript for a minute
    if (st > 0) break;
    vTaskDelay(pdMS_TO_TICKS(kRetryDelayMs));
  }
  if (st != 200) {
    describe_failure("Transcription failed", st);
    return false;
  }
  JsonDocument doc;
  if (deserializeJson(doc, s_buf->resp) != DeserializationError::Ok) {
    fail("Transcript could not be parsed");
    return false;
  }
  const char* t = doc["transcript"].as<const char*>();
  if (t == nullptr || t[0] == '\0') {
    fail("No speech recognised");
    return false;
  }
  // The entry shows printable ASCII only; the backend already folds the text, this is the last guard.
  size_t n = 0;
  for (const char* p = t; *p != '\0' && n + 1 < sizeof(s_text); ++p) {
    const char c = *p;
    if (c >= 0x20 && c <= 0x7E) s_text[n++] = c;
  }
  s_text[n] = '\0';
  if (n == 0) {
    fail("No speech recognised");
    return false;
  }
  s_outcome = State::Ready;
  return true;
}

void delete_upload(const char* sid, const char* upload_id) {
  char path[160];
  std::snprintf(path, sizeof(path), "/v1/sessions/%.47s/voice/%.47s", sid, upload_id);
  (void)http("DELETE", path, nullptr, 0, true);  // best effort: the backend expires it after a minute anyway
}

}  // namespace

// ---- UI task ------------------------------------------------------------------------------------------------

bool start() {
  uint8_t idle = static_cast<uint8_t>(State::Idle);
  if (!s_state.compare_exchange_strong(idle, static_cast<uint8_t>(State::Starting))) return false;
  s_stop.store(false);
  s_cancel.store(false);
  s_produced.store(0);
  s_sent.store(0);
  s_level.store(0);
  s_dropped.store(0);
  WorkRequest r;
  std::memset(&r, 0, sizeof(r));
  r.kind = RequestKind::VoiceQuestion;
  if (!g_runner.enqueue(r)) {
    set_state(State::Idle);
    return false;
  }
  return true;
}

void request_stop() { s_stop.store(true, std::memory_order_release); }

void request_cancel() {
  s_cancel.store(true, std::memory_order_release);
  // Not picked up by the worker yet: nothing to undo (run() re-checks the flag before it starts anything).
  uint8_t starting = static_cast<uint8_t>(State::Starting);
  s_state.compare_exchange_strong(starting, static_cast<uint8_t>(State::Idle));
}

void status(Status& out) {
  out.state = state();
  out.elapsed_ms = s_produced.load(std::memory_order_acquire) / (kBytesPerSecond / 1000);
  out.max_ms = kMaxSeconds * 1000;
  out.level_pct = s_level.load(std::memory_order_relaxed);
  out.sent_bytes = s_sent.load(std::memory_order_acquire);
  out.dropped = s_dropped.load(std::memory_order_relaxed);
}

bool active() {
  const State s = state();
  return s == State::Starting || s == State::Recording || s == State::Finishing;
}

bool take_result(char* text, size_t cap, bool* ok) {
  const State s = state();
  if (s != State::Ready && s != State::Failed) return false;
  copy_bounded(text, cap, s_text);
  if (ok != nullptr) *ok = (s == State::Ready);
  set_state(State::Idle);
  return true;
}

void peek_result(char* text, size_t cap) {
  if (cap == 0) return;
  const State s = state();
  if (s != State::Ready && s != State::Failed) {
    text[0] = '\0';
    return;
  }
  copy_bounded(text, cap, s_text);
}

// ---- worker task --------------------------------------------------------------------------------------------

void run(const char* session_id) {
  if (state() != State::Starting) return;  // cancelled before the worker got to it
  s_capture_stop.store(false);
  s_chunk_limit = kChunkBytes;
  s_outcome = State::Failed;
  g_radio.clear_cancel();  // a stale cancel from an earlier operation must not end this one at once
  if (s_cancel.load(std::memory_order_acquire)) {
    set_state(State::Idle);
    notify_ui();
    return;
  }
  if (session_id == nullptr || !is_valid_session_id(session_id)) {
    fail("Open a session first (Settings > New session)");
    set_state(State::Failed);
    notify_ui();
    return;
  }
  s_buf = static_cast<Buffers*>(std::malloc(sizeof(Buffers)));
  if (s_buf == nullptr) {
    fail("Not enough free memory to record");
    set_state(State::Failed);
    notify_ui();
    return;
  }
  s_ring.reset(s_buf->ring, kRingBytes);
  char upload_id[cfg::kIdBufLen] = "";
  bool ok = open_upload(session_id, upload_id);
  if (ok && s_cancel.load(std::memory_order_acquire)) ok = false;
  if (ok) ok = start_mic();
  if (ok) {
    set_state(State::Recording);
    notify_ui();
    ok = stream(session_id, upload_id);  // stops the microphone before it returns
  }
  const bool cancelled = s_cancel.load(std::memory_order_acquire);
  if (ok && !cancelled) ok = finish(session_id, upload_id);
  if ((!ok || cancelled) && upload_id[0] != '\0') delete_upload(session_id, upload_id);
  s_ring.reset(nullptr, 0);
  std::free(s_buf);
  s_buf = nullptr;
  set_state(cancelled ? State::Idle : s_outcome);
  notify_ui();
}

}  // namespace oyster::voice
