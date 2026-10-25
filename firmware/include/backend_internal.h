// Diagnostics of the backend link that the worker and the UI may show (no secrets). Private to
// the backend-link modules; the public API is backend_client.h. Implementation: backend_client.cpp.
#pragma once

#include <cstdint>

namespace oyster::backend_detail {

const char* last_error();        // human-readable reason for the current state; "" when none
int last_http_status();          // last HTTP status, or a negative transport error code
const char* last_error_code();   // "error" field of the last error body (e.g. "events_missing")
bool last_submit_permanent();    // the last failed result submission will never be accepted
const char* job_parse_error();   // "" when the last polled job parsed cleanly
uint32_t rejected_events();      // single events the backend refused (skipped; kept on SD)
bool session_registered(const char* session_id);
bool session_conflict();         // the active session id is owned by another device (HTTP 409)

}  // namespace oyster::backend_detail
