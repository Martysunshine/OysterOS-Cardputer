// EvidenceStore: append-only JSONL events + manifests + streamed artifacts on SD, with recovery.
// Path layout: /oyster/sessions/<session-id>/events.jsonl, manifest.json, wifi.csv,
// artifacts/<object-id>.bin (+ .partial). Writes happen on the evidence task, never in callbacks.
// Without an SD card a bounded volatile ring is kept and the reduced persistence is surfaced.
// Implementation: evidence_store.cpp. Hashing/recovery logic lives in lib/oyster_core (host-tested).
#pragma once

#include <cstddef>
#include <cstdint>

#include "oyster_core/delivery_policy.h"
#include "oyster_core/types.h"
#include "wifi_survey.h"

namespace oyster {

// Builds the JSON of one event into caller-owned scratch (<= cfg::kMaxEventBytes).
class JsonWriter;
struct EventHeader {
  Operation op;
  Transport transport;
  EventStatus status;
  SourceMode source_mode;
  const char* target_id;   // nullptr -> null
};

class EvidenceStore {
 public:
  bool begin(SourceMode mode);
  bool open_session(const char* session_id, const char* device_id);
  void close_session();
  const char* session_id() const;
  const char* boot_id() const;
  uint32_t next_sequence() const;

  // Begin an event: writes the common envelope header and opens "details". The caller adds
  // operation-specific members through the returned writer, then calls commit_event().
  JsonWriter& begin_event(const EventHeader& h, char* scratch, size_t scratch_len);
  bool commit_event(char out_event_id[cfg::kIdBufLen]);  // closes details, appends the line, flushes at checkpoints

  // Artifact streaming: SHA-256 is computed incrementally; the object is finalised (renamed off
  // .partial) only after declared length and hash verification succeed.
  bool artifact_begin(const char* object_id, uint32_t declared_len);
  bool artifact_write(const uint8_t* data, size_t n);
  bool artifact_finalize(const uint8_t expected_sha256[32], char out_artifact_id[cfg::kIdBufLen]);
  void artifact_abort();  // keeps the labelled .partial for later inspection

  // Startup recovery of the newest PREVIOUS session on SD (named by /oyster/last_session.txt): scans its
  // events.jsonl and manifest.json, appends the missing newline to a truncated or unterminated final record
  // (a complete final record is kept and counted), and remembers what it found. The "recovery" event
  // describing it (previous session id, valid/invalid line counts, unacked range) is written into the NEW
  // session when it opens. Runs once per boot: begin() calls it when the card is mounted, open_session()
  // when it was not. Previous sessions are NOT uploaded by this firmware (milestone M6): their events stay
  // on the card. Returns the highest sequence found in the previous session (0 when none).
  uint32_t recover();

  bool sd_backed() const;          // false -> volatile bounded session
  uint32_t dropped_events() const; // volatile ring or SD-error drops
  // Events the backend refused permanently (skipped so upload could continue) in the OPEN session. They are
  // NOT acknowledged data: they stay on the card, are never counted by acked_sequence(), and are persisted in
  // manifest.json ("skipped_events"). The UI shows them separately.
  uint32_t skipped_events() const;

  // Sessions are created on the device (offline-capable): "sess-<boot-hex8>-<nnn>". The backend
  // registers the id on first sync and binds it to the authenticated device.
  bool new_session_id(char out[cfg::kIdBufLen]);

  // Upload support. Every call names the session whose URL the batch is sent to; a call for any other
  // session than the open one does nothing, so events of session B can never be sent to A's path and an
  // acknowledgement can never be booked on the wrong session.
  //
  // read_batch copies the CONTIGUOUS run of complete valid lines that follow the session's upload cursor,
  // joined with ',' (the caller wraps them in a JSON array), up to max bytes, and stops before a gap in the
  // sequence numbers. Returns bytes written (0 / count 0 when nothing is pending or the session is not open).
  size_t read_batch(const char* session_id, char* buf, size_t max, uint32_t& first_seq, uint32_t& last_seq,
                    uint16_t& count);
  // The backend accepted the batch first_seq..last_seq (as returned by read_batch). Sequences between the
  // cursor and first_seq were never sent (overwritten in the RAM ring) and are recorded as lost.
  void ack_through(const char* session_id, uint32_t first_seq, uint32_t last_seq);  // persisted in manifest.json
  // The backend refused the single event `seq` permanently: upload moves on, but the event is counted in
  // skipped_events(), never in acked_sequence(), and can never be cited by a job result.
  void skip_event(const char* session_id, uint32_t seq);
  uint32_t acked_sequence() const;     // highest sequence the backend ACCEPTED (skipped events excluded)
  uint32_t resolved_sequence() const;  // upload cursor: every sequence up to here is accepted, skipped or lost
  // What happened to one event of `session_id` (delivery_policy.h); ForeignSession when it is not the open session.
  EventFate event_fate(const char* session_id, uint32_t seq) const;

  // wifi.csv export for a completed survey (header row once per file).
  bool append_wifi_csv(const WifiSurveyResult& r, const char* room_label);
};

extern EvidenceStore g_evidence;

}  // namespace oyster
