// Screen text of the Claude Code link: rows, confirm fields, usage line, results.
#include <unity.h>

#include <cstdint>
#include <cstring>
#include <string>

#include "oyster_core/claude_json.h"
#include "test_common.h"

using namespace oyster::claude;

namespace {

Approval make(const char* tool, const char* summary) {
  Approval a;
  std::memset(&a, 0, sizeof(a));
  std::snprintf(a.id, sizeof(a.id), "appr-1");
  std::snprintf(a.tool, sizeof(a.tool), "%s", tool);
  std::snprintf(a.summary, sizeof(a.summary), "%s", summary);
  std::snprintf(a.nonce, sizeof(a.nonce), "SECRETNONCE12345");
  return a;
}

Status make_status() {
  Status s;
  std::memset(&s, 0, sizeof(s));
  s.configured = true;
  s.has_age = true;
  s.updated_s_ago = 7;
  std::snprintf(s.model, sizeof(s.model), "Opus");
  s.has_cost = true;
  s.cost_usd = 0.0123f;
  s.has_context = true;
  s.context_pct = 8.0f;
  return s;
}

bool contains(const char* hay, const char* needle) { return std::strstr(hay, needle) != nullptr; }

uint32_t g_seed = 99;
uint32_t next_random() {
  g_seed = g_seed * 1664525u + 1013904223u;
  return g_seed >> 8;
}

}  // namespace

static void test_entry_row_states() {
  char out[48];
  format_entry_row(EntryState::Pending, 2, out, sizeof(out));
  TEST_ASSERT_EQUAL_STRING("Claude Code  2 pending", out);
  format_entry_row(EntryState::Pending, 0, out, sizeof(out));
  TEST_ASSERT_EQUAL_STRING("Claude Code  none pending", out);
  format_entry_row(EntryState::Offline, 0, out, sizeof(out));
  TEST_ASSERT_EQUAL_STRING("Claude Code  offline", out);
  format_entry_row(EntryState::NotSetUp, 0, out, sizeof(out));
  TEST_ASSERT_EQUAL_STRING("Claude Code  not set up", out);
  format_entry_row(EntryState::Checking, 0, out, sizeof(out));
  TEST_ASSERT_EQUAL_STRING("Claude Code  checking...", out);
  const EntryState all[] = {EntryState::Checking, EntryState::Pending, EntryState::Offline,
                            EntryState::NotSetUp, EntryState::NotAccepted, EntryState::Error};
  for (EntryState st : all) {
    format_entry_row(st, 5, out, sizeof(out));
    TEST_ASSERT_TRUE(std::strlen(out) <= static_cast<size_t>(kRowCols));
    format_list_note(st, 5, out, sizeof(out));
    TEST_ASSERT_TRUE(std::strlen(out) > 0 && std::strlen(out) <= static_cast<size_t>(kRowCols));
  }
  TEST_ASSERT_TRUE(entry_state_for_failure(-1) == EntryState::Offline);
  TEST_ASSERT_TRUE(entry_state_for_failure(-3) == EntryState::Offline);
  TEST_ASSERT_TRUE(entry_state_for_failure(-4) == EntryState::Error);  // an answer came, but not a usable one
  TEST_ASSERT_TRUE(entry_state_for_failure(-5) == EntryState::Error);
  TEST_ASSERT_TRUE(entry_state_for_failure(404) == EntryState::NotSetUp);
  TEST_ASSERT_TRUE(entry_state_for_failure(401) == EntryState::NotAccepted);
  TEST_ASSERT_TRUE(entry_state_for_failure(403) == EntryState::NotAccepted);
  TEST_ASSERT_TRUE(entry_state_for_failure(500) == EntryState::Error);
  format_list_note(EntryState::Pending, 0, out, sizeof(out));
  TEST_ASSERT_EQUAL_STRING("No requests pending", out);
}

// Text on the left, the time at the right edge, exactly one row wide.
std::string row_of(const std::string& left, const std::string& right) {
  return left + std::string(kRowCols - left.size() - right.size(), ' ') + right;
}

