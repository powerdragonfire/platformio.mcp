#include <Arduino.h>
#include <SPI.h>

static constexpr uint8_t CS_PIN = PB12;

void setup() {
  Serial.begin(115200);
  delay(300);
  pinMode(CS_PIN, OUTPUT);
  digitalWrite(CS_PIN, HIGH);
  SPI.begin();
  Serial.println("BOOT_OK");
}

void loop() {
  const uint8_t tx = 0x5A;
  digitalWrite(CS_PIN, LOW);
  uint8_t rx = SPI.transfer(tx);
  digitalWrite(CS_PIN, HIGH);

  if (rx == tx) {
    Serial.println("SPI_LOOPBACK_OK");
  } else {
    Serial.print("test-failure: expected 0x5A got 0x");
    Serial.println(rx, HEX);
  }
  delay(50);
}
