// Lab diagnostic transfer: wire encoding (little-endian, schemas/lab-profile.json "transfer") and the
// streaming receive state machine. All peer input is treated as untrusted.
#include "oyster_core/transfer.h"

#include <cstring>

#include "oyster_core/ids.h"

namespace oyster::xfer {

namespace {

uint16_t rd16(const uint8_t* p) { return static_cast<uint16_t>(p[0] | (p[1] << 8)); }

uint32_t rd32(const uint8_t* p) {
  return static_cast<uint32_t>(p[0]) | (static_cast<uint32_t>(p[1]) << 8) | (static_cast<uint32_t>(p[2]) << 16) |
         (static_cast<uint32_t>(p[3]) << 24);
}

void wr16(uint8_t* p, uint16_t v) {
  p[0] = static_cast<uint8_t>(v);
  p[1] = static_cast<uint8_t>(v >> 8);
}

void wr32(uint8_t* p, uint32_t v) {
  p[0] = static_cast<uint8_t>(v);
  p[1] = static_cast<uint8_t>(v >> 8);
  p[2] = static_cast<uint8_t>(v >> 16);
  p[3] = static_cast<uint8_t>(v >> 24);
}

// Manifest field offsets.
constexpr size_t kOffVersion = 0;
constexpr size_t kOffObjectId = 1;
constexpr size_t kOffLength = kOffObjectId + kObjectIdBytes;  // 17
constexpr size_t kOffSha = kOffLength + 4;                    // 21
constexpr size_t kOffChunkMax = kOffSha + 32;                 // 53
static_assert(kOffChunkMax + 2 == kManifestBytes, "manifest layout");

}  // namespace

const char* to_string(ManifestError e) {
  switch (e) {
    case ManifestError::None: return "none";
    case ManifestError::AppDenied: return "app_denied";
    case ManifestError::TooShort: return "too_short";
    case ManifestError::BadVersion: return "bad_version";
    case ManifestError::BadObjectId: return "bad_object_id";
    case ManifestError::TooLarge: return "too_large";
    case ManifestError::ZeroChunk: return "zero_chunk";
  }
  return "bad_version";
}

// Bytes beyond the 55-byte v1 layout are ignored. `out` is written only on success.
ManifestError parse_manifest(const uint8_t* p, size_t n, size_t max_object_bytes, Manifest& out) {
  if (p == nullptr || n == 0) return ManifestError::TooShort;
  if (n == 1 && p[0] == 0x01) return ManifestError::AppDenied;
  if (n < kManifestBytes) return ManifestError::TooShort;
  if (p[kOffVersion] != kManifestVersion) return ManifestError::BadVersion;

  Manifest m{};
  m.version = p[kOffVersion];
  // object_id: ASCII, zero-padded; nothing but zeros may follow the first zero byte.
  size_t id_len = 0;
  while (id_len < kObjectIdBytes && p[kOffObjectId + id_len] != 0) ++id_len;
  for (size_t i = id_len; i < kObjectIdBytes; ++i) {
    if (p[kOffObjectId + i] != 0) return ManifestError::BadObjectId;
  }
  std::memcpy(m.object_id, p + kOffObjectId, id_len);
  m.object_id[id_len] = '\0';
  if (!is_valid_object_id(m.object_id)) return ManifestError::BadObjectId;

  m.declared_length = rd32(p + kOffLength);
  if (m.declared_length > max_object_bytes) return ManifestError::TooLarge;
  std::memcpy(m.sha256, p + kOffSha, 32);
  m.chunk_max = rd16(p + kOffChunkMax);
  if (m.chunk_max == 0) return ManifestError::ZeroChunk;
  std::memcpy(m.tag, m.sha256, 4);
  out = m;
  return ManifestError::None;
}

size_t encode_manifest(const Manifest& m, uint8_t out[kManifestBytes]) {
  std::memset(out, 0, kManifestBytes);
  out[kOffVersion] = m.version;
  for (size_t i = 0; i < kObjectIdBytes && m.object_id[i] != '\0'; ++i) {
    out[kOffObjectId + i] = static_cast<uint8_t>(m.object_id[i]);
  }
  wr32(out + kOffLength, m.declared_length);
  std::memcpy(out + kOffSha, m.sha256, 32);
  wr16(out + kOffChunkMax, m.chunk_max);
  return kManifestBytes;
}

size_t encode_ctrl(uint8_t out[kCtrlBytes], CtrlOp op, const uint8_t tag[4], uint32_t offset, uint16_t length,
                   uint16_t seq) {
  out[0] = static_cast<uint8_t>(op);
  if (tag != nullptr) {
    std::memcpy(out + 1, tag, 4);
  } else {
    std::memset(out + 1, 0, 4);
  }
  wr32(out + 5, offset);
  wr16(out + 9, length);
  wr16(out + 11, seq);
  return kCtrlBytes;
}

bool parse_chunk(const uint8_t* p, size_t n, ChunkHeader& h, const uint8_t*& payload) {
  if (p == nullptr || n < kChunkHeaderBytes) return false;
  const uint16_t len = rd16(p + 7);
  if (n - kChunkHeaderBytes != len) return false;
  h.status = static_cast<Status>(p[0]);
  h.seq = rd16(p + 1);
  h.offset = rd32(p + 3);
  h.len = len;
  payload = p + kChunkHeaderBytes;
  return true;
}

uint16_t chunk_len_for_mtu(uint16_t chunk_max, uint16_t att_mtu) {
  // A read response carries att_mtu - 1 bytes; the chunk header takes 9 of them.
  if (att_mtu <= 1 + kChunkHeaderBytes) return 0;
  const uint16_t avail = static_cast<uint16_t>(att_mtu - 1 - kChunkHeaderBytes);
  return chunk_max < avail ? chunk_max : avail;
}

const char* to_string(ChunkVerdict v) {
  switch (v) {
    case ChunkVerdict::Accepted: return "accepted";
    case ChunkVerdict::Duplicate: return "duplicate";
    case ChunkVerdict::OutOfOrder: return "out_of_order";
    case ChunkVerdict::WrongSequence: return "wrong_sequence";
    case ChunkVerdict::WrongLength: return "wrong_length";
    case ChunkVerdict::Overflow: return "overflow";
    case ChunkVerdict::Denied: return "denied";
    case ChunkVerdict::Stale: return "stale";
    case ChunkVerdict::Cancelled: return "cancelled";
    case ChunkVerdict::PeerError: return "peer_error";
    case ChunkVerdict::SinkError: return "sink_error";
    case ChunkVerdict::NotRunning: return "not_running";
  }
  return "peer_error";
}

const char* to_string(FinishVerdict v) {
  switch (v) {
    case FinishVerdict::HashMatch: return "hash_match";
    case FinishVerdict::HashMismatch: return "hash_mismatch";
    case FinishVerdict::LengthMismatch: return "length_mismatch";
    case FinishVerdict::Incomplete: return "incomplete";
    case FinishVerdict::NotRunning: return "not_running";
  }
  return "not_running";
}

// Request sequence numbers start at 1 and skip 0 on wrap, so 0 never names a request.
// seq_ is the number the next request will carry.
void TransferFsm::begin(const Manifest& m, uint16_t att_mtu) {
  manifest_ = m;
  hash_.reset();
  received_ = 0;
  chunk_len_ = chunk_len_for_mtu(m.chunk_max, att_mtu);
  seq_ = 1;
  outstanding_ = false;
  cancelled_ = false;
  running_ = chunk_len_ > 0;  // an MTU too small for any payload cannot transfer
}

bool TransferFsm::absorb_existing(const uint8_t* p, size_t n) {
  if (!running_ || outstanding_ || seq_ != 1) return false;  // only before the first request
  if (n == 0) return true;
  if (p == nullptr || n > static_cast<size_t>(manifest_.declared_length - received_)) return false;
  hash_.update(p, n);
  received_ += static_cast<uint32_t>(n);
  return true;
}

bool TransferFsm::next_request(uint8_t out_ctrl[kCtrlBytes]) {
  if (!running_ || received_ >= manifest_.declared_length) return false;
  const uint32_t remaining = manifest_.declared_length - received_;
  const uint16_t len = remaining < chunk_len_ ? static_cast<uint16_t>(remaining) : chunk_len_;
  encode_ctrl(out_ctrl, CtrlOp::Request, manifest_.tag, received_, len, seq_);
  // A repeated call while a request is outstanding re-requests the same offset under a new seq.
  outstanding_ = true;
  ++seq_;
  if (seq_ == 0) seq_ = 1;
  return true;
}

ChunkVerdict TransferFsm::on_chunk(const ChunkHeader& h, const uint8_t* payload, ChunkSink& sink) {
  if (cancelled_) return ChunkVerdict::Cancelled;
  if (!running_) return ChunkVerdict::NotRunning;

  // Application-level status: every non-OK status ends this transfer attempt.
  if (h.status != Status::Ok) {
    running_ = false;
    outstanding_ = false;
    switch (h.status) {
      case Status::NotAuthorized: return ChunkVerdict::Denied;
      case Status::StaleObject: return ChunkVerdict::Stale;
      case Status::Cancelled: return ChunkVerdict::Cancelled;
      default: return ChunkVerdict::PeerError;
    }
  }

  const uint64_t end = static_cast<uint64_t>(h.offset) + h.len;
  if (end <= received_ && h.offset < received_) return ChunkVerdict::Duplicate;  // already stored
  if (h.offset != received_) return ChunkVerdict::OutOfOrder;
  uint16_t expected_seq = static_cast<uint16_t>(seq_ - 1);
  if (expected_seq == 0) expected_seq = 0xFFFF;  // seq_ wrapped from 0xFFFF to 1
  if (!outstanding_ || h.seq != expected_seq) return ChunkVerdict::WrongSequence;
  if (end > manifest_.declared_length) {
    running_ = false;  // the peer is sending past the declared object: protocol violation
    outstanding_ = false;
    return ChunkVerdict::Overflow;
  }
  if (h.len == 0 || h.len > chunk_len_ || (payload == nullptr)) return ChunkVerdict::WrongLength;

  if (!sink.append(payload, h.len)) {
    running_ = false;
    outstanding_ = false;
    return ChunkVerdict::SinkError;
  }
  hash_.update(payload, h.len);
  received_ += h.len;
  outstanding_ = false;
  return ChunkVerdict::Accepted;
}

// Terminal except for Incomplete (a request is still in flight). LengthMismatch still writes the
// digest of the bytes received, for the record.
FinishVerdict TransferFsm::finish(uint8_t out_digest[32]) {
  if (!running_) return FinishVerdict::NotRunning;
  if (outstanding_) return FinishVerdict::Incomplete;
  running_ = false;
  hash_.finish(out_digest);
  if (received_ != manifest_.declared_length) return FinishVerdict::LengthMismatch;
  return equal_ct(out_digest, manifest_.sha256, 32) ? FinishVerdict::HashMatch : FinishVerdict::HashMismatch;
}

void TransferFsm::cancel() {
  cancelled_ = true;
  running_ = false;
  outstanding_ = false;
}

bool artifact_file_name(const char* object_id, bool partial, char* out, size_t n) {
  if (out == nullptr || n == 0) return false;
  out[0] = '\0';
  if (!is_valid_object_id(object_id)) return false;
  const char* ext = partial ? ".partial" : ".bin";
  const size_t id_len = std::strlen(object_id);
  const size_t ext_len = std::strlen(ext);
  if (id_len + ext_len + 1 > n) return false;
  std::memcpy(out, object_id, id_len);
  std::memcpy(out + id_len, ext, ext_len + 1);
  return true;
}

}  // namespace oyster::xfer
