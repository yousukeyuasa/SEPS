#!/usr/bin/env python3
# Minimal Web Config for Mini Field NMS
# - 標準ライブラリのみで動く簡易Web UI
# - /etc/mini_nms/targets.json を編集
# - 変更を UDP(cmd: add/del/set) でランタイムに即反映
#
# 起動例: sudo python3 mini_nms_config_web.py --port 8080
# アクセス: http://<device-ip>:8080/

import json, os, argparse, socket, threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

CONFIG_PATH = "/etc/mini_nms/targets.json"
UDP_PORT    = 5006            # 監視側のポート（あなたの監視プログラム既定）
UDP_HOST    = "127.0.0.1"     # 同じマシンで動かしている想定（必要ならLANのIPへ）

HTML_PAGE = """<!doctype html><html><head><meta charset="utf-8">
<title>Mini NMS Config</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
body{font-family:sans-serif;max-width:960px;margin:24px auto;padding:0 12px;}
h1{font-size:22px} table{width:100%;border-collapse:collapse;margin:8px 0}
th,td{border:1px solid #ddd;padding:8px;font-size:14px}
tr:nth-child(even){background:#fafafa} input,select,button{font-size:14px;padding:6px}
.row{display:flex;gap:8px;flex-wrap:wrap;margin:8px 0}
.card{background:#f6f8fa;border:1px solid #e5e7eb;border-radius:10px;padding:12px}
.small{color:#666;font-size:12px}
.btn{background:#0ea5e9;border:0;color:#fff;border-radius:6px;padding:8px 12px;cursor:pointer}
.btn.danger{background:#ef4444}
.btn.secondary{background:#64748b}
</style></head><body>
<h1>Mini NMS Config</h1>
<div class="small">このページからターゲット(監視項目)の追加・削除・間隔変更ができます。</div>

<div class="card">
  <h3>現在のターゲット</h3>
  <table id="tbl">
    <thead><tr>
      <th>Name</th><th>Host</th><th>Method</th><th>Port</th><th>Interval(ms)</th><th>Action</th>
    </tr></thead>
    <tbody></tbody>
  </table>
  <button class="btn secondary" onclick="reload()">Reload</button>
  <button class="btn" onclick="saveJson()">Save JSON</button>
</div>

<div class="card">
  <h3>追加</h3>
  <div class="row">
    <input id="name"  placeholder="NAME"  style="flex:1">
    <input id="host"  placeholder="HOST (IP or FQDN)" style="flex:1.3">
    <select id="method"><option value="icmp">icmp</option><option value="tcp">tcp</option></select>
    <input id="port"  placeholder="PORT (tcp時)" type="number" value="0" style="width:110px">
    <input id="interval" placeholder="INTERVAL ms" type="number" value="5000" style="width:140px">
    <button class="btn" onclick="add()">Add</button>
  </div>
</div>

<div class="small">JSON保存は /etc/mini_nms/targets.json に書き込みます。ランタイム反映はUDPで即時行います。</div>

<script>
async function reload(){
  const r=await fetch('/api/targets'); const j=await r.json();
  const tb=document.querySelector('#tbl tbody'); tb.innerHTML='';
  j.targets.forEach(t=>{
    const tr=document.createElement('tr');
    tr.innerHTML = `
      <td>${t.name}</td>
      <td>${t.host}</td>
      <td>${t.method}</td>
      <td>${t.port||0}</td>
      <td>
        <input type="number" value="${t.interval_ms||5000}" style="width:120px"
          onchange="setIntervalMs('${t.name}', this.value)">
      </td>
      <td>
        <button class="btn danger" onclick="del('${t.name}')">Delete</button>
      </td>`;
    tb.appendChild(tr);
  })
}
async function add(){
  const body = {
    name: document.querySelector('#name').value.trim(),
    host: document.querySelector('#host').value.trim(),
    method: document.querySelector('#method').value,
    port: parseInt(document.querySelector('#port').value||'0',10),
    interval_ms: parseInt(document.querySelector('#interval').value||'5000',10)
  };
  if(!body.name||!body.host){ alert('name/host は必須'); return; }
  await fetch('/api/add',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  await reload();
}
async function del(name){
  if(!confirm(name+' を削除しますか？')) return;
  await fetch('/api/del',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name})});
  await reload();
}
async function setIntervalMs(name, interval_ms){
  await fetch('/api/set',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name, interval_ms:parseInt(interval_ms,10)})});
}
async function saveJson(){
  const r=await fetch('/api/save',{method:'POST'}); const j=await r.json();
  alert('Saved: '+j.path);
}
reload();
</script></body></html>
"""

