// Adaptive paging of observation arrays into evidence events (host tested).
//
// EvidenceStore drops any serialized event line longer than cfg::kMaxEventBytes, and it prefixes an
// envelope (<= kEventEnvelopeReserve bytes with maximal ids) to the body the caller builds. A page is
// therefore built in a scratch buffer of kEventScratchBytes, and array elements are appended one at a
// time until the next one would exceed the page's array budget. Page boundaries depend only on the
// bytes of the elements, so a dry run (plan_pages) predicts exactly what the real run will write.
#pragma once

#include <cstddef>
#include <cstdint>

#include "oyster_core/config.h"
#include "oyster_core/json_writer.h"

namespace oyster {

constexpr size_t kEventEnvelopeReserve = 256;
constexpr size_t kEventScratchBytes = cfg::kMaxEventBytes - kEventEnvelopeReserve;  // 1792
// Worst-case measured size of the event header written by begin_event() up to and including `"details":{`
// (maximal target id, operation name, timestamp and counters). Checked by a host test.
constexpr size_t kEventHeaderBudget = 440;
// Page fields written before the array plus the closing of array, details and envelope.
constexpr size_t kPageFieldsBudget = 120;
constexpr size_t kPageArrayBytes = kEventScratchBytes - kEventHeaderBudget - kPageFieldsBudget;

// Appends elements first, first+1, ... of an already-open JSON array in `w` (backed by `buf`) while the
// array content stays within `array_budget` bytes. `write_item(w, index)` must write exactly one array
// element. An element that does not fit is removed again (w is restored). One that cannot fit even an
// empty page is skipped and counted in `dropped`. Returns the index of the first element NOT written to
// this page (== count when all are done). Never returns `first` unless w is unusable or count <= first.
template <typename WriteItem>
size_t fill_array_page(JsonWriter& w, char* buf, size_t array_budget, size_t first, size_t count,
                       WriteItem&& write_item, size_t& dropped) {
  const size_t start = w.size();
  size_t i = first;
  while (i < count && w.ok()) {
    const JsonWriter saved = w;
    write_item(w, i);
    if (w.ok() && (w.size() - start) <= array_budget) {
      ++i;
      continue;
    }
    w = saved;
    buf[w.size()] = '\0';
    if (w.size() == start) {  // alone it does not fit an empty page: cannot be stored
      ++dropped;
      ++i;
      continue;
    }
    break;
  }
  return i;
}

struct PagePlan {
  size_t pages = 0;
  size_t dropped = 0;  // elements that cannot be stored even alone in a page
};

// Dry run of fill_array_page over `count` elements, using `scratch` (>= kEventScratchBytes) as workspace.
template <typename WriteItem>
PagePlan plan_pages(char* scratch, size_t array_budget, size_t count, WriteItem&& write_item) {
  PagePlan p;
  size_t i = 0;
  while (i < count) {
    JsonWriter w(scratch, kEventScratchBytes);
    w.begin_array();
    const size_t before = i;
    const size_t dropped_before = p.dropped;
    i = fill_array_page(w, scratch, array_budget, i, count, write_item, p.dropped);
    if (i > before && (i - before) > (p.dropped - dropped_before)) ++p.pages;  // page holds >= 1 element
    if (i == before) break;  // w unusable: nothing more can be planned
  }
  return p;
}

}  // namespace oyster
