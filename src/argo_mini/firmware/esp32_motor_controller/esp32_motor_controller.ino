#include "driver/dac.h"
#include <Wire.h>

// ── Hall sensors ────────────────────────────────────────────────────────────
#define HALL_LA 32
#define HALL_LB 34
#define HALL_LC 35
#define HALL_RA 13
#define HALL_RB 14
#define HALL_RC 27

// ── DAC channels ────────────────────────────────────────────────────────────
#define THROTTLE_L DAC_CHAN_0   // GPIO25
#define THROTTLE_R DAC_CHAN_1   // GPIO26

// ── Direction pins (active-LOW: LOW = reverse) ─────────────────────────────
// Wheels were previously mounted backward, needing an inverted DIR sense to
// compensate in firmware — that's since been fixed by physically rewiring
// the wheels correctly, so this is back to the original, uninverted polarity.
#define DIR_R 2
#define DIR_L 4

// ── DAC range ───────────────────────────────────────────────────────────────
#define DAC_MIN       104   // default floor for both wheels (108 left the left wheel ~20% fast at 25 RPM; 104 vs 100 indistinguishable in testing, and motors reportedly don't start below 104)
#define DAC_MAX       135
// Per-wheel floor, changeable at runtime with "M <left> <right>" (no reflash).
// Measured 2026-10-31: with a shared floor of 108 the left motor cannot run
// below ~37 RPM while the right reaches ~30, so slow straight drives curve right.
int dacMinL = DAC_MIN, dacMinR = DAC_MIN;
#define POLE_PAIRS    10
#define TICKS_PER_REV (POLE_PAIRS * 6)

// ── Ultrasonic sensors (HC-SR04) ────────────────────────────────────────────
// Single shared trigger; each sensor has its own echo pin.
// All 4 sensors fire simultaneously on each trigger pulse.
#define US_TRIG          16
#define US_NUM           4
#define US_INTERVAL_MS   100UL   // 10 Hz; max echo at 4 m → 23 ms, well within window
#define US_TIMEOUT_US    25000UL // ~4.25 m; echo longer than this = no object

const uint8_t US_ECHO[US_NUM] = {18, 19, 5, 17};  // FL, FR, BL, BR

volatile uint32_t _usRise[US_NUM];   // micros() at echo rising edge
volatile uint32_t _usDur[US_NUM];    // echo pulse width in µs
volatile bool     _usHit[US_NUM];    // true once a complete echo is captured

// One ISR per echo pin — CHANGE fires on both rising and falling edge.
// On rising edge: record start time.
// On falling edge: record duration if a valid rise was seen.
#define DEF_US_ISR(IDX, PIN)                                        \
  void IRAM_ATTR _usISR_##IDX() {                                   \
    if (digitalRead(PIN)) {                                          \
      _usRise[IDX] = micros();                                       \
    } else if (_usRise[IDX]) {                                       \
      _usDur[IDX] = micros() - _usRise[IDX];                        \
      _usHit[IDX] = true;                                            \
    }                                                                \
  }

DEF_US_ISR(0, 18)
DEF_US_ISR(1, 19)
DEF_US_ISR(2,  5)
DEF_US_ISR(3, 17)

// ── Light relay (shares the back ultrasonic echo pins) ──────────────────────
// A relay signal wire is on one of the BACK echo pins (GPIO5 = BL, GPIO17 = BR;
// which one is chosen from the UI). Serial "L5 <0|1>" / "L17 <0|1>" turns that
// pin into an output and drives it; that sensor then reports -1 (no echo).
// The light is wired on the relay's NORMALLY-CLOSED contact, so the light is ON
// while the relay coil is de-energised.
//   RELAY_ACTIVE_LOW 1: module energises on LOW  -> light ON = pin HIGH
//   RELAY_ACTIVE_LOW 0: module energises on HIGH -> light ON = pin LOW
// If the light behaves backwards, flip this one define and reflash.
#define RELAY_ACTIVE_LOW 1
bool _usRelay[US_NUM] = {false, false, false, false};   // slot taken over by a relay

