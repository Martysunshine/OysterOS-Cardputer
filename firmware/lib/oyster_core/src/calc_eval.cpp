// Recursive-descent arithmetic evaluator for the Scratchpad module. See oyster_core/calc_eval.h.
//
// Grammar (lowest precedence first):
//   expr    := term   (('+' | '-') term)*
//   term    := factor (('*' | '/' | '%') factor)*
//   factor  := ('+' | '-') factor | primary
//   primary := number | '(' expr ')'
//
// Recursion depth is bounded by the number of parentheses in the input, which the UI caps at the entry length
// (a few dozen characters), so the parser cannot overflow the task stack. strtod() does the number parsing.
#include "oyster_core/calc_eval.h"

#include <cmath>
#include <cstdio>
#include <cstdlib>

namespace oyster::calc {
namespace {

struct Parser {
  const char* p;
  bool ok = true;
  const char* err = nullptr;
};

void skip_ws(Parser& s) {
  while (*s.p == ' ' || *s.p == '\t') ++s.p;
}

void fail(Parser& s, const char* msg) {
  if (s.ok) {  // keep the first error, not the last
    s.ok = false;
    s.err = msg;
  }
}

double parse_expr(Parser& s);

double parse_primary(Parser& s) {
  skip_ws(s);
  if (*s.p == '(') {
    ++s.p;
    const double v = parse_expr(s);
    skip_ws(s);
    if (*s.p == ')') {
      ++s.p;
    } else {
      fail(s, "Missing )");
    }
    return v;
  }
  char* end = nullptr;
  const double v = std::strtod(s.p, &end);
  if (end == s.p) {  // no number here
    fail(s, "Bad expression");
    return 0.0;
  }
  s.p = end;
  return v;
}

double parse_factor(Parser& s) {
  skip_ws(s);
  if (*s.p == '+') {
    ++s.p;
    return parse_factor(s);
  }
  if (*s.p == '-') {
    ++s.p;
    return -parse_factor(s);
  }
  return parse_primary(s);
}

double parse_term(Parser& s) {
  double v = parse_factor(s);
  for (;;) {
    if (!s.ok) return 0.0;
    skip_ws(s);
    const char c = *s.p;
    if (c == '*') {
      ++s.p;
      v *= parse_factor(s);
    } else if (c == '/') {
      ++s.p;
      const double r = parse_factor(s);
      if (r == 0.0) {
        fail(s, "Divide by zero");
        return 0.0;
      }
      v /= r;
    } else if (c == '%') {
      ++s.p;
      const double r = parse_factor(s);
      if (r == 0.0) {
        fail(s, "Divide by zero");
        return 0.0;
      }
      v = std::fmod(v, r);
    } else {
      break;
    }
  }
  return v;
}

double parse_expr(Parser& s) {
  double v = parse_term(s);
  for (;;) {
    if (!s.ok) return 0.0;
    skip_ws(s);
    const char c = *s.p;
    if (c == '+') {
      ++s.p;
      v += parse_term(s);
    } else if (c == '-') {
      ++s.p;
      v -= parse_term(s);
    } else {
      break;
    }
  }
  return v;
}

}  // namespace

bool eval(const char* expr, double* out, char* err, size_t errcap) {
  if (err != nullptr && errcap > 0) err[0] = '\0';
  auto report = [&](const char* msg) {
    if (err != nullptr && errcap > 0) std::snprintf(err, errcap, "%s", msg);
  };

  if (expr == nullptr) {
    report("Empty");
    return false;
  }
  const char* q = expr;
  while (*q == ' ' || *q == '\t') ++q;
  if (*q == '\0') {
    report("Empty");
    return false;
  }

  Parser s{expr};
  const double v = parse_expr(s);
  if (!s.ok) {
    report(s.err != nullptr ? s.err : "Bad expression");
    return false;
  }
  skip_ws(s);
  if (*s.p != '\0') {  // something left over the grammar did not consume
    report("Bad expression");
    return false;
  }
  if (!std::isfinite(v)) {
    report("Out of range");
    return false;
  }
  if (out != nullptr) *out = v;
  return true;
}

void format(double value, char* out, size_t cap) {
  if (out == nullptr || cap == 0) return;
  if (!std::isfinite(value)) {
    std::snprintf(out, cap, "error");
    return;
  }
  if (value == 0.0) value = 0.0;  // fold -0.0 so it prints as "0"
  std::snprintf(out, cap, "%.10g", value);
}

}  // namespace oyster::calc
