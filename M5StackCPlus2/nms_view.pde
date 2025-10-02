// Mini NMS Dashboard (Processing 4.x, Java mode)
// UDP 5005: receive telemetry JSON (broadcast/unicast)
// UDP 5006: send command JSON to M5 (add/del/set)
// Made for M5StickC Plus 2 + your Arduino code.

import java.net.*;
import java.io.*;
import java.util.*;
import processing.data.*;

// ====== Networking ======
DatagramSocket rxSock;      // 5005 listen
DatagramSocket txSock;      // 5006 send
int RX_PORT = 5005;
int TX_PORT = 5006;

// 送信先（M5のIP）: 初期はブロードキャスト
String m5ip = "192.168.11.115";

// ====== Model ======
class Item {
  String name, host;
  boolean down;
  float rtt;
}
ArrayList<Item> items = new ArrayList<>();
long lastUpdateTs = 0;

// レイアウト
int COLS = 3;
int margin = 12;
int cellPad = 8;

// UI
PFont f1, f2;
boolean mouseOverIp = false;

void setup() {
  size(900, 480);
  surface.setTitle("Mini NMS Dashboard (Processing)");
  f1 = createFont("Menlo", 16, true);
  f2 = createFont("Menlo", 12, true);
  
  try {
    rxSock = new DatagramSocket(RX_PORT);
    rxSock.setSoTimeout(1);   // 非ブロッキング受信
    txSock = new DatagramSocket(); // 任意の空きポート
    println("Listening UDP on " + RX_PORT);
  } catch (Exception e) {
    e.printStackTrace();
    exit();
  }
}

void draw() {
  background(20);
  readUdpNonBlocking();
  drawHeader();
  drawGrid();
  drawFooter();
}

// ====== UDP receive ======
void readUdpNonBlocking() {
  try {
    byte[] buf = new byte[2048];
    DatagramPacket p = new DatagramPacket(buf, buf.length);
    rxSock.receive(p); // timeout 1ms
    String s = new String(p.getData(), 0, p.getLength(), "UTF-8");

    JSONObject jo = parseJSONObject(s);
    if (jo == null) return;
    lastUpdateTs = jo.getLong("ts", millis());
    JSONArray arr = jo.getJSONArray("items");
    if (arr == null) return;

    HashMap<String, Item> map = new HashMap<>();
    for (Item it : items) map.put(it.name, it);

    ArrayList<Item> next = new ArrayList<>();
    for (int i=0; i<arr.size(); i++) {
      JSONObject o = arr.getJSONObject(i);
      String name = o.getString("name");
      String host = o.getString("host");
      boolean down = (o.getInt("down") == 1);
      float rtt = (float)o.getDouble("rtt", -1.0);

      Item it = map.get(name);
      if (it == null) it = new Item();
      it.name = name;
      it.host = host;
      it.down = down;
      it.rtt  = rtt;
      next.add(it);
    }
    items = next;
  } catch (SocketTimeoutException te) {
    // no data
  } catch (Exception e) {
    // JSON崩れなどはスキップ
  }
}

// ====== Draw ======
void drawHeader() {
  fill(255);
  textFont(f1);
  text("Mini NMS Dashboard", margin, 32);
  textFont(f2);
  text("UDP RX: " + RX_PORT + " / TX: " + TX_PORT, margin, 54);

  // M5宛先IP
  int x = width - 320, y = 24;
  String label = "M5 target IP: " + m5ip + "  (click to edit)";
  mouseOverIp = (mouseX >= x && mouseX <= x+300 && mouseY >= y-18 && mouseY <= y+6);
  fill(mouseOverIp ? color(180, 220, 255) : 230);
  text(label, x, y);
}

