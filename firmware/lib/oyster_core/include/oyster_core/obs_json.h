// JSON encoding of single observations as elements of an evidence-event array (host tested, so the
// worst-case size of one element is known and checked against the page budget).
#pragma once

#include "oyster_core/json_writer.h"
#include "oyster_core/types.h"

namespace oyster {

// Each function writes exactly one unkeyed object element into an open array.
void write_ap_json(JsonWriter& w, const ApObservation& ap);
void write_adv_json(JsonWriter& w, const BleAdvObservation& o);
void write_gatt_entry_json(JsonWriter& w, const GattEntry& e);   // parent: 0xFF -> null

}  // namespace oyster
