// Upload bookkeeping of one session (see upload_ledger.h).
#include "oyster_core/upload_ledger.h"

#include <cstring>

namespace oyster {

void UploadLedger::reset() { *this = UploadLedger(); }

void UploadLedger::restore(uint32_t acked, uint32_t resolved, uint32_t skipped) {
  reset();
  acked_ = acked;
  cursor_ = resolved < acked ? acked : resolved;
  skipped_ = skipped;
  floor_ = cursor_;  // which of the older sequences were refused or never sent is not restored: never citable
}

void UploadLedger::add_hole(uint32_t first, uint32_t last, bool refused) {
  if (first == 0 || first > last) return;
  if (n_ > 0 && holes_[n_ - 1].refused == refused && holes_[n_ - 1].last + 1 == first) {
    holes_[n_ - 1].last = last;
    return;
  }
  if (n_ == kMaxHoles) {  // forget the oldest: its sequences become "unknown" (never citable)
    floor_ = holes_[0].last;
    std::memmove(holes_, holes_ + 1, (kMaxHoles - 1) * sizeof(Hole));
    --n_;
  }
  holes_[n_++] = Hole{first, last, refused};
}

bool UploadLedger::accept(uint32_t first, uint32_t last, uint32_t last_written) {
  if (last > last_written) last = last_written;
  if (first == 0 || first > last || last <= cursor_) return false;
  if (first > cursor_ + 1) add_hole(cursor_ + 1, first - 1, false);  // never sent
  cursor_ = last;
  if (last > acked_) acked_ = last;
  return true;
}

bool UploadLedger::skip(uint32_t seq, uint32_t last_written) {
  if (seq == 0 || seq > last_written || seq <= cursor_) return false;
  if (seq > cursor_ + 1) add_hole(cursor_ + 1, seq - 1, false);
  add_hole(seq, seq, true);
  ++skipped_;
  cursor_ = seq;  // upload moves on; acked_ is deliberately not raised
  return true;
}

EventFate UploadLedger::fate(uint32_t seq, uint32_t last_written) const {
  if (seq == 0 || seq > last_written || seq <= floor_) return EventFate::Lost;
  for (size_t i = 0; i < n_; ++i) {
    if (seq >= holes_[i].first && seq <= holes_[i].last) return holes_[i].refused ? EventFate::Skipped : EventFate::Lost;
  }
  return seq <= cursor_ ? EventFate::Accepted : EventFate::Pending;
}

}  // namespace oyster
