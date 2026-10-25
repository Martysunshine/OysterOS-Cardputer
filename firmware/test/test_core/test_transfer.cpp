// Lab diagnostic transfer (transfer.h): wire encoding and the receive state machine against an
// in-memory peer that follows schemas/lab-profile.json "transfer".
#include <unity.h>

#include <cstring>

#include "oyster_core/lab_profile_gen.h"
#include "oyster_core/sha256.h"
#include "oyster_core/transfer.h"
#include "test_common.h"

using namespace oyster;
using namespace oyster::xfer;

namespace {

constexpr size_t kObjLen = 1000;
uint8_t g_obj[65536];

class MemSink : public ChunkSink {
 public:
  bool append(const uint8_t* p, size_t n) override {
    if (fail || len + n > sizeof(data)) return false;
    std::memcpy(data + len, p, n);
    len += n;
    return true;
  }
  void clear() {
    len = 0;
    fail = false;
  }
  uint8_t data[65536];
  size_t len = 0;
  bool fail = false;
};

MemSink g_sink;

void fill_object(size_t n) {
  for (size_t i = 0; i < n; ++i) g_obj[i] = static_cast<uint8_t>((i * 7 + 3) & 0xFF);
}

Manifest make_manifest(size_t n, uint16_t chunk_max = 237) {
  Manifest m;
  std::memset(&m, 0, sizeof(m));
  m.version = kManifestVersion;
  std::strcpy(m.object_id, lab::kObjectId);
  m.declared_length = static_cast<uint32_t>(n);
  Sha256 h;
  h.update(g_obj, n);
  h.finish(m.sha256);
  m.chunk_max = chunk_max;
  std::memcpy(m.tag, m.sha256, 4);
  return m;
}

uint16_t le16(const uint8_t* p) { return static_cast<uint16_t>(p[0] | (p[1] << 8)); }
uint32_t le32(const uint8_t* p) {
  return static_cast<uint32_t>(p[0]) | (static_cast<uint32_t>(p[1]) << 8) | (static_cast<uint32_t>(p[2]) << 16) |
         (static_cast<uint32_t>(p[3]) << 24);
}

// Encodes a chunk_data value: [status][seq u16][offset u32][len u16][payload].
size_t build_chunk(uint8_t status, uint16_t seq, uint32_t offset, uint16_t len, const uint8_t* payload, uint8_t* out) {
  out[0] = status;
  out[1] = static_cast<uint8_t>(seq);
  out[2] = static_cast<uint8_t>(seq >> 8);
  out[3] = static_cast<uint8_t>(offset);
  out[4] = static_cast<uint8_t>(offset >> 8);
  out[5] = static_cast<uint8_t>(offset >> 16);
  out[6] = static_cast<uint8_t>(offset >> 24);
  out[7] = static_cast<uint8_t>(len);
  out[8] = static_cast<uint8_t>(len >> 8);
  if (len > 0 && payload != nullptr) std::memcpy(out + 9, payload, len);
  return kChunkHeaderBytes + len;
}

// Honest peer: answers a request ctrl with the requested slice of g_obj.
size_t peer_answer(const uint8_t ctrl[kCtrlBytes], uint8_t* out) {
  const uint32_t off = le32(ctrl + 5);
  const uint16_t len = le16(ctrl + 9);
  const uint16_t seq = le16(ctrl + 11);
  return build_chunk(0, seq, off, len, g_obj + off, out);
}

ChunkVerdict deliver(TransferFsm& fsm, const uint8_t* wire, size_t n) {
  ChunkHeader h;
  const uint8_t* payload = nullptr;
  TEST_ASSERT_TRUE(parse_chunk(wire, n, h, payload));
  return fsm.on_chunk(h, payload, g_sink);
}

// Runs request/answer rounds until the FSM stops requesting. Returns the number of rounds.
size_t pump(TransferFsm& fsm, size_t max_rounds = 100000) {
  uint8_t ctrl[kCtrlBytes];
  uint8_t wire[600];
  size_t rounds = 0;
  while (rounds < max_rounds && fsm.next_request(ctrl)) {
    TEST_ASSERT_EQUAL_UINT8(static_cast<uint8_t>(CtrlOp::Request), ctrl[0]);
    const size_t n = peer_answer(ctrl, wire);
    TEST_ASSERT_EQUAL_STRING(to_string(ChunkVerdict::Accepted), to_string(deliver(fsm, wire, n)));
    ++rounds;
  }
  return rounds;
}

}  // namespace

#define ASSERT_CHUNK(expected, actual) \
  TEST_ASSERT_EQUAL_STRING(to_string(ChunkVerdict::expected), to_string(actual))
