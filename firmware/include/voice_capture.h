// Voice question (push-to-talk): the owner speaks on the Cardputer ADV microphone, the audio streams to the
// backend in small chunks while it is still being recorded, the backend transcribes it and the text lands
// in the question entry for review (docs/VOICE_INPUT.md). Nothing is stored on the device.
//
// Threading: start(), request_stop(), request_cancel(), status(), take_result() run on the UI task; run()
// is the whole capture + upload and runs on the worker task (RequestKind::VoiceQuestion); the microphone
// task created by M5Unified calls the block callback inside run()'s lifetime only. State is exchanged
// through atomics, so no handler ever blocks. Implementation: src/voice_capture.cpp.
#pragma once

#include <cstddef>
#include <cstdint>

namespace oyster::voice {

enum class State : uint8_t {
  Idle,
  Starting,   // queued for the worker, the upload is being opened, the microphone starts
  Recording,  // audio is captured and uploaded
  Finishing,  // the last chunks and the transcription request
  Ready,      // transcript available: take_result()
  Failed,     // error text available: take_result()
};

struct Status {
  State state;
  uint32_t elapsed_ms;    // audio recorded so far
  uint32_t max_ms;        // the recording limit
  uint8_t level_pct;      // coarse peak of the last block, 0..100
  uint32_t sent_bytes;    // uploaded so far
  uint16_t dropped;       // audio blocks lost because the upload fell behind (0 normally)
};

constexpr uint32_t kMaxSeconds = 12;

// UI task. Queues the capture on the worker. False when a capture is already active or the worker queue is
// full (nothing was started).
bool start();
void request_stop();    // Enter while recording: stop now and transcribe what was recorded
void request_cancel();  // Back or the emergency cancel: stop, delete the upload, no transcript
void status(Status& out);
bool active();  // Starting, Recording or Finishing
// Ready or Failed -> Idle. Copies the transcript (ok = true) or the error text (ok = false) into `text`.
// False when there is no result to take.
bool take_result(char* text, size_t cap, bool* ok);
// The same text without consuming the result (for drawing the page); "" when there is none.
void peek_result(char* text, size_t cap);

// Worker task only (assessment_runner.cpp).
void run(const char* session_id);

}  // namespace oyster::voice
