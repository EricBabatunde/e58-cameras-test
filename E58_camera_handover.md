# E58 camera adapter: handover for a separate project

**Purpose.** This file moves the "reuse the E58 cameras" idea out of the Franken-E58 drone project into a project of its own. It records what is known about the hardware, where it came from, what was measured, which routes exist for using the cameras, and what the first session of the new project should establish. Everything here was gathered on 12 November 2026 during the drone project's mechanical capture; nothing has been powered or probed specifically for the camera yet.

**Tag convention (kept from the drone project).** Every fact carries one of `[measured]` (with tool and date), `[datasheet p.N]`, `[assumed]` or `[recalled, verify]`. Numbers without a tag do not exist. Do not design from a recalled figure until it has been checked against a datasheet or a measurement.

---

## 0. Status, 13 November 2026: route 1 done, camera usable from a laptop

**Decision:** route 1 (adapter whole, stock firmware, host-side software). **Routes 2 and 3 are dropped**: no reflashing, no removing the cameras.

Results:
- **Connectors:** both cameras plug into flip-lock (ZIF) connectors marked "20P" on the adapter, one on each side `[measured: photos, 2026-11-12]`. FPC markings, partly read: "V-29-1…" (nose camera) and "W-…" (other camera).
- **Standalone power:** adapter powered from an 18650 cell (3.9 V) through red/black only, yellow unconnected. **0.14 A** idle, **0.22 A** streaming `[measured: DT9205A in series, 2026-11-13]`. The LED lights, the Wi-Fi comes up, and the phone app shows both cameras and can switch between them.
- **Network:** SSID `WIFI_4K_WB3BC10`, adapter `192.168.4.153`, DHCP gives the host `192.168.4.x/24`, 2.4 GHz channel 1, 802.11g 54 Mbit/s, **no open TCP ports** `[measured: probe_adapter.py, logs/probe_20261113_002103.txt]`.
- **Protocol** `[measured: PCAPdroid capture + live tests, 2026-11-13]`: UDP only. `Bv` to port 8080 starts the stream to the sender's port; `By` switches camera (it toggles); video comes back as 8-byte `…TZH\0` headers plus JPEG chunks, with a length trailer per frame. Full spec in `E58_CAMERA_INTEGRATION.md`.
- **Real resolution is 640 × 480 JPEG.** The app's "4K" photos are upscaled.
- **Frame rate:** about **20 fps clean on the laptop** (19.9 fps, 2 bad frames out of 400 in 20 s), against about 7.6 clean fps in the phone capture. The choppiness was on the phone side, not the Wi-Fi link.
- **Software in this project:** `e58_viewer.py` (live window, switch, snapshot, record), `e58cam.py` (reusable client library), `E58_CAMERA_INTEGRATION.md` (drop-in guide for other projects), `probe_adapter.py` (network probe).
- **Yellow wire:** not needed for the camera. See the correction in section 1.

- **Live tests of `e58cam.py`, 2026-11-13** `[measured]`: self-test 20.0 fps, 161 good / 1 damaged / 0 lost. **Only one receiver at a time**: the last client to send `Bv` takes the stream and the previous one gets nothing. **Camera switch latency about 0.45 s** (7–9 frames from the old camera still arrive after `By`).

- **Zoom:** the narrow, "zoomed" view is present in the raw 640 × 480 stream too, so it is the lens or module (possibly a physical defect), not an app crop `[measured: user comparison, 2026-11-13]`.
- **Camera 1 is always active after power-up** `[measured: user, 2026-11-13]`.
- **Video server `e58_server.py`** `[measured: live test, 2026-11-13]`: control page, `/stream.mjpg`, `/snapshot.jpg`, switch, lossless recording with automatic `.avi` wrap. Two stream viewers plus OpenCV at once, ~19–20 fps each.
- **Link quality varies:** damaged frames ranged from 0.5% to 20% within minutes at −32 dBm, with zero UDP drops in the laptop kernel. That is 2.4 GHz interference over the air, not software.

Still open: FPC part numbers (no longer needed now that route 2 is dropped).

---

## 1. Where the cameras come from