#define ASSERT_FINISH(expected, actual) \
  TEST_ASSERT_EQUAL_STRING(to_string(FinishVerdict::expected), to_string(actual))
#define ASSERT_MANIFEST(expected, actual) \
  TEST_ASSERT_EQUAL_STRING(to_string(ManifestError::expected), to_string(actual))

static void test_manifest_encode_parse_round_trip() {
  fill_object(kObjLen);
  const Manifest m = make_manifest(kObjLen);
  uint8_t wire[kManifestBytes];
  TEST_ASSERT_EQUAL_UINT32(55, encode_manifest(m, wire));
  TEST_ASSERT_EQUAL_UINT8(1, wire[0]);
  TEST_ASSERT_EQUAL_MEMORY("diag-synth-0001", wire + 1, 15);
  TEST_ASSERT_EQUAL_UINT8(0, wire[16]);  // zero padding of the 16-byte id field
  TEST_ASSERT_EQUAL_HEX8(0xE8, wire[17]);  // 1000 little-endian
  TEST_ASSERT_EQUAL_HEX8(0x03, wire[18]);
  TEST_ASSERT_EQUAL_HEX8(0x00, wire[19]);
  TEST_ASSERT_EQUAL_HEX8(0x00, wire[20]);
  TEST_ASSERT_EQUAL_MEMORY(m.sha256, wire + 21, 32);
  TEST_ASSERT_EQUAL_HEX8(237, wire[53]);
  TEST_ASSERT_EQUAL_HEX8(0, wire[54]);

  Manifest p;
  ASSERT_MANIFEST(None, parse_manifest(wire, sizeof(wire), lab::kMaxObjectBytes, p));
  TEST_ASSERT_EQUAL_UINT8(1, p.version);
  TEST_ASSERT_EQUAL_STRING("diag-synth-0001", p.object_id);
  TEST_ASSERT_EQUAL_UINT32(kObjLen, p.declared_length);
  TEST_ASSERT_EQUAL_MEMORY(m.sha256, p.sha256, 32);
  TEST_ASSERT_EQUAL_UINT16(237, p.chunk_max);
  TEST_ASSERT_EQUAL_MEMORY(m.sha256, p.tag, 4);

  // Trailing bytes beyond the v1 layout are ignored (characteristic max_length is 64).
  uint8_t longer[64] = {};
  std::memcpy(longer, wire, sizeof(wire));
  ASSERT_MANIFEST(None, parse_manifest(longer, sizeof(longer), lab::kMaxObjectBytes, p));
}

static void test_manifest_app_denied_single_byte() {
  const uint8_t denied[1] = {0x01};
  Manifest p;
  p.declared_length = 12345;
  ASSERT_MANIFEST(AppDenied, parse_manifest(denied, 1, lab::kMaxObjectBytes, p));
  TEST_ASSERT_EQUAL_UINT32(12345, p.declared_length);  // untouched
  const uint8_t other[1] = {0x00};
  ASSERT_MANIFEST(TooShort, parse_manifest(other, 1, lab::kMaxObjectBytes, p));
  ASSERT_MANIFEST(TooShort, parse_manifest(denied, 0, lab::kMaxObjectBytes, p));
  ASSERT_MANIFEST(TooShort, parse_manifest(nullptr, 55, lab::kMaxObjectBytes, p));
}

static void test_manifest_errors() {
  fill_object(kObjLen);
  const Manifest m = make_manifest(kObjLen);
  uint8_t wire[kManifestBytes];
  Manifest p;

  encode_manifest(m, wire);
  ASSERT_MANIFEST(TooShort, parse_manifest(wire, kManifestBytes - 1, lab::kMaxObjectBytes, p));

  encode_manifest(m, wire);
  wire[0] = 2;
  ASSERT_MANIFEST(BadVersion, parse_manifest(wire, sizeof(wire), lab::kMaxObjectBytes, p));
  wire[0] = 0;
  ASSERT_MANIFEST(BadVersion, parse_manifest(wire, sizeof(wire), lab::kMaxObjectBytes, p));

  Manifest big = m;
  big.declared_length = lab::kMaxObjectBytes + 1;
  encode_manifest(big, wire);
  ASSERT_MANIFEST(TooLarge, parse_manifest(wire, sizeof(wire), lab::kMaxObjectBytes, p));
  big.declared_length = lab::kMaxObjectBytes;
  encode_manifest(big, wire);
  ASSERT_MANIFEST(None, parse_manifest(wire, sizeof(wire), lab::kMaxObjectBytes, p));
  big.declared_length = 0xFFFFFFFFu;
  encode_manifest(big, wire);
  ASSERT_MANIFEST(TooLarge, parse_manifest(wire, sizeof(wire), lab::kMaxObjectBytes, p));

  Manifest zero = m;
  zero.chunk_max = 0;
  encode_manifest(zero, wire);
  ASSERT_MANIFEST(ZeroChunk, parse_manifest(wire, sizeof(wire), lab::kMaxObjectBytes, p));
}

