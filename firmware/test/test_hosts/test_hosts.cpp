// Host tests for the Hosts module's parsers and screen text (oyster_core/hosts_json.h).
// Run: pio test -e native -f test_hosts
#include <unity.h>

#include <cstdio>
#include <cstring>
#include <initializer_list>
#include <limits>
#include <string>
#include <utility>

#include "oyster_core/hosts_json.h"

using namespace oyster::hosts;

void setUp() {}
void tearDown() {}

// The enums are enum classes: compare them as integers.
#define TEST_ASSERT_EQUAL_E(expected, actual) TEST_ASSERT_EQUAL_INT(static_cast<int>(expected), static_cast<int>(actual))
#define TEST_ASSERT_EQUAL_E_MESSAGE(expected, actual, msg)   TEST_ASSERT_EQUAL_INT_MESSAGE(static_cast<int>(expected), static_cast<int>(actual), msg)

namespace {

// A buffer followed by guard bytes: a writer that runs past `buf` is caught by intact().
template <size_t N>
struct Guarded {
  char buf[N];
  char guard[16];
  Guarded() {
    std::memset(buf, 'X', sizeof(buf));
    std::memset(guard, 0x5A, sizeof(guard));
  }
  bool intact() const {
    for (char c : guard) {
      if (c != 0x5A) return false;
    }
    return true;
  }
};

Parse list(const std::string& json, HostList& out) { return parse_host_list(json.c_str(), json.size(), out); }

Parse health(const std::string& json, Health& out, const char* expect = "pc") {
  return parse_health(json.c_str(), json.size(), expect, out);
}

// {"hosts":[...]} for the given (id, name) pairs.
std::string hosts_json(std::initializer_list<std::pair<std::string, std::string>> items) {
  std::string s = R"J({"hosts":[)J";
  bool first = true;
  for (const auto& it : items) {
    if (!first) s += ",";
    first = false;
    s += R"J({"host_id":")J" + it.first + R"J(","display_name":")J" + it.second + R"J("})J";
  }
  return s + "]}";
}

const char* kFullHealth = R"J({"host_id":"pc","state":"ok","collected_at":1800000000,
 "metrics":{"cpu_percent":12.5,"load_1m":0.42,"mem_used_percent":61.0,"disk_used_percent":45,"uptime_s":183600},
 "services":[{"name":"Spooler","state":"active"},{"name":"W32Time","state":"inactive"}],"missing":[]})J";

}  // namespace

// ---- host list ----------------------------------------------------------------------------------------------

void test_list_basic() {
  HostList l;
  TEST_ASSERT_EQUAL_E(Parse::Ok, list(hosts_json({{"this-pc", "This PC"}, {"nas_2", "NAS"}}), l));
  TEST_ASSERT_EQUAL_UINT8(2, l.count);
  TEST_ASSERT_EQUAL_STRING("this-pc", l.hosts[0].id);
  TEST_ASSERT_EQUAL_STRING("This PC", l.hosts[0].name);
  TEST_ASSERT_EQUAL_STRING("nas_2", l.hosts[1].id);
  TEST_ASSERT_EQUAL_E(HostState::Unknown, l.hosts[1].state);
  TEST_ASSERT_EQUAL_UINT8(0, l.skipped);
  TEST_ASSERT_FALSE(l.more);
}

void test_list_empty_and_extra_fields() {
  HostList l;
  TEST_ASSERT_EQUAL_E(Parse::Ok, list(R"J({"hosts":[]})J", l));
  TEST_ASSERT_EQUAL_UINT8(0, l.count);
  TEST_ASSERT_EQUAL_E(Parse::Ok, list(R"J({"hosts":[{"host_id":"a","display_name":"A","adapter":"mock","x":[1,2]}],"y":1})J", l));
  TEST_ASSERT_EQUAL_UINT8(1, l.count);
}

void test_list_wrong_shapes_are_malformed() {
  const char* bad[] = {"",           "garbage", "null",        "[]",           "{}",           R"J({"hosts":{}})J",
                       R"J({"hosts":"x"})J", R"J({"hosts":null})J", R"J({"hosts":[)J", R"J({"hosts":[{"host_id":"a")J", "42"};
  for (const char* b : bad) {
    HostList l;
    l.count = 7;
    TEST_ASSERT_EQUAL_E_MESSAGE(Parse::Malformed, parse_host_list(b, std::strlen(b), l), b);
    TEST_ASSERT_EQUAL_UINT8_MESSAGE(0, l.count, b);  // the output is cleared on failure
  }
  HostList l;
  TEST_ASSERT_EQUAL_E(Parse::Malformed, parse_host_list(nullptr, 5, l));
}

