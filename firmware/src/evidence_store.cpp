// EvidenceStore: append-only JSONL evidence on SD with recovery, a bounded RAM ring without SD (or
// after an SD write error), manifests, wifi.csv export and hashed artifact streaming.
// Event layout: begin_event() writes the measured header (source_mode .. status) and opens "details"
// in the caller's scratch; commit_event() prefixes schema_version, event_id, session_id, device_id,
// boot_id and sequence, so envelope fields come first and win over same-named keys in details.
// Events committed before a session opens are held in RAM and written when the session opens.
// One recursive mutex: begin_event() takes it, commit_event() releases it.
// Upload bookkeeping per session lives in s_up (oyster_core/upload_ledger.h, host-tested): the cursor advances
// over accepted, skipped and lost sequences; acked is the highest ACCEPTED sequence; skipped counts events the
// backend refused for good.
// event_fate() tells a job result which of its event ids it may cite.
#include "evidence_store.h"

#include <Arduino.h>
#include <FS.h>
#include <SD.h>
#include <esp_system.h>
#include <freertos/FreeRTOS.h>
#include <freertos/semphr.h>

#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <new>

#include "board_support.h"
#include "oyster_core/delivery_policy.h"
#include "oyster_core/ids.h"
#include "oyster_core/json_writer.h"
#include "oyster_core/jsonl.h"
#include "oyster_core/jsonl_tail.h"
#include "oyster_core/sha256.h"
#include "oyster_core/upload_ledger.h"
#include "oyster_core/transfer.h"
#include "time_sync.h"

#ifndef OYSTER_FIRMWARE_VERSION
#define OYSTER_FIRMWARE_VERSION "0.0.0-dev"
#endif

