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