void drawGrid() {
  // まず空ならプレースホルダ1枚だけ描画（配列アクセスしない）
  if (items.isEmpty()) {
    int cellW = (width - margin*2 - (COLS-1)*margin) / COLS;
    int cellH = (height - 140 - margin*2) / 1; // 1行ぶん
    int x = margin;
    int y = 80;

    noStroke();
    fill(40); rect(x-2, y-2, cellW+4, cellH+4, 10);
    fill(80); rect(x, y, cellW, cellH, 10);

    fill(230);
    textFont(f1);
    text("Waiting telemetry...", x + cellPad, y + 36);
    textFont(f2);
    text("Make sure M5 is sending UDP 5005 on same network", x + cellPad, y + 60);
    return;
  }

  // ここから通常描画（items に要素がある前提）
  int n = items.size();
  int rows = (n + COLS - 1) / COLS;
  int cellW = (width - margin*2 - (COLS-1)*margin) / COLS;
  int cellH = (height - 140 - margin*2 - (rows-1)*margin) / rows;

  textFont(f1);
  for (int i=0; i<n; i++) {
    int r = i / COLS, c = i % COLS;
    int x = margin + c*(cellW + margin);
    int y = 80 + r*(cellH + margin);

    Item it = items.get(i);

    int bg;
    if (it.down) bg = color(220, 70, 70);           // 赤
    else if (it.rtt < 0) bg = color(200, 200, 60);  // 黄（RTTなし）
    else bg = color(70, 200, 120);                  // 緑

    noStroke();
    fill(40); rect(x-2, y-2, cellW+4, cellH+4, 10);
    fill(bg);  rect(x, y, cellW, cellH, 10);

    fill(0);
    text(it.name, x + cellPad, y + 26);
    textFont(f2);
    text(it.host, x + cellPad, y + 46);

    String st = it.down ? "STATE: DOWN" : "STATE: UP";
    text(st, x + cellPad, y + 66);

    if (!it.down) {
      String rtt = (it.rtt >= 0) ? String.format("RTT: %.1f ms", it.rtt) : "RTT: --";
      text(rtt, x + cellPad, y + 86);
    } else {
      text("ALARM ACTIVE", x + cellPad, y + 86);
    }
  }
}


void drawFooter() {
  fill(200);
  textFont(f2);
  String help = "Keys:  [a] add  [d] delete  [s] set-interval   |   Last TS: " + lastUpdateTs;
  text(help, margin, height - 16);
}

// ====== Commands ======
void keyPressed() {
  if (key == 'a' || key == 'A') {
    String name = prompt("Add - name");
    if (name == null || name.trim().isEmpty()) return;
    String host = prompt("Add - host (IP or FQDN)");
    if (host == null || host.trim().isEmpty()) return;
    String itvl = prompt("Add - intervalMs (e.g. 6000)");
    if (itvl == null || itvl.trim().isEmpty()) return;
    sendCommand(jsonCmd("add", name.trim(), host.trim(), Long.parseLong(itvl.trim())));
  } else if (key == 'd' || key == 'D') {
    String name = prompt("Delete - name");
    if (name == null || name.trim().isEmpty()) return;
    sendCommand(jsonCmdDel(name.trim()));
  } else if (key == 's' || key == 'S') {
    String name = prompt("Set interval - name");
    if (name == null || name.trim().isEmpty()) return;
    String itvl = prompt("Set interval - intervalMs");
    if (itvl == null || itvl.trim().isEmpty()) return;
    sendCommand(jsonCmdSet(name.trim(), Long.parseLong(itvl.trim())));
  }
}

void mousePressed() {
  int x = width - 320, y = 24;
  if (mouseOverIp) {
    String ip = prompt("M5 target IP (use 255.255.255.255 for broadcast)");
    if (ip != null && !ip.trim().isEmpty()) m5ip = ip.trim();
  }
}

String prompt(String title) {
  // Processing 4 では frame は存在しないので、親は null を渡す
  return javax.swing.JOptionPane.showInputDialog(
    (java.awt.Component) null,  // 親なし
    title,                      // メッセージ
    ""                          // 初期値
  );
}


String jsonCmd(String cmd, String name, String host, long itvl) {
  JSONObject o = new JSONObject();
  o.setString("cmd", "add");
  o.setString("name", name);
  o.setString("host", host);
  o.setLong("itvl", itvl);
  return o.toString();
}
String jsonCmdDel(String name) {
  JSONObject o = new JSONObject();
  o.setString("cmd", "del");
  o.setString("name", name);
  return o.toString();
}
String jsonCmdSet(String name, long itvl) {
  JSONObject o = new JSONObject();
  o.setString("cmd", "set");
  o.setString("name", name);
  o.setLong("itvl", itvl);
  return o.toString();
}

void sendCommand(String payload) {
  try {
    byte[] b = payload.getBytes("UTF-8");
    InetAddress addr = InetAddress.getByName(m5ip);
    DatagramPacket p = new DatagramPacket(b, b.length, addr, TX_PORT);
    txSock.setBroadcast(true);
    txSock.send(p);
    println("TX -> " + m5ip + ":" + TX_PORT + " " + payload);
  } catch (Exception e) {
    e.printStackTrace();
  }
}