namespace oyster {

EvidenceStore g_evidence;

namespace {

constexpr const char* kRootDir = "/oyster";
constexpr const char* kSessionsDir = "/oyster/sessions";
constexpr size_t kPathLen = 112;
constexpr size_t kPendingBytes = 2048;        // events committed before a session is open
constexpr uint32_t kFlushEveryEvents = 4;     // checkpoint: flush + fsync
constexpr uint32_t kFlushEveryMs = 1000;
constexpr uint64_t kMinFreeBytes = 256ULL * 1024ULL;
constexpr size_t kInternalScratch = 1024;
constexpr uint16_t kMaxSessionsPerBoot = 999;
constexpr const char* kLastSessionPath = "/oyster/last_session.txt";
constexpr const char* kLastSessionTmp = "/oyster/last_session.tmp";

SemaphoreHandle_t s_mu = nullptr;
SourceMode s_mode = SourceMode::Live;
bool s_begun = false;
char s_boot_id[cfg::kIdBufLen] = "";
char s_boot_hex8[9] = "";
uint16_t s_session_counter = 0;

// Current session
char s_session[cfg::kIdBufLen] = "";
char s_device[cfg::kIdBufLen] = "";
char s_created_boot[cfg::kIdBufLen] = "";
char s_dir[kPathLen] = "";
uint32_t s_last_seq = 0;
UploadLedger s_up;          // upload cursor, highest accepted sequence, skipped events, holes
uint32_t s_sd_written = 0;  // highest sequence written to events.jsonl
bool s_last_line_on_sd = false;
uint32_t s_dropped = 0;
bool s_sd = false;         // the session was opened on SD
bool s_sd_failed = false;  // SD full / write error since: later events go to the RAM ring
fs::File s_log;
uint32_t s_unflushed = 0;
uint32_t s_last_flush_ms = 0;
uint64_t s_free_bytes = UINT64_MAX;  // estimate; UINT64_MAX = unknown

// read_batch() resume hint: every line before s_hint_off has sequence <= s_hint_seq.
uint32_t s_hint_seq = 0;
size_t s_hint_off = 0;

// Previous-session recovery (once per boot); the event is written into the first session opened afterwards.
struct PrevSession {
  bool attempted;
  bool found;            // a previous session directory was scanned
  bool event_pending;    // the recovery event still has to be written
  char session_id[cfg::kIdBufLen];
  bool manifest_found;
  bool closed;
  uint32_t manifest_last;
  uint32_t manifest_acked;
  uint32_t manifest_resolved;
  uint32_t manifest_skipped;
  JsonlScanResult scan;
  bool scanned;
  bool tail_terminated;
  bool tail_was_record;
};
PrevSession s_prev;

// Volatile ring of complete lines ('\n' terminated), allocated on first need.
char* s_ring = nullptr;
size_t s_ring_len = 0;

// Pre-session events: envelope bodies (after '{'), '\n' separated.
char s_pending[kPendingBytes];
size_t s_pending_len = 0;

// Event in progress
alignas(JsonWriter) uint8_t s_writer_mem[sizeof(JsonWriter)];
alignas(JsonWriter) uint8_t s_dummy_writer_mem[sizeof(JsonWriter)];
char s_dummy_buf[8];
JsonWriter* s_writer = nullptr;
int s_depth = 0;
bool s_event_ok = false;
char* s_scratch = nullptr;
Operation s_event_op = Operation::Unknown;

char s_line[cfg::kMaxEventBytes + 8];
uint8_t s_io[cfg::kMaxEventBytes];
char s_internal[kInternalScratch];

// Artifact in progress
bool s_art_active = false;
fs::File s_art_file;
Sha256 s_art_hash;
uint32_t s_art_declared = 0;
uint32_t s_art_received = 0;
char s_art_object[xfer::kObjectIdBytes + 1] = "";

void lock() {
  if (s_mu == nullptr) s_mu = xSemaphoreCreateRecursiveMutex();
  xSemaphoreTakeRecursive(s_mu, portMAX_DELAY);
}
void unlock() { xSemaphoreGiveRecursive(s_mu); }

class Guard {
 public:
  Guard() { lock(); }
  ~Guard() { unlock(); }
  Guard(const Guard&) = delete;
  Guard& operator=(const Guard&) = delete;
};

bool session_open() { return s_session[0] != '\0'; }

bool sd_active() { return s_sd && !s_sd_failed && s_dir[0] != '\0'; }

bool session_path(char* out, size_t n, const char* name) {
  const int w = snprintf(out, n, "%s/%s", s_dir, name);
  return w > 0 && static_cast<size_t>(w) < n;
}

bool ensure_dir(const char* p) {
  if (SD.exists(p)) return true;
  return SD.mkdir(p);
}

bool important_op(Operation op) {
  return op == Operation::Boot || op == Operation::SessionOpen || op == Operation::SessionClose ||
         op == Operation::Recovery || op == Operation::LocalConfirmation;
}

bool valid_line(const char* line, size_t len, uint32_t& seq) {
  if (len < 2 || line[0] != '{' || line[len - 1] != '}') return false;
  uint64_t v = 0;
  if (!extract_uint_field(line, len, "sequence", v) || v > UINT32_MAX) return false;
  seq = static_cast<uint32_t>(v);
  return true;
}

// ---- volatile ring ------------------------------------------------------------------------------

bool ring_ready() {
  if (s_ring != nullptr) return true;
  s_ring = static_cast<char*>(malloc(cfg::kVolatileEventRingBytes));
  s_ring_len = 0;
  if (s_ring == nullptr) log_e("evidence: no RAM for the volatile ring");
  return s_ring != nullptr;
}

size_t ring_first_line_len() {
  const char* nl = static_cast<const char*>(std::memchr(s_ring, '\n', s_ring_len));
  return nl != nullptr ? static_cast<size_t>(nl - s_ring) + 1 : s_ring_len;
}

void ring_remove_first(bool count_unresolved) {
  const size_t n = ring_first_line_len();
  if (count_unresolved) {
    uint32_t seq = 0;
    if (!valid_line(s_ring, n > 0 ? n - 1 : 0, seq) || seq > s_up.resolved()) ++s_dropped;  // overwritten before upload
  }
  std::memmove(s_ring, s_ring + n, s_ring_len - n);
  s_ring_len -= n;
}

void ring_drop_resolved() {
  while (s_ring != nullptr && s_ring_len > 0) {
    const size_t n = ring_first_line_len();
    uint32_t seq = 0;
    if (!valid_line(s_ring, n > 0 ? n - 1 : 0, seq) || seq > s_up.resolved()) break;
    ring_remove_first(false);
  }
}

bool ring_has(uint32_t seq) {
  size_t pos = 0;
  while (s_ring != nullptr && pos < s_ring_len) {
    const char* nl = static_cast<const char*>(std::memchr(s_ring + pos, '\n', s_ring_len - pos));
    if (nl == nullptr) break;
    const size_t len = static_cast<size_t>(nl - (s_ring + pos));
    uint32_t v = 0;
    if (valid_line(s_ring + pos, len, v) && v == seq) return true;
    pos += len + 1;
  }
  return false;
}

void ring_clear_counting_unresolved() {
  while (s_ring != nullptr && s_ring_len > 0) ring_remove_first(true);
}

bool ring_append(const char* line, size_t n) {
  if (!ring_ready() || n > cfg::kVolatileEventRingBytes) {
    ++s_dropped;
    return false;
  }
  while (s_ring_len + n > cfg::kVolatileEventRingBytes) ring_remove_first(true);  // oldest first
  std::memcpy(s_ring + s_ring_len, line, n);
  s_ring_len += n;
  return true;
}

// ---- line output --------------------------------------------------------------------------------

void flush_log() {
  if (!s_log) return;
  s_log.flush();
  s_unflushed = 0;
  s_last_flush_ms = millis();
}

bool write_line(const char* line, size_t n, bool important) {
  s_last_line_on_sd = false;
  if (sd_active() && s_log) {
    if (s_free_bytes != UINT64_MAX && s_free_bytes < kMinFreeBytes + n) {
      s_sd_failed = true;
      g_board.report_sd_full();
      log_e("evidence: SD card full; continuing in the RAM ring");
    } else {
      const size_t w = s_log.write(reinterpret_cast<const uint8_t*>(line), n);
      if (w == n) {
        if (s_free_bytes != UINT64_MAX) s_free_bytes -= n;
        ++s_unflushed;
        s_last_line_on_sd = true;
        if (important || s_unflushed >= kFlushEveryEvents || millis() - s_last_flush_ms >= kFlushEveryMs) flush_log();
        return true;
      }
      if (w > 0) s_log.write(static_cast<uint8_t>('\n'));  // terminate the partial line: readers skip it
      s_sd_failed = true;
      g_board.report_sd_write_error();
      log_e("evidence: SD write failed; continuing in the RAM ring");
    }
  }
  return ring_append(line, n);
}

// body: envelope members after the opening '{', ending with the closing '}'.
bool append_body(const char* body, size_t body_len, bool important, char* out_id) {
  const uint32_t seq = s_last_seq + 1;
  char eid[cfg::kIdBufLen];
  if (!format_event_id(seq, eid, sizeof(eid))) {
    ++s_dropped;
    return false;
  }
  const int pre = snprintf(s_line, sizeof(s_line),
                           "{\"schema_version\":\"1.0\",\"event_id\":\"%s\",\"session_id\":\"%s\",\"device_id\":\"%s\","
                           "\"boot_id\":\"%s\",\"sequence\":%lu,",
                           eid, s_session, s_device, s_boot_id, static_cast<unsigned long>(seq));
  if (pre <= 0 || static_cast<size_t>(pre) + body_len + 1 > cfg::kMaxEventBytes) {
    ++s_dropped;
    log_w("evidence: event larger than %u bytes dropped", static_cast<unsigned>(cfg::kMaxEventBytes));
    return false;
  }
  std::memcpy(s_line + pre, body, body_len);
  s_line[pre + body_len] = '\n';
  s_last_seq = seq;
  if (!write_line(s_line, static_cast<size_t>(pre) + body_len + 1, important)) return false;
  if (s_last_line_on_sd) s_sd_written = seq;
  if (out_id != nullptr) copy_bounded(out_id, cfg::kIdBufLen, eid);
  return true;
}

bool stash_pending(const char* body, size_t n) {
  if (s_pending_len + n + 1 > kPendingBytes) {
    ++s_dropped;
    log_w("evidence: pre-session buffer full; event dropped");
    return false;
  }
  std::memcpy(s_pending + s_pending_len, body, n);
  s_pending_len += n;
  s_pending[s_pending_len++] = '\n';
  return true;
}

void flush_pending() {
  size_t pos = 0;
  while (pos < s_pending_len) {
    const char* nl = static_cast<const char*>(std::memchr(s_pending + pos, '\n', s_pending_len - pos));
    if (nl == nullptr) break;
    const size_t n = static_cast<size_t>(nl - (s_pending + pos));
    append_body(s_pending + pos, n, true, nullptr);
    pos += n + 1;
  }
  s_pending_len = 0;
}

// ---- manifest -----------------------------------------------------------------------------------

bool write_manifest(bool closed) {
  if (!sd_active() || !session_open()) return false;
  char buf[1024];
  JsonWriter w(buf, sizeof(buf));
  w.begin_object();
  w.str("schema_version", "1.0");
  w.str("session_id", s_session);
  w.str("device_id", s_device);
  w.str("created_boot_id", s_created_boot);
  w.str("last_boot_id", s_boot_id);
  w.str("source_mode", to_string(s_mode));
  w.str("persistence", "sd");
  w.unum("last_sequence", s_last_seq);
  w.unum("acked_through", s_up.acked());         // highest sequence the backend ACCEPTED
  w.unum("resolved_through", s_up.resolved());  // upload cursor: accepted, skipped or lost up to here
  w.unum("skipped_events", s_up.skipped());      // refused by the backend for good; never counted as acked
  w.begin_array("skipped_sequences");         // the most recent refused sequences
  for (size_t i = 0; i < s_up.hole_count(); ++i) {
    const UploadLedger::Hole& hl = s_up.hole(i);
    if (hl.refused) {
      for (uint32_t q = hl.first; q <= hl.last && q - hl.first < 8; ++q) w.num_value(q);
    }
  }
  w.end_array();
  w.unum("dropped_events", s_dropped);
  w.boolean("closed", closed);
  w.str("events_file", "events.jsonl");
  w.str("wifi_csv", "wifi.csv");
  w.str("artifacts_dir", "artifacts");
  char ts[32];
  const ClockQuality q = timesync::format_utc(ts, sizeof(ts));
  if (q != ClockQuality::Unknown) {
    w.str("updated_utc", ts);
  } else {
    w.null("updated_utc");
  }
  w.str("clock_quality", to_string(q));
  w.unum("updated_uptime_ms", timesync::uptime_ms());
  w.end_object();
  if (!w.ok()) return false;
  char tmp[kPathLen];
  char fin[kPathLen];
  if (!session_path(tmp, sizeof(tmp), "manifest.tmp") || !session_path(fin, sizeof(fin), "manifest.json")) return false;
  File f = SD.open(tmp, FILE_WRITE);
  if (!f) {
    g_board.report_sd_write_error();
    return false;
  }
  const size_t n = w.size();
  const size_t wr = f.write(reinterpret_cast<const uint8_t*>(buf), n);
  f.flush();
  f.close();
  if (wr != n) {
    SD.remove(tmp);
    g_board.report_sd_write_error();
    return false;
  }
  // FAT rename is not power-loss atomic: load_manifest_at() falls back to manifest.tmp.
  if (SD.exists(fin)) SD.remove(fin);
  if (!SD.rename(tmp, fin)) {
    g_board.report_sd_write_error();
    return false;
  }
  return true;
}

struct ManifestInfo {
  bool found = false;
  bool closed = false;
  uint32_t last = 0;
  uint32_t acked = 0;
  uint32_t resolved = 0;
  uint32_t skipped = 0;
  char created_boot[cfg::kIdBufLen] = "";
};

// Reads <dir>/manifest.json (or the manifest.tmp left by an interrupted rename). Uses s_io.
void load_manifest_at(const char* dir, ManifestInfo& m) {
  m = ManifestInfo();
  for (const char* name : {"manifest.json", "manifest.tmp"}) {
    char p[kPathLen];
    const int w = snprintf(p, sizeof(p), "%s/%s", dir, name);
    if (w <= 0 || static_cast<size_t>(w) >= sizeof(p)) continue;
    File f = SD.open(p, FILE_READ);
    if (!f) continue;
    const size_t n = f.read(s_io, sizeof(s_io) - 1);
    f.close();
    if (n == 0) continue;
    s_io[n] = 0;
    const char* t = reinterpret_cast<const char*>(s_io);
    uint64_t v = 0;
    if (extract_uint_field(t, n, "acked_through", v) && v <= UINT32_MAX) m.acked = static_cast<uint32_t>(v);
    if (extract_uint_field(t, n, "last_sequence", v) && v <= UINT32_MAX) m.last = static_cast<uint32_t>(v);
    if (extract_uint_field(t, n, "resolved_through", v) && v <= UINT32_MAX) m.resolved = static_cast<uint32_t>(v);
    if (extract_uint_field(t, n, "skipped_events", v) && v <= UINT32_MAX) m.skipped = static_cast<uint32_t>(v);
    if (m.resolved < m.acked) m.resolved = m.acked;  // manifests written before resolved_through existed
    m.closed = std::strstr(t, "\"closed\":true") != nullptr;
    char boot[cfg::kIdBufLen];
    if (extract_str_field(t, n, "created_boot_id", boot, sizeof(boot)) && is_prefixed_id(boot, "boot-")) {
      copy_bounded(m.created_boot, sizeof(m.created_boot), boot);
    }
    m.found = true;
    return;
  }
}

// ---- recovery -----------------------------------------------------------------------------------

class FileByteSource : public ByteSource {
 public:
  explicit FileByteSource(fs::File& f) : f_(f), size_(f.size()) {}
  size_t size() const override { return size_; }
  size_t read_at(size_t offset, uint8_t* buf, size_t n) override {
    if (buf == nullptr || offset >= size_) return 0;
    if (pos_ != offset) {
      if (!f_.seek(offset)) return 0;
      pos_ = offset;
    }
    const size_t r = f_.read(buf, n);
    pos_ += r;
    return r;
  }