static void test_manifest_rejects_bad_object_ids() {
  fill_object(kObjLen);
  const Manifest m = make_manifest(kObjLen);
  uint8_t wire[kManifestBytes];
  Manifest p;
  const char* bad[] = {"../x", "..", "a/b", "a\\b", "C:x", "/etc", "a.bin", ""};
  for (const char* id : bad) {
    Manifest t = m;
    std::memset(t.object_id, 0, sizeof(t.object_id));
    std::strcpy(t.object_id, id);
    encode_manifest(t, wire);
    ASSERT_MANIFEST(BadObjectId, parse_manifest(wire, sizeof(wire), lab::kMaxObjectBytes, p));
  }
  // Non-zero bytes after the padding starts.
  encode_manifest(m, wire);
  wire[1 + 15] = 0;
  wire[1 + 3] = 0;  // "dia\0-synth-0001\0"
  ASSERT_MANIFEST(BadObjectId, parse_manifest(wire, sizeof(wire), lab::kMaxObjectBytes, p));
  // Non-ASCII byte.
  encode_manifest(m, wire);
  wire[2] = 0xC3;
  ASSERT_MANIFEST(BadObjectId, parse_manifest(wire, sizeof(wire), lab::kMaxObjectBytes, p));
  // A full 16-character id without padding is valid.
  Manifest full = m;
  std::memcpy(full.object_id, "abcdefghijklmnop", 17);
  encode_manifest(full, wire);
  ASSERT_MANIFEST(None, parse_manifest(wire, sizeof(wire), lab::kMaxObjectBytes, p));
  TEST_ASSERT_EQUAL_STRING("abcdefghijklmnop", p.object_id);
}

static void test_ctrl_encoding_byte_layout() {
  const uint8_t tag[4] = {0xAA, 0xBB, 0xCC, 0xDD};
  uint8_t out[kCtrlBytes];
  TEST_ASSERT_EQUAL_UINT32(13, encode_ctrl(out, CtrlOp::Request, tag, 0x01020304u, 0x0506, 0x0708));
  const uint8_t expected[13] = {0x01, 0xAA, 0xBB, 0xCC, 0xDD, 0x04, 0x03, 0x02, 0x01, 0x06, 0x05, 0x08, 0x07};
  TEST_ASSERT_EQUAL_HEX8_ARRAY(expected, out, 13);
  encode_ctrl(out, CtrlOp::Cancel, tag, 0, 0, 1);
  TEST_ASSERT_EQUAL_HEX8(0x02, out[0]);
  encode_ctrl(out, CtrlOp::Reset, nullptr, 0, 0, 0);
  const uint8_t reset[13] = {0x03, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0};
  TEST_ASSERT_EQUAL_HEX8_ARRAY(reset, out, 13);
}

static void test_chunk_parse() {
  const uint8_t payload[4] = {9, 8, 7, 6};
  uint8_t wire[32];
  size_t n = build_chunk(0, 0x1234, 0x00010203u, 4, payload, wire);
  ChunkHeader h;
  const uint8_t* p = nullptr;
  TEST_ASSERT_TRUE(parse_chunk(wire, n, h, p));
  TEST_ASSERT_TRUE(h.status == Status::Ok);
  TEST_ASSERT_EQUAL_HEX16(0x1234, h.seq);
  TEST_ASSERT_EQUAL_HEX32(0x00010203u, h.offset);
  TEST_ASSERT_EQUAL_UINT16(4, h.len);
  TEST_ASSERT_TRUE(p == wire + 9);
  // Length field disagrees with the payload size.
  TEST_ASSERT_FALSE(parse_chunk(wire, n - 1, h, p));
  TEST_ASSERT_FALSE(parse_chunk(wire, n + 1, h, p));
  // Shorter than the header.
  TEST_ASSERT_FALSE(parse_chunk(wire, 8, h, p));
  TEST_ASSERT_FALSE(parse_chunk(nullptr, 9, h, p));
  // Header-only status response.
  n = build_chunk(1, 7, 0, 0, nullptr, wire);
  TEST_ASSERT_TRUE(parse_chunk(wire, n, h, p));
  TEST_ASSERT_TRUE(h.status == Status::NotAuthorized);
  TEST_ASSERT_EQUAL_UINT16(0, h.len);
}

