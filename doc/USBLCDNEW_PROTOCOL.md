# USBLCDNew protocol notes (87ad:70db, raw bulk)

Notes this tool is written against.  They are a **condensed port** of
[thermalright-trcc-linux](https://github.com/Lexonight1/thermalright-trcc-linux)
— `doc/PROTOCOL_USBLCDNEW.md`, `src/trcc/core/protocol.py`,
`src/trcc/adapters/device/bulk_lcd.py` — plus the bytes this panel actually
answered on this machine.  Where the reference and this device agree, that is
stated; where a byte is not understood, it is marked **unknown**, not guessed.

## Device

```
$ lsusb -d 87ad:70db -v | head
Bus 001 Device 007: ID 87ad:70db ChiZhu Tech USBDISPLAY
  bLength 0x12, bDescriptorType 0x01, bcdUSB 200, bDeviceClass 0x00
  idVendor 0x87ad, idProduct 0x70db, iSerialNumber 3 -> "<serial>"
  bNumConfigurations 1
    Interface 0: bInterfaceClass 255 (Vendor Specific), bNumEndpoints 2
      Endpoint 0x01 OUT  Bulk,  wMaxPacketSize 512
      Endpoint 0x81 IN   Bulk,  wMaxPacketSize 512
```

Nothing binds interface 0 in the kernel (`ls /sys/bus/usb/devices/*:1.0/driver`
shows no `usbfs`/`usb-storage` link for it on the bulk device), so libusb can
claim it directly.  The SCSI-wired sibling `87cd:70db` is a *different* wire
(SCSI pass-through, `sg` devices); this document is not about it.

## Frame header (64 bytes, little-endian)

| offset | size | field | our value |
|---|---|---|---|
| 0 | 4 | magic | `12 34 56 78` |
| 4 | 4 | command | 2 = picture as JPEG, 3 = picture as RGB565 |
| 8 | 4 | framebuffer width | panel's landscape width |
| 12 | 4 | framebuffer height | panel's landscape height |
| 16–55 | 40 | zero | zero |
| 56 | 4 | mode/flags | `2` for picture frames, `1` for the device-info query |
| 60 | 4 | payload length | bytes of payload that follow |

Transport: one write = header + payload, split into 16 KiB bulk writes on
EP 0x01 OUT; a zero-length packet follows when the *total* frame length is a
multiple of 512 (the packet delimiter).  A short write is a failure, not a
success — it looks from outside exactly like a dead panel.

## Command codes (`SSCRM_CMD_TYPE_*`)

| code | name | used by us |
|---|---|---|
| 1 | DEV_INFO | yes (handshake) |
| 2 | PICTURE | yes (JPEG payload) |
| 3 | LOGO / RGB565 picture | yes (RGB565 payload) |
| 4 | OTA | no (firmware update — do not send) |
| 5 | UPG_STATE | no |
| 6 | ROTATE | `usblcd cmd 6` — payload layout not published; unverified |
| 7 | SCR_SET | `usblcd cmd 7` — payload layout not published; unverified |
| 8 | BKL_SET | `usblcd cmd 8` — payload layout not published; unverified |
| 9 | LOGO_STATE | no |

TRCC.exe builds those packets in shared memory and `USBLCDNew.exe` forwards
them, so the *header* shape above is right for them; the **payload** shape is
not documented anywhere we could read, and the reference project does not
implement them at all: rotation is done in software before encode, and
brightness is applied by dimming pixels.  We follow the same rule, and
`usblcd cmd N` exists to explore them.

## Handshake

Request (64 bytes): magic, all zero, `byte[56] = 1`.

Reply (this panel answers 64 bytes; read 1024 and take what comes):

| offset | this device | meaning |
|---|---|---|
| 0–3 | `12 34 56 78` | magic |
| 4–11 | `53 53 43 52 4d 2d 56 31` | `"SSCRM-V1"` protocol tag |
| 20–23 | `57 33 38 9b` | unknown (device-specific id) |
| **24** | `0a` = 10 | **PM byte** — resolution + encoder |
| 28 | `40` = 64 | unknown (capability byte?) |
| 32 | `0c` = 12 | unknown |
| **36** | `07` = 7 | **SUB byte** — rotation arm + physical mount |
| 40, 44 | 1, 1 | unknown flags |
| **48–51** | `e7 9e 1f f6` | device tag (rendered as hex when byte 56 == 0x81) |
| 56 | `81` | branch marker: 0x81 → tag is bytes 48–51 (other values take a second branch) |

Validity: reply length ≥ 41 and `reply[24] != 0`.

## PM → geometry (the mode-2 "PM ladder")

A bulk panel negotiates `mode 2`, and the reference's `FormCZTVInit(72, 2, PM, SUB)`
starts from FBL 72 (480x480) and overrides it for the PM bytes below; a PM that
is not in the ladder **stays at 480x480** (a real property of the vendor app,
not a fallback we invented).

| PM | FBL | resolution | PM | FBL | resolution |
|---|---|---|---|---|---|
| 5 | 50 | 320x240 | 15 | 224 | 640x172 |
| 7 | 64 | 640x480 | 16 | 224 | 960x540 |
| 9 | 224 | 854x480 | 17, 18 | 224 | 960x320 |
| 10 | 224 | 960x540 | 20 | 224 | 854x480 |
| 11 | 224 | 854x480 | 32 | 100 | 320x320 (RGB565) |
| 12 | 224 | 800x480 | 50 | 50 | 320x240 (RGB565, big-endian) |
| 13 | 224 | 960x320 | 63, 64 | 114 | 1600x720 |
| 14 | 64 | 640x480 | 65, 66 | 192 | 1920x462 |
| (1,48) | 114 | 1600x720 | 68 | 192 | 1280x480 |
| (1,49) | 192 | 1920x462 | 69 | 192 | 1920x440 |

Encoder: JPEG (`cmd 2`) for every PM **except 32 and 50**, which take RGB565
(`cmd 3`).  FBL 54 (360x360 fan hub) additionally takes RGB565 at SUB 4.

## Rotation

The vendor app rotates the frame **into the landscape framebuffer** at encode
time, per resolution + encoder, and the user's orientation is folded into the
same angle:

```
wire_angle = (encode_base ± orientation) mod 360      (+ when invert = no)
```

`encode_base` comes from the (width, height, encoder) table — note 854x480 and
800x480 count **up** with the orientation (`invert = no`), everything else
counts down, and 960x540 flips its base to 180° at SUB 5/7 (which is this
panel).  A square/non-rotate panel takes `360 − orientation` only.

Separately, a fixed hardware-mount baseline applies per PM: PM 6 (FW360 Ultra,
480x480) is mounted 180° rotated, so its wire frame is pre-rotated 180°.

The SUB byte also says the screen is mounted **portrait** in the cooler
(threshold 5, 6 for 1920x462), which is why a 960x540 panel with SUB 7 needs
180° at orientation 0.  `info` prints all of it.

## Payloads

* **JPEG** — 4:4:4 (no chroma subsampling: the reference encodes 4:2:0 only as
  a last resort, because a saturated edge fringes).  Quality ladder
  95, 85, 75, 60, 45, 30; shrink until ≤ 450000 bytes, the firmware ceiling
  (`ImageToJpg` never ships a payload of 450000 bytes or more).  A frame above
  the ceiling is dropped silently: the write succeeds, the glass keeps the
  previous image.
* **RGB565** — 2 bytes/pixel, 5/6/5; little-endian, big-endian for PM 50 (and
  FBL 49).  On the 640x172 panels each 344-byte row is copied into a 352-byte
  row 4 bytes in (`wire_row_px = 176`).

## Measured on this unit (PM=10 SUB=7, 960x540, serial `<serial>`)

Everything below was read off the glass with `usblcd marker` (white bar = top
edge, yellow = left, magenta square = bottom-right corner), not inferred:

* The catalogued SUB 5/7 arm of `ENCODE_ROTATIONS[(960,540,JPEG)]`
  (`alt_base = 180`) lands **exactly 180° off** on this unit: sending angle 180
  puts the top edge on the bottom, the left edge on the right and reverses the
  grey ramp.  Sending 0° is upright.
* The screen is **physically landscape** (wider than tall in the cooler), so
  `SUB=7` does *not* mean "portrait-mounted" for this SKU, even though the
  reference's `_PORTRAIT_MOUNT_MIN_SUB` (threshold 5) says it should.
* The orientation sign **counts up**: with the 180° offset applied,
  `orientation = 90` must add +90° to land 90° clockwise on the glass; the
  catalogued `invert = yes` (subtract) lands 90° counter-clockwise.  This is
  the family behaviour the reference already ships for 854x480/800x480
  (`invert = no`), which is where its issues #203/#169/#171 live.
* Software dimming is visible and monotonic: a white fill at 100 / 60 / 30 / 10
  % goes full → grey → dark grey → near-black, and back.  No hardware
  brightness command is needed.

Stored as:

```
87ad:70db <serial> pm=10 sub=7 base=180 orient=cw
```

The reference's tables stay the default — they describe the vendor app, and
they are right for the panels its issue reporters measured.  What is not
allowed is shipping them as a promise, so the measured offset is per panel.

## Behaviour notes

* The panel paints its **boot logo ~2–3 s after the last frame**: there is no
  "hold this" command, so keep-alive is a 150 ms re-send loop (the reference
  uses the same cadence).
* Frames must be sent whole.  A partial frame paints only where it overlaps,
  which looks identical to a broken panel; that is the reference's issue #262.
* The reference also warns when a payload's shape disagrees with the header
  (e.g. a 480x854 JPEG under a 960x540 header): the panel paints only the
  overlap, and the command still reports success.
