// Completeness check for the final, unterminated record of an events.jsonl file (used by scan_jsonl and by
// the evidence store when it reports a recovery). Kept apart from jsonl.h so that file's format stays unchanged.
#pragma once

#include <cstddef>
#include <cstdint>

namespace oyster {

// True when the bytes are exactly one balanced JSON object: braces and brackets outside strings pair up and
// the outermost '}' is the last byte. Tells a record that only lacks its '\n' from one cut off at a nested '}'.
bool is_balanced_json_object(const uint8_t* bytes, size_t n);

}  // namespace oyster
