# usblcd — headless CLI for the Thermalright / ChiZhu USB LCD panel

A small C command-line tool for the USB LCD built into Thermalright liquid
coolers (`87ad:70db`, "ChiZhu Tech USBDISPLAY"), for machines with no
desktop: image, colour, test pattern, and a live frame feed, with no GUI, no
daemon, no Python.

It speaks the **raw USB bulk wire** directly (libusb), the same one
`USBLCDNew.exe` uses on Windows. The geometry tables, the frame header, the
JPEG-vs-RGB565 choice and the panel's built-in rotation are ported from the
reverse-engineered reference project
[Lexonight1/thermalright-trcc-linux](https://github.com/Lexonight1/thermalright-trcc-linux)
(`src/trcc/core/protocol.py`, `src/trcc/adapters/device/bulk_lcd.py`), which
documents the protocol in `doc/PROTOCOL_USBLCDNEW.md`.

```
Bus 001 Device 007: ID 87ad:70db ChiZhu Tech USBDISPLAY
  interface 0, bInterfaceClass=255 (vendor specific)
  EP 0x01 OUT bulk, EP 0x81 IN bulk, max packet 512
```

## Quick start

```sh
make                                # builds ./usblcd
sudo ./usblcd list                  # find the panel
sudo ./usblcd info                  # handshake: what panel is it?
sudo ./usblcd pattern               # colour bars + grey ramp + grid
sudo ./usblcd image tests/sample.ppm
sudo ./usblcd color 00ff88
sudo ./usblcd off
sudo ./usblcd watch dashboard.png   # keep a picture up (Ctrl-C releases)
```

`usblcd` needs read/write access to the USB device node. The quick way on a
headless box is the bundled udev rule, which puts the panel in group `usblcd`
at `0660`:

```sh
sudo systemd-sysusers /usr/lib/sysusers.d/usblcd.conf   # creates user+group usblcd
sudo usermod -aG usblcd you            # whoever drives the panel by hand
sudo make install-udev
sudo udevadm control --reload && sudo udevadm trigger
./usblcd info                        # no sudo from now on
```

Before the rule is installed, run the tool as root (`sudo ./usblcd ...`).  The
rule also keeps `TAG+="uaccess"`, so the logged-in local seat works even before
you add anyone to `usblcd`.

`contrib/sysusers.d/usblcd.conf` creates the service account (`nologin`, no
HOME — the dashboard writes nothing); `sudo make install-sysusers` installs and
applies it.

## Commands

| command | what it does |
|---|---|
| `list` | every known USB LCD/cooler id on this machine, with the wire each one speaks |
| `info` | handshake, then PM/SUB → FBL, resolution, encoder, rotation, mount |
| `image FILE` | decode a picture (JPEG/PNG/BMP/TGA/PPM/PGM), fit it, send one frame |
| `color RRGGBB` | solid colour (default white) |
| `pattern` | colour bars + grey ramp + grid + crosshair |
| `marker` | orientation marker: white bar = top edge, yellow = left, magenta square = bottom-right corner |
| `calibrate` | hold each of 0/90/180/270 in turn so you can name the upright one, then store it |
| `off` | black frame |
| `watch FILE` | re-send the same frame every 150 ms — the panel reverts to its logo ~2–3 s after the last frame, so single shots fade |
| `feed` | raw RGBA8888 frames on stdin, encoded live (see the capture pipelines below) |
| `raw FILE [W H]` | send an already-encoded JPEG payload verbatim, with the header |
| `cmd N [HEX]` | raw 64-byte header packet (`cmd 1` = device info, `6` rotate, `7` screen settings, `8` backlight) for experimentation |

Global flags:

```
--vid NNNN --pid NNNN     target another USB id (default 87ad:70db)
--serial STR              pick one of several panels
--rotate 0|90|180|270     user orientation (the panel's own rotation is applied on top)
--brightness 0..100       software dimming (this wire has no hardware brightness command)
--quality 1..100          JPEG quality, default 95
--fit fit|fill|stretch    how a picture fills the panel (default: fill = cover + centre crop)
--max-size N              JPEG byte ceiling, default 450000 (firmware drops bigger frames)
--no-rotate               skip the panel's built-in wire rotation (for diagnosis)
--orient cw|ccw           which way --rotate turns the picture (cw = clockwise)
--no-calib                ignore the stored calibration
--duration SECONDS        calibrate: seconds per candidate (default 20)
--fps N                   `feed` frame cap
--out FILE                write the encoded wire payload to a file instead of the panel
-v                        protocol chatter (handshake, header hexdump, per-frame geometry)
```

## What the panel says about itself

`info` reads the handshake reply and resolves it through the reference
project's PM ladder. On the panel attached to this machine:

```
$ sudo ./usblcd info
panel     : 87ad:70db  USBDISPLAY  serial <serial>
handshake : PM=10 SUB=7 → FBL 224
geometry  : 960x540 JPEG (cmd 2)
rotation  : base 180° invert=yes mount 0°  (portrait-mounted)
wire angle: 180° at orientation 0
calibrate : 180° measured offset applied (usblcd calibrate)
```

`base 180° invert=yes` is the *catalogued* behaviour of the vendor app. The
last line is the offset measured on this unit's glass (see
[Rotation](#rotation-catalogued-vs-measured)) — plain `usblcd image x.png`
sends it at 0°, which is upright on this panel.

The frame header always declares the panel's **landscape framebuffer**
(960x540 here); the picture is rotated into that shape before encoding. The
wire angle is `encode_base ± orientation` (sign from `invert`), which is the
whole of the vendor app's rotation switch — so a picture sent at `--rotate 0`
comes out upright on the glass, exactly like the Windows app.

## The protocol, in one paragraph

A frame is a 64-byte header plus payload, written to EP 0x01 OUT in 16 KiB
bulk chunks, followed by a zero-length packet when the total length is a
multiple of 512:

| offset | bytes | meaning |
|---|---|---|
| 0–3 | `12 34 56 78` | magic |
| 4–7 | u32 LE | command: `2` = JPEG picture, `3` = RGB565 picture |
| 8–11 | u32 LE | framebuffer width |
| 12–15 | u32 LE | framebuffer height |
| 56–59 | u32 LE | `2` (constant on this wire) |
| 60–63 | u32 LE | payload length |

Handshake = the same header with byte 56 = `1` (device info query); the panel
answers 64 bytes, `PM` at byte 24 and `SUB` at byte 36 (plus `"SSCRM-V1"` at
byte 4 and a device tag at bytes 48–51). `PM` selects the resolution and the
encoder (`32` and `50` take RGB565, everything else JPEG), `SUB` selects the
encode rotation and whether the screen is mounted portrait.

Payload encodings:

* **JPEG** (most panels) — encoded 4:4:4 (no chroma subsampling), quality
  ladder 95 → 30, shrinking until it fits under the firmware's 450000-byte
  ceiling; a 4:2:0 attempt is made last so the panel is never sent a frame it
  silently drops.
* **RGB565** (PM 32, PM 50) — 5/6/5 packing, little-endian, big-endian for
  PM 50, each row centred in the panel's framebuffer row (the 640x172 panels).

## Rotation: catalogued vs measured

The vendor app rotates every frame **into the panel's landscape framebuffer**
before encoding, and its per-resolution table (which we port) is the source of
`base`/`invert`. That table is reverse-engineered, and the reference project's
own issue list (#203, #169, #171) is exactly it disagreeing with real panels by
180° and on the sign. So `usblcd` treats the catalogued angle as a starting
point and lets you measure the truth on your own glass:

```sh
./usblcd marker                     # what the panel should look like, upright
./usblcd calibrate                  # cycles 0/90/180/270, 20 s each
./usblcd calibrate --set 180        # "180 was the upright one"
./usblcd --rotate 90 marker         # which way does a turn land?
./usblcd calibrate --set 180 --orient cw
```

The result is stored per panel — USB id, serial, PM, SUB — and applied to every
later command automatically.  Lookup order:

1. `$USBLCD_CALIB` (an explicit file path — what the systemd unit uses)
2. `$XDG_CONFIG_HOME/usblcd/calibration`, else `$HOME/.config/usblcd/calibration`
3. `/etc/usblcd/calibration` — the system-wide file, used when there is no HOME
   at all, which is the case for a unit running as `nobody`

```sh
sudo mkdir -p /etc/usblcd && sudo chmod 755 /etc/usblcd
sudo cp ~/.config/usblcd/calibration /etc/usblcd/calibration && sudo chmod 644 /etc/usblcd/calibration
```

```
87ad:70db <serial> pm=10 sub=7 base=180 orient=cw
```

With that in place `--rotate N` means exactly what it says: **turn my picture N
degrees clockwise, as seen on the screen**. `--base` / `--orient` override the
file per call; `--no-calib` ignores it.

Measured on the panel attached here (`PM=10 SUB=7`, 960x540): the catalogued
arm for SUB 5/7 is 180° off on this unit, the sign counts *up*, and the screen
is physically mounted **landscape** — so `SUB=7` does not mean "portrait" for
this SKU. All three were measured on glass, not assumed; the calibration file
is the record.

## Dashboard (`dash/`)

`dash/dash.py` renders a server dashboard sized for the panel and streams it
through `usblcd feed` — **Python standard library only** (no PIL/numpy/
matplotlib; this box has none) and a **5×7 bitmap font** in `dash/font57.py`,
so it runs on plain Python 3.

**Text rendering.** When FreeType is available (`make` builds `build/ttfplan` if
`pkg-config --exists freetype2` succeeds) the dashboard draws with a real TTF —
**Inter** (`fonts/Inter-var.ttf`, SIL OFL, variable `wght`/`opsz` axes, picked
first), else Adwaita Mono / DejaVu Sans Mono / Liberation Mono; `--font FILE` to
choose, `--weight 500` to set the weight axis (400 regular … 600 semibold).
Inter is proportional, so digits are drawn on the widest digit's cell: every
counter keeps its width as its value changes (tabular figures)
rasterised by FreeType at the exact sizes the layout uses, its 8-bit coverage
quantized to six levels and painted as merged rectangles (so drawing stays a few
dozen rects per line, not per-pixel blending).  Glyph sizes are calibrated on
the font's own digit height, so caps come out as tall as the bitmap font's whole
box, and `--text-rise 112` / `--big-rise 118` (defaults) scale all labels and all
big numbers from there — `--audit` checks the result against every panel, and
`--text-rise` above ~124 starts colliding with the fixed panel geometry.  Where a
list simply has no room at a bigger size, it is cut to what fits and `--audit`
says so (`FIT SYSTEM 6 of 7 rows fit...`).  Without FreeType it falls back to the
built-in bitmap font, antialiased by blurring the glyph mask one pixel and
quantizing coverage — `--no-ttf` forces the fallback, `--no-aa` disables the
soft edges, `--aa-min 4` softens only the big numbers.

```sh
python3 dash/dash.py --once                       # one frame -> /tmp/usblcd-dashboard.ppm
python3 dash/dash.py --exec ./usblcd              # live: 1 frame/s onto the panel
python3 dash/dash.py --raw | ./usblcd --fps 1 feed    # same, with a pipeline you own
python3 dash/dash.py --font-sample --out font.ppm # draw every glyph to check the font
python3 build/ppm2png /tmp/usblcd-dashboard.ppm /tmp/dash.png   # view it anywhere
```

Two knobs exist because the **panel crops its own edges** (rounded corner, bezel):

```sh
--safe-area 8,30,8,8      # top, bottom, left, right: layout stays inside this
--debug-grid              # red panel outlines + ids + sizes + a pixel ruler
```

`--safe-area` keeps content out of the cropped band (the bottom number is the
one that matters on this panel). `--debug-grid` is for taking a photo of the
panel: every panel gets a red outline with its id and `WxH`, so a photo shows
exactly which panel a piece of text belongs to and where its edges are.

Metrics: CPU package/core temps, per-core utilisation bars, load, frequency,
CPU temp + utilisation history; GPU util/VRAM/temp/fan/power via `nvidia-smi`
(`--no-gpu` to skip); the local inference server's own `/metrics` (the AI page,
`--no-strata` to skip); RAM/swap; per-mount disk fill; net rx/tx per interface +
rolling history; NVMe/chassis temps; fan RPM; top processes by CPU and by RSS.
Every section is guarded on its own — a missing sensor drops its panel, it
never stops the frame.

The GPU page is the one fed by a program rather than a sysfs file, so it has
its own rules: `nvidia-smi` runs in a background thread (a slow query can never
stall a frame) and a failed query is retried for as long as the dashboard runs,
with the poll slowing down while it fails. A service that starts at boot is at
the panel before the NVIDIA driver answers `nvidia-smi`, so an empty GPU page
right after boot is the normal case — it fills itself in, no restart. The page
says which of the three cases it is in: `GPU PAGE DISABLED` (`--no-gpu`),
`NO NVIDIA-SMI` (this box has no NVIDIA, the only permanent stop), and
`PROBING nvidia-smi` / `QUERY FAILED` (driver not answering: nvidia-smi's own
reason, the attempt count, and how long ago the last good reply was). Numbers
kept on screen from an older reply are marked `STALE Ns` — an unlabelled stale
number on a dashboard is worse than no number.

The AI page is the strata monitor's Overview (its first view, `strata monitor`
view 1) drawn on the panel: it GETs the same `/metrics` the monitor reads, in a
background thread, with the poll rules the GPU page learned (a server that boots
after this service, a model load that takes minutes and a restart all heal, so
the poll backs off — 3, 6, 12, 24, 30 s, then 2 min — and the first good reply
lights the page back up by itself).  Nothing is installed, started or written:
the poller reads the launch configs a strata install already wrote (its port and
the API key `/metrics` answers 401 without), pairs them with the ready file of
the server that is actually running, and issues one GET per poll.  Point it
elsewhere with `--strata-url` / `--strata-key` / `--strata-state`, or with
`STRATA_URL` / `STRATA_API_KEY` / `STRATA_STATE` in the environment.  The
sparklines use the histories the server itself ships (a minute per series), so
the curve on the panel is the curve the monitor draws.  A number the server did
not report is drawn `--`, never as an invented zero.

Layout (960×540 default; `--width/--height` for other panels). The header
(host, title, kernel, uptime, clock) stays on **every** page, with tabs showing
which page is up and a countdown to the next; the body rotates through four
pages, 12 s each by default:

| page | contents |
|---|---|
`CPU` | big package temp + utilisation, load, clocks, 16 core bars with per-core temps, utilisation and temp histories with peaks, core maxes, NVMe temps · RAM/swap bars, per-mount disk fill, boot time · network rx/tx + history + peak + NIC temp
`GPU` | one panel per GPU (util, temp, VRAM bar, memory-bus %, fan, watts, util and temp histories) · fleet strip: mean util, total VRAM, total watts, board-chip temps, NIC temp, thermal zones, fans
`PROC` | top-14 processes by CPU with bars, smoothed with an EWMA so the list does not reshuffle every frame · top-10 by RSS with bars · system panel (load, threads, procs, ctxt/s, intr/s, RAM, swap, CPU temp, uptime)
`AI` | model strip (state, phase, prompt/decode progress, context fill, LIVE/STALE age, which server answered) · the Overview's eight tiles with sparklines: tok/s (live rate while generating, the run's average while idle), GPU load, VRAM, GPU temp, power, PCIe rx, host CPU, disk read — the PCIe tile is the one with no curve (its counter steps at every request boundary, so the tile says the link and the reverse direction instead: `GEN 3 x4 LINK`, `TX 944 MB/S`) · bars for context fill, experts in VRAM and system RAM · the last requests with hit rate, tokens, tok/s, duration and how each ended

