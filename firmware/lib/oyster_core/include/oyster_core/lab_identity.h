// Identity of the lab fixture and resolution of an enrolled target to one nearby device.
// Both are platform independent and host tested. Neither is authentication: the profile's device_info
// value is public, so a match is an application-level consistency check, and an advertised name or
// service UUID is only a hint for choosing which device to dial.
#pragma once

#include <cstddef>
#include <cstdint>

#include "oyster_core/local_policy.h"
#include "oyster_core/types.h"

namespace oyster {

// "<profile_id>;<profile_version>" as served by the public device_info characteristic, built from the
// generated profile header (e.g. "oyster-lab-v1;1"). Returns the length without NUL; 0 if out is too small.
size_t lab_device_info_expected(char* out, size_t out_len);

// True only for the exact expected string. One trailing NUL is tolerated (some stacks return the
// terminator); anything else - the bare profile id, extra bytes, a second NUL, empty or long values -
// is a mismatch. The value is public, so this is not authentication.
bool lab_device_info_matches(const uint8_t* v, size_t n);

// Reads started by a REMOTE (backend) job are limited to characteristics whose profile read policy is
// public and whose read has no side effect. Protected characteristics belong to the gated access-policy
// assertions. app_auth is "public" in the profile, but every read issues a fresh challenge and so
// invalidates the single outstanding one: it is refused too. Unknown ids are refused. Reads started at
// the device by the person holding it are not subject to this check.
bool lab_remote_read_allowed(const char* characteristic_id);

// How a target was matched to a device. EnrolledAddress is the only rule that names one specific
// device; ServiceUuid and NameHint are hints that may match several devices.
enum class TargetMatch : uint8_t { EnrolledAddress, ServiceUuid, NameHint };
const char* to_string(TargetMatch m);  // "address", "service_uuid", "name_hint"

enum class ResolveResult : uint8_t { Resolved, NotObserved, Ambiguous };

struct TargetResolution {
  ResolveResult result;
  size_t candidates;        // devices that matched (EnrolledAddress: 1)
  TargetMatch rule;         // rule of the single candidate; meaningful when result == Resolved
  uint8_t address[6];       // most significant byte first; valid when result == Resolved
  BleAddrType address_type;
};

// Fail-closed candidate selection. With an enrolled address only that address (and type) is a candidate
// and discovery rows are ignored. Otherwise every discovery row that advertises the enrolled service
// UUID or carries the exact advertised name hint is a candidate (union, so a look-alike is never
// silently preferred); non-connectable devices cannot be dialled and are skipped. Exactly one
// candidate resolves; two or more are ambiguous and must not be connected to.
class TargetResolver {
 public:
  explicit TargetResolver(const EnrolledTarget& t);
  void add(const BleAdvObservation& o);   // call for every row of the latest discovery
  TargetResolution finish() const;

 private:
  const EnrolledTarget& t_;
  size_t candidates_ = 0;
  TargetMatch rule_ = TargetMatch::NameHint;
  uint8_t address_[6] = {};
  BleAddrType address_type_ = BleAddrType::Unknown;
};

}  // namespace oyster
