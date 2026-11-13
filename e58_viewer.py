#!/usr/bin/env python3
"""Laptop viewer for the E58 camera adapter (BK7231U, SSID WIFI_4K_*).

Protocol (from PCAPdroid capture, 2026-11-13):
  * Client sends b"Bv" (42 76) to ADAPTER:8080 -> adapter streams video back
    to the client's source port.
  * Client sends b"By" (42 79) to ADAPTER:8080 -> switch camera.
  * Each video packet: 8-byte header + JPEG chunk.
      hdr[0] frame id (wraps at 256)
      hdr[1] 1 on the last chunk of a frame, else 0
      hdr[2] unknown (varies 2..25), hdr[3] always 2
      hdr[4:8] b"TZH\\0"
    The reassembled frame is a 640x480 JPEG (FFD8 ... FFD9) followed by a
    short trailer whose last 4 bytes are the JPEG length, little-endian.
  * The phone app also sends aa 80 80 00 80 00 80 55 to port 8090 every
    0.79 s. That looks like the flight-control packet (sticks centred), not a
    camera command. It is NOT sent by default (see --heartbeat).

Run while the laptop is connected to the adapter's Wi-Fi, with the phone app
closed:
    /usr/bin/python3 -I e58_viewer.py
Keys in the window:
    c  switch camera        s  save current frame (raw JPEG)
    r  start/stop recording raw JPEGs     q / Esc  quit
"""
import argparse
import datetime
import os
import socket
import struct
import time

import cv2
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
HEARTBEAT = bytes.fromhex("aa80800080008055")


def parse_args():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--ip", default="192.168.4.153", help="adapter IP")
    p.add_argument("--port", type=int, default=8080, help="video/command port")
    p.add_argument("--start", default="4276", help="start command, hex")
    p.add_argument("--switch", default="4279", help="camera switch command, hex")
    p.add_argument("--heartbeat", action="store_true",
                   help="also send the app's 8090 packet every 0.79 s "
                        "(only if the stream stops without it)")
    p.add_argument("--scale", type=float, default=1.5, help="window zoom factor")
    p.add_argument("--record", action="store_true", help="start recording at once")
    p.add_argument("--headless", action="store_true",
                   help="no window; record frames and print stats only")
    p.add_argument("--seconds", type=float, default=0,
                   help="stop after this many seconds (0 = run until q)")
    return p.parse_args()


def jpeg_from(buf):
    """Return the JPEG inside a reassembled frame, or None if damaged."""
    if not buf.startswith(b"\xff\xd8"):
        return None
    end = buf.rfind(b"\xff\xd9")
    if end < 0:
        return None
    jpg = buf[:end + 2]
    tail = buf[end + 2:]
    if len(tail) >= 4:
        (declared,) = struct.unpack("<I", tail[-4:])
        if declared != len(jpg):
            return None
    return jpg