static void test_chunk_len_for_mtu() {
  TEST_ASSERT_EQUAL_UINT16(13, chunk_len_for_mtu(237, 23));    // default MTU: 23 - 1 - 9
  TEST_ASSERT_EQUAL_UINT16(237, chunk_len_for_mtu(237, 247));  // 247 - 10
  TEST_ASSERT_EQUAL_UINT16(237, chunk_len_for_mtu(1000, 247));
  TEST_ASSERT_EQUAL_UINT16(100, chunk_len_for_mtu(100, 247));  // manifest limit wins
  TEST_ASSERT_EQUAL_UINT16(507, chunk_len_for_mtu(65535, 517));
  TEST_ASSERT_EQUAL_UINT16(1, chunk_len_for_mtu(237, 11));
  TEST_ASSERT_EQUAL_UINT16(0, chunk_len_for_mtu(237, 10));     // tiny MTU: no payload fits
  TEST_ASSERT_EQUAL_UINT16(0, chunk_len_for_mtu(237, 0));
  TEST_ASSERT_EQUAL_UINT16(0, chunk_len_for_mtu(0, 247));
}

static void test_fsm_happy_path_mtu23() {
  fill_object(kObjLen);
  g_sink.clear();
  TransferFsm fsm;
  fsm.begin(make_manifest(kObjLen), 23);
  TEST_ASSERT_TRUE(fsm.running());
  TEST_ASSERT_EQUAL_UINT16(13, fsm.chunk_len());
  TEST_ASSERT_EQUAL_UINT32(kObjLen, fsm.expected());
  const size_t rounds = pump(fsm);
  TEST_ASSERT_EQUAL_UINT32((kObjLen + 12) / 13, rounds);
  TEST_ASSERT_EQUAL_UINT32(kObjLen, fsm.received());
  uint8_t ctrl[kCtrlBytes];
  TEST_ASSERT_FALSE(fsm.next_request(ctrl));  // everything received
  uint8_t digest[32];
  ASSERT_FINISH(HashMatch, fsm.finish(digest));
  char hex[65];
  Sha256::to_hex(digest, hex);
  TEST_ASSERT_EQUAL_STRING("1e9bc38cbf860b9ec31918b065f9b52476c549a782e0e7990bed8ce3868d2371", hex);
  TEST_ASSERT_EQUAL_UINT32(kObjLen, g_sink.len);
  TEST_ASSERT_EQUAL_MEMORY(g_obj, g_sink.data, kObjLen);
  TEST_ASSERT_FALSE(fsm.running());
  ASSERT_FINISH(NotRunning, fsm.finish(digest));  // finish is terminal
}

static void test_fsm_happy_path_max_object_mtu247() {
  fill_object(lab::kMaxObjectBytes);
  g_sink.clear();
  TransferFsm fsm;
  fsm.begin(make_manifest(lab::kMaxObjectBytes, 237), 247);
  TEST_ASSERT_EQUAL_UINT16(237, fsm.chunk_len());
  const size_t rounds = pump(fsm);
  TEST_ASSERT_EQUAL_UINT32((lab::kMaxObjectBytes + 236) / 237, rounds);
  uint8_t digest[32];
  ASSERT_FINISH(HashMatch, fsm.finish(digest));
  TEST_ASSERT_EQUAL_MEMORY(g_obj, g_sink.data, lab::kMaxObjectBytes);
}

static void test_fsm_requests_carry_tag_offset_and_sequence() {
  fill_object(kObjLen);
  TransferFsm fsm;
  const Manifest m = make_manifest(kObjLen);
  fsm.begin(m, 23);
  uint8_t c1[kCtrlBytes], c2[kCtrlBytes];
  TEST_ASSERT_TRUE(fsm.next_request(c1));
  TEST_ASSERT_EQUAL_MEMORY(m.tag, c1 + 1, 4);
  TEST_ASSERT_EQUAL_UINT32(0, le32(c1 + 5));
  TEST_ASSERT_EQUAL_UINT16(13, le16(c1 + 9));
  TEST_ASSERT_EQUAL_UINT16(1, le16(c1 + 11));  // sequence numbers start at 1
  // Re-requesting while outstanding repeats the offset under a new sequence number.
  TEST_ASSERT_TRUE(fsm.next_request(c2));
  TEST_ASSERT_EQUAL_UINT32(0, le32(c2 + 5));
  TEST_ASSERT_EQUAL_UINT16(2, le16(c2 + 11));
  // The answer to the superseded request is refused.
  uint8_t wire[64];
  g_sink.clear();
  size_t n = peer_answer(c1, wire);
  ASSERT_CHUNK(WrongSequence, deliver(fsm, wire, n));
  n = peer_answer(c2, wire);
  ASSERT_CHUNK(Accepted, deliver(fsm, wire, n));
  // Final short chunk requests only the remainder.
  TransferFsm small;
  fill_object(20);
  small.begin(make_manifest(20), 23);
  TEST_ASSERT_TRUE(small.next_request(c1));
  n = peer_answer(c1, wire);
  ASSERT_CHUNK(Accepted, deliver(small, wire, n));
  TEST_ASSERT_TRUE(small.next_request(c1));
  TEST_ASSERT_EQUAL_UINT32(13, le32(c1 + 5));
  TEST_ASSERT_EQUAL_UINT16(7, le16(c1 + 9));
}