void setLightRelay(int gpio, bool lightOn) {
  int idx = -1;
  for (int i = 0; i < US_NUM; i++) if (US_ECHO[i] == gpio) idx = i;
  if (idx < 2) return;                       // only the back pair (slots 2,3)
  if (!_usRelay[idx]) {
    detachInterrupt(digitalPinToInterrupt(gpio));
    _usRelay[idx] = true;
    _usHit[idx] = false;
  }
  bool coilOn = !lightOn;                    // NC contact: light on = coil off
  bool level  = RELAY_ACTIVE_LOW ? !coilOn : coilOn;
  digitalWrite(gpio, level);                 // set level before enabling output
  pinMode(gpio, OUTPUT);
  digitalWrite(gpio, level);
  Serial.printf("LIGHT %d %d\n", gpio, lightOn ? 1 : 0);
}

// Fire a 10 µs trigger pulse and reset capture state.
// delayMicroseconds(10) blocks ~0.02 % of the 50 ms motor loop — negligible.
void fireUS() {
  for (int i = 0; i < US_NUM; i++) { _usRise[i] = 0; _usHit[i] = false; }
  digitalWrite(US_TRIG, HIGH);
  delayMicroseconds(10);
  digitalWrite(US_TRIG, LOW);
}

// Non-blocking — call every loop() iteration.
// Reads echo results from the previous trigger, publishes them, then re-fires.
static uint32_t _lastUsTrigMs = 0;

void updateUS(uint32_t now) {
  if (now - _lastUsTrigMs < US_INTERVAL_MS) return;
  _lastUsTrigMs = now;

  int d[US_NUM];
  for (int i = 0; i < US_NUM; i++) {
    if (!_usRelay[i] && _usHit[i] && _usDur[i] < US_TIMEOUT_US) {
      int cm = (int)(_usDur[i] / 58U);          // µs / 58 → cm (round-trip)
      d[i] = (cm >= 2 && cm <= 400) ? cm : -1;  // clamp to sensor spec
    } else {
      d[i] = -1;   // timeout or no echo → out of range
    }
  }

  // "U <fl_cm> <fr_cm> <bl_cm> <br_cm>"  (-1 = out of range / no echo)
  Serial.printf("U %d %d %d %d\n", d[0], d[1], d[2], d[3]);

  fireUS();   // arm the next reading cycle
}

// ── Left encoder correction (left encoder reads half pulses vs right) ──────
// Multiply left measured RPM by this factor to get true physical RPM.
// Set to match left_tick_scale in serial_bridge.py (calibrated = 2.0)
#define LEFT_TICK_SCALE 0.66f

// ── RPM velocity PI controller ──────────────────────────────────────────────
// Tune on hardware if needed:
//   KP too high → oscillation/hunting around target RPM
//   KI too high → overshoot and slow recovery
//   KP too low  → slow to reach target, sluggish response
//   KI too low  → steady-state error (actual RPM doesn't match target)
// Lowered from 3.0 / 2.0: measured RPM moves in ~5 RPM steps (one encoder tick
// per 200 ms window), so KP=3 turned a single tick of noise into ~15 DAC counts
// on a throttle range only DAC_MAX-DAC_MIN = 31 counts wide -> surge/jerk.
#define KP     0.8f
#define KI     0.6f
#define I_MAX  12.0f   // anti-windup: caps integral contribution

// ── Direction flags ─────────────────────────────────────────────────────────
volatile bool leftReverse  = false;
volatile bool rightReverse = false;

// ── Odometry counters ───────────────────────────────────────────────────────
volatile long     leftTicks   = 0;
volatile long     rightTicks  = 0;
volatile uint32_t leftPulses  = 0;
volatile uint32_t rightPulses = 0;

void IRAM_ATTR leftISR() {
  if (leftReverse) leftTicks -= 1; else leftTicks += 1;
  leftPulses += 1;
}
void IRAM_ATTR rightISR() {
  if (rightReverse) rightTicks -= 1; else rightTicks += 1;
  rightPulses += 1;
}

