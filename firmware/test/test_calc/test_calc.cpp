// Host tests for the Scratchpad arithmetic evaluator (oyster_core/calc_eval.h): precedence, parentheses,
// unary signs, modulo, formatting, and the error paths on untrusted input. Run: pio test -e native -f test_calc
#include <unity.h>

#include <cmath>
#include <cstring>
#include <string>

#include "oyster_core/calc_eval.h"

using namespace oyster::calc;

namespace {

// This project's native Unity build disables the double assertions, so arithmetic results are checked
// through format() (itself tested below), whose output is deterministic. calc_str evaluates and formats.
std::string calc_str(const char* expr) {
  double v = 0.0;
  char err[32];
  TEST_ASSERT_TRUE_MESSAGE(eval(expr, &v, err, sizeof(err)), expr);
  char out[32];
  format(v, out, sizeof(out));
  return std::string(out);
}

std::string error_of(const char* expr) {
  double v = 0.0;
  char err[32] = "";
  TEST_ASSERT_FALSE_MESSAGE(eval(expr, &v, err, sizeof(err)), expr);
  return std::string(err);
}

std::string formatted(double v) {
  char out[32];
  format(v, out, sizeof(out));
  return std::string(out);
}

}  // namespace

// ---- arithmetic -----------------------------------------------------------------------------------

void test_integer_addition() { TEST_ASSERT_EQUAL_STRING("7", calc_str("3+4").c_str()); }

void test_precedence_mul_over_add() { TEST_ASSERT_EQUAL_STRING("14", calc_str("2+3*4").c_str()); }

void test_parentheses_override_precedence() { TEST_ASSERT_EQUAL_STRING("20", calc_str("(2+3)*4").c_str()); }

void test_subtraction_left_to_right() { TEST_ASSERT_EQUAL_STRING("2", calc_str("10-5-3").c_str()); }

void test_division() { TEST_ASSERT_EQUAL_STRING("2.5", calc_str("5/2").c_str()); }

void test_decimals() { TEST_ASSERT_EQUAL_STRING("0.3", calc_str("0.1+0.2").c_str()); }

void test_unary_minus() { TEST_ASSERT_EQUAL_STRING("-6", calc_str("-2*3").c_str()); }

void test_unary_minus_in_parens() { TEST_ASSERT_EQUAL_STRING("5", calc_str("10+(-5)").c_str()); }

void test_double_negative() { TEST_ASSERT_EQUAL_STRING("5", calc_str("--5").c_str()); }

void test_unary_plus() { TEST_ASSERT_EQUAL_STRING("3", calc_str("+3").c_str()); }

void test_modulo() { TEST_ASSERT_EQUAL_STRING("1", calc_str("10%3").c_str()); }

void test_modulo_precedence() { TEST_ASSERT_EQUAL_STRING("4", calc_str("2+10%4").c_str()); }  // 2 + (10%4)

void test_nested_parentheses() { TEST_ASSERT_EQUAL_STRING("14", calc_str("2*((1+2)+4)").c_str()); }

void test_whitespace_ignored() { TEST_ASSERT_EQUAL_STRING("10", calc_str("  2 * 5  ").c_str()); }

// ---- error paths (untrusted input) ----------------------------------------------------------------

void test_empty_is_rejected() { TEST_ASSERT_EQUAL_STRING("Empty", error_of("").c_str()); }

void test_whitespace_only_is_rejected() { TEST_ASSERT_EQUAL_STRING("Empty", error_of("   ").c_str()); }

void test_null_is_rejected() {
  double v = 0.0;
  char err[32] = "";
  TEST_ASSERT_FALSE(eval(nullptr, &v, err, sizeof(err)));
  TEST_ASSERT_EQUAL_STRING("Empty", err);
}

void test_divide_by_zero() { TEST_ASSERT_EQUAL_STRING("Divide by zero", error_of("1/0").c_str()); }

void test_modulo_by_zero() { TEST_ASSERT_EQUAL_STRING("Divide by zero", error_of("5%0").c_str()); }