static void test_approval_row_shape() {
  char out[48];
  format_approval_row(make("Bash", "npm test"), 78, out, sizeof(out));
  TEST_ASSERT_EQUAL_UINT32(kRowCols, std::strlen(out));
  TEST_ASSERT_EQUAL_STRING(row_of("Bash npm test", "78s").c_str(), out);
  format_approval_row(make("Bash", "npm test"), 0, out, sizeof(out));
  TEST_ASSERT_EQUAL_STRING(row_of("Bash npm test", "0s").c_str(), out);
  format_approval_row(make("Bash", "npm test"), 120, out, sizeof(out));
  TEST_ASSERT_EQUAL_STRING(row_of("Bash npm test", "120s").c_str(), out);
  format_approval_row(make("Bash", "npm test"), 5000, out, sizeof(out));  // never wider than the row
  TEST_ASSERT_EQUAL_UINT32(kRowCols, std::strlen(out));
  TEST_ASSERT_EQUAL_STRING(row_of("Bash npm test", "999s").c_str(), out);
}

static void test_approval_row_clips_and_never_hides_the_time() {
  char out[48];
  format_approval_row(make("Bash", "rm -rf node_modules && npm install --force"), 61, out, sizeof(out));
  TEST_ASSERT_EQUAL_UINT32(kRowCols, std::strlen(out));
  TEST_ASSERT_EQUAL_STRING("Bash rm -rf node_module~ 61s", out);
  format_approval_row(make("mcp__some_server__some_tool_name", "x"), 9, out, sizeof(out));
  TEST_ASSERT_EQUAL_UINT32(kRowCols, std::strlen(out));
  TEST_ASSERT_EQUAL_STRING("mcp__some_server__some_t~ 9s", out);
}

static void test_approval_row_without_details() {
  char out[48];
  format_approval_row(make("Edit", ""), 30, out, sizeof(out));
  TEST_ASSERT_TRUE(contains(out, "Edit (no details)"));
  TEST_ASSERT_TRUE(contains(out, "30s"));
  format_approval_row(make("Edit", "   "), 30, out, sizeof(out));
  TEST_ASSERT_TRUE(contains(out, "(no details)"));
  TEST_ASSERT_TRUE(summary_is_blank(""));
  TEST_ASSERT_TRUE(summary_is_blank("  "));
  TEST_ASSERT_TRUE(summary_is_blank(nullptr));
  TEST_ASSERT_FALSE(summary_is_blank(" x"));
}

static void test_approval_row_is_always_one_row_wide() {
  for (int i = 0; i < 2000; ++i) {
    char tool[kToolCap];
    char sum[kSummaryCap];
    const size_t tl = 1 + next_random() % (kToolCap - 1);
    const size_t sl = next_random() % kSummaryCap;
    for (size_t k = 0; k < tl; ++k) tool[k] = static_cast<char>('a' + next_random() % 26);
    tool[tl] = '\0';
    for (size_t k = 0; k < sl; ++k) sum[k] = next_random() % 7 == 0 ? ' ' : static_cast<char>(0x21 + next_random() % 94);
    sum[sl] = '\0';
    char out[64];
    format_approval_row(make(tool, sum), next_random() % 1000, out, sizeof(out));
    TEST_ASSERT_EQUAL_UINT32(kRowCols, std::strlen(out));
    TEST_ASSERT_FALSE(contains(out, "SECRETNONCE"));
  }
}

static void test_confirm_fields() {
  char out[40];
  format_confirm_operation("Bash", out, sizeof(out));
  TEST_ASSERT_EQUAL_STRING("Claude Code: Bash", out);
  format_confirm_operation(std::string(40, 't').c_str(), out, sizeof(out));
  TEST_ASSERT_EQUAL_UINT32(sizeof(out) - 1, std::strlen(out));
  TEST_ASSERT_EQUAL_CHAR('~', out[sizeof(out) - 2]);
  format_confirm_target(out, sizeof(out));
  TEST_ASSERT_EQUAL_STRING("Details shortened", out);
}

static void test_wrap_rows_matches_the_widget_wrap() {
  TEST_ASSERT_EQUAL_UINT8(0, wrap_rows("", 28));
  TEST_ASSERT_EQUAL_UINT8(1, wrap_rows("npm test", 28));
  TEST_ASSERT_EQUAL_UINT8(1, wrap_rows(std::string(28, 'a').c_str(), 28));
  TEST_ASSERT_EQUAL_UINT8(2, wrap_rows(std::string(29, 'a').c_str(), 28));
  TEST_ASSERT_EQUAL_UINT8(3, wrap_rows(std::string(80, 'a').c_str(), 28));
  TEST_ASSERT_EQUAL_UINT8(2, wrap_rows("aaaa bbbb cccc dddd eeee ffff gggg hhhh", 28));  // 39 chars, breaks at a space
  TEST_ASSERT_EQUAL_UINT8(2, wrap_rows("one\ntwo", 28));
  TEST_ASSERT_EQUAL_UINT8(1, wrap_rows("abc def", 7));
}

