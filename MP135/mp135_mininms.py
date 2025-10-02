#!/usr/bin/env python3
# Mini Field NMS for MP135 LCD (Framebuffer Direct) - Full Version
# 目的: 現場で“ぱっと見で異常が分かる”簡易NMS
# 改良点:
# - FBを起動時に一度だけmmapし再利用（チラつき/CPU負荷減、描画安定）
# - line_length尊重で行単位書き込み（過走防止、bpp16/32両対応）
# - ビープのクールダウン実装（ダウン継続中の鳴きすぎ防止）
# - evdevキー入力の自動再接続（無線KBの瞬断対策）
# - UDPコマンドの簡易バリデーション（add/del/set）
# 依存: python3-pil, iputils-ping, fonts-dejavu-core, alsa-utils, (任意) python3-evdev
# 実行例: sudo FBDEV=/dev/fb1 python3 mp135_field_nms.py
# 自動起動: systemdで ExecStart=/usr/bin/python3 /root/mp135_field_nms.py

import os, time, json, socket, subprocess, threading, queue, mmap, fcntl, struct
from dataclasses import dataclass
from typing import List, Optional, Tuple
from PIL import Image, ImageDraw, ImageFont

# ========= 設定 =========
CONFIG_PATHS      = ["/etc/mini_nms/targets.json", "./targets.json"]
DEFAULT_FB        = os.environ.get("FBDEV", "/dev/fb1")  # MP135 LCD想定（HDMIはfb0のことも）
BEEP_WAV_DOWN     = "/root/beep.wav"     # 無ければ None
BEEP_WAV_UP       = "/root/beep_up.wav"  # 無ければ None
UDP_CMD_PORT      = 5006                 # PCから add/del/set 可能（JSON）
ENABLE_UDP_CMD    = True
BEEP_COOLDOWN_SEC = 60                   # ダウン継続時の再通知間隔

PING_TRIES        = 3
PING_TIMEOUT_SEC  = 1.0
FAIL_TH           = 2                    # 連続失敗でDOWN
REC_TH            = 1                    # 連続成功でUP
UI_INTERVAL_SEC   = 1.0                  # 画面更新周期

# ========= FB ioctl 定義 =========
FBIOGET_FSCREENINFO = 0x4602
FBIOGET_VSCREENINFO = 0x4600

def fb_get_info(fd):
    # Linux FB fix構造体の必要フィールドを抽出: line_length取得
    fixfmt = "16sL I I I I H H H I 24x"
    fixbuf = bytearray(struct.calcsize(fixfmt))
    fcntl.ioctl(fd, FBIOGET_FSCREENINFO, fixbuf, True)
    _, _, _, _, _, _, _, _, _, line_len = struct.unpack(fixfmt, fixbuf)
    # var: xres,yres,bpp 抜粋
    varfmt = "I I I I I I I 4x 32x"
    varbuf = bytearray(struct.calcsize(varfmt))
    fcntl.ioctl(fd, FBIOGET_VSCREENINFO, varbuf, True)
    xres, yres, _, _, _, _, bpp = struct.unpack(varfmt, varbuf)
    return xres, yres, bpp, line_len

def fb_path():
    fb = os.environ.get("FBDEV", DEFAULT_FB)
    if os.path.exists(fb):
        return fb
    for p in ("/dev/fb1", "/dev/fb0"):
        if os.path.exists(p):
            return p
    raise RuntimeError("No framebuffer found")