static void test_fsm_duplicate_chunk() {
  fill_object(kObjLen);
  g_sink.clear();
  TransferFsm fsm;
  fsm.begin(make_manifest(kObjLen), 23);
  uint8_t ctrl[kCtrlBytes], wire[64];
  TEST_ASSERT_TRUE(fsm.next_request(ctrl));
  const size_t n = peer_answer(ctrl, wire);
  ASSERT_CHUNK(Accepted, deliver(fsm, wire, n));
  ASSERT_CHUNK(Duplicate, deliver(fsm, wire, n));  // re-delivered
  TEST_ASSERT_EQUAL_UINT32(13, fsm.received());
  TEST_ASSERT_EQUAL_UINT32(13, g_sink.len);
  TEST_ASSERT_TRUE(fsm.running());
  pump(fsm);
  uint8_t digest[32];
  ASSERT_FINISH(HashMatch, fsm.finish(digest));
}

static void test_fsm_out_of_order_offset() {
  fill_object(kObjLen);
  g_sink.clear();
  TransferFsm fsm;
  fsm.begin(make_manifest(kObjLen), 23);
  uint8_t ctrl[kCtrlBytes], wire[64];
  TEST_ASSERT_TRUE(fsm.next_request(ctrl));
  size_t n = build_chunk(0, le16(ctrl + 11), 13, 13, g_obj + 13, wire);  // skips ahead
  ASSERT_CHUNK(OutOfOrder, deliver(fsm, wire, n));
  n = build_chunk(0, le16(ctrl + 11), 5, 13, g_obj + 5, wire);  // overlaps
  ASSERT_CHUNK(OutOfOrder, deliver(fsm, wire, n));
  TEST_ASSERT_EQUAL_UINT32(0, fsm.received());
  TEST_ASSERT_EQUAL_UINT32(0, g_sink.len);
  n = peer_answer(ctrl, wire);
  ASSERT_CHUNK(Accepted, deliver(fsm, wire, n));
}

static void test_fsm_wrong_sequence() {
  fill_object(kObjLen);
  g_sink.clear();
  TransferFsm fsm;
  fsm.begin(make_manifest(kObjLen), 23);
  uint8_t ctrl[kCtrlBytes], wire[64];
  // Unsolicited chunk (no request outstanding).
  size_t n = build_chunk(0, 1, 0, 13, g_obj, wire);
  ASSERT_CHUNK(WrongSequence, deliver(fsm, wire, n));
  TEST_ASSERT_TRUE(fsm.next_request(ctrl));
  n = build_chunk(0, static_cast<uint16_t>(le16(ctrl + 11) + 5), 0, 13, g_obj, wire);
  ASSERT_CHUNK(WrongSequence, deliver(fsm, wire, n));
  TEST_ASSERT_EQUAL_UINT32(0, g_sink.len);
  n = peer_answer(ctrl, wire);
  ASSERT_CHUNK(Accepted, deliver(fsm, wire, n));
}

static void test_fsm_overflow_beyond_declared_length() {
  fill_object(64);
  g_sink.clear();
  TransferFsm fsm;
  fsm.begin(make_manifest(20), 23);  // declared 20 bytes, chunk 13
  uint8_t ctrl[kCtrlBytes], wire[64];
  TEST_ASSERT_TRUE(fsm.next_request(ctrl));
  size_t n = peer_answer(ctrl, wire);
  ASSERT_CHUNK(Accepted, deliver(fsm, wire, n));
  TEST_ASSERT_TRUE(fsm.next_request(ctrl));  // asks for the last 7 bytes
  n = build_chunk(0, le16(ctrl + 11), 13, 13, g_obj + 13, wire);  // peer sends 13
  ASSERT_CHUNK(Overflow, deliver(fsm, wire, n));
  TEST_ASSERT_FALSE(fsm.running());
  TEST_ASSERT_EQUAL_UINT32(13, g_sink.len);  // nothing beyond the declared length reached the sink
  uint8_t digest[32];
  ASSERT_FINISH(NotRunning, fsm.finish(digest));
}