static void test_confirm_effect_shows_the_summary_in_full_and_a_hint_when_rows_remain() {
  char out[96];
  format_confirm_effect("npm test", out, sizeof(out));
  TEST_ASSERT_EQUAL_STRING("npm test\nAllow once. Deny what you cannot judge.", out);
  TEST_ASSERT_TRUE(wrap_rows(out, kRowCols) <= kConfirmEffectRows);

  // two rows: the short hint
  const std::string two = "aaaa bbbb cccc dddd eeee ffff gggg hhhh";
  format_confirm_effect(two.c_str(), out, sizeof(out));
  TEST_ASSERT_TRUE(contains(out, two.c_str()));
  TEST_ASSERT_TRUE(contains(out, "\nDeny if unsure."));
  TEST_ASSERT_TRUE(wrap_rows(out, kRowCols) <= kConfirmEffectRows);

  // three rows: the summary alone, nothing added after it
  const std::string three(80, 'q');
  format_confirm_effect(three.c_str(), out, sizeof(out));
  TEST_ASSERT_TRUE(contains(out, std::string(28, 'q').c_str()));
  TEST_ASSERT_FALSE(contains(out, "Deny"));
  TEST_ASSERT_EQUAL_UINT8(3, wrap_rows(out, kRowCols));

  // eighty characters that word wrap would spread over four rows are cut at the row width: all of it stays visible
  const std::string words = std::string(15, 'a') + " " + std::string(15, 'b') + " " + std::string(15, 'c') + " " +
                            std::string(15, 'd') + " " + std::string(16, 'e');
  TEST_ASSERT_EQUAL_UINT32(80, words.size());
  TEST_ASSERT_TRUE(wrap_rows(words.c_str(), kRowCols) > kConfirmEffectRows);
  format_confirm_effect(words.c_str(), out, sizeof(out));
  TEST_ASSERT_EQUAL_UINT8(3, wrap_rows(out, kRowCols));
  std::string flat;
  for (const char* p = out; *p != '\0'; ++p) {
    if (*p != '\n') flat += *p;
  }
  TEST_ASSERT_EQUAL_STRING(words.c_str(), flat.c_str());
}

static void test_confirm_effect_never_stays_empty() {
  char out[96];
  format_confirm_effect("", out, sizeof(out));
  TEST_ASSERT_TRUE(contains(out, "(no details)"));
  format_confirm_effect("    ", out, sizeof(out));
  TEST_ASSERT_TRUE(contains(out, "(no details)"));
  format_confirm_effect(nullptr, out, sizeof(out));
  TEST_ASSERT_TRUE(contains(out, "(no details)"));
}

static void test_confirm_effect_hint_never_pushes_the_summary_out_of_the_widget() {
  for (int i = 0; i < 3000; ++i) {
    char sum[kSummaryCap];
    const size_t sl = next_random() % kSummaryCap;
    for (size_t k = 0; k < sl; ++k) sum[k] = next_random() % 5 == 0 ? ' ' : static_cast<char>(0x21 + next_random() % 94);
    sum[sl] = '\0';
    char out[96];
    format_confirm_effect(sum, out, sizeof(out));
    TEST_ASSERT_TRUE(wrap_rows(out, kRowCols) <= kConfirmEffectRows);  // the widget never has to cut it with '~'
    TEST_ASSERT_TRUE(std::strlen(out) < sizeof(out));
    if (!summary_is_blank(sum)) {
      std::string flat;  // the summary is intact apart from line breaks
      for (const char* p = out; *p != '\0'; ++p) {
        if (*p != '\n') flat += *p;
      }
      TEST_ASSERT_TRUE(flat.compare(0, sl, sum) == 0);
    }
  }
}