class FB:
    """フレームバッファを起動時に一度だけmmapし、毎フレーム使い回す"""
    def __init__(self, path):
        self.path = path
        self.fd = open(path, "r+b")
        self.xres, self.yres, self.bpp, self.line_len = fb_get_info(self.fd.fileno())
        # 画面全体分をmmap
        self.mm = mmap.mmap(self.fd.fileno(), self.line_len * self.yres,
                            mmap.MAP_SHARED, mmap.PROT_WRITE)

    def blit_image(self, img: Image.Image):
        W, H = self.xres, self.yres
        if img.size != (W, H):
            raise RuntimeError(f"Canvas {img.size} != FB {(W, H)}")

        if self.bpp == 16:
            # RGB565行単位で書く（line_len尊重）
            rgb = img.convert("RGB")
            it = iter(rgb.getdata())
            for y in range(H):
                line = bytearray(self.line_len)
                off = 0
                for _ in range(W):
                    r, g, b = next(it)
                    v = ((r >> 3) << 11) | ((g >> 2) << 5) | (b >> 3)
                    line[off] = v & 0xFF
                    line[off + 1] = (v >> 8) & 0xFF
                    off += 2
                self.mm.seek(y * self.line_len)
                self.mm.write(line)

        elif self.bpp == 32:
            # BGRA(BGRX)で書く
            rgba = img.convert("RGBA")
            it = iter(rgba.getdata())
            for y in range(H):
                line = bytearray(self.line_len)
                off = 0
                for _ in range(W):
                    r, g, b, a = next(it)
                    line[off:off+4] = bytes((b, g, r, 0xFF))
                    off += 4
                self.mm.seek(y * self.line_len)
                self.mm.write(line)
        else:
            raise RuntimeError(f"Unsupported bpp: {self.bpp}")

    def close(self):
        try:
            self.mm.flush()
            self.mm.close()
        finally:
            self.fd.close()

# ========= 監視 =========
@dataclass
class Tgt:
    name: str
    host: str
    method: str = "icmp"   # "icmp" or "tcp"
    port: int = 0          # tcp用
    interval_ms: int = 5000
    # runtime
    last_check: int = 0
    consec_ok: int = 0
    consec_ng: int = 0
    is_down: bool = False
    last_avg: float = -1.0
    changed_ms: int = 0
    down_ms: int = 0
    last_beep: int = 0     # 再通知用（epoch ms）

def load_targets() -> List[Tgt]:
    for p in CONFIG_PATHS:
        if os.path.exists(p):
            with open(p) as f:
                raw = json.load(f)
            arr: List[Tgt] = []
            for o in raw.get("targets", raw if isinstance(raw, list) else []):
                arr.append(Tgt(
                    name=o["name"],
                    host=o["host"],
                    method=o.get("method", "icmp"),
                    port=int(o.get("port", 0)),
                    interval_ms=int(o.get("interval_ms", 5000)),
                ))
            print(f"[CFG] loaded {len(arr)} targets from {p}")
            return arr
    # デフォルト
    print("[CFG] using defaults")
    return [
        Tgt("GW",   "192.168.11.1",   "icmp", 0, 4000),
        Tgt("DNS1", "8.8.8.8",        "icmp", 0, 6000),
        Tgt("WEB",  "www.google.com", "tcp",  443, 7000),
    ]

def ping_once(host: str, timeout_s: float) -> Optional[float]:
    # iputils前提（-W=秒）
    cmd = ["ping", "-n", "-c", "1", "-W", str(int(max(1, timeout_s))), host]
    try:
        out = subprocess.check_output(cmd, stderr=subprocess.STDOUT, text=True, timeout=timeout_s + 0.8)
    except Exception:
        return None
    for ln in out.splitlines():
        if "time=" in ln:
            try:
                val = ln.split("time=", 1)[1].split(" ", 1)[0]
                return float(val)
            except Exception:
                return None
    return None

def icmp_avg(host: str, tries=PING_TRIES) -> Tuple[bool, float]:
    s = 0.0; ok = 0
    for _ in range(tries):
        r = ping_once(host, PING_TIMEOUT_SEC)
        if r is not None:
            s += r; ok += 1
        time.sleep(0.03)
    if ok > 0:
        return True, s / ok
    return False, -1.0

def tcp_check(host: str, port: int, timeout=1.0) -> Tuple[bool, float]:
    t0 = time.time()
    try:
        with socket.create_connection((host, port), timeout=timeout):
            rtt = (time.time() - t0) * 1000.0
            return True, rtt
    except Exception:
        return False, -1.0