Panel titles get a measured band: `TITLE_TOP` above the caption, the caption
itself (`text_h(scale 2)`), and `TITLE_PAD` below it, so the first row of every
panel starts the same distance under its title at any font size.  Panel heights,
row pitches and list-length caps are all computed from `band(c)`, never from a
hardcoded 28 px title band.

```sh
python3 dash/dash.py --page CPU                    # pin one page
python3 dash/dash.py --page AI                     # the inference-server page
python3 dash/dash.py --pages CPU,PROC --page-seconds 20
python3 dash/dash.py --page-seconds 0              # rotation off
python3 dash/dash.py --ring 20                     # bigger countdown dial
python3 dash/dash.py --ring 0                      # no countdown dial
python3 dash/dash.py --sensors                     # what was discovered on this box
python3 dash/dash.py --strata-url http://127.0.0.1:8095 --strata-key …
python3 tools/preview.py /tmp/usblcd-dashboard.ppm # ASCII view of a rendered frame
```

Page rotation is shown in the header as a dial in the top-right corner, not a
number: `--page-seconds` sectors around the ring, one sector goes dark per
second, so the ring empties clockwise from 12 o'clock as the page's dwell runs
out.  Its size defaults to the clock's own digit height (`Canvas.ink_h`) so the
dial and the clock read as one object; `--ring N` forces a radius in px.  The
header's second line shows `IP <address>` and the uptime — the address is the
one the default route uses, picked with a connected UDP socket so it works with
no internet — and the clock sits between it and the dial.

