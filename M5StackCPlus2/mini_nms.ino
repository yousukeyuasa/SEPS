#include <M5StickCPlus2.h>
#include <WiFi.h>
#include <WiFiUdp.h>
#include <ESPping.h>
#include <ArduinoJson.h>

// ==================== ユーザ設定 ====================
const char* ssid = "Buffalo-G-E250";
const char* pass = "usf4bfxhv3ned";

const int UDP_TELEM_PORT = 5005; // テレメトリ送信
const int UDP_CMD_PORT   = 5006; // コマンド受信（PC→M5）

// ★ ブザー設定
const bool   BUZZER_ENABLE = true;   // falseで無効化
const uint8_t BUZZER_VOLUME = 255;   // 0〜255

// 監視ターゲット
struct Target {
  char      name[16];
  char      host[48];
  uint32_t  intervalMs;
  // ランタイム
  uint32_t  lastCheckMs = 0;
  uint8_t   consecOk    = 0;
  uint8_t   consecFail  = 0;
  bool      isDown      = false;
  float     lastAvgRtt  = -1.0f;
  uint32_t  lastChangeMs= 0;
  uint32_t  downStartMs = 0;   // DOWN開始の時刻（UPでリセット）
};

const int MAX_TGT = 12;
Target tgts[MAX_TGT];
int N_TGT = 0;

// 初期エントリ
void addDefaultTargets() {
  strncpy(tgts[0].name, "zabbix",   sizeof(tgts[0].name)); strncpy(tgts[0].host, "192.168.11.200", sizeof(tgts[0].host)); tgts[0].intervalMs=5000;
  strncpy(tgts[1].name, "DNS1", sizeof(tgts[1].name)); strncpy(tgts[1].host, "8.8.8.8",      sizeof(tgts[1].host)); tgts[1].intervalMs=7000;
  strncpy(tgts[2].name, "DNS2", sizeof(tgts[2].name)); strncpy(tgts[2].host, "1.1.1.1",      sizeof(tgts[2].host)); tgts[2].intervalMs=7000;
  N_TGT = 3;
}

// しきい値
const uint8_t FAIL_TH = 3;  // 連続失敗でDOWN
const uint8_t REC_TH  = 2;  // 連続成功でUP
const int     PING_TRY= 4;  // 平均化用回数

// ===== 画面モード =====
enum UIMode { UI_DASH, UI_INFO };
UIMode uiMode = UI_DASH;   // 初期はダッシュボード


// 表示グリッド
const int GRID_COLS = 3;
int gridRows() { return (N_TGT + GRID_COLS - 1) / GRID_COLS; }

// Wi-Fi再接続用
uint32_t wifiLastAttemptMs = 0;
uint32_t wifiRetryInterval = 0;

// UDP
WiFiUDP udpTx; // telemetry
WiFiUDP udpRx; // commands


// ★ アラーム制御
bool     alarmActive   = false;
uint32_t alarmStartMs  = 0;
uint32_t alarmNextMs   = 0;
int      alarmStep     = 0;    // 0,1,2 を繰り返して3連ビープ
const    uint32_t ALARM_MAX_MS = 60000; // 最大1分で自動停止

// ★ ブザー用ユーティリティ
// ★ アラーム停止
void stopAlarm() {
  alarmActive = false;
  // M5.Speaker.stop() が無ければ end() でもOK
  #if defined(ARDUINO)
  M5.Speaker.stop();
  #endif
}
// 画面再描画の共通関数
void redrawUI() {
  if (uiMode == UI_DASH) drawDashboard();
  else                   drawInfoScreen();
}

// ★ DOWN時の繰り返しビープ（タイマー駆動／非ブロッキング）
void alarmTick() {
  if (!alarmActive) return;

  uint32_t now = millis();

  // 1) 自動停止（1分経過）
  if (now - alarmStartMs >= ALARM_MAX_MS) {
    stopAlarm();
    return;
  }

  // 2) ボタンで停止（A/Bどちらでも停止）
  if (M5.BtnA.wasPressed() || M5.BtnB.wasPressed()) {
    stopAlarm();
    return;
  }

  // 3) ビープパターン（ピ・ピ・ピ … 小休止を入れてループ）
  if (now >= alarmNextMs) {
    M5.Speaker.setVolume(255); // ★音量アップ（最大 0〜255）

    if (alarmStep == 0) {
      M5.Speaker.tone(650, 180);      // 低めの注意音
      alarmNextMs = now + 300;        // 音長180ms + 間80〜120ms想定
      alarmStep = 1;
    } else if (alarmStep == 1) {
      M5.Speaker.tone(650, 180);
      alarmNextMs = now + 300;
      alarmStep = 2;
    } else { // alarmStep == 2
      M5.Speaker.tone(650, 220);
      alarmNextMs = now + 1000;       // 3発鳴らした後に少し長めの間
      alarmStep = 0;
    }
  }
}