def play_wav(path):
    if not path:
        return
    try:
        subprocess.Popen(["aplay", "-q", path], stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
    except Exception:
        pass

def beep_down():
    def _run():
        for _ in range(3):
            play_wav(BEEP_WAV_DOWN)
            time.sleep(0.28)
    threading.Thread(target=_run, daemon=True).start()

def beep_up():
    play_wav(BEEP_WAV_UP)

class Controller:
    def __init__(self):
        self.targets: List[Tgt] = load_targets()
        self.lock = threading.Lock()
        self.running = True
        if ENABLE_UDP_CMD:
            threading.Thread(target=self._udp_listener, daemon=True).start()
        threading.Thread(target=self._scheduler, daemon=True).start()

    # ---- UDPコマンド ----
    def _udp_listener(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("0.0.0.0", UDP_CMD_PORT))
        print(f"[CMD] UDP listening on 0.0.0.0:{UDP_CMD_PORT}")
        while self.running:
            try:
                data, addr = s.recvfrom(2048)
                try:
                    doc = json.loads(data.decode("utf-8", "ignore"))
                except Exception:
                    print("[CMD] invalid JSON from", addr); continue
                self._handle_cmd(doc)
            except Exception:
                time.sleep(0.05)

    def _handle_cmd(self, doc):
        try:
            cmd = str(doc.get("cmd", ""))
            if cmd == "add":
                name = str(doc["name"]); host = str(doc["host"])
                method = str(doc.get("method", "icmp"))
                port = int(doc.get("port", 0)); itvl = int(doc.get("interval_ms", 5000))
                if method not in ("icmp", "tcp"):
                    raise ValueError("method must be icmp/tcp")
                o = Tgt(name=name, host=host, method=method, port=port, interval_ms=itvl)
                with self.lock: self.targets.append(o)
                print(f"[ADD] {o}")
            elif cmd == "del":
                name = str(doc.get("name", ""))
                with self.lock:
                    before = len(self.targets)
                    self.targets = [t for t in self.targets if t.name != name]
                print(f"[DEL] {name} ({before}→{len(self.targets)})")
            elif cmd == "set":
                name = str(doc.get("name", "")); itvl = int(doc.get("interval_ms", 0))
                with self.lock:
                    for t in self.targets:
                        if t.name == name and itvl > 0:
                            t.interval_ms = itvl; print(f"[SET] {name} interval={itvl}")
            else:
                print(f"[CMD] unknown {doc}")
        except Exception as e:
            print(f"[CMD] error: {e} doc={doc}")

    # ---- 監視スケジューラ ----
    def _check_one(self, t: Tgt):
        if t.method == "icmp":
            ok, avg = icmp_avg(t.host)
        else:
            ok, avg = tcp_check(t.host, t.port, 1.0)
        t.last_avg = avg if ok else -1.0

        now = int(time.time() * 1000)
        if ok:
            t.consec_ok += 1; t.consec_ng = 0
            if t.is_down and t.consec_ok >= REC_TH:
                t.is_down = False; t.changed_ms = now; t.down_ms = 0
                print(f"[UP]   {t.name} ({t.host})")
                beep_up(); t.last_beep = 0
        else:
            t.consec_ng += 1; t.consec_ok = 0
            if not t.is_down and t.consec_ng >= FAIL_TH:
                t.is_down = True; t.changed_ms = now; t.down_ms = now
                print(f"[DOWN] {t.name} ({t.host})")
                beep_down(); t.last_beep = now
            elif t.is_down:
                # ダウン継続再通知（クールダウン）
                if (t.last_beep == 0) or ((now - t.last_beep) > BEEP_COOLDOWN_SEC * 1000):
                    beep_down(); t.last_beep = now

    def _scheduler(self):
        while self.running:
            now = int(time.time() * 1000)
            with self.lock:
                for t in self.targets:
                    if now - t.last_check >= t.interval_ms:
                        t.last_check = now
                        self._check_one(t)
            time.sleep(0.05)

# ========= 情報取得 =========
def get_ip_lines():
    lines = []
    try:
        out = subprocess.check_output(["ip", "-o", "-4", "addr", "show"], text=True)
        for ln in out.splitlines():
            p = ln.split()
            if "inet" in p:
                lines.append(f"{p[1]}: {p[3]}")
    except Exception:
        pass
    try:
        out = subprocess.check_output(["ip", "route", "show", "default"], text=True)
        gw = out.split()
        if len(gw) >= 3:
            lines.append(f"GW: {gw[2]}")
    except Exception:
        pass
    try:
        lines.append(f"Host: {socket.gethostname()}")
    except Exception:
        pass
    return lines or ["No IP info"]

# ========= UI =========
def key_listener(toggle_cb, quit_cb):
    try:
        from evdev import InputDevice, ecodes, list_devices
    except Exception:
        print("[KEY] evdev not available; key toggle disabled")
        return
    while True:
        try:
            dev_path = os.environ.get("INPUT_DEV", "")
            if not dev_path:
                for p in list_devices():
                    try:
                        d = InputDevice(p)
                        nm = (d.name or "").lower()
                        if "kbd" in nm or "keyboard" in nm:
                            dev_path = p; break
                    except Exception:
                        pass
            if not dev_path:
                print("[KEY] no input device found; toggle disabled")
                return
            d = InputDevice(dev_path)
            print(f"[KEY] listening on {dev_path} ({d.name})")
            for ev in d.read_loop():
                if ev.type == 1 and ev.value == 1:
                    if ev.code in (ecodes.KEY_SPACE, ecodes.KEY_ENTER):
                        toggle_cb()
                    elif ev.code in (ecodes.KEY_Q, ecodes.KEY_ESC):
                        quit_cb(); return
        except Exception as e:
            print("[KEY] error:", e)
            time.sleep(1.0)  # 再試行

def run_ui(ctrl: Controller):
    fbdev = fb_path()
    fb = FB(fbdev)
    try:
        xres, yres = fb.xres, fb.yres

        # レスポンシブ・フォント/余白（320x240〜1280x720想定）
        def clamp(v, a, b): return max(a, min(b, v))
        F_BIG   = clamp(int(yres * 0.10), 14, 36)
        F_MED   = clamp(int(yres * 0.075), 12, 28)
        F_SM    = clamp(int(yres * 0.055), 10, 22)
        MARGIN  = clamp(int(min(xres, yres) * 0.05), 8, 32)
        PAD     = clamp(int(min(xres, yres) * 0.045), 6, 18)
        COLS    = 2 if xres <= 400 else 3

        try:
            font_big   = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf", F_BIG)
            font_med   = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf", F_MED)
            font_small = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf", F_SM)
        except Exception:
            font_big = font_med = font_small = ImageFont.load_default()

        ui = {"mode": "DASH", "run": True}
        threading.Thread(
            target=key_listener,
            args=(lambda: ui.update(mode=("INFO" if ui["mode"] == "DASH" else "DASH")),
                  lambda: ui.update(run=False)),
            daemon=True
        ).start()

        while ui["run"]:
            with ctrl.lock:
                items = list(ctrl.targets)

            img = Image.new("RGB", (xres, yres), (18, 18, 18))
            d = ImageDraw.Draw(img)

            # ヘッダ
            nowtxt = time.strftime("%H:%M:%S")
            if any(t.is_down for t in items):
                # ALERTバナー（赤）
                d.rectangle([0, 0, xres, int(yres * 0.14)], fill=(190, 40, 40))
                d.text((MARGIN, int(yres * 0.02)), "ALERT: Down detected", fill=(0, 0, 0), font=font_big)
                d.text((xres - (F_SM * 6), int(yres * 0.02)), nowtxt, fill=(0, 0, 0), font=font_small)
                y0 = int(yres * 0.16)
            else:
                d.text((MARGIN, int(yres * 0.02)), "Mini Field NMS (MP135)", fill=(255, 255, 255), font=font_big)
                d.text((xres - (F_SM * 6), int(yres * 0.02)), nowtxt, fill=(180, 220, 255), font=font_small)
                y0 = int(yres * 0.14)

            if ui["mode"] == "INFO":
                d.text((MARGIN, y0), "Device Info", fill=(255, 255, 255), font=font_big)
                y = y0 + int(F_BIG * 1.3)
                for line in get_ip_lines():
                    d.text((MARGIN, y), line, fill=(220, 220, 220), font=font_med); y += int(F_MED * 1.2)
                d.text((MARGIN, yres - int(F_SM * 1.4)),
                       "SPACE: toggle  /  Q: quit", fill=(180, 220, 255), font=font_small)
            else:
                n = len(items)
                if n == 0:
                    d.rectangle([MARGIN, y0, xres - MARGIN, yres - MARGIN], fill=(60, 60, 60))
                    d.text((MARGIN + PAD, y0 + PAD), "No targets (edit targets.json)",
                           fill=(235, 235, 235), font=font_med)
                else:
                    cols = COLS; rows = (n + cols - 1) // cols
                    cell_w = (xres - MARGIN * 2 - (cols - 1) * MARGIN) // cols
                    cell_h = (yres - y0 - MARGIN - (rows - 1) * MARGIN) // rows

                    def color(down, rtt):
                        if down: return (220, 70, 70)     # 赤
                        if rtt < 0: return (200, 200, 70) # 黄
                        return (70, 200, 120)             # 緑

                    for i, t in enumerate(items):
                        r, c = divmod(i, cols)
                        x = MARGIN + c * (cell_w + MARGIN)
                        y = y0 + r * (cell_h + MARGIN)
                        bg = color(t.is_down, t.last_avg)
                        d.rectangle([x - 2, y - 2, x + cell_w + 2, y + cell_h + 2], fill=(40, 40, 40))
                        d.rectangle([x, y, x + cell_w, y + cell_h], fill=bg)
                        # 4行表示（名前/ホスト/状態/RTT or 経過）
                        d.text((x + PAD, y + int(PAD * 0.7)), t.name, fill=(0, 0, 0), font=font_big)
                        hostline = f"{t.host}" if t.method == "icmp" else f"{t.host}:{t.port} (TCP)"
                        d.text((x + PAD, y + int(PAD * 0.7) + F_BIG + 2), hostline, fill=(0, 0, 0), font=font_med)
                        st = "STATE: DOWN" if t.is_down else "STATE: UP"
                        d.text((x + PAD, y + PAD + F_BIG + F_MED + 6), st, fill=(0, 0, 0), font=font_med)
                        y4 = y + PAD + F_BIG + F_MED * 2 + 12
                        if t.is_down:
                            secs = int((time.time() * 1000 - t.down_ms) / 1000) if t.down_ms else 0
                            d.text((x + PAD, y4), f"DOWN {secs}s", fill=(0, 0, 0), font=font_med)
                        else:
                            s = "RTT: --" if t.last_avg < 0 else f"RTT: {t.last_avg:.1f} ms"
                            d.text((x + PAD, y4), s, fill=(0, 0, 0), font=font_med)

            try:
                fb.blit_image(img)
            except KeyboardInterrupt:
                raise
            except Exception as e:
                print("FB blit error:", e)
                time.sleep(1)
            time.sleep(UI_INTERVAL_SEC)
    finally:
        fb.close()

# ========= メイン =========
def main():
    ctrl = Controller()
    try:
        run_ui(ctrl)
    except KeyboardInterrupt:
        print("\n[INFO] Ctrl-C, exit.")
    finally:
        ctrl.running = False
        time.sleep(0.1)

if __name__ == "__main__":
    main()