- **Aircraft:** a folding "E58" class toy quadcopter (Mavic-style frame, 716 brushed coreless motors, 1S Li-ion pack). The drone project (Franken-E58) replaces the OEM flight-controller board with an ESP32-S3 board and has **decided not to use the camera at all** (decision D-P2-05, "strictly mechanical", 12 November 2026). The camera hardware is therefore free for other use, and the drone board will never need it back.
- **Camera adapter PCB:** a small board that sits on the floor of the camera compartment in the nose of the frame, under the main board. It carries a **Beken BK7231UQN40** Wi-Fi/Bluetooth SoC `[measured: visual reading of the part marking, 2026-11-12]` and two camera connectors. No other large IC was seen on it `[measured: visual, 2026-11-12]`.
- **Cameras:** two, the same type, each on a **20-pin flexible printed circuit (FPC)** that goes to the adapter PCB `[measured: visual, 2026-11-12]`. The camera modules are **glued** into the frame and adapter assembly `[measured: visual, 2026-11-12]`. Neither FPC reaches the main board. One camera looks forward (nose), the other presumably down; which is which was not recorded.
- **Link to the OEM main board:** one **3-wire lead** only, red, black, yellow:
  - red = V+ on the main board, continuous to the VCC net on the adapter `[measured: DT9205A continuity, power off, 2026-11-12]`;
  - black = GND `[measured: continuity]`;
  - yellow = goes to the **RX** pin of the adapter (serial **into** the adapter). The adapter's TX is **not connected** to anything `[measured: continuity, 2026-11-12]`.
  - So the main board can only send commands to the adapter; nothing comes back and **no video passes over this lead**.
  - **Correction, 2026-11-13 (`[assumed]`, contradicts the line above):** the phone app sends a flight-control-looking packet (`aa 80 80 00 80 00 80 55`, sticks centred) to the adapter's UDP port 8090 every 0.79 s `[measured: PCAPdroid capture, 2026-11-13]`. That strongly suggests the OEM design flies the drone *through* the adapter: phone → adapter Wi-Fi → yellow wire → main board. The yellow wire would then be the adapter's **TX** to the main board's RX, and the earlier RX/TX labelling is reversed. Not confirmed; a USB-serial adapter on the yellow wire while sending 8090 packets would settle it. The camera works without the yellow wire `[measured: standalone test, 2026-11-13]`.
- **Not part of the camera:** the OEM main board also has a 2-pin lead to two white nose LEDs (V+ and a low-side L− pin). That is driven by the main board, not the adapter.
- **V+ voltage: 3.82 V** on the aircraft, red to black `[measured: DT9205A, 2026-11-13]`, i.e. the raw 1S pack voltage. (A first reading of 1.5 V was the yellow wire to GND, not V+.) The pack is a 1S Li-ion, DWZ 802540, 3.88 V open circuit `[measured: DT9205A, 2026-11-12]`.
- **Photos** (in the drone repo, `pcb/mechanical/photos/`, copy them into the new project): `camera_adapter_top.jpg`, `camera_adapter_bottom.jpg`, `camera_adapter_in_place.jpg`, `cavity_top.jpg` (compartment with the adapter in place), `frame_overview.jpg`.

## 2. What the OEM system does with it

Inferred from the parts and the wiring, so `[recalled, verify]` unless marked:

- The BK7231U family has a parallel (DVP) camera input and a hardware JPEG encoder, and is used in cheap Wi-Fi cameras and toy-drone camera boards `[recalled, verify against the Beken BK7231U datasheet]`. That matches what was seen: two parallel cameras on 20-pin FPCs going straight to the chip, and no separate encoder.
- The adapter therefore brings up its **own Wi-Fi access point** and streams JPEG frames to the phone app. The drone's control link is separate (in the OEM design, the main board has its own radio; in the Franken-E58 design, the ESP32-S3's Wi-Fi). This is why the drone project dropped the camera: two Wi-Fi radios in one 100 g airframe interfere with the control link.
- The yellow RX wire carries commands from the main board, such as photo and record triggers and possibly switching between the two cameras `[assumed]`. The protocol is unknown.

## 3. Available routes

### Route 1: keep the adapter whole, use its Wi-Fi (recommended first)

Power the adapter at the V+ voltage, join its access point from a phone or PC, and receive the stream.

- **Pros:** no soldering, non-destructive, both cameras available, an afternoon's work to find out whether the cameras are worth anything.
- **Cons:** the video format, resolution and frame rate are fixed by the BK7231U firmware. The stream protocol has to be found. An ESP32-S3 can join the access point as a station and pull the stream, but decoding JPEG at a useful frame rate on an S3 is marginal, so the S3 is a relay at best, not a consumer.
- **The command channel:** to trigger photos or recording, or to switch cameras, the protocol on the yellow wire must be learned. Two ways: (a) connect a USB-to-serial adapter's RX to the OEM main board's yellow line while the OEM aircraft is powered and the app is used (props off), and log what the main board sends; (b) try the phone app's own commands, which may go over Wi-Fi directly to the adapter and need no serial at all.
- **Finding the stream protocol:** hobbyists have reverse-engineered the E58 family's camera protocol (search for E58, "wifi ufo", "JJRC H37" and similar, plus "python stream"). Treat any repository found as a starting point to verify with a packet capture, not as fact.