void test_list_size_limit() {
  HostList l;
  std::string ok = R"J({"hosts":[]})J";
  std::string at_limit = ok + std::string(kMaxReply - ok.size(), ' ');
  TEST_ASSERT_EQUAL_UINT32(kMaxReply, at_limit.size());
  TEST_ASSERT_EQUAL_E(Parse::Ok, list(at_limit, l));
  std::string over = at_limit + " ";
  TEST_ASSERT_EQUAL_E(Parse::Oversized, list(over, l));
  TEST_ASSERT_EQUAL_E(Parse::Oversized, list(std::string(100000, 'a'), l));
}

void test_list_deep_nesting_is_rejected() {
  HostList l;
  TEST_ASSERT_EQUAL_E(Parse::Malformed, list(R"J({"hosts":[[[[[[[[[[[[[[[1]]]]]]]]]]]]]]]})J", l));
  std::string bomb = R"J({"hosts":)J" + std::string(200, '[') + std::string(200, ']') + "}";
  TEST_ASSERT_EQUAL_E(Parse::Malformed, list(bomb, l));
}

void test_list_hostile_ids_are_skipped() {
  HostList l;
  const std::string json =
      R"J({"hosts":[{"host_id":"../etc","display_name":"a"},{"host_id":"a b","display_name":"b"},)J"
      R"J({"host_id":"","display_name":"c"},{"host_id":"x/y","display_name":"d"},)J"
      R"J({"host_id":"q?x=1","display_name":"e"},{"host_id":"nul\u0000id","display_name":"f"},)J"
      R"J({"host_id":5,"display_name":"g"},{"host_id":null},{"display_name":"h"},"str",7,[1],)J"
      R"J({"host_id":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","display_name":"41 chars"},)J"
      R"J({"host_id":"good-1","display_name":"Good"}]})J";
  TEST_ASSERT_EQUAL_E(Parse::Ok, list(json, l));
  TEST_ASSERT_EQUAL_UINT8(1, l.count);
  TEST_ASSERT_EQUAL_STRING("good-1", l.hosts[0].id);
  TEST_ASSERT_EQUAL_UINT8(13, l.skipped);
  TEST_ASSERT_TRUE(valid_id("aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"));  // exactly 40 is allowed
}

void test_list_duplicates_and_cap() {
  HostList l;
  TEST_ASSERT_EQUAL_E(Parse::Ok, list(hosts_json({{"a", "A"}, {"a", "A again"}, {"b", "B"}}), l));
  TEST_ASSERT_EQUAL_UINT8(2, l.count);
  TEST_ASSERT_EQUAL_STRING("A", l.hosts[0].name);
  TEST_ASSERT_EQUAL_UINT8(1, l.skipped);

  std::string many = R"J({"hosts":[)J";
  for (int i = 0; i < 12; ++i) {
    char one[80];
    std::snprintf(one, sizeof(one), R"J(%s{"host_id":"h%d","display_name":"Host %d"})J", i ? "," : "", i, i);
    many += one;
  }
  many += "]}";
  TEST_ASSERT_EQUAL_E(Parse::Ok, list(many, l));
  TEST_ASSERT_EQUAL_UINT8(kMaxHosts, l.count);
  TEST_ASSERT_TRUE(l.more);
  TEST_ASSERT_EQUAL_STRING("h0", l.hosts[0].id);
  TEST_ASSERT_EQUAL_STRING("h7", l.hosts[7].id);
}

