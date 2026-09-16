#include <cstdio>
int add(int a, int b) { return a + b; }
#ifndef PIO_UNIT_TESTING
int main() { std::printf("hello %d\n", add(2, 3)); return 0; }
#endif