static void test_time_helpers_survive_clock_wraparound() {
  TEST_ASSERT_EQUAL_UINT32(78, seconds_left(1000, 1000 + 78000));
  TEST_ASSERT_EQUAL_UINT32(1, seconds_left(1000, 1001));        // rounds up
  TEST_ASSERT_EQUAL_UINT32(1, seconds_left(1000, 2000));
  TEST_ASSERT_EQUAL_UINT32(2, seconds_left(1000, 2001));
  TEST_ASSERT_EQUAL_UINT32(0, seconds_left(2000, 2000));
  TEST_ASSERT_EQUAL_UINT32(0, seconds_left(2001, 2000));
  TEST_ASSERT_FALSE(deadline_passed(1999, 2000));
  TEST_ASSERT_TRUE(deadline_passed(2000, 2000));
  const uint32_t near_wrap = 0xFFFFFF00u;
  const uint32_t d = deadline_from(near_wrap, 3);  // wraps past zero
  TEST_ASSERT_TRUE(d < near_wrap);
  TEST_ASSERT_FALSE(deadline_passed(near_wrap, d));
  TEST_ASSERT_EQUAL_UINT32(3, seconds_left(near_wrap, d));
  TEST_ASSERT_TRUE(deadline_passed(d + 1, d));
  TEST_ASSERT_EQUAL_UINT32(1000u + kMaxExpiresS * 1000u, deadline_from(1000, 99999));
}

static void test_number_formats() {
  char out[24];
  format_cost(0.0123f, out, sizeof(out));
  TEST_ASSERT_EQUAL_STRING("$0.0123", out);
  format_cost(0.0f, out, sizeof(out));
  TEST_ASSERT_EQUAL_STRING("$0.0000", out);
  format_cost(0.99994f, out, sizeof(out));
  TEST_ASSERT_EQUAL_STRING("$0.9999", out);
  format_cost(1.0f, out, sizeof(out));
  TEST_ASSERT_EQUAL_STRING("$1.00", out);
  format_cost(12.5f, out, sizeof(out));
  TEST_ASSERT_EQUAL_STRING("$12.50", out);
  format_cost(-4.0f, out, sizeof(out));
  TEST_ASSERT_EQUAL_STRING("$0.0000", out);
  format_cost(1000000.0f, out, sizeof(out));
  TEST_ASSERT_EQUAL_STRING("$1000000.00", out);
  format_percent(8.0f, out, sizeof(out));
  TEST_ASSERT_EQUAL_STRING("8%", out);
  format_percent(12.5f, out, sizeof(out));
  TEST_ASSERT_EQUAL_STRING("12.5%", out);
  format_percent(0.0f, out, sizeof(out));
  TEST_ASSERT_EQUAL_STRING("0%", out);
  format_percent(100.0f, out, sizeof(out));
  TEST_ASSERT_EQUAL_STRING("100%", out);
  format_percent(99.96f, out, sizeof(out));
  TEST_ASSERT_EQUAL_STRING("100%", out);
  format_percent(-3.0f, out, sizeof(out));
  TEST_ASSERT_EQUAL_STRING("0%", out);
  format_age(7, out, sizeof(out));
  TEST_ASSERT_EQUAL_STRING("7 s ago", out);
  format_age(60, out, sizeof(out));
  TEST_ASSERT_EQUAL_STRING("1 min ago", out);
  format_age(3599, out, sizeof(out));
  TEST_ASSERT_EQUAL_STRING("59 min ago", out);
  format_age(3600, out, sizeof(out));
  TEST_ASSERT_EQUAL_STRING("1 h ago", out);
  format_age(90000, out, sizeof(out));
  TEST_ASSERT_EQUAL_STRING("1 d ago", out);
}

static void test_usage_line() {
  char out[48];
  Status s = make_status();
  format_usage_line(s, out, sizeof(out));
  TEST_ASSERT_EQUAL_STRING("Usage Opus $0.0123 ctx 8%", out);

  // the plan percentages are added only when present and only when they fit on the row
  s.has_cost = false;
  s.has_five_hour = true;
  s.five_hour_pct = 12.0f;
  s.has_seven_day = true;
  s.seven_day_pct = 3.0f;
  std::snprintf(s.model, sizeof(s.model), "Op");
  format_usage_line(s, out, sizeof(out));
  TEST_ASSERT_EQUAL_STRING("Usage Op ctx 8% 5h 12% 7d 3%", out);
  TEST_ASSERT_TRUE(std::strlen(out) <= static_cast<size_t>(kRowCols));

  s.has_context = false;
  s.has_five_hour = false;
  s.has_seven_day = false;
  format_usage_line(s, out, sizeof(out));
  TEST_ASSERT_EQUAL_STRING("Usage Op", out);  // nothing else was sent

  std::snprintf(s.model, sizeof(s.model), "claude-opus-4-1-20250805");
  s.has_cost = true;
  s.has_context = true;
  s.has_five_hour = s.has_seven_day = true;
  format_usage_line(s, out, sizeof(out));
  TEST_ASSERT_TRUE(std::strlen(out) <= static_cast<size_t>(kRowCols));
  TEST_ASSERT_TRUE(contains(out, "claude-op~"));

  Status none;
  std::memset(&none, 0, sizeof(none));
  format_usage_line(none, out, sizeof(out));
  TEST_ASSERT_EQUAL_STRING("Usage: no status yet", out);
}

