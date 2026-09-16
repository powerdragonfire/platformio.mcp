#include <unity.h>
int add(int a, int b);
void setUp(void) {}
void tearDown(void) {}
void test_add(void) { TEST_ASSERT_EQUAL(5, add(2, 3)); }
void test_add_fail(void) { TEST_ASSERT_EQUAL(6, add(2, 3)); }
int main() { UNITY_BEGIN(); RUN_TEST(test_add); RUN_TEST(test_add_fail); return UNITY_END(); }
