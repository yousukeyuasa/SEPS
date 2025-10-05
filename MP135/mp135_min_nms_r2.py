#!/usr/bin/env python3
# Mini Field NMS for MP135 LCD (Framebuffer Direct)
# 完全版 v3（全画面タップでDASH/INFO切替・タッチ強化・白丸フィードバック・補正対応）
#
# 依存: python3-pil, iputils-ping, alsa-utils, (任意) python3-evdev, fonts-dejavu-core
# 実行例: sudo FBDEV=/dev/fb1 python3 mp135_field_nms2.py
# 主な環境変数:
#   FBDEV=/dev/fb1
#   ALSA_DEVICE=default
#   TOUCH_DEV=/dev/input/event0
#   TOUCH_SWAP_XY=0/1, TOUCH_INV_X=0/1, TOUCH_INV_Y=0/1
#   TOUCH_SCALE_X=1.0, TOUCH_SCALE_Y=1.0, TOUCH_OFFSET_X=0, TOUCH_OFFSET_Y=0
#   DEBUG_TOUCH=0/1

import os, time, json, socket, subprocess, threading, queue, mmap, fcntl, struct
import math, wave, shutil, collections

from dataclasses import dataclass
from typing import List, Optional, Tuple
from PIL import Image, ImageDraw, ImageFont

# ========= 設定 =========
CONFIG_PATHS      = ["/etc/mini_nms/targets.json", "./targets.json"]
DEFAULT_FB        = os.environ.get("FBDEV", "/dev/fb1")
BEEP_WAV_DOWN     = "/root/beep.wav"       # 無ければ自動生成（/tmp）
BEEP_WAV_UP       = "/root/beep_up.wav"    # 無ければ自動生成（/tmp）
UDP_CMD_PORT      = 5006
ENABLE_UDP_CMD    = True
BEEP_COOLDOWN_SEC = 60

PING_TRIES        = 3
PING_TIMEOUT_SEC  = 1.0
FAIL_TH           = 2
REC_TH            = 1
UI_INTERVAL_SEC   = 0.2   # 白丸を見やすく＆タップ即反映

ALSA_DEVICE = os.environ.get("ALSA_DEVICE")

# ダウン音のパターン
DOWN_BURST_BEEPS       = 3
DOWN_BURST_REPEAT      = 3
DOWN_INNER_INTERVAL_S  = 0.08
DOWN_REPEAT_INTERVAL_S = 0.5

FONT_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"

# タッチ座標補正
TOUCH_SWAP_XY = bool(int(os.environ.get("TOUCH_SWAP_XY", "0")))
TOUCH_INV_X   = bool(int(os.environ.get("TOUCH_INV_X",   "0")))
TOUCH_INV_Y   = bool(int(os.environ.get("TOUCH_INV_Y",   "0")))
TOUCH_SCALE_X = float(os.environ.get("TOUCH_SCALE_X", "1.0"))
TOUCH_SCALE_Y = float(os.environ.get("TOUCH_SCALE_Y", "1.0"))
TOUCH_OFFSET_X = int(os.environ.get("TOUCH_OFFSET_X", "0"))
TOUCH_OFFSET_Y = int(os.environ.get("TOUCH_OFFSET_Y", "0"))
DEBUG_TOUCH   = bool(int(os.environ.get("DEBUG_TOUCH",   "0")))

def dbg(*a):
    if DEBUG_TOUCH:
        print("[TOUCHDBG]", *a)

# ========= FB ioctl =========
FBIOGET_FSCREENINFO = 0x4602
FBIOGET_VSCREENINFO = 0x4600

