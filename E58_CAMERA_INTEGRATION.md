# E58 camera adapter: integration guide for coding agents

**Audience:** a coding agent (or developer) adding the E58 Wi-Fi camera to a project. Read the whole file before writing code. Section 8 is a complete, tested Python client you can copy into the project as `e58cam.py`.

**Status:** protocol reverse-engineered from a phone capture and verified against the real adapter on 2026-11-13. Each fact below is marked **[verified]** (tested on the hardware) or **[unverified]** (inferred; test before relying on it).

---

## 1. What the hardware is

- A small camera board from an "E58" toy drone: Beken **BK7231U** Wi-Fi chip with **two cameras** on 20-pin ribbon cables. Only one camera streams at a time.
- It runs its **own Wi-Fi access point**. The host (laptop, PC, Raspberry Pi, phone...) joins that network and pulls video over UDP.
- Power: 3.0-4.2 V (one Li-ion cell; measured working at 3.82-3.9 V), 0.14 A idle, about 0.22 A while streaming **[verified]**. Never 5 V.
- The firmware is stock and must not be modified. Everything here is done from the host side.

## 2. Network facts

| Item | Value |
|---|---|
| SSID | `WIFI_4K_WB3BC10` (pattern `WIFI_4K_*`, suffix varies per board) **[verified]** |
| Security | probably open (no password); not recorded **[unverified]** |
| Adapter IP | `192.168.4.153` **[verified]** |
| Host IP | DHCP from the adapter, `192.168.4.x/24` (we got `.100`) **[verified]** |
| Channel / mode | 2.4 GHz channel 1, 802.11g (54 Mbit/s), fixed **[verified]** |
| Internet | none on this network; the adapter offers itself as the default gateway |
| Open TCP ports | none (all video and commands are UDP) **[verified: full TCP scan]** |

**Multi-homed hosts:** if the host also needs internet, give it a second interface (Ethernet, USB tethering, or a second Wi-Fi dongle). With NetworkManager the other interface normally wins the default route (lower metric) and traffic to `192.168.4.0/24` still goes to the adapter. To stop the adapter network from ever becoming the default route:
```
nmcli connection modify WIFI_4K_WB3BC10 ipv4.never-default yes
```
NetworkManager may also silently switch the Wi-Fi back to a known home network. If the code stops receiving video, check the host is still associated (`iw dev <wlan> link`) before debugging the protocol. To prevent the switch, lower the priority or disable autoconnect of the competing networks.

## 3. Protocol

All traffic is UDP between the host and `192.168.4.153:8080`.

### 3.1 Commands (host -> adapter:8080)

| Bytes | ASCII | Effect | Status |
|---|---|---|---|
| `42 76` | `Bv` | Start streaming video to the sender's IP and source port | **[verified]** |
| `42 79` | `By` | Toggle to the other camera | **[verified]**: two `By` return to the first camera |

- Send commands **from the same socket that receives the video**: the adapter replies to the command's source port.
- One `Bv` keeps the stream running for at least 40 s with no further traffic **[verified]**. Clients should still resend `Bv` if no video arrives for about 2 s (covers adapter reboots and Wi-Fi reconnects).
- **One receiver at a time [verified]:** the adapter streams only to the most recent sender of `Bv`. When a second client sends `Bv`, the first gets nothing more. The phone app and your code will steal the stream from each other, and two copies of a client with auto-restart (section 8) will fight every ~2 s. To serve several consumers, run one client and fan the frames out inside the host (section 7.3).
- There is **no stop command** known. Close the socket; the adapter keeps sending to a dead port for a while, which is harmless.
- The adapter does **not report which camera is active**. **Camera 1 is always active after power-up [verified]**; the reference client tracks the state in `cam.camera` from there. Track state in the client and offer a "switch" action, not "select camera N".
- **Switch latency ~0.45 s [verified]:** after `By`, about 7-9 more frames from the old camera arrive before the new camera's frames. If code must not mix cameras (for example recordings or vision), discard frames for ~0.5 s after a switch.
- There are **no on-board photo or record commands**. The phone app's photo/video buttons save frames on the phone. "Take picture" and "record video" are **host-side** operations on the received JPEG frames (section 4).
- Other byte pairs (`Bw`, `Bx`, `Bz`...) were **not tested**. Do not send unknown commands from production code.

### 3.2 Port 8090 (do not use)

The phone app also sends `aa 80 80 00 80 00 80 55` to port 8090 every ~0.79 s. It is very probably the **drone flight-control packet** (sticks centred) that the adapter forwards to the drone's flight board. The video works without it **[verified]**. Do not send it.