static void test_fsm_wrong_length() {
  fill_object(kObjLen);
  g_sink.clear();
  TransferFsm fsm;
  fsm.begin(make_manifest(kObjLen), 23);
  uint8_t ctrl[kCtrlBytes], wire[64];
  TEST_ASSERT_TRUE(fsm.next_request(ctrl));
  size_t n = build_chunk(0, le16(ctrl + 11), 0, 0, nullptr, wire);  // empty OK chunk
  ASSERT_CHUNK(WrongLength, deliver(fsm, wire, n));
  n = build_chunk(0, le16(ctrl + 11), 0, 14, g_obj, wire);  // larger than the negotiated chunk
  ASSERT_CHUNK(WrongLength, deliver(fsm, wire, n));
  TEST_ASSERT_TRUE(fsm.running());
  // A shorter (non-empty) chunk is accepted and the next request continues from it.
  n = build_chunk(0, le16(ctrl + 11), 0, 5, g_obj, wire);
  ASSERT_CHUNK(Accepted, deliver(fsm, wire, n));
  TEST_ASSERT_TRUE(fsm.next_request(ctrl));
  TEST_ASSERT_EQUAL_UINT32(5, le32(ctrl + 5));
}

static void test_fsm_length_mismatch_and_incomplete_at_finish() {
  fill_object(kObjLen);
  g_sink.clear();
  TransferFsm fsm;
  fsm.begin(make_manifest(kObjLen), 23);
  uint8_t ctrl[kCtrlBytes], wire[64], digest[32];
  TEST_ASSERT_TRUE(fsm.next_request(ctrl));
  ASSERT_FINISH(Incomplete, fsm.finish(digest));  // request in flight: not terminal
  TEST_ASSERT_TRUE(fsm.running());
  size_t n = peer_answer(ctrl, wire);
  ASSERT_CHUNK(Accepted, deliver(fsm, wire, n));
  ASSERT_FINISH(LengthMismatch, fsm.finish(digest));  // 13 of 1000 bytes
  TEST_ASSERT_FALSE(fsm.running());
  TEST_ASSERT_FALSE(fsm.next_request(ctrl));
  // The digest written is that of the bytes actually received.
  uint8_t partial[32];
  Sha256 h;
  h.update(g_obj, 13);
  h.finish(partial);
  TEST_ASSERT_EQUAL_MEMORY(partial, digest, 32);
}

static void test_fsm_hash_mismatch() {
  fill_object(kObjLen);
  g_sink.clear();
  Manifest m = make_manifest(kObjLen);
  m.sha256[31] ^= 0x01;  // manifest disagrees with the bytes served
  TransferFsm fsm;
  fsm.begin(m, 247);
  pump(fsm);
  TEST_ASSERT_EQUAL_UINT32(kObjLen, fsm.received());
  uint8_t digest[32];
  ASSERT_FINISH(HashMismatch, fsm.finish(digest));
  TEST_ASSERT_FALSE(fsm.running());
}

static void test_fsm_resume_via_absorb_existing() {
  fill_object(kObjLen);
  const Manifest m = make_manifest(kObjLen);
  // First attempt: 3 chunks reach the .partial sink, then the link drops.
  g_sink.clear();
  TransferFsm first;
  first.begin(m, 23);
  TEST_ASSERT_EQUAL_UINT32(3, pump(first, 3));
  TEST_ASSERT_EQUAL_UINT32(39, g_sink.len);
  static uint8_t partial[65536];
  const size_t partial_len = g_sink.len;
  std::memcpy(partial, g_sink.data, partial_len);

  // Resume with an unchanged manifest and a larger MTU; the sink keeps appending to the partial file.
  TransferFsm second;
  second.begin(m, 247);
  TEST_ASSERT_TRUE(second.absorb_existing(partial, 20));
  TEST_ASSERT_TRUE(second.absorb_existing(partial + 20, partial_len - 20));
  TEST_ASSERT_TRUE(second.absorb_existing(partial, 0));
  TEST_ASSERT_EQUAL_UINT32(39, second.received());
  uint8_t ctrl[kCtrlBytes];
  TEST_ASSERT_TRUE(second.next_request(ctrl));
  TEST_ASSERT_EQUAL_UINT32(39, le32(ctrl + 5));
  TEST_ASSERT_FALSE(second.absorb_existing(partial, 1));  // only before the first request
  uint8_t wire[600];
  size_t n = peer_answer(ctrl, wire);
  ASSERT_CHUNK(Accepted, deliver(second, wire, n));
  pump(second);
  uint8_t digest[32];
  ASSERT_FINISH(HashMatch, second.finish(digest));
  TEST_ASSERT_EQUAL_UINT32(kObjLen, g_sink.len);
  TEST_ASSERT_EQUAL_MEMORY(g_obj, g_sink.data, kObjLen);

  // More stored bytes than declared cannot be absorbed.
  TransferFsm third;
  third.begin(make_manifest(10), 23);
  TEST_ASSERT_FALSE(third.absorb_existing(g_obj, 11));
  TEST_ASSERT_TRUE(third.absorb_existing(g_obj, 10));
  TEST_ASSERT_FALSE(third.next_request(ctrl));  // already complete
  ASSERT_FINISH(HashMatch, third.finish(digest));

  // Corrupted stored bytes are caught by the final hash.
  g_sink.clear();
  TransferFsm fourth;
  fourth.begin(m, 247);
  partial[0] ^= 0xFF;
  TEST_ASSERT_TRUE(fourth.absorb_existing(partial, partial_len));
  pump(fourth);
  ASSERT_FINISH(HashMismatch, fourth.finish(digest));
}