 private:
  fs::File& f_;
  size_t size_;
  size_t pos_ = SIZE_MAX;
};

// Scans the events file at `path`; a truncated or unterminated final record gets a '\n', so a truncated one
// becomes a complete invalid line (skipped by readers) and a complete one stays valid, and the valid
// records before it are kept. tail_was_record: the final bytes were a complete JSON record that only lacked
// its newline. Uses s_io.
bool scan_and_repair_file(const char* path, JsonlScanResult& r, bool& tail_terminated, bool& tail_was_record) {
  tail_terminated = false;
  tail_was_record = false;
  File f = SD.open(path, FILE_READ);
  if (!f) return false;
  FileByteSource src(f);
  r = scan_jsonl(src, s_io, sizeof(s_io));
  if (r.truncated_tail > 0 && r.truncated_tail < sizeof(s_io) && f.seek(r.last_good_end)) {
    const size_t got = f.read(s_io, r.truncated_tail);
    uint32_t seq = 0;
    tail_was_record = got == r.truncated_tail && valid_line(reinterpret_cast<const char*>(s_io), got, seq) &&
                      is_balanced_json_object(s_io, got);
  }
  f.close();
  if (r.truncated_tail > 0) {
    File a = SD.open(path, FILE_APPEND);
    tail_terminated = a && a.write(static_cast<uint8_t>('\n')) == 1;
    if (a) {
      a.flush();
      a.close();
    }
    if (!tail_terminated) g_board.report_sd_write_error();
  }
  return true;
}

// The open session's log: close our handle first, repair, and drop the read-resume hint.
bool scan_and_repair(JsonlScanResult& r, bool& tail_terminated, bool& tail_was_record) {
  char p[kPathLen];
  if (!session_path(p, sizeof(p), "events.jsonl")) return false;
  if (s_log) {
    s_log.flush();
    s_log.close();
  }
  const bool ok = scan_and_repair_file(p, r, tail_terminated, tail_was_record);
  s_hint_seq = 0;
  s_hint_off = 0;
  return ok;
}

// Sequential reader of complete lines over an SD file using s_io. Over-long lines are skipped; a
// final line without '\n' (unrepaired truncated tail) is not returned.
class FileLines {
 public:
  FileLines(fs::File& f, size_t start) : f_(f), base_(start) { f_.seek(start); }