// ── Motor drive ──────────────────────────────────────────────────────────────
// Direction comes from the global flags, NOT the sign of l/r.
// This way setDAC(0, 0) during a hold still drives DIR correctly.
void setDAC(int l, int r) {
  digitalWrite(DIR_L, leftReverse  ? LOW : HIGH);
  digitalWrite(DIR_R, rightReverse ? LOW : HIGH);
  dac_output_voltage(THROTTLE_L, (l == 0) ? 0 : constrain(abs(l), dacMinL, DAC_MAX));
  dac_output_voltage(THROTTLE_R, (r == 0) ? 0 : constrain(abs(r), dacMinR, DAC_MAX));
}

// ── RPM targets and PI state ────────────────────────────────────────────────
float targetRpmL = 0.0f, targetRpmR = 0.0f;
float integralL  = 0.0f, integralR  = 0.0f;
int   dacL = 0, dacR = 0;
// Cycles to hold DAC=0 after a direction flip so the ESC sees the new DIR pin
// before throttle is applied (3 × 50 ms = 150 ms).
uint8_t holdCyclesL = 0, holdCyclesR = 0;
#define DIR_HOLD_CYCLES 3

// 0 = commands are used as sent. The old rule forced |right| = |left| whenever
// the signs differed, which assumed a tank turn is (+,-). With the right motor
// inverted in serial_bridge (invert_right) forward is (+,-), so the rule erased
// every curved forward/reverse command. Set to 1 only for the old wiring.
#define SYMMETRIC_TANK_RULE 0

// Speed is averaged over the last RPM_WIN control cycles (4 x 50 ms = 200 ms).
// One tick in a single 50 ms window is 20 RPM (right) / 13 RPM (left), far too
// coarse for the PI loop, which hunted between 20 and 40 RPM on a 30 RPM target.
#define RPM_WIN 4
// Extra smoothing on the windowed RPM (first-order low-pass, per 50 ms cycle).
// 0.3 -> ~160 ms time constant: hides the one-tick quantisation steps without
// making the loop sluggish.
#define RPM_LPF_ALPHA 0.3f
float filtRpmL = 0.0f, filtRpmR = 0.0f;
// Throttle slew limit, DAC counts per 50 ms cycle. The wheel can never be
// kicked by more than this in one cycle, whatever the PI computes. Down is a
// bit faster than up so stopping stays prompt (31-count range: full stop from
// top throttle in ~0.5 s). The 0 <-> floor step is the motor's own deadband
// and is not slewed.
#define DAC_SLEW_UP   2
#define DAC_SLEW_DOWN 3
// Next applied throttle given the current one and the PI's desired one (0 = off).
int slewDac(int cur, int want, int floorDac) {
  if (want <= 0) return (cur <= floorDac) ? 0 : max(floorDac, cur - DAC_SLEW_DOWN);
  if (cur < floorDac) return min(want, floorDac);          // start at the floor
  return (want > cur) ? min(want, cur + DAC_SLEW_UP) : max(want, cur - DAC_SLEW_DOWN);
}
uint32_t winL[RPM_WIN] = {0}, winR[RPM_WIN] = {0};
float    winDt[RPM_WIN] = {0};
uint8_t  winIdx = 0;

// ── IMU (MPU6050, I2C) ──────────────────────────────────────────────────────
// Only gyro Z (yaw rate) is used: serial_bridge.py blends it into odometry
// heading (IMU_ALPHA). Wiring: SDA=GPIO21, SCL=GPIO22 (ESP32 default I2C,
// both unused elsewhere in this sketch), VCC=3V3, GND, AD0 low -> addr 0x68
// (AD0 high -> 0x69; change MPU_ADDR). Mount flat with Z up: positive output
// = counter-clockwise seen from above (ROS convention, rad/s). If it is
// mounted differently / reads backwards, flip it on the Jetson with the
// serial_bridge parameter imu_flip_z:=true rather than editing this.
//
// Output: the odometry line becomes "O <left> <right> <gyro_z_rad_s>". If the
// IMU is missing or a read fails, the 3-field "O <left> <right>" is sent
// instead and serial_bridge falls back to wheel-only heading, exactly as
// before this was added.
#define IMU_SDA          21
#define IMU_SCL          22
#define MPU_ADDR         0x68
#define MPU_REG_PWR      0x6B
#define MPU_REG_CONFIG   0x1A
#define MPU_REG_GYRO_CFG 0x1B
#define MPU_REG_GYRO_ZH  0x47
#define MPU_REG_WHOAMI   0x75
#define MPU_GYRO_LSB_DPS 65.5f   // +/-500 dps full scale
#define IMU_CAL_SAMPLES  200     // ~1 s at 5 ms; robot must be stationary at boot