void beepDown() {
  // ★ここでは鳴らさず、アラームを有効化するだけに変更
  if (!BUZZER_ENABLE) return;
  alarmActive  = true;
  alarmStartMs = millis();
  alarmNextMs  = 0;      // すぐに alarmTick() が発音開始
  alarmStep    = 0;
}

void beepUp() {
  if (!BUZZER_ENABLE) return;
  stopAlarm();                         // ★UPで強制停止
  M5.Speaker.setVolume(230);           // 少し大きめ
  M5.Speaker.tone(1500, 160);          // 明るい終了音
}
// ==================== ユーティリティ ====================
void logTransition(bool toDown, Target& t) {
  t.lastChangeMs = millis();
  if (toDown)  {
    t.downStartMs = t.lastChangeMs;   // ★ DOWN開始を記録
    Serial.printf("[DOWN] %s (%s)\n", t.name, t.host);
    beepDown();                 // ★ DOWNでビープ
  } else {
    t.downStartMs = 0;                // ★ UPでリセット
    Serial.printf("[UP]   %s (%s)\n", t.name, t.host);
    beepUp();                   // ★ UPでビープ
  }
}

void ensureWiFi() {
  if (WiFi.status() == WL_CONNECTED) { wifiRetryInterval = 0; return; }
  uint32_t now = millis();
  if (now - wifiLastAttemptMs < wifiRetryInterval) return;

  Serial.println("[WiFi] reconnecting...");
  WiFi.disconnect(true, true);
  WiFi.begin(ssid, pass);

  wifiRetryInterval = (wifiRetryInterval == 0) ? 2000 : min<uint32_t>(wifiRetryInterval * 2, 60000);
  wifiLastAttemptMs = now;
}

bool icmpAvgMs(const char* host, float* avgMs) {
  float sum = 0; int ok = 0;
  for (int i = 0; i < PING_TRY; ++i) {
    if (Ping.ping(host, 1)) { sum += Ping.averageTime(); ok++; }
    delay(40);
  }
  if (ok > 0) { *avgMs = sum / ok; return true; }
  *avgMs = -1.0f; return false;
}

// ==================== 描画 ====================
void drawDashboard() {
  M5.Lcd.fillScreen(BLACK);

  int rows = max(1, gridRows());
  int w = M5.Lcd.width()  / GRID_COLS;
  int h = M5.Lcd.height() / rows;

  for (int i = 0; i < N_TGT; ++i) {
    int r = i / GRID_COLS, c = i % GRID_COLS;
    int x = c * w, y = r * h;

    uint16_t fill = tgts[i].isDown ? RED : (tgts[i].consecFail > 0 ? YELLOW : GREEN);

    M5.Lcd.drawRect(x+1, y+1, w-2, h-2, DARKGREY);
    M5.Lcd.fillRect(x+2, y+2, w-4, h-4, fill);

    M5.Lcd.setTextColor(BLACK, fill);
    M5.Lcd.setTextSize(2);
    M5.Lcd.setCursor(x+6, y+6);
    M5.Lcd.printf("%s", tgts[i].name);

    M5.Lcd.setTextSize(1);
    M5.Lcd.setCursor(x+6, y+28); M5.Lcd.printf("%s", tgts[i].host);

    M5.Lcd.setCursor(x+6, y+42);
    if (tgts[i].isDown) {
      M5.Lcd.print("STATE: DOWN");

      // ★ 経過秒数を表示
      uint32_t secs = (millis() - tgts[i].downStartMs) / 1000;
      M5.Lcd.setCursor(x+6, y+56);
      M5.Lcd.printf("DOWN %lus", (unsigned long)secs);
    } else {
      M5.Lcd.print("STATE: UP");
      M5.Lcd.setCursor(x+6, y+56);
      if (tgts[i].lastAvgRtt >= 0) M5.Lcd.printf("RTT: %.1f ms", tgts[i].lastAvgRtt);
      else M5.Lcd.print("RTT: --");
    }
  }

  M5.Lcd.setTextColor(WHITE, BLACK);
  M5.Lcd.setTextSize(1);
  M5.Lcd.setCursor(4, M5.Lcd.height() - 12);
  M5.Lcd.print("ICMP only / UDP: Telemetry 5005, Command 5006");
}