def load_targets():
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH) as f:
            raw=json.load(f)
        return raw.get("targets", raw if isinstance(raw,list) else [])
    return []

def save_targets(targets):
    os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
    with open(CONFIG_PATH, "w") as f:
        json.dump({"targets": targets}, f, ensure_ascii=False, indent=2)
    return CONFIG_PATH

def send_udp(obj):
    data=json.dumps(obj).encode("utf-8")
    s=socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.sendto(data, (UDP_HOST, UDP_PORT))
    s.close()

class Handler(BaseHTTPRequestHandler):
    def _json(self, obj, code=200):
        body=json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type","application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers(); self.wfile.write(body)

    def _bad(self, msg, code=400):
        self._json({"error": msg}, code)

    def do_GET(self):
        if self.path == "/" or self.path.startswith("/index"):
            body=HTML_PAGE.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type","text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers(); self.wfile.write(body); return
        if self.path.startswith("/api/targets"):
            self._json({"targets": load_targets()}); return
        self._bad("not found", 404)

    def do_POST(self):
        ln=int(self.headers.get("Content-Length","0") or "0")
        raw=self.rfile.read(ln) if ln>0 else b"{}"
        try: doc=json.loads(raw.decode("utf-8"))
        except Exception: doc={}
        if self.path == "/api/add":
            name=doc.get("name","").strip(); host=doc.get("host","").strip()
            method=(doc.get("method","icmp") or "icmp").lower()
            port=int(doc.get("port",0)); itvl=int(doc.get("interval_ms",5000))
            if not name or not host: return self._bad("name/host required")
            if method not in ("icmp","tcp"): return self._bad("method icmp/tcp")
            targets=load_targets()
            # 名前重複 → 上書き
            targets=[t for t in targets if t.get("name")!=name]
            targets.append({"name":name,"host":host,"method":method,"port":port,"interval_ms":itvl})
            save_targets(targets)
            # ランタイム反映
            send_udp({"cmd":"add","name":name,"host":host,"method":method,"port":port,"interval_ms":itvl})
            return self._json({"ok":True})
        if self.path == "/api/del":
            name=doc.get("name","").strip()
            if not name: return self._bad("name required")
            targets=[t for t in load_targets() if t.get("name")!=name]
            save_targets(targets)
            send_udp({"cmd":"del","name":name})
            return self._json({"ok":True})
        if self.path == "/api/set":
            name=doc.get("name","").strip(); itvl=int(doc.get("interval_ms",0))
            if not name or itvl<=0: return self._bad("name/interval_ms required")
            targets=load_targets()
            for t in targets:
                if t.get("name")==name: t["interval_ms"]=itvl
            save_targets(targets)
            send_udp({"cmd":"set","name":name,"interval_ms":itvl})
            return self._json({"ok":True})
        if self.path == "/api/save":
            p=save_targets(load_targets()); return self._json({"ok":True,"path":p})
        self._bad("not found", 404)

def main():
    # ← これを関数の一番上に置く
    global CONFIG_PATH, UDP_HOST, UDP_PORT

    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--config", default=CONFIG_PATH)
    ap.add_argument("--udp-host", default=UDP_HOST)
    ap.add_argument("--udp-port", type=int, default=UDP_PORT)
    args = ap.parse_args()

    # 起動引数で上書き（以後、モジュール全体で使われる）
    CONFIG_PATH = args.config
    UDP_HOST    = args.udp_host
    UDP_PORT    = args.udp_port

    httpd = HTTPServer((args.host, args.port), Handler)
    print(f"[CFGWEB] serving on http://{args.host}:{args.port}  "
          f"(config={CONFIG_PATH}, udp={UDP_HOST}:{UDP_PORT})")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__=="__main__":
    main()