bool  imuOk      = false;
float gyroBiasZ  = 0.0f;         // raw LSB offset, measured at rest

bool mpuWrite(uint8_t reg, uint8_t val) {
  Wire.beginTransmission(MPU_ADDR);
  Wire.write(reg);
  Wire.write(val);
  return Wire.endTransmission() == 0;
}

// Raw signed gyro Z in LSB; false on any I2C failure.
bool mpuReadGyroZRaw(int16_t &raw) {
  Wire.beginTransmission(MPU_ADDR);
  Wire.write(MPU_REG_GYRO_ZH);
  if (Wire.endTransmission(false) != 0) return false;
  if (Wire.requestFrom((uint8_t)MPU_ADDR, (uint8_t)2) != 2) return false;
  uint8_t hi = Wire.read();
  uint8_t lo = Wire.read();
  raw = (int16_t)((hi << 8) | lo);
  return true;
}

// Average gyro Z at rest to remove its constant offset. Returns false if the
// IMU stopped answering. The same routine backs the runtime "G" command.
bool mpuCalibrate() {
  float sum = 0.0f;
  int n = 0;
  for (int i = 0; i < IMU_CAL_SAMPLES; i++) {
    int16_t raw;
    if (mpuReadGyroZRaw(raw)) { sum += raw; n++; }
    delay(5);
  }
  if (n < IMU_CAL_SAMPLES / 2) return false;
  gyroBiasZ = sum / n;
  return true;
}

bool mpuInit() {
  Wire.begin(IMU_SDA, IMU_SCL);
  Wire.setClock(400000);
  Wire.setTimeOut(5);
  if (!mpuWrite(MPU_REG_PWR, 0x00)) return false;        // wake (clears SLEEP)
  delay(100);
  mpuWrite(MPU_REG_CONFIG, 0x03);                         // DLPF ~44 Hz
  mpuWrite(MPU_REG_GYRO_CFG, 0x08);                       // +/-500 dps
  return mpuCalibrate();
}

// Yaw rate in rad/s (bias removed). false -> omit it from the O line.
bool imuGyroZ(float &rad_s) {
  if (!imuOk) return false;
  int16_t raw;
  if (!mpuReadGyroZRaw(raw)) return false;
  rad_s = ((raw - gyroBiasZ) / MPU_GYRO_LSB_DPS) * (PI / 180.0f);
  return true;
}

// ── Setup ────────────────────────────────────────────────────────────────────
void setup() {
  Serial.begin(115200);
  Serial.setTimeout(50);

  pinMode(HALL_LA, INPUT); pinMode(HALL_LB, INPUT); pinMode(HALL_LC, INPUT);
  pinMode(HALL_RA, INPUT); pinMode(HALL_RB, INPUT); pinMode(HALL_RC, INPUT);

  attachInterrupt(digitalPinToInterrupt(HALL_LA), leftISR,  CHANGE);
  attachInterrupt(digitalPinToInterrupt(HALL_LB), leftISR,  CHANGE);
  attachInterrupt(digitalPinToInterrupt(HALL_LC), leftISR,  CHANGE);
  attachInterrupt(digitalPinToInterrupt(HALL_RA), rightISR, CHANGE);
  attachInterrupt(digitalPinToInterrupt(HALL_RB), rightISR, CHANGE);
  attachInterrupt(digitalPinToInterrupt(HALL_RC), rightISR, CHANGE);

  pinMode(DIR_L, OUTPUT); digitalWrite(DIR_L, HIGH);
  pinMode(DIR_R, OUTPUT); digitalWrite(DIR_R, HIGH);

  dac_output_enable(THROTTLE_L);
  dac_output_enable(THROTTLE_R);
  setDAC(0, 0);

  // ── Ultrasonic setup ────────────────────────────────────────────────────────
  pinMode(US_TRIG, OUTPUT);
  digitalWrite(US_TRIG, LOW);
  for (int i = 0; i < US_NUM; i++) {
    pinMode(US_ECHO[i], INPUT);
  }
  attachInterrupt(digitalPinToInterrupt(18), _usISR_0, CHANGE);
  attachInterrupt(digitalPinToInterrupt(19), _usISR_1, CHANGE);
  attachInterrupt(digitalPinToInterrupt( 5), _usISR_2, CHANGE);
  attachInterrupt(digitalPinToInterrupt(17), _usISR_3, CHANGE);
  delay(10);   // let sensors settle before first trigger
  fireUS();    // prime the first reading cycle

  // ── IMU (keep still during boot: ~1 s gyro calibration) ────────────────────
  imuOk = mpuInit();
  Serial.println(imuOk ? "IMU OK" : "IMU NOT FOUND (wheel-only heading)");

  Serial.println("ARGO MINI READY");
}