def main():
    a = parse_args()
    target = (a.ip, a.port)
    start_cmd, switch_cmd = bytes.fromhex(a.start), bytes.fromhex(a.switch)

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 * 1024 * 1024)
    sock.bind(("0.0.0.0", 0))
    sock.settimeout(0.05)
    print(f"Listening on UDP {sock.getsockname()[1]}, sending start to {target}")
    sock.sendto(start_cmd, target)

    session = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    rec_dir = os.path.join(HERE, "captures", session)
    recording = a.record or a.headless
    if recording:
        os.makedirs(rec_dir, exist_ok=True)
        print(f"Recording raw JPEGs to {rec_dir}")

    cur_id, buf = None, bytearray()
    last_jpg, last_img = None, None
    last_rx = last_hb = last_stat = t_begin = time.time()
    n_ok = n_bad = n_missing = n_saved = 0
    prev_id = None
    win_ok, win_t, fps = 0, time.time(), 0.0
    camera = 0

    if not a.headless:
        cv2.namedWindow("E58 camera", cv2.WINDOW_AUTOSIZE)

    while True:
        now = time.time()
        if a.seconds and now - t_begin > a.seconds:
            break
        if a.heartbeat and now - last_hb > 0.79:
            sock.sendto(HEARTBEAT, (a.ip, 8090))
            last_hb = now
        if now - last_rx > 2.0:
            print("No video for 2 s, resending start command")
            sock.sendto(start_cmd, target)
            last_rx = now

        # Drain the socket, but return to the UI/timers at least every 30 ms.
        drain_until = time.time() + 0.03
        while time.time() < drain_until:
            try:
                pkt, _ = sock.recvfrom(2048)
            except socket.timeout:
                break
            except BlockingIOError:
                break
            last_rx = time.time()
            if len(pkt) < 8 or pkt[4:7] != b"TZH":
                continue
            fid, last = pkt[0], pkt[1]
            if fid != cur_id:
                if cur_id is not None and buf:
                    n_bad += 1          # previous frame never completed
                cur_id, buf = fid, bytearray()
            buf += pkt[8:]
            if last == 1:
                jpg = jpeg_from(bytes(buf))
                if prev_id is not None:
                    n_missing += max(0, ((fid - prev_id) % 256) - 1)
                prev_id = fid
                if jpg:
                    n_ok += 1
                    win_ok += 1
                    last_jpg = jpg
                    if not a.headless:
                        last_img = cv2.imdecode(np.frombuffer(jpg, np.uint8),
                                                cv2.IMREAD_COLOR)
                    if recording:
                        with open(os.path.join(rec_dir, f"{n_saved:06d}.jpg"), "wb") as f:
                            f.write(jpg)
                        n_saved += 1
                else:
                    n_bad += 1
                cur_id, buf = None, bytearray()

        if time.time() - win_t >= 1.0:
            fps = win_ok / (time.time() - win_t)
            win_ok, win_t = 0, time.time()
        if time.time() - last_stat >= 5.0:
            print(f"fps {fps:4.1f} | good {n_ok} damaged {n_bad} "
                  f"lost {n_missing} | saved {n_saved}")
            last_stat = time.time()

        if a.headless:
            continue
        if last_img is not None:
            img = last_img
            if a.scale != 1.0:
                img = cv2.resize(img, None, fx=a.scale, fy=a.scale,
                                 interpolation=cv2.INTER_NEAREST)
            else:
                img = img.copy()
            txt = (f"{fps:4.1f} fps  cam {camera}  ok {n_ok} bad {n_bad} "
                   f"lost {n_missing}" + ("  REC" if recording else ""))
            cv2.putText(img, txt, (8, 22), cv2.FONT_HERSHEY_SIMPLEX, 0.6,
                        (0, 0, 0), 3, cv2.LINE_AA)
            cv2.putText(img, txt, (8, 22), cv2.FONT_HERSHEY_SIMPLEX, 0.6,
                        (0, 255, 0), 1, cv2.LINE_AA)
            cv2.imshow("E58 camera", img)
        key = cv2.waitKey(1) & 0xFF
        if key in (ord("q"), 27):
            break
        if key == ord("c"):
            sock.sendto(switch_cmd, target)
            camera ^= 1
            print("Switch camera")
        if key == ord("s") and last_jpg:
            os.makedirs(os.path.join(HERE, "captures"), exist_ok=True)
            p = os.path.join(HERE, "captures",
                             datetime.datetime.now().strftime("snap_%H%M%S_%f.jpg"))
            with open(p, "wb") as f:
                f.write(last_jpg)
            print(f"Saved {p}")
        if key == ord("r"):
            recording = not recording
            if recording:
                os.makedirs(rec_dir, exist_ok=True)
            print(("Recording to " + rec_dir) if recording else "Recording stopped")

    total = time.time() - t_begin
    print(f"\nSummary: {total:.1f} s, good {n_ok} ({n_ok / total:.1f} fps avg), "
          f"damaged {n_bad}, lost {n_missing}, saved {n_saved}")
    if not a.headless:
        cv2.destroyAllWindows()


if __name__ == "__main__":
    main()
