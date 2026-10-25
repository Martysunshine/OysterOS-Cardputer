// Bounded, filesystem-safe identifiers. Every id that reaches a path or a JSON key is validated here.
#pragma once

#include <cstddef>
#include <cstdint>

#include "oyster_core/config.h"

namespace oyster {

// True when every character is [A-Za-z0-9_-] and 1 <= length <= max_len.
bool is_safe_id_chars(const char* s, size_t max_len = cfg::kMaxIdLen);

// "<prefix>" followed by 1..40 safe characters, e.g. prefix "sess-".
bool is_prefixed_id(const char* s, const char* prefix);

bool is_valid_session_id(const char* s);  // ^sess-[0-9a-zA-Z_-]{1,40}$
bool is_valid_job_id(const char* s);      // ^job-...
bool is_valid_target_id(const char* s);   // ^target-...
bool is_valid_device_id(const char* s);   // ^[0-9a-zA-Z_-]{1,40}$
bool is_valid_nonce(const char* s);       // ^[0-9a-zA-Z_-]{1,40}$

// Lab object ids (diagnostic transfer). Rejects empty strings, '/', '\\', "..", ':' and anything
// outside [A-Za-z0-9_-]; maximum 16 characters (the manifest field width).
bool is_valid_object_id(const char* s);

// "evt-" + sequence zero-padded to at least 6 digits. False if the buffer is too small.
bool format_event_id(uint32_t sequence, char* out, size_t n);

// "boot-" + 16 lowercase hex digits from two random words.
bool format_boot_id(uint32_t r1, uint32_t r2, char* out, size_t n);

// Copy with guaranteed NUL termination. Returns false (and copies a truncated prefix) when src does not fit.
bool copy_bounded(char* dst, size_t dst_len, const char* src);

}  // namespace oyster