Thresholds colour temp green/amber/red at 70/85 °C, disk/RAM at 80 %.
Cost per frame at 960×540, measured: 7 ms metrics, 7 ms draw (11 ms on the AI
page, which draws eleven panels and eight sparklines), 0.4 ms hand-off to
`usblcd` — 15 ms total, **2.6 % of one core at 1 fps**.  The `nvidia-smi` and
`/metrics` queries run in their own threads, so a slow query is never part of a
frame.  The canvas is RGBA8888 in native order, so a frame is one buffer write;
the RGBA→RGB PPM for preview drops alpha with a single regex pass.
`--sensors` is the debugging entry point: it
prints the CPU package/core/thermal-zone/NVMe/NIC/board temps, fans, GPUs
(`nvidia-smi`), the strata endpoints it found (the URL, which launch config the
key came from, the model, the poll state) and mounts it found — every panel is
built from exactly that list, so a panel showing `NONE` means the sensor is not
exposed by the kernel (or no server is configured), not that the renderer failed.

`tools/ppm2png.c` converts the rendered PPM to PNG so you can inspect a frame
from your laptop; `usblcd` reads the PPM directly, so nothing on the panel path
depends on it.

`contrib/usblcd-dashboard.service` runs it 24/7 (one instance, `--interval 1`).