### 3.3 Video packets (adapter -> host)

Each UDP datagram (max 1472 bytes) = 8-byte header + a chunk of a JPEG.

| Offset | Meaning |
|---|---|
| 0 | frame id, 0-255, increments per frame, wraps |
| 1 | `1` on the last chunk of a frame, else `0` |
| 2 | unknown (values 2-25 seen); ignore |
| 3 | always `2` |
| 4-7 | magic `54 5A 48 00` = `"TZH\0"` |
| 8.. | JPEG data chunk |

There is **no chunk index**. Chunks arrive in order; append payloads until the last-chunk flag, then:

1. The buffer starts with `FF D8` (JPEG SOI).
2. Find the last `FF D9` (EOI). The JPEG is `buf[:eoi+2]`.
3. Bytes after the EOI are a trailer whose **last 4 bytes are the JPEG length, little-endian uint32**. If it differs from `len(jpeg)` a middle chunk was lost: **drop the frame**. Such frames often still decode, but as smeared garbage.
4. If the frame id changes before the last chunk arrives, the previous frame is incomplete: drop it.

Field of view: the image is strongly "zoomed" (narrow view) in the raw stream too, so it is the lens, not an app crop **[verified]**. Nothing in software widens it.

Frame format: baseline JPEG, **640 x 480**, about 7-12 KB at night and up to about 34 KB in a busy scene. Typical rate **~20 fps** to a laptop **[verified: 19.9 fps, 2 bad frames out of 400]**. The phone app advertises "4K"; that is upscaling in the app, and 640 x 480 is the true resolution.

## 4. Mapping user features to code

| Feature | Implementation |
|---|---|
| Live view | decode each JPEG (OpenCV `imdecode`, PIL, browser `<img>`...) and display |
| Take picture | write the next good JPEG **as-is** to `*.jpg` (lossless; do not re-encode) |
| Record video | append each good JPEG to a `.mjpg` file (lossless). Log a timestamp per frame. Convert later with `ffmpeg -f mjpeg -framerate <fps> -i clip.mjpg -c:v copy clip.avi` (or `.mkv`); `-c:v copy` avoids re-encoding. Use fps = frames / (t_last - t_first) from the timestamp log. To make an H.264 MP4 instead: `-c:v libx264 -crf 18 clip.mp4` |
| Switch camera | send `By`; flip a local `active_camera` variable |
| Health / status | frames per second, damaged and lost counts; "no video for N s" means a disconnect |
| Reconnect | resend `Bv` after ~2 s without packets (the client in section 8 does this) |

## 5. Pitfalls (each one bit us)

1. **Do not block the receive loop.** The stream never pauses, so a loop that "drains the socket until empty" never exits. Bound the drain by time (for example 30 ms), or receive on a dedicated thread (section 8 does this).
2. **Set a large receive buffer** (`SO_RCVBUF` 4 MB). One lost datagram ruins a whole frame (~8 datagrams per frame, no retransmission).
3. **Decode off the receive thread** if decoding or processing is slow. Hand frames over through a "latest frame" slot or a bounded queue, never an unbounded one.
4. **Validate with the length trailer**, not only SOI/EOI (section 3.3).
5. **Loss varies a lot over the air.** Damaged frames ranged from 0.5% to 20% within minutes at a strong signal (-32 dBm), with no drops in the host kernel. That is 2.4 GHz interference, not a client bug. Design consumers to tolerate gaps of several frames.
6. **Phone-side capture tools lie about loss.** The same stream showed ~40% bad frames through the phone app and a VPN capture, and 0.5% on a laptop.
7. **Only one receiver at a time [verified].** The last client to send `Bv` gets the stream. Close the phone app while your code runs, and never run two instances of the client against one adapter.

## 6. Requirements for the reference client

- Python 3.9+, standard library only.
- Optional: `opencv-python` + `numpy` for `decode_bgr()` and display; `ffmpeg` on PATH for video conversion.

## 7. Integration recipes

### 7.1 Minimal

```python
from e58cam import E58Camera

with E58Camera() as cam:               # sends "Bv", starts receive thread
    cam.snapshot("photo.jpg")          # next good frame, untouched
    cam.switch_camera()                # toggle cameras
    cam.start_recording("clip.mjpg")   # + clip.mjpg.csv timestamps
    import time; time.sleep(10)
    cam.stop_recording()
    print(cam.stats)                   # good / damaged / lost / fps
```

### 7.2 OpenCV processing loop

