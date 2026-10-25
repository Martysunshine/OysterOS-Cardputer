// Upload bookkeeping of ONE session (pure, host-tested; the evidence store owns one instance).
//
//   cursor (resolved) - every sequence at or below it is accepted, skipped or lost (never sent)
//   acked             - highest sequence the backend ACCEPTED; a refused (skipped) event never raises it
//   skipped           - events the backend refused permanently: kept on the card, never acknowledged
//
// Sequences at or below the cursor that the backend did not accept are remembered as "holes" (refused events and
// gaps that were never sent, e.g. overwritten in the volatile ring). Only the newest kMaxHoles are kept; older
// ones fall below hole_floor() and are answered as Lost, so an id is never reported accepted without proof.
#pragma once

#include <cstddef>
#include <cstdint>

#include "oyster_core/delivery_policy.h"

namespace oyster {

class UploadLedger {
 public:
  static constexpr size_t kMaxHoles = 16;
  struct Hole {
    uint32_t first;
    uint32_t last;
    bool refused;  // true: the backend refused it; false: never sent
  };

  void reset();
  // A resumed session: counters come from its manifest, which side each older sequence fell on is unknown.
  void restore(uint32_t acked, uint32_t resolved, uint32_t skipped);

  // The backend accepted the contiguous batch first..last. Sequences between the cursor and `first` were never
  // sent and become a hole. last_written = highest sequence assigned so far. False when ignored (stale or invalid).
  bool accept(uint32_t first, uint32_t last, uint32_t last_written);
  // The backend refused the single event `seq` for good. The cursor moves past it; acked() does not.
  bool skip(uint32_t seq, uint32_t last_written);

  uint32_t acked() const { return acked_; }
  uint32_t resolved() const { return cursor_; }
  uint32_t skipped() const { return skipped_; }
  uint32_t hole_floor() const { return floor_; }
  size_t hole_count() const { return n_; }
  const Hole& hole(size_t i) const { return holes_[i]; }

  // Accepted / Skipped / Lost for sequences the cursor has passed; Pending for later ones - the caller turns
  // Pending into Lost when the event is no longer stored (RAM ring overwrote it). Never returns ForeignSession.
  EventFate fate(uint32_t seq, uint32_t last_written) const;

 private:
  void add_hole(uint32_t first, uint32_t last, bool refused);

  uint32_t acked_ = 0;
  uint32_t cursor_ = 0;
  uint32_t skipped_ = 0;
  uint32_t floor_ = 0;
  Hole holes_[kMaxHoles] = {};
  size_t n_ = 0;
};

}  // namespace oyster
