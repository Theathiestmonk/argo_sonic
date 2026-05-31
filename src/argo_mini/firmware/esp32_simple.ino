// Argo Mini — Simple ESP32 Firmware
// Pairs with esp32_teleop.py
//
// Serial protocol:
//   Receive:  "V <left_dac> <right_dac>\n"   signed: + = forward, - = reverse
//             "S\n"                            emergency stop
//   Send:     "O <leftTicks> <rightTicks>\n"  at 20 Hz (signed cumulative)

#include "driver/dac.h"

// ── Pins ────────────────────────────────────────────────────────────────────
#define HALL_LA 32
#define HALL_LB 34
#define HALL_LC 35
#define HALL_RA 13
#define HALL_RB 14
#define HALL_RC 27

#define THROTTLE_L DAC_CHANNEL_1   // GPIO25
#define THROTTLE_R DAC_CHANNEL_2   // GPIO26

#define DIR_L 2    // active-LOW: LOW = reverse
#define DIR_R 4

#define DAC_MIN  104
#define DAC_MAX  120
#define COAST_MS  80   // ms of neutral before direction change

// ── Odometry ─────────────────────────────────────────────────────────────────
volatile long leftTicks  = 0;
volatile long rightTicks = 0;
volatile bool leftRev    = false;
volatile bool rightRev   = false;

void IRAM_ATTR leftISR()  { if (leftRev)  leftTicks--;  else leftTicks++;  }
void IRAM_ATTR rightISR() { if (rightRev) rightTicks--; else rightTicks++; }

// ── Motor output ─────────────────────────────────────────────────────────────
// Hub-motor ESCs need to see DAC = 0 (neutral / coast) before they will
// accept a direction change.  Without the coast, the ESC ignores the reverse
// command and the motor either keeps going or just brakes.
// COAST_MS of neutral is applied any time either wheel changes direction.

void setMotors(int l, int r) {
    bool newLRev = (l < 0);
    bool newRRev = (r < 0);

    if (newLRev != leftRev || newRRev != rightRev) {
        // Coast both wheels briefly so the ESC registers neutral
        dac_output_voltage(THROTTLE_L, 0);
        dac_output_voltage(THROTTLE_R, 0);
        delay(COAST_MS);

        leftRev  = newLRev;
        rightRev = newRRev;
        digitalWrite(DIR_L, leftRev  ? LOW : HIGH);
        digitalWrite(DIR_R, rightRev ? LOW : HIGH);
    }

    dac_output_voltage(THROTTLE_L, l == 0 ? 0 : constrain(abs(l), DAC_MIN, DAC_MAX));
    dac_output_voltage(THROTTLE_R, r == 0 ? 0 : constrain(abs(r), DAC_MIN, DAC_MAX));
}

// ── Setup ─────────────────────────────────────────────────────────────────────
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
    setMotors(0, 0);

    Serial.println("READY");
}

// ── Loop ──────────────────────────────────────────────────────────────────────
void loop() {
    if (Serial.available()) {
        String line = Serial.readStringUntil('\n');
        line.trim();

        if (line.startsWith("V ")) {
            int sp = line.indexOf(' ', 2);
            if (sp > 0) {
                int l = line.substring(2, sp).toInt();
                int r = line.substring(sp + 1).toInt();
                setMotors(l, r);
            }
        } else if (line == "S") {
            setMotors(0, 0);
            Serial.println("STOP");
        }
    }

    // Odom at 20 Hz
    static uint32_t lastPrint = 0;
    uint32_t now = millis();
    if (now - lastPrint >= 50) {
        noInterrupts();
        long lt = leftTicks;
        long rt = rightTicks;
        interrupts();
        Serial.printf("O %ld %ld\n", lt, rt);
        lastPrint = now;
    }
}