static void test_fsm_cancel() {
  fill_object(kObjLen);
  g_sink.clear();
  TransferFsm fsm;
  fsm.begin(make_manifest(kObjLen), 23);
  uint8_t ctrl[kCtrlBytes], wire[64], digest[32];
  TEST_ASSERT_TRUE(fsm.next_request(ctrl));
  const size_t n = peer_answer(ctrl, wire);
  fsm.cancel();
  TEST_ASSERT_TRUE(fsm.cancelled());
  TEST_ASSERT_FALSE(fsm.running());
  ASSERT_CHUNK(Cancelled, deliver(fsm, wire, n));  // late answer is dropped
  TEST_ASSERT_EQUAL_UINT32(0, g_sink.len);
  TEST_ASSERT_FALSE(fsm.next_request(ctrl));
  ASSERT_FINISH(NotRunning, fsm.finish(digest));
  TEST_ASSERT_FALSE(fsm.absorb_existing(g_obj, 1));
  // begin() starts a fresh attempt.
  fsm.begin(make_manifest(kObjLen), 23);
  TEST_ASSERT_FALSE(fsm.cancelled());
  TEST_ASSERT_TRUE(fsm.running());
}

static void test_fsm_peer_status_codes() {
  fill_object(kObjLen);
  struct Row {
    uint8_t status;
    ChunkVerdict expected;
  };
  const Row rows[] = {
      {static_cast<uint8_t>(Status::NotAuthorized), ChunkVerdict::Denied},
      {static_cast<uint8_t>(Status::StaleObject), ChunkVerdict::Stale},
      {static_cast<uint8_t>(Status::Cancelled), ChunkVerdict::Cancelled},
      {static_cast<uint8_t>(Status::BadRequest), ChunkVerdict::PeerError},
      {static_cast<uint8_t>(Status::OutOfRange), ChunkVerdict::PeerError},
      {static_cast<uint8_t>(Status::NoRequest), ChunkVerdict::PeerError},
      {0x7F, ChunkVerdict::PeerError},
  };
  for (const Row& r : rows) {
    g_sink.clear();
    TransferFsm fsm;
    fsm.begin(make_manifest(kObjLen), 23);
    uint8_t ctrl[kCtrlBytes], wire[64];
    TEST_ASSERT_TRUE(fsm.next_request(ctrl));
    const size_t n = build_chunk(r.status, le16(ctrl + 11), 0, 0, nullptr, wire);
    TEST_ASSERT_EQUAL_STRING(to_string(r.expected), to_string(deliver(fsm, wire, n)));
    TEST_ASSERT_FALSE(fsm.running());
    TEST_ASSERT_FALSE(fsm.cancelled());
    TEST_ASSERT_EQUAL_UINT32(0, g_sink.len);
  }
}

static void test_fsm_sink_error_and_not_running() {
  fill_object(kObjLen);
  g_sink.clear();
  TransferFsm idle;
  uint8_t ctrl[kCtrlBytes], wire[64], digest[32];
  TEST_ASSERT_FALSE(idle.running());
  TEST_ASSERT_FALSE(idle.next_request(ctrl));
  size_t n = build_chunk(0, 1, 0, 13, g_obj, wire);
  ASSERT_CHUNK(NotRunning, deliver(idle, wire, n));
  ASSERT_FINISH(NotRunning, idle.finish(digest));

  TransferFsm fsm;
  fsm.begin(make_manifest(kObjLen), 23);
  TEST_ASSERT_TRUE(fsm.next_request(ctrl));
  n = peer_answer(ctrl, wire);
  g_sink.fail = true;
  ASSERT_CHUNK(SinkError, deliver(fsm, wire, n));
  TEST_ASSERT_FALSE(fsm.running());
  TEST_ASSERT_EQUAL_UINT32(0, fsm.received());
  g_sink.clear();
}