```python
import cv2
from e58cam import E58Camera, decode_bgr

with E58Camera() as cam:
    for jpg, info in cam.frames():     # newest frames; skips if you are slow
        img = decode_bgr(jpg)          # 640x480 BGR ndarray
        # ... your processing ...
        cv2.imshow("e58", img)
        if cv2.waitKey(1) == ord("q"):
            break
```

### 7.3 Expose as a standard MJPEG HTTP stream (for browsers, VLC, OBS, other languages)

A tested implementation exists in the source project as `e58_server.py` (standard library + `e58cam.py`). If it is available, copy it rather than rewriting. It serves a control page at `/`, `/stream.mjpg`, `/snapshot.jpg`, `POST /switch`, `POST /record/start|stop` (lossless `.mjpg` + `.csv`, auto-wrapped to `.avi` by ffmpeg) and `GET /status` (JSON), and it hides the ~0.5 s of old-camera frames after a switch. Verified 2026-11-13: two browser/curl viewers plus `cv2.VideoCapture` at the same time, ~19-20 fps each. Otherwise build it as follows:


Wrap the client in an HTTP server that serves `multipart/x-mixed-replace; boundary=frame` and writes `--frame\r\nContent-Type: image/jpeg\r\nContent-Length: N\r\n\r\n<jpeg>\r\n` for each frame from `cam.frames()`. Then any consumer can use `http://localhost:8081/stream.mjpg` (`cv2.VideoCapture(url)`, `<img src=...>`, VLC, OBS). Add `/snapshot.jpg` and `/switch` endpoints as needed. Use `ThreadingHTTPServer`, one generator per client, all reading from the single `E58Camera` instance.

### 7.4 Other languages and platforms

The protocol is small enough to reimplement directly from section 3: one UDP socket, send `Bv`, a reassembly buffer keyed on frame id, the trailer check, send `By` to switch. Notes:
- **Node.js:** `dgram.createSocket('udp4')`, `socket.send(Buffer.from('Bv'), 8080, '192.168.4.153')`, reassemble in the `'message'` handler.
- **C#/.NET:** `UdpClient`; set `Client.ReceiveBufferSize = 4 << 20`.
- **ESP32 (ESP-IDF/Arduino):** join the SSID as a station, use lwIP UDP. A frame is up to ~35 KB, so reassemble into PSRAM. Decoding 640 x 480 JPEG at full rate on an ESP32 is marginal; forwarding or storing the raw JPEGs is easy.
- **Browser JS:** cannot do raw UDP; use the HTTP bridge (7.3).

## 8. Reference implementation: `e58cam.py`

Copy this verbatim into the project. Verified on the real adapter on 2026-11-13: self-test `python e58cam.py 8` gave a snapshot and 20.0 fps with 161 good frames, 1 damaged, 0 lost. Recording and ffmpeg conversion were tested against a packet replay. Run the self-test once on new hardware to confirm the setup.