// ── Loop ─────────────────────────────────────────────────────────────────────
void loop() {
  uint32_t now = millis();
  updateUS(now);   // non-blocking; publishes "U <fl> <fr> <bl> <br>" at 10 Hz

  // ── Serial command parser ──────────────────────────────────────────────────
  if (Serial.available()) {
    String line = Serial.readStringUntil('\n');
    line.trim();

    if (line.startsWith("V ")) {
      // Format: "V <rpm_left> <rpm_right>" — floats, signed (negative = reverse)
      int spaceIdx = line.indexOf(' ', 2);
      if (spaceIdx > 0) {
        float rL = line.substring(2, spaceIdx).toFloat();
        float rR = line.substring(spaceIdx + 1).toFloat();

#if SYMMETRIC_TANK_RULE
        // Enforce symmetric tank turns: equal magnitude, opposite direction
        if (rL > 0 && rR < 0) rR = -rL;
        else if (rL < 0 && rR > 0) rL = -rR;
#endif

        bool newLeftRev  = (rL < 0);
        bool newRightRev = (rR < 0);

        // On direction flip: clear integral and hold DAC at 0 so the ESC sees
        // the new DIR pin state before throttle is reapplied.
        if (newLeftRev  != leftReverse)  { integralL = 0.0f; holdCyclesL = DIR_HOLD_CYCLES; }
        if (newRightRev != rightReverse) { integralR = 0.0f; holdCyclesR = DIR_HOLD_CYCLES; }

        targetRpmL = rL;
        targetRpmR = rR;

        leftReverse  = newLeftRev;
        rightReverse = newRightRev;
        digitalWrite(DIR_L, leftReverse  ? LOW : HIGH);
        digitalWrite(DIR_R, rightReverse ? LOW : HIGH);
      }
    } else if (line.startsWith("M ")) {
      // "M <left> <right>": per-wheel throttle floor, clamped to a sane range.
      int sp = line.indexOf(' ', 2);
      if (sp > 0) {
        dacMinL = constrain(line.substring(2, sp).toInt(), 60, DAC_MAX);
        dacMinR = constrain(line.substring(sp + 1).toInt(), 60, DAC_MAX);
        integralL = 0.0f; integralR = 0.0f;
        Serial.printf("DACMIN %d %d\n", dacMinL, dacMinR);
      }
    } else if (line.startsWith("L")) {
      // "L<gpio> <0|1>": light relay on back echo pin (5 or 17). 1 = light on.
      int sp = line.indexOf(' ');
      if (sp > 1) {
        int gpio = line.substring(1, sp).toInt();
        int on   = line.substring(sp + 1).toInt();
        setLightRelay(gpio, on != 0);
      }
    } else if (line == "S") {
      targetRpmL = 0; targetRpmR = 0;
      integralL  = 0; integralR  = 0;
      dacL = 0;       dacR = 0;
      holdCyclesL = 0; holdCyclesR = 0;
      leftReverse  = false;
      rightReverse = false;
      setDAC(0, 0);   // globals false → DIR HIGH, DAC 0
      Serial.println("STOP");
    } else if (line == "R") {
      noInterrupts();
      leftTicks = 0; rightTicks = 0;
      interrupts();
      Serial.println("ODOM_RESET");
    } else if (line == "G") {
      // Re-zero the gyro; robot must be stationary (blocks ~1 s).
      if (!imuOk) imuOk = mpuInit();
      else        imuOk = mpuCalibrate();
      Serial.println(imuOk ? "IMU_CAL OK" : "IMU_CAL FAIL");
    }
  }

  // ── Odometry + PI velocity control at 20 Hz ─────────────────────────────────
  static uint32_t lastPrint = 0;
  if (now - lastPrint >= 50) {
    float elapsed = (now - lastPrint) / 1000.0f;
    lastPrint = now;

    noInterrupts();
    uint32_t lp = leftPulses;  leftPulses  = 0;
    uint32_t rp = rightPulses; rightPulses = 0;
    long lt = leftTicks;
    long rt = rightTicks;
    interrupts();

    // Measured RPM — apply LEFT_TICK_SCALE to correct encoder asymmetry
    winL[winIdx] = lp; winR[winIdx] = rp; winDt[winIdx] = elapsed;
    winIdx = (winIdx + 1) % RPM_WIN;
    uint32_t sumL = 0, sumR = 0; float sumDt = 0.0f;
    for (uint8_t i = 0; i < RPM_WIN; i++) { sumL += winL[i]; sumR += winR[i]; sumDt += winDt[i]; }
    if (sumDt < 0.001f) sumDt = elapsed;
    float rawRpmL = (float)sumL / sumDt * 60.0f / TICKS_PER_REV * LEFT_TICK_SCALE;
    float rawRpmR = (float)sumR / sumDt * 60.0f / TICKS_PER_REV;
    // At rest (no target, no pulses) snap to the raw value so a stale filtered
    // speed can't leak into the next start; otherwise low-pass it.
    if (abs(targetRpmL) < 1.0f) filtRpmL = rawRpmL; else filtRpmL += RPM_LPF_ALPHA * (rawRpmL - filtRpmL);
    if (abs(targetRpmR) < 1.0f) filtRpmR = rawRpmR; else filtRpmR += RPM_LPF_ALPHA * (rawRpmR - filtRpmR);
    float measRpmL = filtRpmL;
    float measRpmR = filtRpmR;

    // PI — left wheel
    if (holdCyclesL > 0) {
      // DIR pin already set; hold throttle at 0 so ESC recognises direction change
      dacL = 0;
      holdCyclesL--;
    } else if (abs(targetRpmL) < 1.0f) {
      dacL = slewDac(dacL, 0, dacMinL);
      integralL = 0.0f;
    } else {
      float errL = abs(targetRpmL) - measRpmL;
      integralL = constrain(integralL + errL * elapsed, -I_MAX, I_MAX);
      int wantL = constrain(dacMinL + (int)(KP * errL + KI * integralL), dacMinL, DAC_MAX);
      dacL = slewDac(dacL, wantL, dacMinL);
    }

    // PI — right wheel
    if (holdCyclesR > 0) {
      dacR = 0;
      holdCyclesR--;
    } else if (abs(targetRpmR) < 1.0f) {
      dacR = slewDac(dacR, 0, dacMinR);
      integralR = 0.0f;
    } else {
      float errR = abs(targetRpmR) - measRpmR;
      integralR = constrain(integralR + errR * elapsed, -I_MAX, I_MAX);
      int wantR = constrain(dacMinR + (int)(KP * errR + KI * integralR), dacMinR, DAC_MAX);
      dacR = slewDac(dacR, wantR, dacMinR);
    }

    // dacL/dacR are always positive magnitudes; setDAC uses leftReverse/rightReverse
    // globals for direction, so passing 0 during a hold still keeps DIR correct.
    setDAC(dacL, dacR);

    // Publish odometry ticks at 20 Hz
    float gz;
    if (imuGyroZ(gz)) Serial.printf("O %ld %ld %.4f\n", lt, rt, gz);
    else              Serial.printf("O %ld %ld\n", lt, rt);

    // Publish measured RPM at 2 Hz for monitoring
    if (now % 500 < 50) {
      Serial.printf("R %.1f %.1f\n", measRpmL, measRpmR);
      Serial.printf("D %d %d\n", dacL, dacR);   // throttle actually applied
    }
  }
}