void test_list_names_are_made_printable_and_bounded() {
  HostList l;
  // UTF-8 u-umlaut (two bytes), a tab, a newline and surrounding spaces
  const std::string json =
      std::string(R"J({"hosts":[{"host_id":"a","display_name":"  M)J") + "\xC3\xBC" + R"J(ller\tPC\n  "},)J" +
      R"J({"host_id":"b","display_name":"0123456789012345678901234567890123456789ABCDEF"},)J" +
      R"J({"host_id":"c"},{"host_id":"d","display_name":"   "},{"host_id":"e","display_name":7}]})J";
  TEST_ASSERT_EQUAL_E(Parse::Ok, list(json, l));
  TEST_ASSERT_EQUAL_UINT8(5, l.count);
  TEST_ASSERT_EQUAL_STRING("M?ller?PC?", l.hosts[0].name);
  TEST_ASSERT_EQUAL_UINT32(kNameLen, std::strlen(l.hosts[1].name));
  TEST_ASSERT_EQUAL_STRING("0123456789012345678901234567890123456789", l.hosts[1].name);
  TEST_ASSERT_EQUAL_STRING("c", l.hosts[2].name);  // no name: the id
  TEST_ASSERT_EQUAL_STRING("d", l.hosts[3].name);  // blank name: the id
  TEST_ASSERT_EQUAL_STRING("e", l.hosts[4].name);  // not a string: the id
}

void test_carry_states_keeps_known_hosts_only() {
  HostList old, fresh;
  TEST_ASSERT_EQUAL_E(Parse::Ok, list(hosts_json({{"a", "A"}, {"b", "B"}}), old));
  old.hosts[0].state = HostState::Ok;
  old.hosts[1].state = HostState::Down;
  TEST_ASSERT_EQUAL_E(Parse::Ok, list(hosts_json({{"b", "B"}, {"c", "C"}}), fresh));
  carry_states(old, fresh);
  TEST_ASSERT_EQUAL_E(HostState::Down, fresh.hosts[0].state);
  TEST_ASSERT_EQUAL_E(HostState::Unknown, fresh.hosts[1].state);
  TEST_ASSERT_EQUAL_INT(1, find_host(old, "b"));
  TEST_ASSERT_EQUAL_INT(-1, find_host(old, "zz"));
  TEST_ASSERT_EQUAL_INT(-1, find_host(old, nullptr));
}

// ---- health report ------------------------------------------------------------------------------------------

void test_health_full_report() {
  Health h;
  TEST_ASSERT_EQUAL_E(Parse::Ok, health(kFullHealth, h));
  TEST_ASSERT_FALSE(h.partial);
  TEST_ASSERT_EQUAL_UINT8(metric_bit(kCpu) | metric_bit(kLoad) | metric_bit(kMem) | metric_bit(kDisk) | metric_bit(kUptime) | metric_bit(kServices), h.have);
  TEST_ASSERT_EQUAL_UINT8(0, h.missing);
  TEST_ASSERT_EQUAL_UINT8(13, h.cpu);  // 12.5 rounds up
  TEST_ASSERT_EQUAL_UINT8(61, h.mem);
  TEST_ASSERT_EQUAL_UINT8(45, h.disk);
  TEST_ASSERT_EQUAL_UINT32(42, h.load_x100);
  TEST_ASSERT_EQUAL_UINT32(183600, h.uptime_s);
  TEST_ASSERT_EQUAL_UINT8(2, h.service_count);
  TEST_ASSERT_EQUAL_STRING("Spooler", h.services[0].name);
  TEST_ASSERT_EQUAL_E(ServiceState::Active, h.services[0].state);
  TEST_ASSERT_EQUAL_E(ServiceState::Inactive, h.services[1].state);
}

void test_health_partial_report_lists_what_is_missing() {
  Health h;
  const std::string json =
      R"J({"host_id":"pc","state":"partial","collected_at":1,"metrics":{"cpu_percent":3},)J"
      R"J("missing":["disk_used_percent","load_1m","bogus_name","services"]})J";
  TEST_ASSERT_EQUAL_E(Parse::Ok, health(json, h));
  TEST_ASSERT_TRUE(h.partial);
  TEST_ASSERT_EQUAL_UINT8(metric_bit(kCpu), h.have);
  TEST_ASSERT_EQUAL_UINT8(metric_bit(kDisk) | metric_bit(kLoad) | metric_bit(kServices), h.missing);
}

void test_health_unusable_values_count_as_missing() {
  Health h;
  const std::string json =
      R"J({"host_id":"pc","state":"ok","metrics":{"cpu_percent":150,"mem_used_percent":-1,)J"
      R"J("disk_used_percent":"50","load_1m":20000,"uptime_s":-5}})J";
  TEST_ASSERT_EQUAL_E(Parse::Ok, health(json, h));
  TEST_ASSERT_EQUAL_UINT8(0, h.have);
  TEST_ASSERT_EQUAL_UINT8(metric_bit(kCpu) | metric_bit(kMem) | metric_bit(kDisk) | metric_bit(kLoad) | metric_bit(kUptime), h.missing);
  TEST_ASSERT_TRUE(h.partial);  // even though the backend said "ok"

  const char* uptimes[] = {"1.5", "315360001", "true", "null", "[1]", "{}", "1e3"};
  for (const char* u : uptimes) {
    const std::string j = std::string(R"J({"host_id":"pc","state":"ok","metrics":{"uptime_s":)J") + u + "}}";
    Health x;
    TEST_ASSERT_EQUAL_E_MESSAGE(Parse::Ok, health(j, x), u);
    TEST_ASSERT_EQUAL_UINT8_MESSAGE(0, x.have & metric_bit(kUptime), u);
  }
  Health up;
  TEST_ASSERT_EQUAL_E(Parse::Ok, health(R"J({"host_id":"pc","state":"ok","metrics":{"uptime_s":315360000}})J", up));
  TEST_ASSERT_EQUAL_UINT32(315360000, up.uptime_s);
}

void test_health_rounding_and_clamping() {
  Health h;
  const std::string json =
      R"J({"host_id":"pc","state":"ok","metrics":{"cpu_percent":99.6,"mem_used_percent":0.4,"disk_used_percent":100}})J";
  TEST_ASSERT_EQUAL_E(Parse::Ok, health(json, h));
  TEST_ASSERT_EQUAL_UINT8(100, h.cpu);
  TEST_ASSERT_EQUAL_UINT8(0, h.mem);
  TEST_ASSERT_EQUAL_UINT8(100, h.disk);
}

void test_health_for_another_host_is_refused() {
  Health h;
  TEST_ASSERT_EQUAL_E(Parse::Invalid, health(kFullHealth, h, "other"));
  TEST_ASSERT_EQUAL_E(Parse::Ok, parse_health(kFullHealth, std::strlen(kFullHealth), nullptr, h));  // no expectation
  TEST_ASSERT_EQUAL_E(Parse::Invalid, health(R"J({"state":"ok","metrics":{}})J", h));
  TEST_ASSERT_EQUAL_E(Parse::Invalid, health(R"J({"host_id":7,"state":"ok"})J", h));
}

void test_health_state_must_be_known() {
  Health h;
  TEST_ASSERT_EQUAL_E(Parse::Invalid, health(R"J({"host_id":"pc","state":"down","metrics":{}})J", h));
  TEST_ASSERT_EQUAL_E(Parse::Invalid, health(R"J({"host_id":"pc","metrics":{}})J", h));
  TEST_ASSERT_EQUAL_E(Parse::Invalid, health(R"J({"host_id":"pc","state":1,"metrics":{}})J", h));
  TEST_ASSERT_EQUAL_E(Parse::Invalid, health(R"J({"host_id":"pc","state":"OK","metrics":{}})J", h));
}

void test_health_wrong_shapes() {
  Health h;
  const char* bad[] = {"", "x", "null", "[]", "42", "\"s\"", R"J({"host_id":"pc","state":"ok")J"};
  for (const char* b : bad) {
    TEST_ASSERT_EQUAL_E_MESSAGE(Parse::Malformed, parse_health(b, std::strlen(b), "pc", h), b);
  }
  TEST_ASSERT_EQUAL_E(Parse::Oversized, health(std::string(5000, ' ') + kFullHealth, h));
  const std::string deep = R"J({"host_id":"pc","state":"ok","metrics":)J" + std::string(100, '{');
  TEST_ASSERT_EQUAL_E(Parse::Malformed, health(deep, h));
  // metrics and services of the wrong type are ignored, not trusted
  TEST_ASSERT_EQUAL_E(Parse::Ok, health(R"J({"host_id":"pc","state":"ok","metrics":"x","services":{"a":1},"missing":"b"})J", h));
  TEST_ASSERT_EQUAL_UINT8(0, h.have);
  TEST_ASSERT_FALSE(h.partial);
}

void test_health_services_are_bounded_and_sanitised() {
  std::string json = R"J({"host_id":"pc","state":"ok","metrics":{},"services":[)J";
  json += R"J({"name":"a","state":"failed"},{"name":"b","state":"weird"},{"name":"c"},{"name":""},{"state":"active"},)J";
  json += R"J(5,"x",null,{"name":"long)J" + std::string(60, 'n') + R"J(","state":"active"})J";
  for (int i = 0; i < 20; ++i) json += R"J(,{"name":"s","state":"active"})J";
  json += "]}";
  Health h;
  TEST_ASSERT_EQUAL_E(Parse::Ok, health(json, h));
  TEST_ASSERT_EQUAL_UINT8(kMaxServices, h.service_count);
  TEST_ASSERT_EQUAL_E(ServiceState::Failed, h.services[0].state);
  TEST_ASSERT_EQUAL_E(ServiceState::Unknown, h.services[1].state);  // unknown word
  TEST_ASSERT_EQUAL_E(ServiceState::Unknown, h.services[2].state);  // no state
  TEST_ASSERT_EQUAL_STRING("c", h.services[2].name);
  TEST_ASSERT_EQUAL_UINT32(kNameLen, std::strlen(h.services[3].name));  // cut to 40
  TEST_ASSERT_EQUAL_UINT8(metric_bit(kServices), h.have);
}

void test_health_services_only_host_and_value_beats_missing_claim() {
  Health h;
  TEST_ASSERT_EQUAL_E(Parse::Ok, health(R"J({"host_id":"pc","state":"ok","metrics":{},"services":[{"name":"a","state":"active"}]})J", h));
  TEST_ASSERT_EQUAL_UINT8(metric_bit(kServices), h.have);
  TEST_ASSERT_EQUAL_UINT8(1, h.service_count);
  TEST_ASSERT_EQUAL_E(Parse::Ok, health(R"J({"host_id":"pc","state":"partial","metrics":{"cpu_percent":5},"missing":["cpu_percent"]})J", h));
  TEST_ASSERT_EQUAL_UINT8(metric_bit(kCpu), h.have);
  TEST_ASSERT_EQUAL_UINT8(0, h.missing);
}

// ---- failures -----------------------------------------------------------------------------------------------

void test_classify_status_codes() {
  struct Case {
    int status;
    Fail fail;
  } cases[] = {{200, Fail::None},     {-1, Fail::Offline},   {-6, Fail::Clock},     {-5, Fail::TooLarge},
               {-3, Fail::Transport}, {-4, Fail::Transport}, {-99, Fail::Offline},  {401, Fail::Auth},
               {403, Fail::Auth},     {404, Fail::NotFound}, {429, Fail::Busy},     {400, Fail::Refused},
               {422, Fail::Refused},  {500, Fail::Server},   {503, Fail::Server},   {204, Fail::Malformed},
               {302, Fail::Malformed}, {0, Fail::Malformed}};
  for (const Case& c : cases) {
    TEST_ASSERT_EQUAL_E_MESSAGE(c.fail, classify_reply(c.status, Ctx::Health, "", 0), std::to_string(c.status).c_str());
  }
}

void test_classify_502_reads_the_host_state() {
  const char* timeout = R"J({"error":"host_unavailable","detail":"x","state":"timeout","host_id":"pc"})J";
  const char* unreachable = R"J({"error":"host_unavailable","detail":"x","state":"unreachable","host_id":"pc"})J";
  TEST_ASSERT_EQUAL_E(Fail::Timeout, classify_reply(502, Ctx::Health, timeout, std::strlen(timeout)));
  TEST_ASSERT_EQUAL_E(Fail::Unreachable, classify_reply(502, Ctx::Health, unreachable, std::strlen(unreachable)));
  const char* others[] = {"", "garbage", "{}", R"J({"error":"other","state":"timeout"})J",
                          R"J({"error":"host_unavailable","state":"weird"})J",
                          R"J({"error":"host_unavailable"})J"};
  for (const char* b : others) {
    TEST_ASSERT_EQUAL_E_MESSAGE(Fail::Server, classify_reply(502, Ctx::Health, b, std::strlen(b)), b);
  }
  TEST_ASSERT_EQUAL_E(Fail::Server, classify_reply(502, Ctx::Health, nullptr, 0));
  TEST_ASSERT_EQUAL_E(Fail::Server, classify_reply(502, Ctx::Health, timeout, 100000));  // oversized body
  TEST_ASSERT_EQUAL_E(Fail::TooLarge, from_parse(Parse::Oversized));
  TEST_ASSERT_EQUAL_E(Fail::Malformed, from_parse(Parse::Malformed));
  TEST_ASSERT_EQUAL_E(Fail::Malformed, from_parse(Parse::Invalid));
  TEST_ASSERT_EQUAL_E(Fail::None, from_parse(Parse::Ok));
}

void test_fail_text_is_actionable_and_bounded() {
  const Fail all[] = {Fail::Offline,   Fail::Clock,  Fail::Transport, Fail::TooLarge, Fail::Auth,
                      Fail::NotFound,  Fail::Unreachable, Fail::Timeout, Fail::Busy,  Fail::Server,
                      Fail::Refused,   Fail::Malformed,   Fail::NoAnswer,  Fail::Cancelled};
  for (Fail f : all) {
    for (Ctx ctx : {Ctx::List, Ctx::Health}) {
      Guarded<560> g;
      fail_text(f, ctx, 500, "link down", g.buf, sizeof(g.buf));
      TEST_ASSERT_TRUE(g.intact());
      TEST_ASSERT_TRUE(std::strlen(g.buf) > 10);
      TEST_ASSERT_TRUE(std::strlen(g.buf) < 300);
      TEST_ASSERT_TRUE(std::strlen(fail_label(f)) > 0);
      TEST_ASSERT_TRUE(std::strlen(fail_label(f)) <= 18);
      for (size_t cap : {1u, 2u, 7u, 30u}) {  // tiny buffers: never overrun, always terminated
        Guarded<40> t;
        fail_text(f, ctx, 500, "link down", t.buf, cap);
        TEST_ASSERT_TRUE(t.intact());
        TEST_ASSERT_TRUE(std::strlen(t.buf) < cap);
      }
    }
  }
  char buf[560];
  fail_text(Fail::Offline, Ctx::List, -1, "request failed\n(code -3)", buf, sizeof(buf));
  TEST_ASSERT_NOT_NULL(std::strstr(buf, "offline"));
  TEST_ASSERT_NOT_NULL(std::strstr(buf, "request failed?(code -3)"));  // detail made printable
  TEST_ASSERT_NOT_NULL(std::strstr(buf, "Enter or r: retry"));
  fail_text(Fail::Auth, Ctx::List, 401, "must not appear", buf, sizeof(buf));
  TEST_ASSERT_NULL(std::strstr(buf, "must not appear"));  // detail only for transport-level failures
  TEST_ASSERT_NOT_NULL(std::strstr(buf, "HTTP 401"));
  fail_text(Fail::NotFound, Ctx::Health, 404, nullptr, buf, sizeof(buf));
  TEST_ASSERT_NULL(std::strstr(buf, "retry"));  // a host that is gone is not retried
  fail_text(Fail::NotFound, Ctx::List, 404, nullptr, buf, sizeof(buf));
  TEST_ASSERT_NOT_NULL(std::strstr(buf, "no host API"));
  fail_text(Fail::None, Ctx::List, 200, nullptr, buf, sizeof(buf));
  TEST_ASSERT_EQUAL_STRING("", buf);
}

// ---- text helpers -------------------------------------------------------------------------------------------

void test_ids_and_paths() {
  TEST_ASSERT_TRUE(valid_id("this-pc"));
  TEST_ASSERT_TRUE(valid_id("A_b-9"));
  const char* bad[] = {"", "a b", "a/b", "..", "a.b", "a?b", "a%20b", "a\nb", "\xC3\xBC"};
  for (const char* b : bad) TEST_ASSERT_FALSE_MESSAGE(valid_id(b), b);
  TEST_ASSERT_FALSE(valid_id(nullptr));
  TEST_ASSERT_FALSE(valid_id("aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"));  // 41

  char path[64];
  TEST_ASSERT_TRUE(health_path("this-pc", path, sizeof(path)));
  TEST_ASSERT_EQUAL_STRING("/v1/hosts/this-pc/health", path);
  TEST_ASSERT_FALSE(health_path("../x", path, sizeof(path)));
  TEST_ASSERT_EQUAL_STRING("", path);
  char tiny[10];
  TEST_ASSERT_FALSE(health_path("this-pc", tiny, sizeof(tiny)));
  TEST_ASSERT_EQUAL_STRING("", tiny);
  TEST_ASSERT_FALSE(health_path("a", nullptr, 0));
}

void test_sanitize_text() {
  char out[16];
  sanitize_text(out, sizeof(out), "  hello  ");
  TEST_ASSERT_EQUAL_STRING("hello", out);
  sanitize_text(out, sizeof(out), "a\x01" "b\x7F" "c");
  TEST_ASSERT_EQUAL_STRING("a?b?c", out);
  sanitize_text(out, sizeof(out), "caf\xC3\xA9 \xE2\x82\xAC");  // e-acute, euro sign
  TEST_ASSERT_EQUAL_STRING("caf? ?", out);
  sanitize_text(out, sizeof(out), "0123456789ABCDEFGHIJ");
  TEST_ASSERT_EQUAL_STRING("0123456789ABCDE", out);  // cap-1 characters
  sanitize_text(out, sizeof(out), nullptr);
  TEST_ASSERT_EQUAL_STRING("", out);
  sanitize_text(out, 1, "abc");
  TEST_ASSERT_EQUAL_STRING("", out);
  sanitize_text(out, 0, "abc");  // must not touch anything
  sanitize_text(nullptr, 5, "abc");
  sanitize_text(out, sizeof(out), "\x80\x81 x");  // stray continuation bytes are dropped
  TEST_ASSERT_EQUAL_STRING("x", out);
}

void test_numbers_and_durations() {
  TEST_ASSERT_EQUAL_UINT8(0, percent_of(-5.0f));
  TEST_ASSERT_EQUAL_UINT8(0, percent_of(0.49f));
  TEST_ASSERT_EQUAL_UINT8(1, percent_of(0.5f));
  TEST_ASSERT_EQUAL_UINT8(13, percent_of(12.5f));
  TEST_ASSERT_EQUAL_UINT8(100, percent_of(99.5f));
  TEST_ASSERT_EQUAL_UINT8(100, percent_of(250.0f));
  TEST_ASSERT_EQUAL_UINT8(0, percent_of(std::numeric_limits<float>::quiet_NaN()));

  struct Up {
    uint32_t s;
    const char* text;
  } ups[] = {{0, "0s"},        {59, "59s"},         {60, "1m"},        {3599, "59m"},        {3600, "1h 0m"},
             {18720, "5h 12m"}, {86399, "23h 59m"}, {86400, "1d 0h"},  {183600, "2d 3h"},    {315360000, "3650d 0h"}};
  for (const Up& u : ups) {
    char b[24];
    format_uptime(u.s, b, sizeof(b));
    TEST_ASSERT_EQUAL_STRING(u.text, b);
  }
  struct Age {
    uint32_t s;
    const char* text;
  } ages[] = {{0, "0 s"}, {12, "12 s"}, {59, "59 s"}, {60, "1 min"}, {3599, "59 min"}, {3600, "1 h"}, {86399, "23 h"}, {86400, ">1 d"}};
  for (const Age& a : ages) {
    char b[24];
    format_age(a.s, b, sizeof(b));
    TEST_ASSERT_EQUAL_STRING(a.text, b);
  }
  char b[16];
  format_load(42, b, sizeof(b));
  TEST_ASSERT_EQUAL_STRING("0.42", b);
  format_load(1500, b, sizeof(b));
  TEST_ASSERT_EQUAL_STRING("15.00", b);
  format_load(7, b, sizeof(b));
  TEST_ASSERT_EQUAL_STRING("0.07", b);
  Guarded<4> g;
  format_uptime(183600, g.buf, sizeof(g.buf));  // truncated, terminated, no overrun
  TEST_ASSERT_TRUE(g.intact());
  TEST_ASSERT_EQUAL_STRING("2d ", g.buf);
}

void test_rows() {
  Host h{};
  std::strcpy(h.id, "pc");
  std::strcpy(h.name, "This PC");
  char row[64];
  format_host_row(h, row, sizeof(row), 27);
  TEST_ASSERT_EQUAL_STRING("This PC", row);  // no state yet: the name alone

  h.state = HostState::Ok;
  format_host_row(h, row, sizeof(row), 27);
  TEST_ASSERT_EQUAL_STRING(("This PC" + std::string(18, ' ') + "ok").c_str(), row);
  TEST_ASSERT_EQUAL_UINT32(27, std::strlen(row));
  h.state = HostState::Partial;
  format_host_row(h, row, sizeof(row), 27);
  TEST_ASSERT_EQUAL_STRING(("This PC" + std::string(13, ' ') + "partial").c_str(), row);
  h.state = HostState::Down;
  std::strcpy(h.name, "A very long host display name!!");  // 31 characters: cut to fit next to "down"
  format_host_row(h, row, sizeof(row), 27);
  TEST_ASSERT_EQUAL_UINT32(27, std::strlen(row));
  TEST_ASSERT_EQUAL_STRING("A very long host disp~ down", row);
  h.state = HostState::Unknown;
  format_host_row(h, row, sizeof(row), 27);  // no state: the name may use the whole row
  TEST_ASSERT_EQUAL_STRING("A very long host display n~", row);

  Service s{};
  std::strcpy(s.name, "Spooler");
  s.state = ServiceState::Inactive;
  format_service_row(s, row, sizeof(row), 27);
  TEST_ASSERT_EQUAL_STRING(("Spooler" + std::string(12, ' ') + "inactive").c_str(), row);
  std::strcpy(s.name, "a-service-with-a-name-too-long-x");
  s.state = ServiceState::Unknown;
  format_service_row(s, row, sizeof(row), 27);
  TEST_ASSERT_EQUAL_UINT32(27, std::strlen(row));
  TEST_ASSERT_EQUAL_STRING("a-service-with-a-n~ unknown", row);

  // width larger than the buffer and tiny widths never overrun
  for (size_t cols : {0u, 1u, 3u, 9u, 200u}) {
    Guarded<20> g;
    format_service_row(s, g.buf, sizeof(g.buf), cols);
    TEST_ASSERT_TRUE(g.intact());
    TEST_ASSERT_TRUE(std::strlen(g.buf) < sizeof(g.buf));
  }
}

void test_host_count_text() {
  char b[12];
  format_host_count(0, false, b, sizeof(b));
  TEST_ASSERT_EQUAL_STRING("no hosts", b);
  format_host_count(1, false, b, sizeof(b));
  TEST_ASSERT_EQUAL_STRING("1 host", b);
  format_host_count(3, false, b, sizeof(b));
  TEST_ASSERT_EQUAL_STRING("3 hosts", b);
  format_host_count(8, true, b, sizeof(b));
  TEST_ASSERT_EQUAL_STRING("8+ hosts", b);
  TEST_ASSERT_TRUE(std::strlen(b) <= 11);
}

void test_overview_text() {
  Health h;
  TEST_ASSERT_EQUAL_E(Parse::Ok, health(kFullHealth, h));
  char body[560];
  format_overview(h, 12, body, sizeof(body));
  TEST_ASSERT_EQUAL_STRING(
      "CPU 13%  MEM 61%\n"
      "DISK 45%  UP 2d 3h\n"
      "LOAD 0.42\n"
      "State ok, 12 s ago\n"
      "Enter: services (2)",
      body);

  // partial: dropped values show as "--" and are named once more at the end
  Health p;
  TEST_ASSERT_EQUAL_E(Parse::Ok, health(R"J({"host_id":"pc","state":"partial","metrics":{"cpu_percent":3,"uptime_s":90},"missing":["disk_used_percent","mem_used_percent"]})J", p));
  format_overview(p, 200, body, sizeof(body));
  TEST_ASSERT_EQUAL_STRING(
      "CPU 3%  MEM --\n"
      "DISK --  UP 1m\n"
      "State partial, 3 min ago\n"
      "No data: MEM DISK",
      body);

  // a host that shares only some values shows only those, and no hint without services
  Health c;
  TEST_ASSERT_EQUAL_E(Parse::Ok, health(R"J({"host_id":"pc","state":"ok","metrics":{"cpu_percent":50}})J", c));
  format_overview(c, 0, body, sizeof(body));
  TEST_ASSERT_EQUAL_STRING("CPU 50%\nState ok, 0 s ago", body);

  Health s;
  TEST_ASSERT_EQUAL_E(Parse::Ok, health(R"J({"host_id":"pc","state":"partial","metrics":{},"services":[],"missing":["load_1m","services"]})J", s));
  format_overview(s, 5, body, sizeof(body));
  TEST_ASSERT_EQUAL_STRING("LOAD --\nState partial, 5 s ago\nNo data: LOAD\nNo services listed", body);

  Guarded<30> g;  // too small: cut, terminated, no overrun
  format_overview(h, 12, g.buf, sizeof(g.buf));
  TEST_ASSERT_TRUE(g.intact());
  TEST_ASSERT_TRUE(std::strlen(g.buf) < sizeof(g.buf));
}

void test_the_largest_overview_fits_the_page_body() {
  Health h{};
  h.have = 0x3F;
  h.partial = true;
  h.missing = 0x3F;  // pathological: everything both present and missing
  h.cpu = h.mem = h.disk = 100;
  h.load_x100 = 99999999;
  h.uptime_s = 315360000;
  h.service_count = kMaxServices;
  char body[560];
  format_overview(h, 4000000000u, body, sizeof(body));
  TEST_ASSERT_TRUE(std::strlen(body) < 300);
}

int main(int, char**) {
  UNITY_BEGIN();
  RUN_TEST(test_list_basic);
  RUN_TEST(test_list_empty_and_extra_fields);
  RUN_TEST(test_list_wrong_shapes_are_malformed);
  RUN_TEST(test_list_size_limit);
  RUN_TEST(test_list_deep_nesting_is_rejected);
  RUN_TEST(test_list_hostile_ids_are_skipped);
  RUN_TEST(test_list_duplicates_and_cap);
  RUN_TEST(test_list_names_are_made_printable_and_bounded);
  RUN_TEST(test_carry_states_keeps_known_hosts_only);
  RUN_TEST(test_health_full_report);
  RUN_TEST(test_health_partial_report_lists_what_is_missing);
  RUN_TEST(test_health_unusable_values_count_as_missing);
  RUN_TEST(test_health_rounding_and_clamping);
  RUN_TEST(test_health_for_another_host_is_refused);
  RUN_TEST(test_health_state_must_be_known);
  RUN_TEST(test_health_wrong_shapes);
  RUN_TEST(test_health_services_are_bounded_and_sanitised);
  RUN_TEST(test_health_services_only_host_and_value_beats_missing_claim);
  RUN_TEST(test_classify_status_codes);
  RUN_TEST(test_classify_502_reads_the_host_state);
  RUN_TEST(test_fail_text_is_actionable_and_bounded);
  RUN_TEST(test_ids_and_paths);
  RUN_TEST(test_sanitize_text);
  RUN_TEST(test_numbers_and_durations);
  RUN_TEST(test_rows);
  RUN_TEST(test_host_count_text);
  RUN_TEST(test_overview_text);
  RUN_TEST(test_the_largest_overview_fits_the_page_body);
  return UNITY_END();
}
