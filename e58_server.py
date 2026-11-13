#!/usr/bin/env python3
"""E58 camera video server: rebroadcasts the adapter's stream over HTTP.

The adapter streams to only one client at a time. This server is that one
client, and any number of local programs can then use the camera through
standard HTTP:

    http://localhost:8081/              control page (live view + buttons)
    http://localhost:8081/stream.mjpg   MJPEG stream (VLC, OBS, browsers,
                                        cv2.VideoCapture(url), ffmpeg ...)
    http://localhost:8081/snapshot.jpg  latest frame, raw 640x480 JPEG
    POST /switch                        toggle camera
    POST /record/start, /record/stop    lossless recording into captures/
    GET  /status                        JSON: fps, counts, camera, recording

Run while connected to the adapter's Wi-Fi (close the phone app):
    /usr/bin/python3 -I e58_server.py            # localhost only
    /usr/bin/python3 -I e58_server.py --host 0.0.0.0   # also from the LAN
Standard library only (plus e58cam.py next to this file).
"""
import argparse
import datetime
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from e58cam import E58Camera  # noqa: E402

SETTLE = 0.5   # s of old-camera frames to hide after a switch
BOUNDARY = "e58frame"

PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>E58 Camera</title>
<style>
:root{--bg:#f6f6f4;--fg:#1d1d1b;--muted:#6b6b66;--card:#fff;--line:#deded8;
 --accent:#1f6feb;--rec:#d1242f}
@media (prefers-color-scheme:dark){:root{--bg:#141413;--fg:#ececea;
 --muted:#9a9a94;--card:#1e1e1c;--line:#33332f;--accent:#4c8dff;--rec:#ff5c5c}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
 font:15px/1.4 system-ui,-apple-system,"Segoe UI",sans-serif}
main{max-width:980px;margin:0 auto;padding:16px}
h1{font-size:18px;margin:0 0 12px}
.view{background:#000;border-radius:8px;overflow:hidden;aspect-ratio:4/3}
.view img{width:100%;height:100%;object-fit:contain;display:block}
.bar{display:flex;flex-wrap:wrap;gap:8px;margin:12px 0}
button,a.btn{font:inherit;padding:8px 14px;border-radius:6px;cursor:pointer;
 border:1px solid var(--line);background:var(--card);color:var(--fg);
 text-decoration:none}
button:hover,a.btn:hover{border-color:var(--accent)}
button.rec{border-color:var(--rec);color:var(--rec)}
.stats{display:flex;flex-wrap:wrap;gap:6px 18px;color:var(--muted);
 font-variant-numeric:tabular-nums}
.stats b{color:var(--fg);font-weight:600}
#msg{color:var(--muted);min-height:1.4em}
</style></head><body><main>
<h1>E58 Camera</h1>
<div class="view"><img id="v" src="/stream.mjpg" alt="Live camera stream"></div>
<div class="bar">
 <button id="sw">Switch camera</button>
 <a class="btn" href="/snapshot.jpg" download>Snapshot</a>
 <button id="rec">Start recording</button>
</div>
<div class="stats">
 <span>Camera <b id="cam">-</b></span><span><b id="fps">-</b> fps</span>
 <span>good <b id="good">-</b></span><span>damaged <b id="bad">-</b></span>
 <span>lost <b id="lost">-</b></span><span>viewers <b id="cl">-</b></span>
</div>
<p id="msg"></p>
</main><script>
const $=id=>document.getElementById(id);
async function post(u){const r=await fetch(u,{method:'POST'});return r.json()}
$('sw').onclick=async()=>{await post('/switch');refresh()};
$('rec').onclick=async()=>{
  const s=await (await fetch('/status')).json();
  const r=await post(s.recording?'/record/stop':'/record/start');
  $('msg').textContent=r.message||'';refresh()};
async function refresh(){
  try{const s=await (await fetch('/status')).json();
   $('cam').textContent=s.camera;$('fps').textContent=s.fps.toFixed(1);
   $('good').textContent=s.good;$('bad').textContent=s.damaged;
   $('lost').textContent=s.lost;$('cl').textContent=s.stream_clients;
   $('rec').textContent=s.recording?'Stop recording':'Start recording';
   $('rec').className=s.recording?'rec':'';
   if(!s.video_ok)$('msg').textContent='No video from the adapter - check the Wi-Fi connection.';
   else if($('msg').textContent.startsWith('No video'))$('msg').textContent='';
  }catch(e){$('msg').textContent='Server not reachable.'}}
setInterval(refresh,1000);refresh();
</script></body></html>
"""


class Server:
    def __init__(self, args):
        self.cam = E58Camera(ip=args.ip, port=args.port)
        self.capture_dir = os.path.join(HERE, "captures")
        self.lock = threading.Lock()
        self.clients = 0
        self.rec_path = None
        self.rec_started = 0.0

    # Latest frame, hiding old-camera frames for SETTLE s after a switch.
    def current_jpeg(self, timeout=3.0):
        end = time.time() + timeout
        while time.time() < end:
            jpg, info = self.cam.latest()
            if jpg and info.t >= self.cam.switched_at + SETTLE:
                return jpg
            self.cam.wait_jpeg(0.2, newer_than=info.seq if info else -1)
        return None

    def start_recording(self):
        with self.lock:
            if self.rec_path:
                return f"Already recording to {os.path.basename(self.rec_path)}"
            os.makedirs(self.capture_dir, exist_ok=True)
            name = datetime.datetime.now().strftime("rec_%Y%m%d_%H%M%S.mjpg")
            self.rec_path = os.path.join(self.capture_dir, name)
            self.rec_started = time.time()
            self.cam.start_recording(self.rec_path)
            return f"Recording to captures/{name}"

    def stop_recording(self):
        with self.lock:
            if not self.rec_path:
                return "Not recording"
            self.cam.stop_recording()
            path, self.rec_path = self.rec_path, None
        return self._convert(path)

    def _convert(self, path):
        """Wrap the raw .mjpg in an .avi at the measured frame rate, without
        re-encoding. Keeps the .mjpg and its .csv timestamps."""
        try:
            with open(path + ".csv") as f:
                rows = [r.split(",") for r in f.read().split("\n")[1:] if r]
        except OSError:
            rows = []
        if len(rows) < 2:
            return f"Saved {os.path.basename(path)} ({len(rows)} frames)"
        span = float(rows[-1][2]) - float(rows[0][2])
        fps = (len(rows) - 1) / span if span > 0 else 20.0
        if not shutil.which("ffmpeg"):
            return (f"Saved {os.path.basename(path)} ({len(rows)} frames, "
                    f"{fps:.1f} fps); install ffmpeg for .avi output")
        avi = path[:-5] + ".avi"
        r = subprocess.run(["ffmpeg", "-loglevel", "error", "-y",
                            "-f", "mjpeg", "-framerate", f"{fps:.3f}",
                            "-i", path, "-c:v", "copy", avi],
                           capture_output=True, text=True)
        if r.returncode != 0:
            return f"Saved {os.path.basename(path)}; ffmpeg failed: {r.stderr[-200:]}"
        return (f"Saved captures/{os.path.basename(avi)} "
                f"({len(rows)} frames, {span:.1f} s, {fps:.1f} fps)")

    def status(self):
        _, info = self.cam.latest()
        s = self.cam.stats
        return {
            "camera": self.cam.camera,
            "fps": round(s.fps, 2),
            "good": s.good, "damaged": s.damaged, "lost": s.lost,
            "video_ok": bool(info and time.time() - info.t < 2.0),
            "recording": self.rec_path is not None,
            "recording_file": os.path.basename(self.rec_path) if self.rec_path else None,
            "recording_seconds": round(time.time() - self.rec_started, 1) if self.rec_path else 0,
            "stream_clients": self.clients,
        }


def make_handler(srv: Server):
    class H(BaseHTTPRequestHandler):
        server_version = "e58-server/1.0"

        def log_message(self, fmt, *args):
            pass  # keep the console quiet; stream requests would flood it

        def _send(self, code, body, ctype):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj, code=200):
            self._send(code, json.dumps(obj).encode(), "application/json")

        def do_GET(self):
            path = self.path.split("?")[0]
            if path in ("/", "/index.html"):
                self._send(200, PAGE.encode(), "text/html; charset=utf-8")
            elif path == "/stream.mjpg":
                self._stream()
            elif path == "/snapshot.jpg":
                jpg = srv.current_jpeg()
                if jpg is None:
                    self._json({"error": "no video from adapter"}, 503)
                else:
                    self._send(200, jpg, "image/jpeg")
            elif path == "/status":
                self._json(srv.status())
            else:
                self._json({"error": "not found"}, 404)

        def do_POST(self):
            path = self.path.split("?")[0]
            if path == "/switch":
                srv.cam.switch_camera()
                self._json({"camera": srv.cam.camera})
            elif path == "/record/start":
                self._json({"message": srv.start_recording()})
            elif path == "/record/stop":
                self._json({"message": srv.stop_recording()})
            else:
                self._json({"error": "not found"}, 404)

        def _stream(self):
            self.send_response(200)
            self.send_header("Content-Type",
                             f"multipart/x-mixed-replace; boundary={BOUNDARY}")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            with srv.lock:
                srv.clients += 1
            last = -1
            try:
                while True:
                    jpg = srv.cam.wait_jpeg(3.0, newer_than=last)
                    if jpg is None:
                        continue  # adapter silent; e58cam resends start
                    jpg, info = srv.cam.latest()
                    last = info.seq
                    if info.t < srv.cam.switched_at + SETTLE:
                        continue
                    self.wfile.write(
                        f"--{BOUNDARY}\r\nContent-Type: image/jpeg\r\n"
                        f"Content-Length: {len(jpg)}\r\n\r\n".encode()
                        + jpg + b"\r\n")
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                with srv.lock:
                    srv.clients -= 1

    return H


def main():
    p = argparse.ArgumentParser(description="E58 camera HTTP video server")
    p.add_argument("--host", default="127.0.0.1",
                   help="interface to serve on (0.0.0.0 = all, incl. LAN)")
    p.add_argument("--http-port", type=int, default=8081)
    p.add_argument("--ip", default="192.168.4.153", help="adapter IP")
    p.add_argument("--port", type=int, default=8080, help="adapter UDP port")
    a = p.parse_args()

    srv = Server(a)
    srv.cam.start()
    httpd = ThreadingHTTPServer((a.host, a.http_port), make_handler(srv))
    httpd.daemon_threads = True
    host = "localhost" if a.host in ("127.0.0.1", "0.0.0.0") else a.host
    print(f"E58 server on http://{host}:{a.http_port}/  "
          f"(stream: /stream.mjpg, snapshot: /snapshot.jpg)  Ctrl+C to stop")
    if srv.cam.wait_jpeg(4.0) is None:
        print("Warning: no video yet. Is this machine on the adapter's Wi-Fi, "
              "and is the phone app closed?")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if srv.rec_path:
            print(srv.stop_recording())
        httpd.server_close()
        srv.cam.stop()


if __name__ == "__main__":
    main()