## Running the dashboard permanently

```sh
sudo make install install-udev          # /usr/local/bin/usblcd + non-root panel access
sudo make install-service               # project -> /opt/usblcd, unit -> systemd
sudo systemctl daemon-reload
sudo systemctl enable --now usblcd-dashboard.service
journalctl -u usblcd-dashboard -f       # layout/write reports land here
```

`make install-service` copies `dash/`, `vendor/`, `build/` (the `ttfplan`
helper), `fonts/` and the README to `/opt/usblcd`, and installs
`contrib/usblcd-dashboard.service` as `/etc/systemd/system/usblcd-dashboard.service`.
It does not enable anything itself — run the `systemctl` lines above.

Tune it without editing the unit: `/etc/usblcd/dashboard.conf` overrides
`TITLE`, `PAGES` (`CPU,GPU,PROC,AI`), `PAGE_SECONDS`, `INTERVAL`, `WEIGHT`,
`CPUS`, `NICE` and, for the AI page, `STRATA_URL` / `STRATA_API_KEY` /
`STRATA_STATE` (see the AI page note above — on a service box the first two are
the ones that work, and the file then holds a secret).

The unit starts at boot (`WantedBy=multi-user.target`), restarts on any exit
(`Restart=always`, `RestartSec=5`, no start limit, so an hour-long unplug
recovers by itself), and logs to the journal as `usblcd-dashboard`.  It runs as
the dedicated system user `usblcd` (created by `contrib/sysusers.d/usblcd.conf`,
`/usr/bin/nologin`, no HOME) with `Nice=10`, `ProtectSystem=strict`,
`ProtectHome=read-only`, `NoNewPrivileges`, `PrivateTmp`, `RestrictSUIDSGID` and
`LockPersonality`.  Its primary group is `usblcd`, which is exactly the group
the udev rule grants panel access to, so it needs no supplementary groups.  The
header title defaults to the machine hostname (`TITLE` empty), and `CPUS=all`
means the scheduler places the processes wherever it likes — on a hybrid-CPU box
you probably want the low-MHz group for the dashboard: find it with
`lscpu -e=CPU,CORE,MAXMHZ`, put it in `CPUS`, and add a drop-in with the same
list as `CPUAffinity=` if you want it applied to the whole unit.  Being a system
user it has no HOME, so the unit sets
`USBLCD_CALIB=/etc/usblcd/calibration` (see the calibration section).
`systemctl stop` releases the panel and the firmware returns to its boot logo.
It needs FreeType for TTF text; without `build/ttfplan` it falls back to the
bitmap font.