# ========= サウンド =========
def _gen_tone_wav(path, freq_hz=1000, dur_ms=120, vol=0.5, rate=22050):
    n = int(rate * (dur_ms / 1000.0))
    with wave.open(path, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(rate)
        for i in range(n):
            s = vol * math.sin(2 * math.pi * freq_hz * (i / rate))
            v = int(max(-1, min(1, s)) * 32767)
            w.writeframesraw(v.to_bytes(2, "little", signed=True))

def ensure_beep_wavs():
    global BEEP_WAV_DOWN, BEEP_WAV_UP
    if not BEEP_WAV_DOWN or not os.path.exists(str(BEEP_WAV_DOWN)):
        p = "/tmp/mini_nms_beep_down.wav"
        if not os.path.exists(p): _gen_tone_wav(p, 700, 140, 0.7)
        BEEP_WAV_DOWN = p
    if not BEEP_WAV_UP or not os.path.exists(str(BEEP_WAV_UP)):
        p = "/tmp/mini_nms_beep_up.wav"
        if not os.path.exists(p): _gen_tone_wav(p, 1200, 110, 0.7)
        BEEP_WAV_UP = p

def play_wav(path: Optional[str]) -> bool:
    if not path or not os.path.exists(path):
        print(f"[SND] missing wav: {path}"); return False
    if not shutil.which("aplay"):
        print("[SND] 'aplay' not found. sudo apt-get install -y alsa-utils"); return False
    cmd = ["aplay", "-q"];  cmd += (["-D", ALSA_DEVICE] if ALSA_DEVICE else []); cmd += [path]
    try:
        r = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
        return r.returncode == 0
    except Exception as e:
        print("[SND] aplay error:", e); return False

def beep_down(repeats=1, beeps_per_burst=DOWN_BURST_BEEPS):
    def _run():
        for n in range(int(max(1,repeats))):
            for _ in range(int(max(1,beeps_per_burst))):
                ok = play_wav(BEEP_WAV_DOWN)
                time.sleep(DOWN_INNER_INTERVAL_S if ok else 0.2)
            if n < repeats-1: time.sleep(DOWN_REPEAT_INTERVAL_S)
    threading.Thread(target=_run, daemon=True).start()

def beep_up():
    threading.Thread(target=lambda: play_wav(BEEP_WAV_UP), daemon=True).start()

# ========= FB =========
def fb_get_info(fd):
    fixfmt = "16sL I I I I H H H I 24x"
    fixbuf = bytearray(struct.calcsize(fixfmt))
    fcntl.ioctl(fd, FBIOGET_FSCREENINFO, fixbuf, True)
    *_, line_len = struct.unpack(fixfmt, fixbuf)
    varfmt = "I I I I I I I 4x 32x"
    varbuf = bytearray(struct.calcsize(varfmt))
    fcntl.ioctl(fd, FBIOGET_VSCREENINFO, varbuf, True)
    xres, yres, *_, bpp = struct.unpack(varfmt, varbuf)
    return xres, yres, bpp, line_len

def fb_path():
    fb = os.environ.get("FBDEV", DEFAULT_FB)
    if os.path.exists(fb): return fb
    for p in ("/dev/fb1","/dev/fb0"):
        if os.path.exists(p): return p
    raise RuntimeError("No framebuffer found")

class FB:
    def __init__(self, path):
        self.fd = open(path, "r+b")
        self.xres, self.yres, self.bpp, self.line_len = fb_get_info(self.fd.fileno())
        self.mm = mmap.mmap(self.fd.fileno(), self.line_len*self.yres, mmap.MAP_SHARED, mmap.PROT_WRITE)

    def blit_image(self, img: Image.Image):
        W,H = self.xres, self.yres
        if img.size != (W,H): raise RuntimeError(f"Canvas {img.size} != FB {(W,H)}")
        if self.bpp == 16:
            it = iter(img.convert("RGB").getdata())
            for y in range(H):
                line = bytearray(self.line_len); off = 0
                for _ in range(W):
                    r,g,b = next(it); v=((r>>3)<<11)|((g>>2)<<5)|(b>>3)
                    line[off]=v&0xFF; line[off+1]=(v>>8)&0xFF; off+=2
                self.mm.seek(y*self.line_len); self.mm.write(line)
        elif self.bpp == 32:
            it = iter(img.convert("RGBA").getdata())
            for y in range(H):
                line = bytearray(self.line_len); off=0
                for _ in range(W):
                    r,g,b,a = next(it); line[off:off+4]=bytes((b,g,r,0xFF)); off+=4
                self.mm.seek(y*self.line_len); self.mm.write(line)
        else:
            raise RuntimeError(f"Unsupported bpp: {self.bpp}")

    def clear(self, rgb=(0,0,0)):
        try:
            self.blit_image(Image.new("RGB",(self.xres,self.yres),rgb))
        except Exception as e:
            print("[FB] clear failed:", e)

    def close(self):
        try: self.mm.flush(); self.mm.close()
        finally: self.fd.close()

# ========= 監視 =========
@dataclass
class Tgt:
    name: str; host: str; method: str="icmp"; port: int=0; interval_ms: int=5000
    last_check: int=0; consec_ok: int=0; consec_ng: int=0; is_down: bool=False
    last_avg: float=-1.0; changed_ms: int=0; down_ms: int=0; last_beep: int=0

def load_targets() -> List[Tgt]:
    for p in CONFIG_PATHS:
        if os.path.exists(p):
            with open(p) as f: raw = json.load(f)
            arr=[]; src=raw.get("targets", raw if isinstance(raw,list) else [])
            for o in src:
                arr.append(Tgt(o["name"], o["host"], o.get("method","icmp"),
                               int(o.get("port",0)), int(o.get("interval_ms",5000))))
            print(f"[CFG] loaded {len(arr)} targets from {p}"); return arr
    print("[CFG] using defaults")
    return [Tgt("GW","192.168.11.1","icmp",0,4000),
            Tgt("DNS1","8.8.8.8","icmp",0,6000),
            Tgt("WEB","www.google.com","tcp",443,7000)]

def ping_once(host: str, timeout_s: float) -> Optional[float]:
    cmd=["ping","-n","-c","1","-W",str(int(max(1,timeout_s))),host]
    try: out=subprocess.check_output(cmd,stderr=subprocess.STDOUT,text=True,timeout=timeout_s+0.8)
    except Exception: return None
    for ln in out.splitlines():
        if "time=" in ln:
            try: return float(ln.split("time=",1)[1].split(" ",1)[0])
            except Exception: return None
    return None

def icmp_avg(host: str, tries=PING_TRIES) -> Tuple[bool,float]:
    s=0.0; ok=0
    for _ in range(tries):
        r=ping_once(host,PING_TIMEOUT_SEC)
        if r is not None: s+=r; ok+=1
        time.sleep(0.03)
    return (True, s/ok) if ok>0 else (False, -1.0)

def tcp_check(host: str, port: int, timeout=1.0) -> Tuple[bool,float]:
    t0=time.time()
    try:
        with socket.create_connection((host,port),timeout=timeout):
            return True,(time.time()-t0)*1000.0
    except Exception:
        return False,-1.0

# ========= テキスト描画 =========
def text_w(draw, s, font):  return draw.textbbox((0,0), s, font=font)[2]
def ellipsize(draw,s,font,max_w):
    if text_w(draw,s,font)<=max_w: return s
    if max_w<=text_w(draw,"...",font): return "..."
    lo,hi=0,len(s)
    while lo<hi:
        mid=(lo+hi+1)//2; cand=s[:mid]+"..."
        if text_w(draw,cand,font)<=max_w: lo=mid
        else: hi=mid-1
    return s[:lo]+"..."
def wrap_lines(draw,s,font,max_w,max_lines):
    words=s.split(" "); lines=[]; cur=""
    for w in words:
        cand=w if not cur else (cur+" "+w)
        if text_w(draw,cand,font)<=max_w: cur=cand
        else:
            if cur: lines.append(cur); cur=w
            else:
                part=""
                for ch in w:
                    if text_w(draw,part+ch,font)<=max_w: part+=ch
                    else:
                        if part: lines.append(part); part=ch
                cur=part
        if len(lines)==max_lines: break
    if len(lines)<max_lines and cur: lines.append(cur)
    if len(lines)>max_lines: lines=lines[:max_lines]
    if lines and text_w(draw,lines[-1],font)>max_w:
        lines[-1]=ellipsize(draw,lines[-1],font,max_w)
    return lines
def make_scaled_fonts(F_BIG0,F_MED0,font_path,y_space):
    BIG_LINE0=F_BIG0+2; MED_LINE0=int(F_MED0*1.05); est=BIG_LINE0+MED_LINE0*4
    s=1.0 if est<=0 else min(1.0,max(0.6,y_space/float(est)))
    F_BIG=max(10,int(F_BIG0*s)); F_MED=max(9,int(F_MED0*s))
    try:
        font_big=ImageFont.truetype(font_path,F_BIG); font_med=ImageFont.truetype(font_path,F_MED)
    except Exception:
        font_big=font_med=ImageFont.load_default()
    return font_big, font_med, F_BIG, F_MED, (F_BIG+2), int(F_MED*1.05), s

# ========= タッチ =========
TouchEvent = collections.namedtuple("TouchEvent","x y t_ms type")  # "down"|"up"|"up_long"

def _find_touch_device():
    try:
        from evdev import InputDevice, list_devices, ecodes
    except Exception:
        return None
    explicit=os.environ.get("TOUCH_DEV")
    if explicit and os.path.exists(explicit): return explicit
    cands=[]
    for p in list_devices():
        try:
            d=InputDevice(p); caps=d.capabilities()
            if not caps: continue
            if ecodes.EV_ABS not in caps: continue
            abs_list=caps[ecodes.EV_ABS]
            codes=set(c if isinstance(c,int) else c[0] for c in abs_list)
            has_xy={ecodes.ABS_X,ecodes.ABS_Y}.issubset(codes)
            has_mt={ecodes.ABS_MT_POSITION_X,ecodes.ABS_MT_POSITION_Y}.issubset(codes)
            if has_xy or has_mt: cands.append((p,d.name or ""))
        except Exception: pass
    if cands:
        try: print("[TOUCH] candidates:", ", ".join([f"{p}({n})" for p,n in cands]))
        except Exception: pass
        return cands[0][0]
    return None

def touch_listener(xres,yres,q:"queue.Queue[TouchEvent]"):
    """
    ABS_X/ABS_Y と ABS_MT_POSITION_X/Y のどちらでも拾い、
    XYの初回変化で down、停止で up（BTN/PRESSURE/MT_ID があればそれ優先）
    """
    try:
        from evdev import InputDevice, ecodes
    except Exception:
        print("[TOUCH] evdev not available; touch disabled"); return

    def open_dev():
        path = _find_touch_device() or os.environ.get("TOUCH_DEV", "/dev/input/event0")
        if not path or not os.path.exists(path):
            print("[TOUCH] no touch device found; set TOUCH_DEV=/dev/input/eventX"); return None
        try:
            d = InputDevice(path)
            print(f"[TOUCH] listening on {path} ({d.name})")
            return d
        except Exception as e:
            print("[TOUCH] open error:", e); return None

    while True:
        d = open_dev()
        if not d:
            time.sleep(2.0); continue

        def get_abs_range(dev, code):
            try: info=dev.absinfo(code); return info.min, info.max, True
            except Exception: return 0,0,False

        Xmin,Xmax,HAS_X   = get_abs_range(d, ecodes.ABS_X)
        Ymin,Ymax,HAS_Y   = get_abs_range(d, ecodes.ABS_Y)
        MXmin,MXmax,HAS_MX= get_abs_range(d, ecodes.ABS_MT_POSITION_X)
        MYmin,MYmax,HAS_MY= get_abs_range(d, ecodes.ABS_MT_POSITION_Y)

        if not ((HAS_X and HAS_Y) or (HAS_MX and HAS_MY)):
            print("[TOUCH] no usable ABS ranges; retry")
            try: d.close()
            except: pass
            time.sleep(1.5); continue

        if Xmax==Xmin: Xmin,Xmax=0,4095
        if Ymax==Ymin: Ymin,Ymax=0,4095
        if MXmax==MXmin: MXmin,MXmax=0,4095
        if MYmax==MYmin: MYmin,MYmax=0,4095

        def has_abs(code):
            try: d.absinfo(code); return True
            except Exception: return False

        has_btn=False
        try:
            keys=set(d.capabilities().get(ecodes.EV_KEY,[]))
            has_btn=(ecodes.BTN_TOUCH in keys) or (getattr(ecodes,"BTN_TOOL_FINGER",330) in keys)
        except Exception: pass
        has_mt_id    = has_abs(ecodes.ABS_MT_TRACKING_ID)
        has_pressure = has_abs(ecodes.ABS_PRESSURE)

        def norm(v,vmin,vmax,out):
            v=max(vmin,min(vmax,v)); return int((v-vmin)*(out-1)/float(max(1,vmax-vmin)))

        def map_xy(src, rx, ry):
            if src=="MT": x=norm(rx,MXmin,MXmax,xres); y=norm(ry,MYmin,MYmax,yres)
            else:         x=norm(rx,Xmin,Xmax,xres);   y=norm(ry,Ymin,Ymax,yres)
            if TOUCH_SWAP_XY: x,y=y,x
            if TOUCH_INV_X:   x=(xres-1)-x
            if TOUCH_INV_Y:   y=(yres-1)-y
            return x,y

        def apply_cal(x,y):
            x=int(round(TOUCH_SCALE_X*x + TOUCH_OFFSET_X))
            y=int(round(TOUCH_SCALE_Y*y + TOUCH_OFFSET_Y))
            if x<0: x=0
            if y<0: y=0
            if x>=xres: x=xres-1
            if y>=yres: y=yres-1
            return x,y

        touching=False; down_t_ms=0
        IDLE_RELEASE=220  # ms

        last_ax=(Xmin+Xmax)//2; last_ay=(Ymin+Ymax)//2
        last_mx=(MXmin+MXmax)//2; last_my=(MYmin+MYmax)//2
        last_src="MT" if (HAS_MX and HAS_MY) else "ABS"
        last_xy=apply_cal(*map_xy(last_src, last_mx if last_src=="MT" else last_ax,
                                           last_my if last_src=="MT" else last_ay))
        last_move_ms=0

        def press(now):
            nonlocal touching, down_t_ms
            if not touching:
                touching=True; down_t_ms=now
                q.put(TouchEvent(last_xy[0], last_xy[1], now, "down")); dbg("DOWN", last_xy)

        def release(now):
            nonlocal touching
            if touching:
                touching=False
                typ="up_long" if (now-down_t_ms)>=1000 else "up"
                q.put(TouchEvent(last_xy[0], last_xy[1], now, typ)); dbg("UP  ", last_xy, typ)

        try:
            for ev in d.read_loop():
                now=int(time.time()*1000)

                if ev.type==ecodes.EV_ABS:
                    if ev.code==ecodes.ABS_X:
                        last_ax=ev.value; last_src="ABS"
                        last_xy=apply_cal(*map_xy("ABS", last_ax, last_ay)); last_move_ms=now
                        if not touching: press(now)
                    elif ev.code==ecodes.ABS_MT_POSITION_X:
                        last_mx=ev.value; last_src="MT"
                        last_xy=apply_cal(*map_xy("MT", last_mx, last_my)); last_move_ms=now
                        if not touching: press(now)

                    if ev.code==ecodes.ABS_Y:
                        last_ay=ev.value; last_src="ABS"
                        last_xy=apply_cal(*map_xy("ABS", last_ax, last_ay)); last_move_ms=now
                        if not touching: press(now)
                    elif ev.code==ecodes.ABS_MT_POSITION_Y:
                        last_my=ev.value; last_src="MT"
                        last_xy=apply_cal(*map_xy("MT", last_mx, last_my)); last_move_ms=now
                        if not touching: press(now)

                    if has_mt_id and ev.code==ecodes.ABS_MT_TRACKING_ID:
                        if ev.value==-1: release(now)
                        else:            press(now)
                    elif has_pressure and ev.code==ecodes.ABS_PRESSURE:
                        if ev.value>0:  press(now)
                        else:           release(now)

                elif ev.type==ecodes.EV_KEY and ev.code in (ecodes.BTN_TOUCH, getattr(ecodes,"BTN_TOOL_FINGER",330)):
                    if ev.value==1: press(now)
                    elif ev.value==0: release(now)

                elif ev.type==ecodes.EV_SYN and ev.code==ecodes.SYN_REPORT:
                    if touching and last_move_ms and (now-last_move_ms)>IDLE_RELEASE:
                        release(now)

        except Exception as e:
            print("[TOUCH] error:", e)
        finally:
            try: d.close()
            except: pass
            time.sleep(1.0)  # 再接続

# ========= キー入力（任意） =========
def key_listener(toggle_cb, quit_cb):
    try:
        from evdev import InputDevice, ecodes, list_devices
    except Exception:
        print("[KEY] evdev not available; key toggle disabled"); return
    while True:
        try:
            dev_path=os.environ.get("INPUT_DEV","")
            if not dev_path:
                for p in list_devices():
                    try:
                        d=InputDevice(p); nm=(d.name or "").lower()
                        if "kbd" in nm or "keyboard" in nm: dev_path=p; break
                    except Exception: pass
            if not dev_path:
                print("[KEY] no input device found; toggle disabled"); return
            d=InputDevice(dev_path)
            print(f"[KEY] listening on {dev_path} ({d.name})")
            for ev in d.read_loop():
                if ev.type==1 and ev.value==1:
                    if ev.code in (ecodes.KEY_SPACE, ecodes.KEY_ENTER): toggle_cb()
                    elif ev.code in (ecodes.KEY_Q, ecodes.KEY_ESC): quit_cb(); return
        except Exception as e:
            print("[KEY] error:", e); time.sleep(1.0)

# ========= コントローラ =========
class Controller:
    def __init__(self):
        self.targets: List[Tgt]=load_targets()
        self.lock=threading.Lock(); self.running=True
        if ENABLE_UDP_CMD: threading.Thread(target=self._udp_listener,daemon=True).start()
        threading.Thread(target=self._scheduler,daemon=True).start()

    def _udp_listener(self):
        s=socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("0.0.0.0", UDP_CMD_PORT))
        print(f"[CMD] UDP listening on 0.0.0.0:{UDP_CMD_PORT}")
        while self.running:
            try:
                data,addr=s.recvfrom(2048)
                try: doc=json.loads(data.decode("utf-8","ignore"))
                except Exception: print("[CMD] invalid JSON from",addr); continue
                self._handle_cmd(doc)
            except Exception:
                time.sleep(0.05)

    def _handle_cmd(self, doc):
        try:
            cmd=str(doc.get("cmd",""))
            if cmd=="add":
                o=Tgt(str(doc["name"]), str(doc["host"]), str(doc.get("method","icmp")),
                      int(doc.get("port",0)), int(doc.get("interval_ms",5000)))
                with self.lock: self.targets.append(o); print(f"[ADD] {o}")
            elif cmd=="del":
                name=str(doc.get("name",""))
                with self.lock:
                    before=len(self.targets); self.targets=[t for t in self.targets if t.name!=name]
                print(f"[DEL] {name} ({before}→{len(self.targets)})")
            elif cmd=="set":
                name=str(doc.get("name","")); itvl=int(doc.get("interval_ms",0))
                with self.lock:
                    for t in self.targets:
                        if t.name==name and itvl>0:
                            t.interval_ms=itvl; print(f"[SET] {name} interval={itvl}")
            else:
                print(f"[CMD] unknown {doc}")
        except Exception as e:
            print(f"[CMD] error: {e} doc={doc}")

    def _check_one(self, t: Tgt):
        ok,avg = icmp_avg(t.host) if t.method=="icmp" else tcp_check(t.host,t.port,1.0)
        t.last_avg = avg if ok else -1.0
        now=int(time.time()*1000)
        if ok:
            t.consec_ok+=1; t.consec_ng=0
            if t.is_down and t.consec_ok>=REC_TH:
                t.is_down=False; t.changed_ms=now; t.down_ms=0; print(f"[UP]   {t.name} ({t.host})")
                beep_up(); t.last_beep=0
        else:
            t.consec_ng+=1; t.consec_ok=0
            if (not t.is_down) and t.consec_ng>=FAIL_TH:
                t.is_down=True; t.changed_ms=now; t.down_ms=now; print(f"[DOWN] {t.name} ({t.host})")
                beep_down(repeats=DOWN_BURST_REPEAT); t.last_beep=now
            elif t.is_down:
                if (t.last_beep==0) or ((now-t.last_beep)>BEEP_COOLDOWN_SEC*1000):
                    beep_down(repeats=1); t.last_beep=now

    def _scheduler(self):
        while self.running:
            now=int(time.time()*1000)
            with self.lock:
                for t in self.targets:
                    if now-t.last_check >= t.interval_ms:
                        t.last_check=now; self._check_one(t)
            time.sleep(0.05)