static void test_fsm_tiny_mtu_and_empty_object() {
  fill_object(kObjLen);
  TransferFsm fsm;
  fsm.begin(make_manifest(kObjLen), 10);  // no payload fits
  TEST_ASSERT_FALSE(fsm.running());
  TEST_ASSERT_EQUAL_UINT16(0, fsm.chunk_len());
  uint8_t ctrl[kCtrlBytes], digest[32];
  TEST_ASSERT_FALSE(fsm.next_request(ctrl));

  // A zero-length object needs no request and verifies against the empty-input hash.
  TransferFsm empty;
  empty.begin(make_manifest(0), 23);
  TEST_ASSERT_TRUE(empty.running());
  TEST_ASSERT_FALSE(empty.next_request(ctrl));
  ASSERT_FINISH(HashMatch, empty.finish(digest));
}

static void test_artifact_file_name() {
  char out[32];
  TEST_ASSERT_TRUE(artifact_file_name("diag-synth-0001", false, out, sizeof(out)));
  TEST_ASSERT_EQUAL_STRING("diag-synth-0001.bin", out);
  TEST_ASSERT_TRUE(artifact_file_name("diag-synth-0001", true, out, sizeof(out)));
  TEST_ASSERT_EQUAL_STRING("diag-synth-0001.partial", out);
  const char* bad[] = {"../x", "..", "a/b", "a\\b", "C:x", "/etc", "x.bin", "", "abcdefghijklmnopq"};
  for (const char* id : bad) {
    std::strcpy(out, "untouched");
    TEST_ASSERT_FALSE_MESSAGE(artifact_file_name(id, false, out, sizeof(out)), id);
    TEST_ASSERT_EQUAL_STRING("", out);
  }
  TEST_ASSERT_FALSE(artifact_file_name(nullptr, false, out, sizeof(out)));
  char exact[20];  // "diag-synth-0001.bin" = 19 + NUL
  TEST_ASSERT_TRUE(artifact_file_name("diag-synth-0001", false, exact, sizeof(exact)));
  TEST_ASSERT_FALSE(artifact_file_name("diag-synth-0001", false, exact, 19));
  TEST_ASSERT_FALSE(artifact_file_name("diag-synth-0001", true, exact, sizeof(exact)));
}

static void test_transfer_names() {
  TEST_ASSERT_EQUAL_STRING("app_denied", to_string(ManifestError::AppDenied));
  TEST_ASSERT_EQUAL_STRING("hash_match", to_string(FinishVerdict::HashMatch));
  TEST_ASSERT_EQUAL_STRING("length_mismatch", to_string(FinishVerdict::LengthMismatch));
  TEST_ASSERT_EQUAL_STRING("out_of_order", to_string(ChunkVerdict::OutOfOrder));
  TEST_ASSERT_EQUAL_STRING("sink_error", to_string(ChunkVerdict::SinkError));
}

void run_transfer_tests() {
  RUN_TEST(test_manifest_encode_parse_round_trip);
  RUN_TEST(test_manifest_app_denied_single_byte);
  RUN_TEST(test_manifest_errors);
  RUN_TEST(test_manifest_rejects_bad_object_ids);
  RUN_TEST(test_ctrl_encoding_byte_layout);
  RUN_TEST(test_chunk_parse);
  RUN_TEST(test_chunk_len_for_mtu);
  RUN_TEST(test_fsm_happy_path_mtu23);
  RUN_TEST(test_fsm_happy_path_max_object_mtu247);
  RUN_TEST(test_fsm_requests_carry_tag_offset_and_sequence);
  RUN_TEST(test_fsm_duplicate_chunk);
  RUN_TEST(test_fsm_out_of_order_offset);
  RUN_TEST(test_fsm_wrong_sequence);
  RUN_TEST(test_fsm_overflow_beyond_declared_length);
  RUN_TEST(test_fsm_wrong_length);
  RUN_TEST(test_fsm_length_mismatch_and_incomplete_at_finish);
  RUN_TEST(test_fsm_hash_mismatch);
  RUN_TEST(test_fsm_resume_via_absorb_existing);
  RUN_TEST(test_fsm_cancel);
  RUN_TEST(test_fsm_peer_status_codes);
  RUN_TEST(test_fsm_sink_error_and_not_running);
  RUN_TEST(test_fsm_tiny_mtu_and_empty_object);
  RUN_TEST(test_artifact_file_name);
  RUN_TEST(test_transfer_names);
}
