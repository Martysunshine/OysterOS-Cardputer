#include "oyster_core/lab_identity.h"

#include <cstdio>
#include <cstring>

#include "oyster_core/lab_profile_gen.h"

namespace oyster {

size_t lab_device_info_expected(char* out, size_t out_len) {
  if (out == nullptr || out_len == 0) return 0;
  out[0] = '\0';
  const int n = std::snprintf(out, out_len, "%s;%lu", lab::kProfileId, static_cast<unsigned long>(lab::kProfileVersion));
  if (n <= 0 || static_cast<size_t>(n) >= out_len) {
    out[0] = '\0';
    return 0;
  }
  return static_cast<size_t>(n);
}

bool lab_device_info_matches(const uint8_t* v, size_t n) {
  char expected[48];
  const size_t len = lab_device_info_expected(expected, sizeof(expected));
  if (len == 0 || v == nullptr) return false;
  if (n == len + 1 && v[len] != 0) return false;
  if (n != len && n != len + 1) return false;
  return std::memcmp(v, expected, len) == 0;
}

bool lab_remote_read_allowed(const char* characteristic_id) {
  const lab::CharDef* def = lab::find_char(characteristic_id);
  if (def == nullptr || def->read != lab::Policy::Public) return false;
  if (std::strcmp(def->id, "app_auth") == 0) return false;  // a read consumes the outstanding challenge
  return true;
}

const char* to_string(TargetMatch m) {
  switch (m) {
    case TargetMatch::EnrolledAddress: return "address";
    case TargetMatch::ServiceUuid:     return "service_uuid";
    case TargetMatch::NameHint:        return "name_hint";
  }
  return "address";
}

TargetResolver::TargetResolver(const EnrolledTarget& t) : t_(t) {}

void TargetResolver::add(const BleAdvObservation& o) {
  if (t_.has_address) return;               // an enrolled address is the only candidate
  if (o.connectable.is_false()) return;     // cannot be dialled

  bool by_uuid = false;
  if (t_.has_service_uuid) {
    for (uint8_t k = 0; k < o.svc_uuid_count && k < cfg::kMaxSvcUuidsPerAdv; ++k) {
      if (std::memcmp(o.svc_uuid128[k], t_.service_uuid128, 16) == 0) {
        by_uuid = true;
        break;
      }
    }
  }
  bool by_name = false;
  const size_t nl = std::strlen(t_.advertised_name);
  if (nl > 0 && o.name_len == nl && std::memcmp(o.name, t_.advertised_name, nl) == 0) by_name = true;

  if (!by_uuid && !by_name) return;
  ++candidates_;
  rule_ = by_uuid ? TargetMatch::ServiceUuid : TargetMatch::NameHint;
  std::memcpy(address_, o.addr, 6);
  address_type_ = o.addr_type;
}

TargetResolution TargetResolver::finish() const {
  TargetResolution r{};
  r.address_type = BleAddrType::Unknown;
  if (t_.has_address) {
    r.result = ResolveResult::Resolved;
    r.candidates = 1;
    r.rule = TargetMatch::EnrolledAddress;
    std::memcpy(r.address, t_.address, 6);
    r.address_type = t_.address_type;
    return r;
  }
  r.candidates = candidates_;
  if (candidates_ == 0) {
    r.result = ResolveResult::NotObserved;
    return r;
  }
  if (candidates_ > 1) {
    r.result = ResolveResult::Ambiguous;
    return r;
  }
  r.result = ResolveResult::Resolved;
  r.rule = rule_;
  std::memcpy(r.address, address_, 6);
  r.address_type = address_type_;
  return r;
}

}  // namespace oyster