  bool next(const char*& line, size_t& len, size_t& end_off) {
    for (;;) {
      if (start_ < end_) {
        const uint8_t* nl = static_cast<const uint8_t*>(std::memchr(s_io + start_, '\n', end_ - start_));
        if (nl != nullptr) {
          const size_t i = static_cast<size_t>(nl - s_io);
          line = reinterpret_cast<const char*>(s_io + start_);
          len = i - start_;
          end_off = base_ + i + 1;
          start_ = i + 1;
          if (skipping_) {
            skipping_ = false;
            continue;
          }
          return true;
        }
      }
      if (eof_) return false;
      if (start_ == 0 && end_ == sizeof(s_io)) {
        skipping_ = true;  // no newline in a full buffer: drop it and skip to the next newline
        base_ += end_;
        end_ = 0;
      } else if (start_ > 0) {
        std::memmove(s_io, s_io + start_, end_ - start_);
        base_ += start_;
        end_ -= start_;
        start_ = 0;
      }
      const size_t r = f_.read(s_io + end_, sizeof(s_io) - end_);
      if (r == 0) {
        eof_ = true;
        return false;
      }
      end_ += r;
    }
  }

 private:
  fs::File& f_;
  size_t base_;
  size_t start_ = 0;
  size_t end_ = 0;
  bool skipping_ = false;
  bool eof_ = false;
};

// ---- store-generated events ---------------------------------------------------------------------

template <typename Fill>
bool internal_event(Operation op, EventStatus st, Fill fill) {
  EventHeader h{op, Transport::Local, st, s_mode, nullptr};
  JsonWriter& w = g_evidence.begin_event(h, s_internal, sizeof(s_internal));
  fill(w);
  char id[cfg::kIdBufLen];
  return g_evidence.commit_event(id);
}

bool from_this_boot(const char* session_id) {
  char prefix[24];
  snprintf(prefix, sizeof(prefix), "sess-%s-", s_boot_hex8);
  return std::strncmp(session_id, prefix, std::strlen(prefix)) == 0;
}

// /oyster/last_session.txt names the newest session, so the next boot knows which one to recover.
bool read_last_session(char out[cfg::kIdBufLen]) {
  out[0] = '\0';
  for (const char* p : {kLastSessionPath, kLastSessionTmp}) {
    File f = SD.open(p, FILE_READ);
    if (!f) continue;
    char buf[cfg::kIdBufLen + 4];
    const size_t n = f.read(reinterpret_cast<uint8_t*>(buf), sizeof(buf) - 1);
    f.close();
    size_t len = 0;
    while (len < n && buf[len] != '\r' && buf[len] != '\n') ++len;
    buf[len] = '\0';
    if (is_valid_session_id(buf)) {
      copy_bounded(out, cfg::kIdBufLen, buf);
      return true;
    }
  }
  return false;
}

void write_last_session(const char* session_id) {
  char line[cfg::kIdBufLen + 2];
  const int n = snprintf(line, sizeof(line), "%s\n", session_id);
  if (n <= 0) return;
  File f = SD.open(kLastSessionTmp, FILE_WRITE);
  if (!f) {
    g_board.report_sd_write_error();
    return;
  }
  const bool ok = f.write(reinterpret_cast<const uint8_t*>(line), static_cast<size_t>(n)) == static_cast<size_t>(n);
  f.flush();
  f.close();
  if (!ok) {
    SD.remove(kLastSessionTmp);
    g_board.report_sd_write_error();
    return;
  }
  // FAT rename is not power-loss atomic: read_last_session() falls back to the .tmp file.
  if (SD.exists(kLastSessionPath)) SD.remove(kLastSessionPath);
  if (!SD.rename(kLastSessionTmp, kLastSessionPath)) g_board.report_sd_write_error();
}

// Scans and repairs the newest previous session once per boot (before this boot's first session creates
// its files). Never uploads anything: previous sessions stay on the card (milestone M6).
void run_recovery_locked() {
  if (s_prev.attempted || !g_board.sd_ready()) return;  // no card yet: open_session() tries again
  s_prev = PrevSession();
  s_prev.attempted = true;
  char id[cfg::kIdBufLen];
  if (!read_last_session(id)) return;  // first boot with this card, or no earlier session
  char dir[kPathLen];
  snprintf(dir, sizeof(dir), "%s/%s", kSessionsDir, id);
  if (!SD.exists(dir)) return;
  copy_bounded(s_prev.session_id, sizeof(s_prev.session_id), id);
  s_prev.found = true;
  s_prev.event_pending = true;
  ManifestInfo m;
  load_manifest_at(dir, m);
  s_prev.manifest_found = m.found;
  s_prev.closed = m.closed;
  s_prev.manifest_last = m.last;
  s_prev.manifest_acked = m.acked;
  s_prev.manifest_resolved = m.resolved;
  s_prev.manifest_skipped = m.skipped;
  char p[kPathLen];
  snprintf(p, sizeof(p), "%s/events.jsonl", dir);
  if (SD.exists(p)) s_prev.scanned = scan_and_repair_file(p, s_prev.scan, s_prev.tail_terminated, s_prev.tail_was_record);
  log_i("evidence: previous session %s: %u valid, %u invalid, tail %u bytes (%s); not uploaded by this firmware",
        id, static_cast<unsigned>(s_prev.scan.valid_records), static_cast<unsigned>(s_prev.scan.invalid_lines),
        static_cast<unsigned>(s_prev.scan.truncated_tail), s_prev.tail_terminated ? "terminated" : "none/unrepaired");
}

void reset_upload_state() {
  s_up.reset();
  s_sd_written = 0;
  s_hint_seq = 0;
  s_hint_off = 0;
}

void artifact_abort_locked() {
  if (!s_art_active) return;
  if (s_art_file) {
    s_art_file.flush();
    s_art_file.close();
  }
  s_art_active = false;
  log_w("evidence: artifact %s left as .partial", s_art_object);
}

void csv_text(const char* in, char* out, size_t n) {
  // Quoted CSV field; a leading formula character is neutralised for spreadsheet safety.
  size_t o = 0;
  if (n < 3) {
    if (n > 0) out[0] = '\0';
    return;
  }
  out[o++] = '"';
  if (in != nullptr && (in[0] == '=' || in[0] == '+' || in[0] == '-' || in[0] == '@' || in[0] == '\t' || in[0] == '\r')) {
    out[o++] = '\'';
  }
  for (const char* p = in; p != nullptr && *p != '\0' && o + 3 < n; ++p) {
    if (*p == '"') out[o++] = '"';
    out[o++] = *p;
  }
  out[o++] = '"';
  out[o] = '\0';
}

bool write_str(File& f, const char* s) {
  const size_t n = std::strlen(s);
  return f.write(reinterpret_cast<const uint8_t*>(s), n) == n;
}

}  // namespace

// ---- lifecycle ------------------------------------------------------------------------------------

bool EvidenceStore::begin(SourceMode mode) {
  if (s_mu == nullptr) s_mu = xSemaphoreCreateRecursiveMutex();
  if (s_mu == nullptr) return false;
  Guard g;
  s_mode = mode;
  if (!s_begun) {
    format_boot_id(esp_random(), esp_random(), s_boot_id, sizeof(s_boot_id));
    std::memcpy(s_boot_hex8, s_boot_id + 5, 8);  // "boot-" + 16 hex digits
    s_boot_hex8[8] = '\0';
    s_begun = true;
  }
  if (g_board.sd_ready()) {
    if (!ensure_dir(kRootDir) || !ensure_dir(kSessionsDir)) {
      g_board.report_sd_write_error();
      log_e("evidence: cannot create %s", kSessionsDir);
    }
    recover();  // the newest previous session is scanned and repaired before a new one is created
  } else {
    ring_ready();
    log_w("evidence: no SD card; sessions are volatile (RAM ring, lost on reboot)");
  }
  return true;
}

bool EvidenceStore::open_session(const char* session_id, const char* device_id) {
  if (!is_valid_session_id(session_id) || !is_valid_device_id(device_id)) return false;
  Guard g;
  if (s_depth > 0) return false;  // this task is building an event
  if (session_open()) close_session();
  recover();  // no-op when begin() already did it or no card was mounted then

  copy_bounded(s_session, sizeof(s_session), session_id);
  copy_bounded(s_device, sizeof(s_device), device_id);
  copy_bounded(s_created_boot, sizeof(s_created_boot), s_boot_id);
  s_last_seq = 0;
  reset_upload_state();
  s_sd_failed = false;
  s_unflushed = 0;
  s_free_bytes = UINT64_MAX;
  s_dir[0] = '\0';
  ring_clear_counting_unresolved();

  bool resumed = false;
  bool scanned = false;
  bool tail_terminated = false;
  bool tail_was_record = false;
  JsonlScanResult scan;
  s_sd = g_board.sd_ready();
  if (s_sd) {
    snprintf(s_dir, sizeof(s_dir), "%s/%s", kSessionsDir, session_id);
    char p[kPathLen];
    bool ok = ensure_dir(kRootDir) && ensure_dir(kSessionsDir) && ensure_dir(s_dir) &&
              session_path(p, sizeof(p), "artifacts") && ensure_dir(p) && session_path(p, sizeof(p), "events.jsonl");
    if (ok && SD.exists(p)) {
      resumed = true;
      ManifestInfo m;
      load_manifest_at(s_dir, m);
      if (m.found && m.created_boot[0] != '\0') copy_bounded(s_created_boot, sizeof(s_created_boot), m.created_boot);
      scanned = scan_and_repair(scan, tail_terminated, tail_was_record);
      uint32_t last = scanned && scan.has_sequence ? scan.last_sequence : 0;
      if (m.last > last) last = m.last;
      if (m.acked > last) last = m.acked;
      s_last_seq = last;
      s_sd_written = last;
      s_up.restore(m.acked < last ? m.acked : last, m.resolved < last ? m.resolved : last, m.skipped);
    }
    if (ok) {
      s_log = SD.open(p, FILE_APPEND);
      ok = static_cast<bool>(s_log);
    }
    if (!ok) {
      g_board.report_sd_write_error();
      log_e("evidence: cannot open the session log on SD; using the RAM ring");
      s_sd = false;
      s_dir[0] = '\0';
    } else {
      const StorageInfo st = g_board.storage();
      if (st.total_bytes > 0 && st.total_bytes >= st.used_bytes) s_free_bytes = st.total_bytes - st.used_bytes;
      if (s_free_bytes != UINT64_MAX && s_free_bytes < kMinFreeBytes) {
        g_board.report_sd_full();
        s_sd_failed = true;
      }
      write_last_session(session_id);  // the next boot recovers this session
    }
  }
  if (!s_sd) {
    if (!from_this_boot(session_id)) {
      log_w("evidence: a volatile store cannot resume a session from an earlier boot");
      s_session[0] = '\0';
      s_device[0] = '\0';
      return false;
    }
    ring_ready();
  }
  s_last_flush_ms = millis();
  write_manifest(false);

  if (s_prev.event_pending) {
    // What the boot-time recovery found in the newest previous session. This session is the one that
    // records it; the previous session's events are not uploaded by this firmware (milestone M6).
    s_prev.event_pending = false;
    const PrevSession& pv = s_prev;
    const bool clean = pv.scanned && pv.scan.truncated_tail == 0 && pv.scan.invalid_lines == 0;
    internal_event(Operation::Recovery, clean ? EventStatus::Success : EventStatus::Partial, [&](JsonWriter& w) {
      w.str("scope", "previous_session");
      w.str("previous_session_id", pv.session_id);
      w.str("file", "events.jsonl");
      w.boolean("scanned", pv.scanned);
      if (pv.manifest_found) {
        w.boolean("previous_session_closed", pv.closed);
      } else {
        w.null("previous_session_closed");
      }
      w.unum("valid_records", pv.scan.valid_records);
      w.unum("invalid_lines", pv.scan.invalid_lines);
      w.unum("truncated_tail_bytes", pv.scan.truncated_tail);
      w.boolean("tail_terminated", pv.tail_terminated);
      w.boolean("tail_was_complete_record", pv.tail_was_record);
      w.boolean("line_too_long", pv.scan.line_too_long);
      if (pv.scanned && pv.scan.has_sequence) {
        w.unum("last_sequence_found", pv.scan.last_sequence);
      } else {
        w.null("last_sequence_found");
      }
      w.unum("manifest_last_sequence", pv.manifest_last);
      w.unum("acked_through", pv.manifest_acked);
      w.unum("skipped_events", pv.manifest_skipped);
      const uint32_t last_found = pv.scanned && pv.scan.has_sequence ? pv.scan.last_sequence : pv.manifest_last;
      if (last_found > pv.manifest_acked) {
        w.unum("unacked_first_sequence", static_cast<uint64_t>(pv.manifest_acked) + 1);
        w.unum("unacked_last_sequence", last_found);
      } else {
        w.null("unacked_first_sequence");
        w.null("unacked_last_sequence");
      }
      w.str("sync_status", "not_uploaded_milestone_m6");
    });
  }
  if (scanned) {
    const bool clean = scan.truncated_tail == 0 && scan.invalid_lines == 0;
    internal_event(Operation::Recovery, clean ? EventStatus::Success : EventStatus::Partial, [&](JsonWriter& w) {
      w.str("scope", "resumed_session");
      w.str("file", "events.jsonl");
      w.str("previous_boot_id", s_created_boot);
      w.unum("valid_records", scan.valid_records);
      w.unum("invalid_lines", scan.invalid_lines);
      w.unum("truncated_tail_bytes", scan.truncated_tail);
      w.boolean("tail_terminated", tail_terminated);
      w.boolean("tail_was_complete_record", tail_was_record);
      w.boolean("line_too_long", scan.line_too_long);
      if (scan.has_sequence) {
        w.unum("last_sequence_found", scan.last_sequence);
      } else {
        w.null("last_sequence_found");
      }
      w.unum("acked_through", s_up.acked());
    });
  }
  internal_event(Operation::SessionOpen, EventStatus::Success, [&](JsonWriter& w) {
    w.str("persistence", sd_active() ? "sd" : "volatile");
    w.boolean("resumed", resumed);
    if (!sd_active()) w.unum("volatile_ring_bytes", cfg::kVolatileEventRingBytes);
    w.str("firmware_version", OYSTER_FIRMWARE_VERSION);
  });
  flush_pending();
  return true;
}

void EvidenceStore::close_session() {
  Guard g;
  if (!session_open() || s_depth > 0) return;
  artifact_abort_locked();
  const uint32_t before = s_last_seq;
  internal_event(Operation::SessionClose, EventStatus::Success, [&](JsonWriter& w) {
    w.unum("events_before_close", before);
    w.unum("acked_through", s_up.acked());
    w.unum("skipped_events", s_up.skipped());
    w.unum("dropped_events", s_dropped);
    w.str("persistence", sd_active() ? "sd" : "volatile");
  });
  if (s_log) {
    s_log.flush();
    s_log.close();
  }
  write_manifest(true);
  ring_clear_counting_unresolved();  // RAM-only events cannot outlive their session
  s_session[0] = '\0';
  s_device[0] = '\0';
  s_dir[0] = '\0';
  s_sd = false;
  s_sd_failed = false;
}

const char* EvidenceStore::session_id() const { return s_session; }
const char* EvidenceStore::boot_id() const { return s_boot_id; }
uint32_t EvidenceStore::next_sequence() const { return s_last_seq + 1; }

// ---- events ---------------------------------------------------------------------------------------

JsonWriter& EvidenceStore::begin_event(const EventHeader& h, char* scratch, size_t scratch_len) {
  lock();
  ++s_depth;
  if (s_depth > 1 || scratch == nullptr || scratch_len < 64) {
    // Nested begin or unusable scratch: a writer that holds nothing; commit_event() reports failure.
    if (s_depth == 1) s_event_ok = false;
    return *new (s_dummy_writer_mem) JsonWriter(s_dummy_buf, 1);
  }
  s_scratch = scratch;
  s_event_op = h.op;
  s_writer = new (s_writer_mem) JsonWriter(scratch, scratch_len);
  JsonWriter& w = *s_writer;
  w.begin_object();
  w.str("source_mode", to_string(h.source_mode));
  w.str("transport", to_string(h.transport));
  if (h.target_id != nullptr && h.target_id[0] != '\0') {
    w.str("target_id", h.target_id);
  } else {
    w.null("target_id");
  }
  w.str("operation", to_string(h.op));
  char ts[32];
  const ClockQuality q = timesync::format_utc(ts, sizeof(ts));
  if (q != ClockQuality::Unknown) {
    w.str("timestamp_utc", ts);
  } else {
    w.null("timestamp_utc");
  }
  w.str("clock_quality", to_string(q));
  w.unum("uptime_ms", timesync::uptime_ms());
  w.str("status", to_string(h.status));
  w.begin_object("details");
  s_event_ok = true;
  return w;
}

bool EvidenceStore::commit_event(char out_event_id[cfg::kIdBufLen]) {
  if (out_event_id != nullptr) out_event_id[0] = '\0';
  if (s_depth <= 0) return false;  // no begin_event(): the lock is not held
  if (s_depth > 1) {
    --s_depth;
    ++s_dropped;
    unlock();
    return false;
  }
  bool ok = false;
  if (s_event_ok && s_writer != nullptr && s_scratch != nullptr) {
    JsonWriter& w = *s_writer;
    w.end_object();  // details
    w.end_object();  // envelope
    if (w.ok() && w.size() >= 2 && s_scratch[0] == '{') {
      const char* body = s_scratch + 1;
      const size_t body_len = w.size() - 1;
      if (session_open()) {
        ok = append_body(body, body_len, important_op(s_event_op), out_event_id);
      } else {
        ok = stash_pending(body, body_len);  // event id assigned when a session opens
      }
    } else {
      ++s_dropped;
      log_w("evidence: event did not fit its scratch buffer; dropped");
    }
  } else {
    ++s_dropped;
  }
  s_writer = nullptr;
  s_scratch = nullptr;
  s_event_ok = false;
  s_depth = 0;
  unlock();
  return ok;
}

// ---- artifacts ------------------------------------------------------------------------------------

bool EvidenceStore::artifact_begin(const char* object_id, uint32_t declared_len) {
  Guard g;
  artifact_abort_locked();
  if (!session_open() || !sd_active()) return false;  // artifacts need SD; never buffered whole in RAM
  if (!is_valid_object_id(object_id) || declared_len == 0 || declared_len > cfg::kMaxDiagObjectBytes) return false;
  char name[40];
  char p[kPathLen];
  if (!xfer::artifact_file_name(object_id, true, name, sizeof(name))) return false;
  char rel[64];
  snprintf(rel, sizeof(rel), "artifacts/%s", name);
  if (!session_path(p, sizeof(p), rel)) return false;
  s_art_file = SD.open(p, FILE_WRITE);
  if (!s_art_file) {
    g_board.report_sd_write_error();
    return false;
  }
  s_art_hash.reset();
  s_art_declared = declared_len;
  s_art_received = 0;
  copy_bounded(s_art_object, sizeof(s_art_object), object_id);
  s_art_active = true;
  return true;
}

bool EvidenceStore::artifact_write(const uint8_t* data, size_t n) {
  Guard g;
  if (!s_art_active || (data == nullptr && n > 0)) return false;
  if (n == 0) return true;
  if (static_cast<uint64_t>(s_art_received) + n > s_art_declared) {
    log_w("evidence: artifact longer than declared; stopped");
    artifact_abort_locked();
    return false;
  }
  const size_t w = s_art_file.write(data, n);
  if (w != n) {
    g_board.report_sd_write_error();
    artifact_abort_locked();
    return false;
  }
  s_art_hash.update(data, n);
  s_art_received += static_cast<uint32_t>(n);
  return true;
}

bool EvidenceStore::artifact_finalize(const uint8_t expected_sha256[32], char out_artifact_id[cfg::kIdBufLen]) {
  Guard g;
  if (out_artifact_id != nullptr) out_artifact_id[0] = '\0';
  if (!s_art_active || expected_sha256 == nullptr) return false;
  s_art_file.flush();
  s_art_file.close();
  s_art_active = false;
  uint8_t digest[32];
  s_art_hash.finish(digest);
  if (s_art_received != s_art_declared || !equal_ct(digest, expected_sha256, 32)) {
    log_w("evidence: artifact %s failed length/hash verification; kept as .partial", s_art_object);
    return false;
  }
  char pname[40];
  char fname[40];
  char rel[64];
  char ppath[kPathLen];
  char fpath[kPathLen];
  if (!xfer::artifact_file_name(s_art_object, true, pname, sizeof(pname)) ||
      !xfer::artifact_file_name(s_art_object, false, fname, sizeof(fname))) {
    return false;
  }
  snprintf(rel, sizeof(rel), "artifacts/%s", pname);
  if (!session_path(ppath, sizeof(ppath), rel)) return false;
  snprintf(rel, sizeof(rel), "artifacts/%s", fname);
  if (!session_path(fpath, sizeof(fpath), rel)) return false;
  if (SD.exists(fpath)) SD.remove(fpath);  // an earlier verified copy of the same object id
  if (!SD.rename(ppath, fpath)) {
    g_board.report_sd_write_error();
    return false;
  }
  if (out_artifact_id != nullptr) snprintf(out_artifact_id, cfg::kIdBufLen, "artifact-%s", s_art_object);
  return true;
}

void EvidenceStore::artifact_abort() {
  Guard g;
  artifact_abort_locked();
}

// ---- recovery / status ----------------------------------------------------------------------------

uint32_t EvidenceStore::recover() {
  Guard g;
  run_recovery_locked();
  return s_prev.scanned && s_prev.scan.has_sequence ? s_prev.scan.last_sequence : 0;
}

bool EvidenceStore::sd_backed() const {
  // Lock-free (UI reads it every frame); flags are single words.
  if (session_open()) return sd_active();
  return g_board.sd_ready();
}

uint32_t EvidenceStore::dropped_events() const { return s_dropped; }
uint32_t EvidenceStore::skipped_events() const { return s_up.skipped(); }

bool EvidenceStore::new_session_id(char out[cfg::kIdBufLen]) {
  if (out == nullptr) return false;
  out[0] = '\0';
  Guard g;
  if (!s_begun) return false;
  const bool sd = g_board.sd_ready();
  while (s_session_counter < kMaxSessionsPerBoot) {
    ++s_session_counter;
    char id[cfg::kIdBufLen];
    snprintf(id, sizeof(id), "sess-%s-%03u", s_boot_hex8, static_cast<unsigned>(s_session_counter));
    if (sd) {
      char p[kPathLen];
      snprintf(p, sizeof(p), "%s/%s", kSessionsDir, id);
      if (SD.exists(p)) continue;
    }
    copy_bounded(out, cfg::kIdBufLen, id);
    return true;
  }
  return false;
}

// ---- upload support -------------------------------------------------------------------------------

size_t EvidenceStore::read_batch(const char* session_id, char* buf, size_t max, uint32_t& first_seq,
                                 uint32_t& last_seq, uint16_t& count) {
  first_seq = 0;
  last_seq = 0;
  count = 0;
  if (buf == nullptr || max == 0 || session_id == nullptr) return 0;
  Guard g;
  // Read under the lock open_session() takes, for exactly the session the caller sends to (the one in its
  // URL): a session switch cannot slip another session's events into this batch.
  if (!session_open() || std::strcmp(session_id, s_session) != 0) return 0;
  const uint32_t after_seq = s_up.resolved();
  size_t used = 0;
  bool full = false;
  // Contiguous run only: a gap in the sequence numbers ends the batch (the gap is recorded when it is acked).
  auto take = [&](const char* line, size_t len, uint32_t seq) -> bool {
    if (count > 0 && seq <= last_seq) return true;  // duplicate or out-of-order line: ignored
    if (count > 0 && seq != last_seq + 1) return false;
    const size_t need = len + (count > 0 ? 1 : 0);
    if (used + need > max || count == UINT16_MAX) return false;
    if (count > 0) buf[used++] = ',';
    std::memcpy(buf + used, line, len);
    used += len;
    if (count == 0) first_seq = seq;
    last_seq = seq;
    ++count;
    return true;
  };

  if (s_sd && s_dir[0] != '\0') {
    flush_log();
    char p[kPathLen];
    File f;
    if (session_path(p, sizeof(p), "events.jsonl")) f = SD.open(p, FILE_READ);
    if (f) {
      const size_t fsize = f.size();
      const size_t start = (after_seq >= s_hint_seq && s_hint_off <= fsize) ? s_hint_off : 0;
      FileLines lines(f, start);
      const char* line = nullptr;
      size_t len = 0;
      size_t end_off = 0;
      while (!full && lines.next(line, len, end_off)) {
        uint32_t seq = 0;
        if (!valid_line(line, len, seq)) continue;
        if (seq <= after_seq) {
          if (seq >= s_hint_seq) {
            s_hint_seq = seq;
            s_hint_off = end_off;
          }
          continue;
        }
        if (!take(line, len, seq)) full = true;
      }
      f.close();
    }
  }
  if (!full && s_ring != nullptr) {
    size_t pos = 0;
    while (pos < s_ring_len) {
      const char* nl = static_cast<const char*>(std::memchr(s_ring + pos, '\n', s_ring_len - pos));
      if (nl == nullptr) break;
      const size_t len = static_cast<size_t>(nl - (s_ring + pos));
      uint32_t seq = 0;
      if (valid_line(s_ring + pos, len, seq) && seq > after_seq) {
        if (!take(s_ring + pos, len, seq)) break;
      }
      pos += len + 1;
    }
  }
  return used;
}

void EvidenceStore::ack_through(const char* session_id, uint32_t first_seq, uint32_t last_seq) {
  Guard g;
  if (!session_open() || session_id == nullptr || std::strcmp(session_id, s_session) != 0) return;
  if (!s_up.accept(first_seq, last_seq, s_last_seq)) return;
  ring_drop_resolved();
  write_manifest(false);
}

void EvidenceStore::skip_event(const char* session_id, uint32_t seq) {
  Guard g;
  if (!session_open() || session_id == nullptr || std::strcmp(session_id, s_session) != 0) return;
  if (!s_up.skip(seq, s_last_seq)) return;
  ring_drop_resolved();
  write_manifest(false);
}

uint32_t EvidenceStore::acked_sequence() const { return s_up.acked(); }
uint32_t EvidenceStore::resolved_sequence() const { return s_up.resolved(); }

EventFate EvidenceStore::event_fate(const char* session_id, uint32_t seq) const {
  Guard g;
  if (!session_open() || session_id == nullptr || std::strcmp(session_id, s_session) != 0) {
    return EventFate::ForeignSession;
  }
  const EventFate f = s_up.fate(seq, s_last_seq);
  if (f != EventFate::Pending) return f;
  // Not resolved yet: still stored (on the card or in the RAM ring) -> it can be uploaded; else it is gone.
  return seq <= s_sd_written || ring_has(seq) ? EventFate::Pending : EventFate::Lost;
}

// ---- wifi.csv -------------------------------------------------------------------------------------

// Columns: survey_started_ms is device uptime at the start of the scan. exported_at_utc / export_clock_quality
// describe the moment these rows were WRITTEN (after the survey), not when an AP was observed.
bool EvidenceStore::append_wifi_csv(const WifiSurveyResult& r, const char* room_label) {
  Guard g;
  if (!session_open() || !sd_active()) return false;
  char p[kPathLen];
  if (!session_path(p, sizeof(p), "wifi.csv")) return false;
  File f = SD.open(p, FILE_APPEND);
  if (!f) {
    g_board.report_sd_write_error();
    return false;
  }
  bool ok = true;
  if (f.size() == 0) {
    ok = write_str(f,
                   "survey_started_ms,exported_at_utc,export_clock_quality,room_label,bssid,channel,rssi_dbm,auth_mode,"
                   "ssid_hex,ssid_display,survey_complete,survey_overflow\n");
  }
  char ts[32];
  const ClockQuality q = timesync::format_utc(ts, sizeof(ts));
  char room[2 * cfg::kMaxRoomLabelLen + 8];
  csv_text(room_label != nullptr ? room_label : "", room, sizeof(room));
  const size_t n = r.count < cfg::kMaxApObservations ? r.count : cfg::kMaxApObservations;
  for (size_t i = 0; i < n && ok; ++i) {
    const ApObservation& ap = r.aps[i];
    const size_t slen = ap.ssid_len < cfg::kMaxSsidBytes ? ap.ssid_len : cfg::kMaxSsidBytes;
    char mac[18];
    format_mac(ap.bssid, mac, sizeof(mac));
    char hex[2 * cfg::kMaxSsidBytes + 1];
    for (size_t k = 0; k < slen; ++k) snprintf(hex + 2 * k, 3, "%02x", ap.ssid[k]);
    hex[2 * slen] = '\0';
    char disp[cfg::kMaxSsidBytes + 1];
    char disp_csv[2 * cfg::kMaxSsidBytes + 8];
    const bool safe = slen > 0 && display_safe(ap.ssid, slen, disp, sizeof(disp));
    csv_text(safe ? disp : "", disp_csv, sizeof(disp_csv));
    char row[320];
    snprintf(row, sizeof(row), "%lu,%s,%s,%s,%s,%u,%d,%s,%s,%s,%s,%u\n", static_cast<unsigned long>(r.started_ms),
             q != ClockQuality::Unknown ? ts : "", to_string(q), room, mac, static_cast<unsigned>(ap.channel),
             static_cast<int>(ap.rssi_dbm), to_string(ap.auth), hex, disp_csv, r.complete ? "true" : "false",
             static_cast<unsigned>(r.overflow));
    ok = write_str(f, row);
  }
  f.flush();
  f.close();
  if (!ok) g_board.report_sd_write_error();
  return ok;
}

}  // namespace oyster