# ========= 情報取得 =========
def get_ip_lines():
    lines=[]
    try:
        out=subprocess.check_output(["ip","-o","-4","addr","show"],text=True)
        for ln in out.splitlines():
            p=ln.split()
            if "inet" in p: lines.append(f"{p[1]}: {p[3]}")
    except Exception: pass
    try:
        out=subprocess.check_output(["ip","route","show","default"],text=True); gw=out.split()
        if len(gw)>=3: lines.append(f"GW: {gw[2]}")
    except Exception: pass
    try: lines.append(f"Host: {socket.gethostname()}")
    except Exception: pass
    return lines or ["No IP info"]

# ========= UI =========
def run_ui(ctrl: "Controller"):
    fb = FB(fb_path())
    try:
        xres,yres=fb.xres, fb.yres

        def clamp(v,a,b): return max(a,min(b,v))
        F_BIG   = clamp(int(yres * 0.10), 14, 36)
        F_MED   = clamp(int(yres * 0.075), 12, 28)
        F_SM    = clamp(int(yres * 0.055), 10, 22)
        MARGIN  = clamp(int(min(xres, yres) * 0.05), 8, 32)
        PAD     = clamp(int(min(xres, yres) * 0.045), 6, 18)
        COLS    = 2 if xres <= 400 else 3

        try:
            font_big   = ImageFont.truetype(FONT_PATH, F_BIG)
            font_med   = ImageFont.truetype(FONT_PATH, F_MED)
            font_small = ImageFont.truetype(FONT_PATH, F_SM)
        except Exception:
            font_big = font_med = font_small = ImageFont.load_default()

        ui = {"mode": "DASH", "run": True}

        # キーボード（任意）
        threading.Thread(
            target=key_listener,
            args=(lambda: ui.update(mode=("INFO" if ui["mode"] == "DASH" else "DASH")),
                  lambda: ui.update(run=False)),
            daemon=True
        ).start()

        # タッチイベント
        touch_q: "queue.Queue[TouchEvent]" = queue.Queue(maxsize=64)
        threading.Thread(target=touch_listener, args=(xres, yres, touch_q), daemon=True).start()

        # タップ白丸
        last_touch_ms = 0
        last_touch_xy = (0, 0)

        while ui["run"]:
            # ---- タッチイベントを先に処理（全画面トグル）----
            try:
                while True:
                    ev = touch_q.get_nowait()
                    if ev.type == "down":
                        last_touch_ms = ev.t_ms
                        last_touch_xy = (ev.x, ev.y)
                        ui["mode"] = "INFO" if ui["mode"] == "DASH" else "DASH"
            except queue.Empty:
                pass

            # ---- 監視データ取得 ----
            with ctrl.lock:
                items = list(ctrl.targets)

            # ---- 描画開始 ----
            img = Image.new("RGB", (xres, yres), (18, 18, 18))
            d = ImageDraw.Draw(img)

            # ヘッダ
            nowtxt = time.strftime("%H:%M:%S")
            if any(t.is_down for t in items):
                d.rectangle([0, 0, xres, int(yres * 0.14)], fill=(190, 40, 40))
                d.text((MARGIN, int(yres * 0.02)), "ALERT: Down detected", fill=(0, 0, 0), font=font_big)
                d.text((xres - (F_SM * 6), int(yres * 0.02)), nowtxt, fill=(0, 0, 0), font=font_small)
                y0 = int(yres * 0.16)
            else:
                d.text((MARGIN, int(yres * 0.02)), "Mini NMS (MP135)", fill=(255, 255, 255), font=font_big)
                d.text((xres - (F_SM * 6), int(yres * 0.02)), nowtxt, fill=(180, 220, 255), font=font_small)
                y0 = int(yres * 0.14)

            if ui["mode"] == "INFO":
                d.text((MARGIN, y0), "Device Info", fill=(255, 255, 255), font=font_big)
                y = y0 + int(F_BIG * 1.3)
                for line in get_ip_lines():
                    d.text((MARGIN, y), line, fill=(220, 220, 220), font=font_med); y += int(F_MED * 1.2)
                info_lines = [
                    f"Targets: {len(items)}  UI_INTERVAL={UI_INTERVAL_SEC}s",
                    f"PING_TRIES={PING_TRIES}  TIMEOUT={PING_TIMEOUT_SEC}s",
                    f"FAIL_TH={FAIL_TH}  REC_TH={REC_TH}",
                    f"BEEP_COOLDOWN={BEEP_COOLDOWN_SEC}s  ALSA_DEVICE={ALSA_DEVICE or 'auto'}",
                    f"DOWN_BURST={DOWN_BURST_BEEPS} x {DOWN_BURST_REPEAT}",
                    f"TOUCH cal: scale=({TOUCH_SCALE_X},{TOUCH_SCALE_Y}) offset=({TOUCH_OFFSET_X},{TOUCH_OFFSET_Y})",
                ]
                for line in info_lines:
                    d.text((MARGIN, y), line, fill=(200, 220, 200), font=font_med); y += int(F_MED * 1.1)
                y += int(F_MED * 0.4)
                d.text((MARGIN, y), "Monitors:", fill=(255, 255, 255), font=font_med); y += int(F_MED * 1.1)
                for t in items[:10]:
                    m = "ICMP" if t.method == "icmp" else f"TCP:{t.port}"
                    txt = f"- {t.name}  {t.host}  {m}  {t.interval_ms}ms"
                    d.text((MARGIN, y), ellipsize(d, txt, font_small, xres - MARGIN*2),
                           fill=(220, 220, 220), font=font_small)
                    y += int(F_SM * 1.0)

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
                        if down: return (220, 70, 70)
                        if rtt < 0: return (200, 200, 70)
                        return (70, 200, 120)

                    for i, t in enumerate(items):
                        r, c = divmod(i, cols)
                        x = MARGIN + c * (cell_w + MARGIN)
                        y = y0 + r * (cell_h + MARGIN)
                        bg = color(t.is_down, t.last_avg)
                        d.rectangle([x - 2, y - 2, x + cell_w + 2, y + cell_h + 2], fill=(40, 40, 40))
                        d.rectangle([x, y, x + cell_w, y + cell_h], fill=bg)

                        usable_h = max(10, cell_h - PAD*2)
                        cell_font_big, cell_font_med, F_BIG_cell, F_MED_cell, BIG_LINE, MED_LINE, scale = \
                            make_scaled_fonts(F_BIG, F_MED, FONT_PATH, usable_h)

                        tx = x + PAD
                        ty = y + int(PAD * 0.7)
                        usable_w = max(4, cell_w - PAD*2)
                        bottom_limit = y + cell_h - PAD

                        def can_place(line_h):
                            return ty + line_h <= bottom_limit

                        name_txt = ellipsize(d, t.name, cell_font_big, usable_w)
                        if can_place(BIG_LINE):
                            d.text((tx, ty), name_txt, fill=(0, 0, 0), font=cell_font_big)
                            ty += BIG_LINE
                        else:
                            continue

                        reserve_for_state = MED_LINE
                        reserve_for_tail  = MED_LINE
                        remaining_for_host = max(0, bottom_limit - (ty + reserve_for_state + reserve_for_tail))
                        host_max_lines = int(min(2, max(0, remaining_for_host // MED_LINE)))

                        hostline = f"{t.host}" if t.method == "icmp" else f"{t.host}:{t.port} (TCP)"
                        host_lines = wrap_lines(d, hostline, cell_font_med, usable_w, max_lines=host_max_lines)
                        for hl in host_lines:
                            if not can_place(MED_LINE): break
                            d.text((tx, ty), hl, fill=(0, 0, 0), font=cell_font_med)
                            ty += MED_LINE

                        st = "STATE: DOWN" if t.is_down else "STATE: UP"
                        st = ellipsize(d, st, cell_font_med, usable_w)
                        if can_place(MED_LINE):
                            d.text((tx, ty), st, fill=(0, 0, 0), font=cell_font_med)
                            ty += MED_LINE

                        if t.is_down:
                            secs = int((time.time() * 1000 - t.down_ms) / 1000) if t.down_ms else 0
                            tail = f"DOWN {secs}s"
                        else:
                            tail = "RTT: --" if t.last_avg < 0 else f"RTT: {t.last_avg:.1f} ms"
                        tail = ellipsize(d, tail, cell_font_med, usable_w)
                        if can_place(MED_LINE):
                            d.text((tx, ty), tail, fill=(0, 0, 0), font=cell_font_med)

            # タップ白丸（最後に描く）
            now_ms = int(time.time()*1000)
            show_ms = max(600, int(UI_INTERVAL_SEC * 1000 * 3))
            if last_touch_ms and (now_ms - last_touch_ms) < show_ms:
                cx, cy = last_touch_xy
                r = max(10, min(xres, yres)//24)
                d.ellipse([cx-r, cy-r, cx+r, cy+r], fill=(255,255,255))
                d.ellipse([cx-r, cy-r, cx+r, cy+r], outline=(0,0,0), width=2)

            # blit
            try:
                fb.blit_image(img)
            except KeyboardInterrupt:
                raise
            except Exception as e:
                print("FB blit error:", e)
                time.sleep(1)

            time.sleep(UI_INTERVAL_SEC)

    finally:
        try:
            fb.clear((0, 0, 0))
            time.sleep(0.05)
        except Exception as e:
            print("[FB] final clear error:", e)
        fb.close()

# ========= メイン =========
def main():
    ensure_beep_wavs()
    ctrl=Controller()
    try:
        run_ui(ctrl)
    except KeyboardInterrupt:
        print("\n[INFO] Ctrl-C, exit.")
    finally:
        ctrl.running=False
        time.sleep(0.1)

if __name__=="__main__":
    main()