static void test_status_body() {
  char out[560];
  Status none;
  std::memset(&none, 0, sizeof(none));
  format_status_body(none, 0, out, sizeof(out));
  TEST_ASSERT_EQUAL_STRING("No Claude Code status received yet (see docs/CLAUDE_CODE_LINK.md).", out);

  Status s = make_status();
  format_status_body(s, 125, out, sizeof(out));
  TEST_ASSERT_TRUE(contains(out, "Model: Opus"));
  TEST_ASSERT_TRUE(contains(out, "Cost (estimate): $0.0123"));
  TEST_ASSERT_TRUE(contains(out, "Context used: 8%"));
  TEST_ASSERT_TRUE(contains(out, "Updated 2 min ago."));
  TEST_ASSERT_FALSE(contains(out, "plan"));  // the backend sent no plan numbers
  s.has_five_hour = true;
  s.five_hour_pct = 12.5f;
  s.has_seven_day = true;
  s.seven_day_pct = 3.0f;
  format_status_body(s, 7, out, sizeof(out));
  TEST_ASSERT_TRUE(contains(out, "5-hour plan used: 12.5%"));
  TEST_ASSERT_TRUE(contains(out, "7-day plan used: 3%"));
  TEST_ASSERT_TRUE(contains(out, "Updated 7 s ago."));
  TEST_ASSERT_TRUE(std::strlen(out) < 400);

  Status tiny = s;
  format_status_body(tiny, 0, out, 20);  // a small buffer is cut, never overrun
  TEST_ASSERT_TRUE(std::strlen(out) < 20);
}

static void test_answer_texts() {
  char title[32];
  char body[200];
  format_answer_ok(true, "Bash", title, sizeof(title), body, sizeof(body));
  TEST_ASSERT_EQUAL_STRING("Allowed", title);
  TEST_ASSERT_TRUE(contains(body, "Claude Code: Bash"));
  TEST_ASSERT_TRUE(contains(body, "Allowed once."));
  TEST_ASSERT_TRUE(contains(body, "may still ask or refuse"));
  format_answer_ok(false, "Bash", title, sizeof(title), body, sizeof(body));
  TEST_ASSERT_EQUAL_STRING("Denied", title);
  TEST_ASSERT_TRUE(contains(body, "Denied."));
  format_expired("Write", title, sizeof(title), body, sizeof(body));
  TEST_ASSERT_EQUAL_STRING("!Expired", title);
  TEST_ASSERT_TRUE(contains(body, "Answer it at the PC"));
}

static oyster::deck::ApiError err_of(const char* code, const char* detail = "") {
  oyster::deck::ApiError e;
  std::memset(&e, 0, sizeof(e));
  std::snprintf(e.code, sizeof(e.code), "%s", code);
  std::snprintf(e.detail, sizeof(e.detail), "%s", detail);
  return e;
}