The unit deliberately depends on nothing NVIDIA: it starts seconds before the
driver answers `nvidia-smi`, and the dashboard's GPU poller retries, so the GPU
page fills in by itself a few seconds after boot.  It depends on nothing strata
either, for the same reason — the inference server comes up minutes after the
unit does, and the AI page's poller waits for it.

The AI page is the one panel source the service user cannot see by itself: the
strata state (`launch` configs, `runtime` ready files, and the API key
`/metrics` demands) is written 0600 by the login that runs the model, and the
unit runs as `usblcd` with `ProtectHome=read-only` — read-only is not readable
by another user.  So on a service box the AI page starts empty (`NO STRATA
SERVER`, with the paths it looked in) until you tell it where to go, in
`/etc/usblcd/dashboard.conf`:

```conf
PAGES=CPU,GPU,PROC,AI
STRATA_URL=http://127.0.0.1:47832
STRATA_API_KEY=…                # the key in ~/.local/state/strata/launch/<profile>.json
```

`chmod 600 /etc/usblcd/dashboard.conf` (it then holds a secret).  The two keys
take precedence over anything found on disk, so the page does not depend on the
service being able to read your home.

`STRATA_STATE=<dir>` (or `--strata-state`) is the third key: it points the
discovery at a strata state tree, for a dashboard that runs as the login that
runs the model — your own `python3 dash/dash.py`, a user unit, or a state
directory that is group-readable.  A dashboard running as that login needs
nothing at all: it finds `~/.local/state/strata` by itself, and under `sudo` it
searches the login that typed `sudo` too (root can read its 0600 configs).