// ==================== 監視コア ====================
void checkOne(Target& t) {
  bool up = false; float avg = -1.0f;

  if (WiFi.status() == WL_CONNECTED) {
    up = icmpAvgMs(t.host, &avg);   // ← ICMPのみ
  }
  t.lastAvgRtt = up ? avg : -1.0f;

  if (up) {
    t.consecOk++; t.consecFail = 0;
    if (t.isDown && t.consecOk >= REC_TH) { t.isDown = false; logTransition(false, t); }
  } else {
    t.consecFail++; t.consecOk = 0;
    if (!t.isDown && t.consecFail >= FAIL_TH) { t.isDown = true; logTransition(true, t); }
  }
}

// ==================== Telemetry(JSON) 送信 ====================
void sendTelemetryUDP() {
  if (WiFi.status() != WL_CONNECTED) return;

  StaticJsonDocument<2048> doc;  // 余裕を持って拡張
  doc["ts"] = millis();
  JsonArray items = doc.createNestedArray("items");
  for (int i=0;i<N_TGT;i++){
    JsonObject o = items.createNestedObject();
    o["name"] = tgts[i].name;
    o["host"] = tgts[i].host;
    o["down"] = tgts[i].isDown ? 1 : 0;
    o["rtt"]  = tgts[i].lastAvgRtt;
  }

  size_t need = measureJson(doc);
  if (need <= 1400) { // Ethernet MTU考慮の目安
    char* out = (char*)malloc(need + 4);
    if (!out) return;
    size_t len = serializeJson(doc, out, need + 4);
    IPAddress bcast(192,168,11,255);
    udpTx.beginPacket(bcast, UDP_TELEM_PORT);
    udpTx.write((const uint8_t*)out, len);
    udpTx.endPacket();
    free(out);
  } else {
    // 簡易：アイテムごとに送る（受信側は上書きマージするのでOK）
    for (int i=0;i<N_TGT;i++){
      StaticJsonDocument<256> d2;
      d2["ts"] = millis();
      JsonArray one = d2.createNestedArray("items");
      JsonObject o = one.createNestedObject();
      o["name"] = tgts[i].name;
      o["host"] = tgts[i].host;
      o["down"] = tgts[i].isDown ? 1 : 0;
      o["rtt"]  = tgts[i].lastAvgRtt;
      char out[256];
      size_t len = serializeJson(d2, out, sizeof(out));
      IPAddress bcast(255,255,255,255);
      udpTx.beginPacket(bcast, UDP_TELEM_PORT);
      udpTx.write((const uint8_t*)out, len);
      udpTx.endPacket();
      delay(5);
    }
  }
}


// ==================== Command(JSON) 受信 ====================
int findByName(const char* name) {
  for (int i=0;i<N_TGT;i++) if (strcmp(tgts[i].name, name)==0) return i;
  return -1;
}
bool addTarget(const char* name, const char* host, uint32_t itvl){
  if (N_TGT >= MAX_TGT) return false;
  strncpy(tgts[N_TGT].name, name, sizeof(tgts[N_TGT].name)-1); tgts[N_TGT].name[sizeof(tgts[N_TGT].name)-1]=0;
  strncpy(tgts[N_TGT].host, host, sizeof(tgts[N_TGT].host)-1); tgts[N_TGT].host[sizeof(tgts[N_TGT].host)-1]=0;
  tgts[N_TGT].intervalMs = itvl;
  tgts[N_TGT].lastCheckMs= 0; tgts[N_TGT].consecOk=0; tgts[N_TGT].consecFail=0; tgts[N_TGT].isDown=false; tgts[N_TGT].lastAvgRtt=-1;
  N_TGT++;
  Serial.printf("[ADD] %s %s %lu\n", name, host, (unsigned long)itvl);
  return true;
}
bool delTargetByName(const char* name){
  int idx = findByName(name);
  if (idx < 0) return false;
  for (int i=idx; i<N_TGT-1; i++) tgts[i] = tgts[i+1];
  N_TGT--;
  Serial.printf("[DEL] %s\n", name);
  return true;
}
bool setIntervalByName(const char* name, uint32_t itvl){
  int idx = findByName(name);
  if (idx < 0) return false;
  tgts[idx].intervalMs = itvl;
  Serial.printf("[SET] %s interval=%lu\n", name, (unsigned long)itvl);
  return true;
}

