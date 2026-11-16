#include <Arduino.h>

__attribute__((noipa)) void level3(volatile int *p) { *p = 42; }
__attribute__((noipa)) void level2(volatile int *p) { level3(p); Serial.println("unreachable"); }
__attribute__((noipa)) void level1() { level2((volatile int *)nullptr); }

void setup() {
  Serial.begin(115200);
  delay(500);
  Serial.println("boot");
  level1();
}

void loop() {}
