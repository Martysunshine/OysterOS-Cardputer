// IR transmitter of the Cardputer ADV (Smart Home tile, docs/HOME_ASSISTANT.md for the sibling integration).
// One LED, no receiver: there is no learn mode and no feedback, so a send never tells whether the device obeyed.
//
// The LED pin is the one the M5Stack Cardputer example uses (GPIO44, the UART0 RX pin, which is free because
// the serial console is the native USB CDC). UNVERIFIED on this unit until a TV reacts.
#pragma once

#include <cstdint>

#include "oyster_core/ir_codes.h"

namespace oyster::ir {

constexpr int kTxPin = 44;

// Sends one key press. Worker task only: it blocks for the frame (about 70 ms for NEC/Samsung32/RC5, about 140 ms
// for SIRC, which sends three frames). The carrier comes from the LEDC peripheral, no timer interrupt is
// installed, and the pin is parked low (and detached from the peripheral) when the frame is done.
// Returns false when the frame is not valid for its protocol; nothing is sent then.
bool send(const ircodes::Frame& frame);

}  // namespace oyster::ir