`tools/strata_conf.py` writes those keys for you, from the same discovery the
AI page uses, so there is nothing to copy by hand:

```sh
python3 tools/strata_conf.py --check                       # what it would write, and proof it answers
sudo sh -c 'umask 077; python3 tools/strata_conf.py >> /etc/usblcd/dashboard.conf'
sudo systemctl restart usblcd-dashboard
```

It prints `STRATA_URL=` / `STRATA_API_KEY=` lines only (never a half-config: it
exits 1 and writes nothing when it finds no server), `--url`/`--key` override the
discovery, and `--state` reads another login's state tree.  `make install-service`
copies `dash/`, not `tools/`, so run it from the project directory.

## Capture pipelines (`feed`)

`feed` reads raw RGBA8888 frames of the panel's canvas size on stdin, so
anything that can emit raw video drives the screen:

```sh
# this screen, scaled to the panel (X11)
ffmpeg -f x11grab -framerate 15 -i :0.0 -vf scale=540:960 -f rawvideo -pix_fmt rgba - \
  | ./usblcd --rotate 90 --fps 15 feed

# a video file
ffmpeg -i holiday.mp4 -vf scale=540:960 -f rawvideo -pix_fmt rgba - | ./usblcd feed

# a dashboard you render yourself (gnuplot / matplotlib / shlvl)
watch -n 5 -c ./make_chart.sh          # writes chart.png
./usblcd watch chart.png               # keeps it on the panel
```

## Keeping something up permanently

