/*
  main.cpp — MedCart unified Arduino sketch
  Handles: RFID reading, Bluetooth motor control, Servo dispenser (later)

  Pin map:
  ── RC522 (SPI) ──────────────────────────────
    Pin 13 → SCK
    Pin 12 → MISO
    Pin 11 → MOSI
    Pin 10 → SDA (SS)
    Pin A1 → RST

  ── L298N (motor driver) ──────────────────────
    Pin 2  → IN1 (left motor A)
    Pin 3  → IN2 (left motor B)
    Pin 4  → IN3 (right motor A)
    Pin 5  → IN4 (right motor B)
    Pin 6  → ENA (left motor PWM)
    Pin 7  → ENB (right motor PWM)

  ── HC-05 Bluetooth (SoftwareSerial) ─────────
    Pin 8  → RX (HC-05 TX)
    Pin 9  → TX (HC-05 RX via voltage divider)

  ── Servo dispenser (future) ──────────────────
    Pin A0 → Servo signal

  ── Free for future sensors ───────────────────
    A2, A3, A4, A5

  Bluetooth movement commands (single char):
    F/f — forward
    B/b — backward
    L/l — turn left
    R/r — turn right
    S/s — stop
    0-9 — speed level (0=0%, 9=100%)
    q   — speed 100%

  Serial to Pi (JSON):
    RFID scan:  {"type":"rfid","uid":"A1B2C3D4","status":"ok"}
    RFID error: {"type":"rfid","uid":"","status":"error","msg":"read_fail"}
    Move ack:   {"type":"move","cmd":"F","speed":180}

  Libraries (platformio.ini lib_deps):
    miguelbalboa/MFRC522@^1.4.10
    Servo (built-in)
*/

#include <SPI.h>
#include <MFRC522.h>
#include <SoftwareSerial.h>
#include <Servo.h>

// ── RC522 ─────────────────────────────────────────────────────────────────────
#define SS_PIN  10
#define RST_PIN A1
MFRC522 rfid(SS_PIN, RST_PIN);

// ── HC-05 ─────────────────────────────────────────────────────────────────────
SoftwareSerial BT(8, 9);   // RX, TX

// ── L298N ─────────────────────────────────────────────────────────────────────
const int IN1 = 2;
const int IN2 = 3;
const int IN3 = 4;
const int IN4 = 5;
const int ENA = 6;
const int ENB = 7;

// ── Servo (dispenser — wired later) ──────────────────────────────────────────
Servo dispenserServo;
const int SERVO_PIN = A0;
bool servoAttached = false;   // set true when dispenser is wired

// ── State ─────────────────────────────────────────────────────────────────────
int  currentSpeed             = 180;
const unsigned long RFID_COOLDOWN_MS = 3000;
String  lastUID               = "";
unsigned long lastScanTime    = 0;


// ═══════════════════════════════════════════════════════════════════════════════
// RFID
// ═══════════════════════════════════════════════════════════════════════════════

void handleRFID() {
  if (!rfid.PICC_IsNewCardPresent()) return;

  if (!rfid.PICC_ReadCardSerial()) {
    Serial.println("{\"type\":\"rfid\",\"uid\":\"\",\"status\":\"error\",\"msg\":\"read_fail\"}");
    return;
  }

  // Build UID string
  String uid = "";
  for (byte i = 0; i < rfid.uid.size; i++) {
    if (rfid.uid.uidByte[i] < 0x10) uid += "0";
    uid += String(rfid.uid.uidByte[i], HEX);
  }
  uid.toUpperCase();

  unsigned long now = millis();

  // Debounce
  if (uid == lastUID && (now - lastScanTime) < RFID_COOLDOWN_MS) {
    rfid.PICC_HaltA();
    rfid.PCD_StopCrypto1();
    return;
  }

  lastUID      = uid;
  lastScanTime = now;

  // Send to Pi
  Serial.print("{\"type\":\"rfid\",\"uid\":\"");
  Serial.print(uid);
  Serial.println("\",\"status\":\"ok\"}");

  rfid.PICC_HaltA();
  rfid.PCD_StopCrypto1();
}


// ═══════════════════════════════════════════════════════════════════════════════
// MOTORS
// ═══════════════════════════════════════════════════════════════════════════════

void setLeftMotor(int dir) {
  if (dir == 1)       { digitalWrite(IN1, HIGH); digitalWrite(IN2, LOW); }
  else if (dir == -1) { digitalWrite(IN1, LOW);  digitalWrite(IN2, HIGH); }
  else                { digitalWrite(IN1, LOW);  digitalWrite(IN2, LOW); }
}

void setRightMotor(int dir) {
  if (dir == 1)       { digitalWrite(IN3, HIGH); digitalWrite(IN4, LOW); }
  else if (dir == -1) { digitalWrite(IN3, LOW);  digitalWrite(IN4, HIGH); }
  else                { digitalWrite(IN3, LOW);  digitalWrite(IN4, LOW); }
}

void setSpeed(int left, int right) {
  analogWrite(ENA, left);
  analogWrite(ENB, right);
}

void moveForward() {
  setLeftMotor(1); setRightMotor(1);
  setSpeed(currentSpeed, currentSpeed);
  Serial.print("{\"type\":\"move\",\"cmd\":\"F\",\"speed\":");
  Serial.print(currentSpeed);
  Serial.println("}");
}

