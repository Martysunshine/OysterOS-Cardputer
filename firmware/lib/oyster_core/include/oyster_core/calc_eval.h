// Pure, host-testable arithmetic evaluator for the Scratchpad module (see firmware/src/modules/scratch.cpp).
// A small recursive-descent parser over a C string: + - * / % (modulo), parentheses, decimals, and unary
// +/-. No Arduino headers, no allocation, no globals, no locale: the Scratchpad works with the laptop off,
// same spirit as the Wi-Fi recon and Scripts tools. Everything here is a function of its arguments only.
//
// The input is whatever the user typed on the Cardputer keyboard, so it is untrusted: eval() tolerates empty
// input, trailing junk, unbalanced parentheses, division or modulo by zero, and overflow, and reports each as
// a short message instead of returning a wrong number.
#pragma once

#include <cstddef>

namespace oyster::calc {

// Evaluate the expression in `expr`. On success returns true and (when `out` is non-null) writes the value to
// *out. On failure returns false and writes a short human-readable reason to `err` (NUL-terminated, clipped to
// errcap-1): "Empty", "Bad expression", "Missing )", "Divide by zero" or "Out of range". `err` may be null.
bool eval(const char* expr, double* out, char* err, size_t errcap);

// Format a result for the small display: up to 10 significant figures, trailing zeros trimmed, scientific
// notation for very large or very small magnitudes (the same shape %.10g gives). Writes to `out`, clipped to
// cap-1 and always NUL-terminated. A non-finite value (which eval() never returns) prints "error".
void format(double value, char* out, size_t cap);

}  // namespace oyster::calc
