#!/usr/bin/env python3
"""Offline probe of the E58 camera adapter's Wi-Fi access point.

Run while the laptop is connected to the adapter's Wi-Fi:
    /usr/bin/python3 -I probe_adapter.py
Writes everything to logs/probe_<timestamp>.txt next to this script.
Read-only: it connects, reads banners and sends harmless HTTP/RTSP
queries. Nothing is configured or written on the adapter.
"""
import concurrent.futures as cf
import datetime
import os
import socket
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
LOGDIR = os.path.join(HERE, "logs")
os.makedirs(LOGDIR, exist_ok=True)
STAMP = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
LOGFILE = os.path.join(LOGDIR, f"probe_{STAMP}.txt")
_log = open(LOGFILE, "w")


def log(*a):
    s = " ".join(str(x) for x in a)
    print(s)
    _log.write(s + "\n")
    _log.flush()


def sh(cmd, timeout=20):
    log(f"\n$ {cmd}")
    try:
        out = subprocess.run(cmd, shell=True, capture_output=True, text=True,
                             timeout=timeout)
        log((out.stdout + out.stderr).rstrip())
    except Exception as e:
        log(f"[error] {e}")


def gateway():
    out = subprocess.run("ip -4 route show default", shell=True,
                         capture_output=True, text=True).stdout.split()
    if "via" in out:
        return out[out.index("via") + 1]
    return None


def tcp_open(host, port, timeout=0.4):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        return port if s.connect_ex((host, port)) == 0 else None
    except OSError:
        return None
    finally:
        s.close()


def talk(host, port, payload=None, timeout=1.5):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    data = b""
    try:
        s.connect((host, port))
        if payload:
            s.sendall(payload)
        while len(data) < 2048:
            chunk = s.recv(2048)
            if not chunk:
                break
            data += chunk
    except Exception as e:
        if not data:
            return f"[{type(e).__name__}: {e}]"
    finally:
        s.close()
    return repr(data[:2048])


def main():
    log(f"E58 adapter probe {STAMP}")
    sh("ip -4 addr")
    sh("ip -4 route")
    sh("iw dev | grep -E 'Interface|ssid|channel'")
    sh("for d in $(ls /sys/class/net); do iw dev $d link 2>/dev/null; done")

    gw = gateway() if len(sys.argv) < 2 else sys.argv[1]
    if not gw:
        log("\n[!] No default gateway found. Are you connected to the adapter's Wi-Fi?")
        log("    You can pass the adapter IP by hand: probe_adapter.py 192.168.x.1")
        return
    log(f"\nTarget (gateway) = {gw}")
    sh(f"ping -c 4 -W 1 {gw}")
    sh("ip neigh")

    log("\n== TCP scan of ports 1-65535 (takes ~1-3 min) ==")
    open_ports = []
    with cf.ThreadPoolExecutor(max_workers=300) as ex:
        for p in ex.map(lambda p: tcp_open(gw, p), range(1, 65536)):
            if p:
                open_ports.append(p)
                log(f"  open: {p}")
    log(f"Open TCP ports: {open_ports}")

    for p in open_ports:
        log(f"\n-- port {p} --")
        log("  banner (no send): " + talk(gw, p))
        log("  HTTP GET /:       " + talk(gw, p,
            f"GET / HTTP/1.0\r\nHost: {gw}\r\n\r\n".encode()))
        log("  RTSP OPTIONS:     " + talk(gw, p,
            f"OPTIONS rtsp://{gw}:{p}/ RTSP/1.0\r\nCSeq: 1\r\n\r\n".encode()))

    log("\n== Passive listen: 15 s of traffic from the adapter (needs sudo) ==")
    sh(f"sudo -n timeout 15 tcpdump -n -c 200 host {gw} 2>&1 || "
       "echo 'tcpdump skipped (no passwordless sudo) - fine'", timeout=30)

    log(f"\nDone. Log saved to {LOGFILE}")


if __name__ == "__main__":
    main()
