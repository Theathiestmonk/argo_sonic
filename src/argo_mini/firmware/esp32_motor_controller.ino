#include "driver/dac.h"

// ── Hall sensors ───────────────────────────────────────────────────────────
#define HALL_LA 32
#define HALL_LB 34
#define HALL_LC 35
#define HALL_RA 13
#define HALL_RB 14
#define HALL_RC 27

// ── DAC channels ───────────────────────────────────────────────────────────
// GPIO25 = DAC_CHANNEL_1 → LEFT  motor throttle
// GPIO26 = DAC_CHANNEL_2 → RIGHT motor throttle
#define THROTTLE_L DAC_CHANNEL_1   // GPIO25
#define THROTTLE_R DAC_CHANNEL_2   // GPIO26

// ── DAC range (hub-motor ESC throttle) ─────────────────────────────────────
// 104 ≈ neutral/stop, 100 = min reverse, 120 = max forward
#define DAC_MIN    100
#define DAC_MAX    120
#define POLE_PAIRS 15

// ── Ramp parameters ────────────────────────────────────────────────────────
#define RAMP_STEP  1    // 1 DAC unit per cycle
#define RAMP_MS    20   // 20 ms → smooth 200 ms ramp from 100 to 120

// ── Odometry counters ──────────────────────────────────────────────────────
volatile long     leftTicks   = 0;
volatile long     rightTicks  = 0;
volatile uint32_t leftPulses  = 0;
volatile uint32_t rightPulses = 0;

void IRAM_ATTR leftISR()  { leftTicks++;  leftPulses++;  }
void IRAM_ATTR rightISR() { rightTicks++; rightPulses++; }

int      targetL  = 0, targetR  = 0;
int      currentL = 0, currentR = 0;
uint32_t lastRamp  = 0;
uint32_t lastPrint = 0;

// ── DAC output ─────────────────────────────────────────────────────────────
// value = 0   → motor off (DAC outputs 0 V)
// value ≠ 0   → constrain to [DAC_MIN, DAC_MAX]
void setDAC(int l, int r) {
  if (l == 0) dac_output_voltage(THROTTLE_L, 0);
  else        dac_output_voltage(THROTTLE_L, constrain(l, DAC_MIN, DAC_MAX));

  if (r == 0) dac_output_voltage(THROTTLE_R, 0);
  else        dac_output_voltage(THROTTLE_R, constrain(r, DAC_MIN, DAC_MAX));
}

// ── Smooth ramp ─────────────────────────────────────────────────────────────
int rampToward(int current, int target) {
  if (target == 0)               return 0;        // stop immediately, safety
  if (current == 0 && target > 0) return DAC_MIN; // jump-start from rest
  if (current < target) return min(current + RAMP_STEP, target);
  if (current > target) return max(current - RAMP_STEP, target);
  return current;
}

void setup() {
  Serial.begin(115200);
  Serial.setTimeout(50);

  // Hall sensor inputs
  pinMode(HALL_LA, INPUT); pinMode(HALL_LB, INPUT); pinMode(HALL_LC, INPUT);
  pinMode(HALL_RA, INPUT); pinMode(HALL_RB, INPUT); pinMode(HALL_RC, INPUT);

  // 3 hall phases × RISING per wheel
  attachInterrupt(digitalPinToInterrupt(HALL_LA), leftISR,  RISING);
  attachInterrupt(digitalPinToInterrupt(HALL_LB), leftISR,  RISING);
  attachInterrupt(digitalPinToInterrupt(HALL_LC), leftISR,  RISING);
  attachInterrupt(digitalPinToInterrupt(HALL_RA), rightISR, RISING);
  attachInterrupt(digitalPinToInterrupt(HALL_RB), rightISR, RISING);
  attachInterrupt(digitalPinToInterrupt(HALL_RC), rightISR, RISING);

  dac_output_enable(THROTTLE_L);
  dac_output_enable(THROTTLE_R);
  setDAC(0, 0);

  Serial.println("ARGO MINI READY");
}

void loop() {
  uint32_t now = millis();

  // ── Serial command parser ─────────────────────────────────────────────────
  if (Serial.available()) {
    String line = Serial.readStringUntil('\n');
    line.trim();

    if (line.startsWith("V ")) {
      int spaceIdx = line.indexOf(' ', 2);
      if (spaceIdx > 0) {
        targetL = line.substring(2, spaceIdx).toInt();
        targetR = line.substring(spaceIdx + 1).toInt();
      }
    } else if (line == "S") {
      targetL = 0; targetR = 0;
      currentL = 0; currentR = 0;
      setDAC(0, 0);
      Serial.println("STOP");
    }
  }

  // ── Smooth ramp every RAMP_MS ─────────────────────────────────────────────
  if (now - lastRamp >= RAMP_MS) {
    currentL = rampToward(currentL, targetL);
    currentR = rampToward(currentR, targetR);
    setDAC(currentL, currentR);
    lastRamp = now;
  }

  // ── Odometry publish at 20 Hz ─────────────────────────────────────────────
  if (now - lastPrint >= 50) {
    float elapsed = (now - lastPrint) / 1000.0f;

    noInterrupts();
    uint32_t lp = leftPulses;  leftPulses  = 0;
    uint32_t rp = rightPulses; rightPulses = 0;
    long lt = leftTicks;
    long rt = rightTicks;
    interrupts();

    float lRPM = (lp / elapsed) * 60.0f / (POLE_PAIRS * 3);
    float rRPM = (rp / elapsed) * 60.0f / (POLE_PAIRS * 3);

    Serial.printf("O %ld %ld\n", lt, rt);

    if (now % 500 < 50) {
      Serial.printf("R %.1f %.1f\n", lRPM, rRPM);
    }

    lastPrint = now;
  }
}