void test_trailing_junk_rejected() { TEST_ASSERT_EQUAL_STRING("Bad expression", error_of("2+3 foo").c_str()); }

void test_trailing_operator_rejected() { TEST_ASSERT_EQUAL_STRING("Bad expression", error_of("2+").c_str()); }

void test_bare_operator_rejected() { TEST_ASSERT_EQUAL_STRING("Bad expression", error_of("*").c_str()); }

void test_unbalanced_paren_rejected() { TEST_ASSERT_EQUAL_STRING("Missing )", error_of("(2+3").c_str()); }

void test_overflow_rejected() {
  // 1e308 * 10 overflows a double to +inf; eval reports it rather than returning inf.
  TEST_ASSERT_EQUAL_STRING("Out of range", error_of("1e308*10").c_str());
}

void test_out_param_untouched_on_error() {
  double v = 42.0;
  char err[32];
  TEST_ASSERT_FALSE(eval("1/0", &v, err, sizeof(err)));
  TEST_ASSERT_TRUE(v == 42.0);  // left exactly as the caller set it
}

void test_null_err_buffer_is_safe() {
  double v = 0.0;
  TEST_ASSERT_TRUE(eval("1+1", &v, nullptr, 0));
  TEST_ASSERT_TRUE(v == 2.0);
}

// ---- formatting -----------------------------------------------------------------------------------

void test_format_integer() { TEST_ASSERT_EQUAL_STRING("40", formatted(40.0).c_str()); }

void test_format_zero() { TEST_ASSERT_EQUAL_STRING("0", formatted(0.0).c_str()); }

void test_format_negative_zero_is_zero() { TEST_ASSERT_EQUAL_STRING("0", formatted(-0.0).c_str()); }

void test_format_trims_float_noise() {
  // 0.1 + 0.2 stored as a double; at 10 significant figures the noise is gone.
  TEST_ASSERT_EQUAL_STRING("0.3", formatted(0.1 + 0.2).c_str());
}

void test_format_fraction() { TEST_ASSERT_EQUAL_STRING("0.3333333333", formatted(1.0 / 3.0).c_str()); }

void test_format_large_is_scientific() {
  // Round-trips back to the same magnitude; exact text is %g's business, we only check it is compact.
  std::string s = formatted(1.23e20);
  TEST_ASSERT_TRUE(s.find('e') != std::string::npos);
}

void setUp() {}
void tearDown() {}

int main(int, char**) {
  UNITY_BEGIN();
  RUN_TEST(test_integer_addition);
  RUN_TEST(test_precedence_mul_over_add);
  RUN_TEST(test_parentheses_override_precedence);
  RUN_TEST(test_subtraction_left_to_right);
  RUN_TEST(test_division);
  RUN_TEST(test_decimals);
  RUN_TEST(test_unary_minus);
  RUN_TEST(test_unary_minus_in_parens);
  RUN_TEST(test_double_negative);
  RUN_TEST(test_unary_plus);
  RUN_TEST(test_modulo);
  RUN_TEST(test_modulo_precedence);
  RUN_TEST(test_nested_parentheses);
  RUN_TEST(test_whitespace_ignored);
  RUN_TEST(test_empty_is_rejected);
  RUN_TEST(test_whitespace_only_is_rejected);
  RUN_TEST(test_null_is_rejected);
  RUN_TEST(test_divide_by_zero);
  RUN_TEST(test_modulo_by_zero);
  RUN_TEST(test_trailing_junk_rejected);
  RUN_TEST(test_trailing_operator_rejected);
  RUN_TEST(test_bare_operator_rejected);
  RUN_TEST(test_unbalanced_paren_rejected);
  RUN_TEST(test_overflow_rejected);
  RUN_TEST(test_out_param_untouched_on_error);
  RUN_TEST(test_null_err_buffer_is_safe);
  RUN_TEST(test_format_integer);
  RUN_TEST(test_format_zero);
  RUN_TEST(test_format_negative_zero_is_zero);
  RUN_TEST(test_format_trims_float_noise);
  RUN_TEST(test_format_fraction);
  RUN_TEST(test_format_large_is_scientific);
  return UNITY_END();
}