### Route 2: take the camera modules off the adapter and drive them from the ESP32-S3

The ESP32-S3 has a DVP camera interface (LCD_CAM peripheral) and the `esp32-camera` driver supports common parallel sensors (OV2640, OV7670, GC0308 class and others) `[recalled, verify against the esp32-camera repository's supported-sensor list]`.

- **Works only if** the sensors are parallel DVP parts on the driver's list. A 20-pin FPC is consistent with a DVP sensor but does not prove it; identification first (section 5).
- **One camera at a time:** the S3 has a single camera interface.
- **Destructive risk:** the modules are glued; cutting them free may damage the lens housing or the FPC. If the FPCs plug into ZIF connectors on the adapter, the modules can be unplugged but still have to be freed from the glue.
- **Pros:** full control of resolution, frame rate and format, frames available in the S3's memory for local processing, standard ESP-IDF tooling.
- **Cons:** identification work, an adapter board or breakout for a 20-pin FPC, and the glue.

### Route 3: reflash the BK7231U with open firmware (not recommended)

OpenBeken and LibreTiny support the BK7231N and BK7231T well; the **U** variant with the camera path has little community support `[recalled, verify]`. Camera streaming under open firmware would be a research project in itself. Keep as a fallback only.

### Decision guide

| Want | Route |
|---|---|
| A quick look at what the cameras can do, a phone or PC viewer, both cameras | 1 |
| Frames in an ESP32-S3 for processing, custom resolution or rate | 2 (after identification) |
| Full control of the Wi-Fi camera itself | 3, with low odds |

## 4. Equipment and environment on hand

- ESP32-S3 dev board from Phase 1 of the drone project (ESP32-S3-N16R8 class; the exact board is the one used for the Phase 1 firmware). ESP-IDF 5.5.3 via PlatformIO `espressif32 @ 6.13.0` is installed and working. The drone project's rules (split IDF driver components, no Arduino) are a choice, not a requirement for the camera project.
- Multimeter DT9205A (20 A range, continuity). Steel ruler. **No calipers, no oscilloscope, no logic analyser, no scale** recorded. A USB-to-serial adapter is needed for the command-channel capture; whether one is on hand was not recorded.
- The user hand-solders; no fab assembly.
- Shell gotcha on this machine: the default `python3` is PlatformIO's and a ROS `PYTHONPATH` is exported. Use `/usr/bin/python3 -I` for scripts, or a project venv.

## 5. First session of the new project: what to establish, in order

All steps are without power except step 3 and step 4.

1. **Connector type on the adapter.** Do the FPCs plug into ZIF connectors or are they soldered? ZIF makes route 2 non-destructive at the adapter end.
2. **Sensor identity.** Read any marking on the camera modules, the FPCs and the lens housings. Record the FPC pitch (0.5 mm or 0.3 mm class, by ruler against several pins) and confirm the pin count. A part number decides route 2 outright.
3. **V+ voltage.** On the OEM aircraft, props off, powered on, read the voltage between the red and black wires of the 3-pin lead. This is the supply the adapter expects. If it is the raw pack voltage, a bench supply at 3.8 to 4.0 V with a 1 A current limit is appropriate for the standalone test `[assumed, confirm from the reading]`.
4. **Standalone test.** Power the adapter from the bench supply (or from the drone pack through the 3-pin lead, with the main board disconnected), watch for a Wi-Fi network, join it, try the phone app. Note the SSID, the current drawn, and whether both cameras can be viewed.
5. **Command channel capture** (optional, route 1): log the yellow line as in section 3.
6. **Decide the route** from the decision guide and write the project's own plan.

## 6. Open questions

- Which camera faces where, and whether the app switches between them or shows one only.
- Whether the BK7231U really encodes on-chip (datasheet check) or the sensors already output JPEG.
- The V+ voltage and the adapter's current draw.
- Whether the sensors are DVP parts known to `esp32-camera`.
- The command protocol on the yellow wire, if any is needed.

## 7. Safety notes

- The adapter is designed for a 1S Li-ion supply. Never apply 5 V to V+ until a measurement shows it is tolerated.
- If the drone pack is used, it has no protection circuit recorded and no fuse. Prefer a current-limited bench supply.
- Camera modules and FPCs are static-sensitive and the FPCs tear easily. Handle at the connector, not the ribbon.
- Nothing in this project touches the drone's flight board. Any change to how the camera could be used in flight goes back to the drone project as a blueprint diff, not into this one.
