// Lab diagnostic transfer protocol (schemas/lab-profile.json "transfer"): manifest / chunk_ctrl /
// chunk_data encoding and a streaming state machine. Bytes go to a ChunkSink (a ".partial" file on
// SD, memory in tests); the object is finalised only after length AND SHA-256 verification.
// Device-side execution is milestone M4; the protocol logic is implemented and host-tested now.
#pragma once

#include <cstddef>
#include <cstdint>

#include "oyster_core/sha256.h"

namespace oyster::xfer {

constexpr uint8_t kManifestVersion = 1;
constexpr size_t kManifestBytes = 55;      // [ver 1][object_id 16][len u32][sha256 32][chunk_max u16]
constexpr size_t kCtrlBytes = 13;          // [op 1][tag 4][offset u32][length u16][seq u16]
constexpr size_t kChunkHeaderBytes = 9;    // [status 1][seq u16][offset u32][len u16]
constexpr size_t kObjectIdBytes = 16;

enum class CtrlOp : uint8_t { Request = 1, Cancel = 2, Reset = 3 };
enum class Status : uint8_t { Ok = 0, NotAuthorized = 1, BadRequest = 2, StaleObject = 3, OutOfRange = 4, Cancelled = 5, NoRequest = 6 };

struct Manifest {
  uint8_t version;
  char object_id[kObjectIdBytes + 1];
  uint32_t declared_length;
  uint8_t sha256[32];
  uint16_t chunk_max;
  uint8_t tag[4];  // first 4 bytes of sha256: binds requests to one object version
};

enum class ManifestError : uint8_t { None, AppDenied, TooShort, BadVersion, BadObjectId, TooLarge, ZeroChunk };
const char* to_string(ManifestError e);

// A single byte 0x01 is the application-level denial (NOT an ATT error) and yields AppDenied.
ManifestError parse_manifest(const uint8_t* p, size_t n, size_t max_object_bytes, Manifest& out);
size_t encode_manifest(const Manifest& m, uint8_t out[kManifestBytes]);  // used by tests / simulators

size_t encode_ctrl(uint8_t out[kCtrlBytes], CtrlOp op, const uint8_t tag[4], uint32_t offset, uint16_t length, uint16_t seq);

struct ChunkHeader {
  Status status;
  uint16_t seq;
  uint32_t offset;
  uint16_t len;
};
// False when the buffer is shorter than the header or len disagrees with the payload size.
bool parse_chunk(const uint8_t* p, size_t n, ChunkHeader& h, const uint8_t*& payload);

// chunk_len <= min(manifest.chunk_max, att_mtu - 1 - 9); 0 when the MTU is too small.
uint16_t chunk_len_for_mtu(uint16_t chunk_max, uint16_t att_mtu);

class ChunkSink {
 public:
  virtual ~ChunkSink() = default;
  virtual bool append(const uint8_t* p, size_t n) = 0;  // strictly sequential writes
};

enum class ChunkVerdict : uint8_t {
  Accepted, Duplicate, OutOfOrder, WrongSequence, WrongLength, Overflow,
  Denied, Stale, Cancelled, PeerError, SinkError, NotRunning
};
enum class FinishVerdict : uint8_t { HashMatch, HashMismatch, LengthMismatch, Incomplete, NotRunning };
const char* to_string(ChunkVerdict v);
const char* to_string(FinishVerdict v);

class TransferFsm {
 public:
  void begin(const Manifest& m, uint16_t att_mtu);
  // Resume: feed bytes already stored in the .partial file, in order, before requesting more.
  // Only valid when the freshly re-read manifest equals the one the partial file was started with.
  bool absorb_existing(const uint8_t* p, size_t n);
  // Next request to write to chunk_ctrl. False when every byte has been received or not running.
  bool next_request(uint8_t out_ctrl[kCtrlBytes]);
  ChunkVerdict on_chunk(const ChunkHeader& h, const uint8_t* payload, ChunkSink& sink);
  FinishVerdict finish(uint8_t out_digest[32]);
  void cancel();

  bool running() const { return running_; }
  bool cancelled() const { return cancelled_; }
  uint32_t received() const { return received_; }
  uint32_t expected() const { return manifest_.declared_length; }
  uint16_t chunk_len() const { return chunk_len_; }
  uint16_t next_seq() const { return seq_; }

 private:
  Manifest manifest_{};
  Sha256 hash_;
  uint32_t received_ = 0;
  uint16_t chunk_len_ = 0;
  uint16_t seq_ = 0;
  bool outstanding_ = false;
  bool running_ = false;
  bool cancelled_ = false;
};

// Local artifact file names are generated, never taken from the peer: "<object_id>.bin" / ".partial".
// False when object_id is not a valid object id (see ids.h) or the buffer is too small.
bool artifact_file_name(const char* object_id, bool partial, char* out, size_t n);

}  // namespace oyster::xfer