static void test_failures_have_plain_texts() {
  struct Case {
    int status;
    const char* code;
    const char* detail;
    FailureKind kind;
    const char* title;
    const char* text;
  };
  const Case cases[] = {
      {-1, "", "", FailureKind::NotSent, "!Not sent", "NOT sent"},
      {-2, "", "", FailureKind::NotSent, "!Not sent", "radio is busy"},
      {-6, "", "", FailureKind::NotSent, "!Not sent", "clock is not set"},
      {kStatusNoMemory, "", "", FailureKind::NotSent, "!Not sent", "Out of memory"},
      {kStatusBuildFailed, "", "", FailureKind::NotSent, "!Not sent", "NOT sent"},
      {-3, "", "", FailureKind::Unknown, "!Status unknown", "may or may not have been recorded"},
      {-4, "", "", FailureKind::Unknown, "!Status unknown", "NOT sent again"},
      {-5, "", "", FailureKind::Unknown, "!Status unknown", "NOT sent again"},
      {200, "", "", FailureKind::Unknown, "!Status unknown", "NOT sent again"},
      {409, "expired", "the approval window has closed", FailureKind::TooLate, "!Too late", "timed out"},
      {409, "already_decided", "x", FailureKind::AlreadyAnswered, "!Already answered", "Not sent again"},
      {409, "something_new", "x", FailureKind::Other, "!Not done", "HTTP 409"},
      {403, "forbidden", "decision rejected", FailureKind::Rejected, "!Rejected", "stale"},
      {401, "unauthorized", "x", FailureKind::Rejected, "!Rejected", "Enrol"},
      {404, "not_found", "unknown id", FailureKind::Gone, "!No longer there", "no longer exists"},
      {429, "rate_limited", "too many requests; retry in 9 s", FailureKind::RateLimited, "!Rate limit", "Wait 9 s"},
      {429, "rate_limited", "", FailureKind::RateLimited, "!Rate limit", "Wait a moment"},
      {500, "", "", FailureKind::Other, "!Not done", "HTTP 500"},
      {503, "busy", "", FailureKind::Other, "!Not done", "NOT sent again"},
      {422, "validation_error", "", FailureKind::Other, "!Not done", "HTTP 422"},
  };
  for (const Case& c : cases) {
    const oyster::deck::ApiError e = err_of(c.code, c.detail);
    TEST_ASSERT_TRUE_MESSAGE(classify_failure(c.status, e) == c.kind, c.text);
    char title[32];
    char body[200];
    format_failure(c.kind, c.status, e, "Bash", title, sizeof(title), body, sizeof(body));
    TEST_ASSERT_EQUAL_STRING(c.title, title);
    TEST_ASSERT_TRUE_MESSAGE(contains(body, c.text), body);
    TEST_ASSERT_TRUE(contains(body, "Claude Code: Bash"));
    TEST_ASSERT_TRUE(std::strlen(title) <= 31);
    TEST_ASSERT_FALSE(contains(body, "SECRETNONCE"));
  }
  // a retry hint that is not a number is ignored
  char title[32];
  char body[200];
  const oyster::deck::ApiError e = err_of("rate_limited", "retry in soon");
  format_failure(FailureKind::RateLimited, 429, e, "Bash", title, sizeof(title), body, sizeof(body));
  TEST_ASSERT_TRUE(contains(body, "Wait a moment"));
}

static void test_failure_text_for_a_real_error_body() {
  const char body_json[] = "{\"error\":\"expired\",\"detail\":\"the approval window has closed\"}";
  oyster::deck::ApiError e;
  oyster::deck::parse_api_error(body_json, sizeof(body_json) - 1, e);
  TEST_ASSERT_TRUE(classify_failure(409, e) == FailureKind::TooLate);
  const char junk[] = "<html>502</html>";
  oyster::deck::parse_api_error(junk, sizeof(junk) - 1, e);
  TEST_ASSERT_TRUE(classify_failure(409, e) == FailureKind::Other);  // an unreadable body still gets a plain text
}

void run_claude_text_tests() {
  RUN_TEST(test_entry_row_states);
  RUN_TEST(test_approval_row_shape);
  RUN_TEST(test_approval_row_clips_and_never_hides_the_time);
  RUN_TEST(test_approval_row_without_details);
  RUN_TEST(test_approval_row_is_always_one_row_wide);
  RUN_TEST(test_confirm_fields);
  RUN_TEST(test_wrap_rows_matches_the_widget_wrap);
  RUN_TEST(test_confirm_effect_shows_the_summary_in_full_and_a_hint_when_rows_remain);
  RUN_TEST(test_confirm_effect_never_stays_empty);
  RUN_TEST(test_confirm_effect_hint_never_pushes_the_summary_out_of_the_widget);
  RUN_TEST(test_time_helpers_survive_clock_wraparound);
  RUN_TEST(test_number_formats);
  RUN_TEST(test_usage_line);
  RUN_TEST(test_status_body);
  RUN_TEST(test_answer_texts);
  RUN_TEST(test_failures_have_plain_texts);
  RUN_TEST(test_failure_text_for_a_real_error_body);
}
