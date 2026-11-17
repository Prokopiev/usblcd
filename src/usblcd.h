/* SPDX-License-Identifier: Apache-2.0
 * Copyright 2026 Prokopiev; see LICENSE and NOTICE in the repository root. */
/* usblcd — headless CLI for the ChiZhu/Thermalright USB LCD (87ad:70db).
 *
 * Raw USB bulk "USBLCDNew" protocol: 64-byte header with magic 0x12345678,
 * bulk OUT 0x01 / bulk IN 0x81, frames as JPEG (cmd=2) or RGB565 (cmd=3).
 *
 * Protocol tables are a port of the reverse-engineered reference project
 * github.com/Lexonight1/thermalright-trcc-linux (src/trcc/core/protocol.py,
 * src/trcc/adapters/device/bulk_lcd.py).
 */
#ifndef USBLCD_H
#define USBLCD_H

#include <stddef.h>
#include <stdint.h>

#define USBLCD_VID          0x87ad
#define USBLCD_PID          0x70db

#define USBLCD_MAGIC        0x12345678u
#define USBLCD_HDR_SIZE     64
#define USBLCD_EP_IN        0x81
#define USBLCD_EP_OUT       0x01
#define USBLCD_CHUNK_SIZE   (16 * 1024)
#define USBLCD_ALIGN        512

/* SSCRM command codes (TRCC.exe constants). */
enum {
    SSCRM_DEV_INFO  = 1,   /* device information query            */
    SSCRM_PICTURE   = 2,   /* picture / frame data (JPEG)         */
    SSCRM_LOGO      = 3,   /* logo / frame data (RGB565)          */
    SSCRM_OTA       = 4,   /* over-the-air update                 */
    SSCRM_UPG_STATE = 5,   /* upgrade state                       */
    SSCRM_ROTATE    = 6,   /* rotation control                    */
    SSCRM_SCR_SET   = 7,   /* screen settings                     */
    SSCRM_BKL_SET   = 8,   /* backlight / brightness              */
    SSCRM_LOGO_STATE= 9    /* logo state                          */
};

/* ── profile (handshake-derived geometry + encoder) ─────────────────── */

typedef struct {
    int  width, height;        /* panel framebuffer, landscape          */
    int  jpeg;                 /* 1 = JPEG (cmd 2), 0 = RGB565 (cmd 3)  */
    int  rotate;               /* panel takes the per-resolution wire rotation */
    int  widescreen;
    int  big_endian;           /* RGB565 byte order                     */
    int  wire_row_px;          /* RGB565 framebuffer row (0 = width)    */
    int  encode_base;          /* C# directionB base, degrees           */
    int  encode_invert;        /* sign of the user orientation term     */
    int  encode_baseline;      /* fixed hardware-mount rotation         */
    int  portrait_mounted;     /* SUB byte says screen is turned 90°    */
    int  calib_deg;            /* measured-on-glass offset, added to the
                                * table's base (0 = catalogued behaviour)  */
    int  pm, sub, fbl;
    int  max_frame_bytes;      /* firmware JPEG ceiling                 */
    char name[32];             /* decoded handshake product tag         */
} profile_t;

/* Resolve PM/SUB (handshake bytes) into a bulk-panel profile. */
void  bulk_profile(int pm, int sub, profile_t *out);
/* Clockwise wire rotation for a user orientation (0/90/180/270). */
int   wire_angle(const profile_t *p, int orientation);
/* Canvas a user-supplied picture fills at that orientation. */
void  oriented_canvas(const profile_t *p, int orientation, int *w, int *h);
/* Fill a 64-byte USBLCDNew frame header. */
void  build_header(uint8_t *hdr, uint32_t cmd, uint32_t w, uint32_t h,
                   uint32_t payload_len);

/* ── device (libusb) ────────────────────────────────────────────────── */

typedef struct {
    void   *handle;            /* libusb_device_handle*                 */
    int     iface;
    uint8_t ep_in, ep_out;
    uint16_t vid, pid;
    int     verbose;
    char    serial[64];
    char    product[64];
} lcd_t;

int  lcd_open(lcd_t *d, uint16_t vid, uint16_t pid, const char *serial, int verbose);
void lcd_close(lcd_t *d);
/* 64-byte device-info query → reply (up to cap bytes). Returns reply length */
int  lcd_handshake(lcd_t *d, uint8_t *resp, int cap, profile_t *prof);
/* header + payload → 16 KiB bulk writes (+ ZLP on 512-byte alignment) */
int  lcd_write_frame(lcd_t *d, const uint8_t *frame, size_t len);
/* Ad-hoc command packet: 64-byte header + payload, returns reply length. */
int  lcd_command(lcd_t *d, uint32_t cmd, const uint8_t *payload, size_t len,
                 uint8_t *resp, int cap);

/* ── per-panel calibration (measured on glass) ──────────────────────── */

/* The reference tables give the vendor app's behaviour, which disagrees with
 * some panels by 180° (its own issues #203/#169/#171 are exactly this).  An
 * offset measured on the glass is stored per panel, keyed on
 * vid:pid + serial + PM + SUB, and added to the table's encode base. */
/* deg  = measured rotation offset (0..350).
 * orient = -1 unset, 0 = counts DOWN with the orientation (the reference's
 * `invert = yes`), 1 = counts UP (clockwise).  Pass -1 to leave a field as-is. */
int  calib_lookup(uint16_t vid, uint16_t pid, const char *serial,
                  int pm, int sub, int *deg, int *orient);
int  calib_save(uint16_t vid, uint16_t pid, const char *serial,
                int pm, int sub, int deg, int orient);
const char *calib_path(void);

/* ── rendering ──────────────────────────────────────────────────────── */

typedef struct {
    uint8_t *px;               /* RGBA, 4 bytes per pixel, w*h          */
    int w, h;
} img_t;

/* img_new allocates a zeroed RGBA canvas (alpha 0 = transparent). */
img_t *img_new(int w, int h);
img_t *img_load(const char *path, char *err, size_t errlen);
img_t *img_fill(int w, int h, uint8_t r, uint8_t g, uint8_t b);
img_t *img_test_pattern(int w, int h);
/* Orientation marker: TOP=white bar, BOTTOM=blue, LEFT=yellow, RIGHT=cyan,
 * magenta square bottom-right, colour bars + grey ramp in the middle. */
img_t *img_marker(int w, int h);
/* mode: 0 = fit (letterbox), 1 = fill (cover + centre crop), 2 = stretch */
img_t *img_resize(img_t *src, int w, int h, int mode);
img_t *img_rotate(img_t *src, int deg);           /* clockwise, 0/90/180/270 */
void   img_brightness(img_t *img, int percent);
void   img_free(img_t *img);

/* Encoders return malloc'd payload buffers (+ length); NULL on failure. */
uint8_t *encode_jpeg  (const img_t *img, int quality, int max_size, size_t *len);
uint8_t *encode_rgb565(const img_t *img, int big_endian, int wire_row_px,
                       size_t *len);

/* ── misc ───────────────────────────────────────────────────────────── */
void hexdump(const uint8_t *p, size_t n, size_t show);
int  parse_color(const char *s, uint8_t out[3]);

#endif /* USBLCD_H */