void moveBackward() {
  setLeftMotor(-1); setRightMotor(-1);
  setSpeed(currentSpeed, currentSpeed);
  Serial.print("{\"type\":\"move\",\"cmd\":\"B\",\"speed\":");
  Serial.print(currentSpeed);
  Serial.println("}");
}

void turnLeft() {
  setLeftMotor(1); setRightMotor(0);
  setSpeed(currentSpeed / 2, currentSpeed);
  Serial.print("{\"type\":\"move\",\"cmd\":\"L\",\"speed\":");
  Serial.print(currentSpeed);
  Serial.println("}");
}

void turnRight() {
  setLeftMotor(0); setRightMotor(1);
  setSpeed(currentSpeed / 2, currentSpeed);
  Serial.print("{\"type\":\"move\",\"cmd\":\"R\",\"speed\":");
  Serial.print(currentSpeed);
  Serial.println("}");
}

void stopMotors() {
  setLeftMotor(0); setRightMotor(0);
  setSpeed(0, 0);
  Serial.println("{\"type\":\"move\",\"cmd\":\"S\",\"speed\":0}");
}

void handleSpeedCommand(char cmd) {
  if (cmd == 'q') {
    currentSpeed = 255;
  } else {
    currentSpeed = map(cmd - '0', 0, 9, 0, 255);
  }
  if (currentSpeed > 0 && currentSpeed < 100) currentSpeed = 100;

  setSpeed(currentSpeed, currentSpeed);
}

void handleMovement(char cmd) {
  switch (cmd) {
    case 'F': case 'f': moveForward();  break;
    case 'B': case 'b': moveBackward(); break;
    case 'L': case 'l': turnLeft();     break;
    case 'R': case 'r': turnRight();    break;
    case 'S': case 's': stopMotors();   break;
    default:
      if ((cmd >= '0' && cmd <= '9') || cmd == 'q') {
        handleSpeedCommand(cmd);
      }
      break;
  }
}


// ═══════════════════════════════════════════════════════════════════════════════
// DISPENSER (stub — activated when servo is wired)
// ═══════════════════════════════════════════════════════════════════════════════

void dispense(int slot) {
  if (!servoAttached) {
    Serial.println("{\"type\":\"dispense\",\"status\":\"error\",\"msg\":\"servo_not_attached\"}");
    return;
  }
  // Rotate to dispense position, pause, return
  dispenserServo.write(90);
  delay(800);
  dispenserServo.write(0);
  delay(500);
  Serial.print("{\"type\":\"dispense\",\"slot\":");
  Serial.print(slot);
  Serial.println(",\"status\":\"ok\"}");
}

void handleDispenseCommand(String json) {
  // Expected: {"cmd":"dispense","slot":1}
  int slotIdx = json.indexOf("\"slot\":");
  if (slotIdx == -1) {
    Serial.println("{\"type\":\"dispense\",\"status\":\"error\",\"msg\":\"no_slot\"}");
    return;
  }
  int slot = json.substring(slotIdx + 7).toInt();
  dispense(slot);
}


// ═══════════════════════════════════════════════════════════════════════════════
// Pi → Arduino serial commands
// ═══════════════════════════════════════════════════════════════════════════════

String serialBuffer = "";

void handlePiCommand(String line) {
  // Pi sends JSON for dispense commands
  if (line.indexOf("\"cmd\":\"dispense\"") >= 0) {
    handleDispenseCommand(line);
  }
}


// ═══════════════════════════════════════════════════════════════════════════════
// SETUP & LOOP
// ═══════════════════════════════════════════════════════════════════════════════

void setup() {
  Serial.begin(9600);    // Pi communication
  BT.begin(9600);        // HC-05

  // RC522
  SPI.begin();
  rfid.PCD_Init();

  rfid.PCD_Init();
  byte version = rfid.PCD_ReadRegister(rfid.VersionReg);
  Serial.print("{\"type\":\"system\",\"rfid_version\":\"");
  Serial.print(version, HEX);
  Serial.println("\"}");

  // L298N
  pinMode(IN1, OUTPUT); pinMode(IN2, OUTPUT);
  pinMode(IN3, OUTPUT); pinMode(IN4, OUTPUT);
  pinMode(ENA, OUTPUT); pinMode(ENB, OUTPUT);
  stopMotors();

  // Servo — attach when wired
  if (servoAttached) {
    dispenserServo.attach(SERVO_PIN);
    dispenserServo.write(0);
  }

  delay(500);
  Serial.println("{\"type\":\"system\",\"status\":\"ready\"}");
}


void loop() {
  // ── 1. RFID scan check ───────────────────────────────────────────────────
  handleRFID();

  // ── 2. Bluetooth movement commands ───────────────────────────────────────
  if (BT.available()) {
    char cmd = BT.read();
    handleMovement(cmd);
  }

  // ── 3. Pi serial commands (dispense etc.) ────────────────────────────────
  while (Serial.available()) {
    char c = Serial.read();
    if (c == '\n') {
      if (serialBuffer.length() > 0) {
        handlePiCommand(serialBuffer);
        serialBuffer = "";
      }
    } else {
      serialBuffer += c;
    }
  }

  delay(10);   // small delay keeps loop stable without missing events
}