void handleCommandUDP() {
  int sz = udpRx.parsePacket();
  if (!sz) return;

  char buf[512];
  int n = udpRx.read(buf, sizeof(buf)-1);
  if (n <= 0) return;
  buf[n] = 0;

  StaticJsonDocument<512> doc;
  DeserializationError e = deserializeJson(doc, buf);
  if (e) { Serial.printf("[CMD] JSON parse error: %s\n", e.c_str()); return; }

  const char* cmd  = doc["cmd"]  | "";
  const char* name = doc["name"] | "";
  if (!strcmp(cmd,"add")) {
    const char* host = doc["host"] | "";
    uint32_t itvl    = doc["itvl"] | 6000;
    if (strlen(name)&&strlen(host)) addTarget(name, host, itvl);
  } else if (!strcmp(cmd,"del")) {
    if (strlen(name)) delTargetByName(name);
  } else if (!strcmp(cmd,"set")) {
    uint32_t itvl = doc["itvl"] | 0;
    if (strlen(name) && itvl>0) setIntervalByName(name, itvl);
  }
}
void drawInfoScreen() {
  M5.Lcd.fillScreen(BLACK);
  M5.Lcd.setTextColor(WHITE, BLACK);

  M5.Lcd.setTextSize(2);
  M5.Lcd.setCursor(8, 10);
  M5.Lcd.println("Device Info");

  M5.Lcd.setTextSize(1);
  int y = 40;
  auto line = [&](const char* key, const String& val){
    M5.Lcd.setCursor(8, y);
    M5.Lcd.printf("%-10s: %s\n", key, val.c_str());
    y += 14;
  };

  IPAddress ip   = WiFi.localIP();
  IPAddress gw   = WiFi.gatewayIP();
  IPAddress mask = WiFi.subnetMask();
  String mac     = WiFi.macAddress();
  int rssi       = WiFi.RSSI();
  int bat        = 0;
  #ifdef M5STICKCPLUS2
  bat = M5.Power.getBatteryLevel(); // StickC Plus2 はこれでOK
  #endif

  line("SSID", WiFi.SSID());
  line("IP",   ip.toString());
  line("GW",   gw.toString());
  line("MASK", mask.toString());
  line("MAC",  mac);
  line("RSSI", String(rssi) + " dBm");
  line("BAT",  String(bat) + " %");

  // ヒント
  y += 8;
  M5.Lcd.setCursor(8, y);
  M5.Lcd.setTextColor(0xC6FF, BLACK); // 薄いシアン
  M5.Lcd.print("BtnA: Toggle screen  /  BtnB: Mute alarm");
  M5.Lcd.setTextColor(WHITE, BLACK);
}

// ==================== Arduino標準 ====================
void setup() {
  M5.begin();
  Serial.begin(115200);

  // ★ スピーカー初期化
  if (BUZZER_ENABLE) {
    M5.Speaker.begin();
    M5.Speaker.setVolume(BUZZER_VOLUME);
  }

  addDefaultTargets();

  M5.Lcd.setRotation(3);
  M5.Lcd.fillScreen(BLACK);
  M5.Lcd.setTextColor(WHITE, BLACK);
  M5.Lcd.setTextSize(2);
  M5.Lcd.setCursor(8, 16);
  M5.Lcd.println("Mini NMS (ICMP + UDP)");

  WiFi.mode(WIFI_STA);
  WiFi.begin(ssid, pass);

  uint32_t t0 = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - t0 < 4000) {
    delay(250); M5.Lcd.print(".");
  }

  udpTx.begin(UDP_TELEM_PORT); // 送信用（begin不要だがOK）
  udpRx.begin(UDP_CMD_PORT);   // 受信用
  // drawDashboard();
  redrawUI();
}

void loop() {
  M5.update();

  // ★ DOWNアラームの駆動（非ブロッキング）
  alarmTick();
  
    // === 画面切替 ===
  if (M5.BtnA.wasPressed()) {
    if (alarmActive) {
      // アラーム中はまず停止を優先（既存仕様）
      stopAlarm();
    } else {
      // 画面切替
      uiMode = (uiMode == UI_DASH) ? UI_INFO : UI_DASH;
      if (uiMode == UI_INFO) drawInfoScreen();
      else                   redrawUI();
    }
  }
  // BtnB はミュート優先（既存動作のまま）
  if (M5.BtnB.wasPressed()) {
    stopAlarm();
    // （必要ならここに別の機能を足してOK）
  }

  ensureWiFi();

  // コマンド受信（PC→M5）
  handleCommandUDP();

  // 監視スケジューラ
  uint32_t now = millis();
  for (int i=0; i<N_TGT; ++i) {
    if (now - tgts[i].lastCheckMs >= tgts[i].intervalMs) {
      tgts[i].lastCheckMs = now;
      checkOne(tgts[i]);
    }
  }

  // 1秒に1回：描画＆テレメトリ送信
  static uint32_t lastTick = 0;
  if (millis() - lastTick > 1000) {
  lastTick = millis();
  redrawUI();
  sendTelemetryUDP();
}

  delay(10);
}