The firmware paints its boot logo ~2–3 s after the last frame; there is no
"keep this image" command on this wire. Two options:

```sh
# 1. keepalive loop in the foreground
./usblcd --brightness 60 watch dashboard.png

# 2. as a service (contrib/usblcd@.service): restarts the loop on boot,
#    on USB replug, and when the picture changes
sudo systemctl enable --now "usblcd@/opt/usblcd/share/dashboard.png"
```

`contrib/usblcd@.service` is that unit: the picture path is the service
instance name, `Restart=always` keeps the loop up, and `UdevTrigger=usb`
(systemd ≥ 256) re-attaches it right after a replug.

## Scope and honest limitations

* **Wire coverage**: the raw-bulk `87ad:70db` family only. The SCSI-wired
  sibling (`87cd:70db`), the HID Type-2 panels (`0416:5302`), the ALi panel
  (`0416:5406`) and the LY panels (`0416:5408`/`5409`) use the same
  command set with a *different* handshake and framing; `usblcd list` names
  them and says so. Use the reference project's `trcc` for those.
* **No text/themes**: this wire only paints frames. The vendor app's themes,
  masks, clocks and sensor dashboards are rendered *by the app* into a frame.
  Render yours (matplotlib/gnuplot/ImageMagick) and send it, or use `trcc`
  for the full theme engine.
* **Brightness is software** — measured to work: `--brightness 100/60/30/10`
  on a white fill goes from full to near-black on this unit. The `BKL_SET`
  (cmd 8) payload is undocumented and unimplemented in the reference, so
  there is no hardware knob to drive.
* **Sensor data is not read from the panel** — it is not a sensor device; get
  temperatures from `hwmon`/`sensors` and paint them into your image.
* Untested geometry for FBL codes nobody has catalogued falls back to the
  reference's own fallbacks (480x480 for mode-2 panels outside the PM ladder,
  320x320 for an unknown FBL) and says so on stderr.

## Layout

```
src/usblcd.c      CLI, frame pipeline, keepalive, feed
src/device.c      libusb: open/claim/handshake/16 KiB bulk writes/ZLP
src/profile.c     PM+SUB → FBL → resolution, encoder, rotation, mount
src/render.c      decode (stb_image), fit/crop, rotate, dim, JPEG/RGB565
vendor/stb_image.h, vendor/stb_image_write.h   (public-domain image decoders)
rules.d/99-usblcd.rules                        non-root access
contrib/usblcd@.service
tests/            offline checks: geometry tables + encoders
```

## Build

Dependencies: `libusb-1.0`, `libjpeg` (libjpeg-turbo), a C compiler, `pkg-config`.

```sh
make                # ./usblcd
make test           # offline checks (no panel needed)
sudo make install   # /usr/local/bin/usblcd
sudo make install-udev
```

## Licence

Your code here — `src/`, `dash/`, `tools/`, `tests/`, `contrib/`, `rules.d/`,
`Makefile` — is **Apache-2.0**: see `LICENSE` (full text) and `NOTICE`
(third-party notices that must travel with a redistribution).  Every source
file carries an `SPDX-License-Identifier: Apache-2.0` line.

Third-party pieces keep their own licences and are listed in `NOTICE`:

| piece | licence |
|---|---|
| `vendor/stb_image.h`, `vendor/stb_image_write.h` | public domain / MIT-style |
| `fonts/Inter-var.ttf` | SIL Open Font License 1.1 (`fonts/OFL.txt`) |
| `libusb-1.0`, `libjpeg`, `FreeType` (linked, not vendored) | LGPL-2.1+, IJG, MIT/FTL |

The font is not covered by Apache-2.0 and a modified copy of it may not be
called "Inter" (Reserved Font Name).

## References

* `doc/USBLCDNEW_PROTOCOL.md` in this repo — the protocol notes this tool was
  written against, quoted from the reference project.
* [thermalright-trcc-linux](https://github.com/Lexonight1/thermalright-trcc-linux)
  — the GUI/daemon implementation and the reverse-engineering this ports from
  (`doc/PROTOCOL_USBLCDNEW.md`, `doc/REFERENCE_PORTS.md`).