```python
"""e58cam: client for the E58 drone camera adapter (Beken BK7231U, SSID WIFI_4K_*).

Standard library only. OpenCV/numpy are optional (only for decode_bgr()).

    from e58cam import E58Camera
    cam = E58Camera()            # adapter at 192.168.4.153:8080
    cam.start()
    jpg = cam.wait_jpeg(timeout=3)   # raw 640x480 JPEG bytes
    cam.snapshot("photo.jpg")
    cam.switch_camera()
    cam.start_recording("clip.mjpg")
    ...
    cam.stop_recording()
    cam.stop()

Protocol (verified 2026-11-13 against the real adapter):
    host -> 192.168.4.153:8080  b"Bv"  start streaming to the sender's UDP port
    host -> 192.168.4.153:8080  b"By"  toggle between the two cameras
    adapter -> host  UDP packets: 8-byte header + JPEG chunk
        hdr[0] frame id (0..255, wraps), hdr[1] 1 = last chunk of frame,
        hdr[2] unknown, hdr[3] = 2, hdr[4:8] = b"TZH\\0"
    reassembled frame = JPEG (FFD8..FFD9) + trailer; the trailer's last
    4 bytes are the JPEG length (little-endian) and detect lost chunks.
"""
from __future__ import annotations

import socket
import struct
import threading
import time
from dataclasses import dataclass
from typing import Callable, Iterator, Optional

DEFAULT_IP = "192.168.4.153"
DEFAULT_PORT = 8080
CMD_START = b"Bv"
CMD_SWITCH = b"By"
MAGIC = b"TZH"


@dataclass
class FrameInfo:
    seq: int            # running count of good frames since start()
    frame_id: int       # adapter's 8-bit frame id
    t: float            # host receive time, time.time()
    size: int           # JPEG size in bytes


@dataclass
class Stats:
    good: int = 0       # frames delivered intact
    damaged: int = 0    # frames with a lost chunk (dropped)
    lost: int = 0       # frame ids that never arrived at all
    fps: float = 0.0    # good frames per second over the last ~1 s


def extract_jpeg(buf: bytes) -> Optional[bytes]:
    """Return the JPEG in a reassembled frame, or None if it is damaged."""
    if not buf.startswith(b"\xff\xd8"):
        return None
    end = buf.rfind(b"\xff\xd9")
    if end < 0:
        return None
    jpg = buf[:end + 2]
    tail = buf[end + 2:]
    if len(tail) >= 4 and struct.unpack("<I", tail[-4:])[0] != len(jpg):
        return None
    return jpg


def decode_bgr(jpg: bytes):
    """Decode JPEG bytes to an OpenCV BGR image (needs opencv-python, numpy)."""
    import cv2
    import numpy as np
    return cv2.imdecode(np.frombuffer(jpg, np.uint8), cv2.IMREAD_COLOR)


class E58Camera:
    def __init__(self, ip: str = DEFAULT_IP, port: int = DEFAULT_PORT,
                 bind_ip: str = "0.0.0.0", bind_port: int = 0,
                 restart_after: float = 2.0,
                 on_frame: Optional[Callable[[bytes, FrameInfo], None]] = None):
        """
        ip/port: adapter address. bind_ip: set to the host's address on the
        adapter network (e.g. "192.168.4.100") if the host has several
        interfaces and you want to be explicit. restart_after: resend the
        start command if no video arrives for this many seconds.
        on_frame: called from the receive thread for every good frame; keep
        it fast (copy/queue the data, do not block).
        """
        self.addr = (ip, port)
        self._bind = (bind_ip, bind_port)
        self.restart_after = restart_after
        self.on_frame = on_frame
        self.stats = Stats()
        self.camera = 1            # tracked, not reported by the adapter
        self.switched_at = 0.0     # time.time() of the last switch_camera()
        self._sock: Optional[socket.socket] = None
        self._thread: Optional[threading.Thread] = None
        self._running = threading.Event()
        self._cond = threading.Condition()
        self._latest: Optional[bytes] = None
        self._latest_info: Optional[FrameInfo] = None
        self._rec = None
        self._rec_index = None
        self._rec_lock = threading.Lock()

    # ---- lifecycle -------------------------------------------------------
    def start(self) -> "E58Camera":
        if self._running.is_set():
            return self
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 * 1024 * 1024)
        s.bind(self._bind)
        s.settimeout(0.2)
        self._sock = s
        self._running.set()
        self._send(CMD_START)
        self._thread = threading.Thread(target=self._rx_loop, name="e58cam-rx",
                                        daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._running.clear()
        if self._thread:
            self._thread.join(timeout=2)
        self.stop_recording()
        if self._sock:
            self._sock.close()
        self._sock = self._thread = None

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.stop()

    # ---- commands --------------------------------------------------------
    def switch_camera(self) -> None:
        """Toggle between the two cameras. The adapter does not report which
        one is active; `camera` tracks it, assuming camera 1 at start()
        (camera 1 is always active after power-up). If another client
        switched the adapter earlier, `camera` will be inverted.
        About 7-9 frames from the old camera still arrive in the ~0.45 s
        after a switch; use `switched_at` to skip them if needed."""
        self._send(CMD_SWITCH)
        self.camera = 2 if self.camera == 1 else 1
        self.switched_at = time.time()

    def send_raw(self, payload: bytes) -> None:
        """Send an arbitrary command to the adapter's port (experiments)."""
        self._send(payload)

    # ---- frames ----------------------------------------------------------
    def latest_jpeg(self) -> Optional[bytes]:
        return self._latest

    def latest(self) -> tuple[Optional[bytes], Optional[FrameInfo]]:
        with self._cond:
            return self._latest, self._latest_info

    def wait_jpeg(self, timeout: float = 3.0, newer_than: int = -1) -> Optional[bytes]:
        """Block until a frame with seq > newer_than arrives (default: any)."""
        end = time.time() + timeout
        with self._cond:
            while (self._latest_info is None or self._latest_info.seq <= newer_than):
                left = end - time.time()
                if left <= 0:
                    return None
                self._cond.wait(left)
            return self._latest

    def frames(self, timeout: float = 3.0) -> Iterator[tuple[bytes, FrameInfo]]:
        """Yield every new good frame (may skip frames if the consumer is slow)."""
        last = -1
        while self._running.is_set():
            jpg = self.wait_jpeg(timeout, newer_than=last)
            if jpg is None:
                continue
            jpg, info = self.latest()
            last = info.seq
            yield jpg, info

    def snapshot(self, path: str, timeout: float = 3.0) -> bool:
        """Save the next good frame, untouched, as a .jpg file."""
        cur = self._latest_info.seq if self._latest_info else -1
        jpg = self.wait_jpeg(timeout, newer_than=cur)
        if jpg is None:
            return False
        with open(path, "wb") as f:
            f.write(jpg)
        return True

    # ---- recording -------------------------------------------------------
    def start_recording(self, path: str) -> None:
        """Record raw JPEGs back to back into an .mjpg file (lossless, no
        re-encoding) plus <path>.csv with one 'seq,frame_id,time' row per
        frame. Convert with:
          ffmpeg -framerate 20 -f mjpeg -i clip.mjpg -c:v copy clip.avi"""
        with self._rec_lock:
            self._close_rec()
            self._rec = open(path, "wb")
            self._rec_index = open(path + ".csv", "w")
            self._rec_index.write("seq,frame_id,time\n")

    def stop_recording(self) -> None:
        with self._rec_lock:
            self._close_rec()

    @property
    def recording(self) -> bool:
        return self._rec is not None

    # ---- internals -------------------------------------------------------
    def _close_rec(self):
        for f in (self._rec, self._rec_index):
            if f:
                f.close()
        self._rec = self._rec_index = None

    def _send(self, payload: bytes) -> None:
        if self._sock:
            self._sock.sendto(payload, self.addr)

    def _rx_loop(self) -> None:
        cur_id, buf = None, bytearray()
        prev_id = None
        last_rx = time.time()
        win_n, win_t = 0, time.time()
        seq = 0
        while self._running.is_set():
            try:
                pkt = self._sock.recv(2048)
            except socket.timeout:
                pkt = None
            except OSError:
                break
            now = time.time()
            if now - win_t >= 1.0:
                self.stats.fps = win_n / (now - win_t)
                win_n, win_t = 0, now
            if pkt is None:
                if now - last_rx > self.restart_after:
                    self._send(CMD_START)
                    last_rx = now
                continue
            last_rx = now
            if len(pkt) < 8 or pkt[4:7] != MAGIC:
                continue
            fid, last = pkt[0], pkt[1]
            if fid != cur_id:
                if cur_id is not None and buf:
                    self.stats.damaged += 1
                cur_id, buf = fid, bytearray()
            buf += pkt[8:]
            if last != 1:
                continue
            if prev_id is not None:
                self.stats.lost += max(0, ((fid - prev_id) % 256) - 1)
            prev_id = fid
            jpg = extract_jpeg(bytes(buf))
            cur_id, buf = None, bytearray()
            if jpg is None:
                self.stats.damaged += 1
                continue
            self.stats.good += 1
            win_n += 1
            info = FrameInfo(seq=seq, frame_id=fid, t=now, size=len(jpg))
            seq += 1
            with self._cond:
                self._latest, self._latest_info = jpg, info
                self._cond.notify_all()
            with self._rec_lock:
                if self._rec:
                    self._rec.write(jpg)
                    self._rec_index.write(f"{info.seq},{fid},{now:.4f}\n")
            if self.on_frame:
                try:
                    self.on_frame(jpg, info)
                except Exception as e:  # never kill the receive thread
                    print(f"e58cam: on_frame raised {e!r}")


if __name__ == "__main__":
    import sys
    secs = float(sys.argv[1]) if len(sys.argv) > 1 else 5
    with E58Camera() as cam:
        ok = cam.snapshot("e58_test.jpg")
        print("snapshot:", "e58_test.jpg" if ok else "FAILED (no video)")
        time.sleep(secs)
        print(cam.stats)
```

## 9. Testing without the hardware

If the project has a recorded capture (`.pcap` of the adapter's UDP 8080 traffic), write a small fake adapter: bind UDP on `127.0.0.1:<port>`, wait for `Bv`, then replay the captured 8080 payloads to the sender with their original timing. Point `E58Camera(ip="127.0.0.1", port=<port>)` at it. This was how the reference client was tested before the hardware was available.

## 10. Open questions (do not assume; test if the feature needs it)

- Whether `By` is strictly a toggle on every firmware.
- Meaning of header byte 2.
- Whether any undocumented command changes resolution or quality. The firmware appears fixed at 640 x 480.
