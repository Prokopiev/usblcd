/* SPDX-License-Identifier: Apache-2.0
 * Copyright 2026 Prokopiev; see LICENSE and NOTICE in the repository root. */
/* usblcd — headless CLI for the raw-bulk USB LCD (ChiZhu 87ad:70db).
 *
 *   usblcd list                     find the panel
 *   usblcd info                     handshake: PM/SUB, resolution, encoder
 *   usblcd image FILE               show a picture (JPEG/PNG/BMP/TGA/PPM)
 *   usblcd color ff0000             solid colour
 *   usblcd pattern                  colour bars + ramp (bring-your-own-cables test)
 *   usblcd off                      black frame
 *   usblcd watch FILE               hold a picture on screen (re-send @ 150 ms)
 *   usblcd feed                     raw RGBA frames on stdin (screen capture, video)
 *   usblcd cmd N [hex]              raw protocol packet, for experimentation
 *
 * The panel reverts to its boot logo ~2-3 s after the last frame, so single
 * shots fade; `watch` keeps one alive.
 */
#define _POSIX_C_SOURCE 200809L

#include <getopt.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

#include <libusb-1.0/libusb.h>

#include "usblcd.h"

static volatile int g_stop;

static void on_signal(int sig)
{
    (void)sig;
    g_stop = 1;
}

typedef struct {
    uint16_t vid, pid;
    const char *serial;
    int  verbose;
    int  orientation;         /* 0/90/180/270, user orientation        */
    int  brightness;          /* 100 = full                            */
    int  quality;             /* JPEG quality                          */
    int  fit;                 /* 0 fit, 1 fill, 2 stretch              */
    int  max_size;            /* JPEG ceiling (firmware)               */
    int  no_rotate;           /* ignore the panel's wire rotation      */
    int  calib;               /* measured rotation offset, degrees     */
    int  have_calib;          /* --base was given (overrides the file)  */
    int  no_calib;            /* ignore the stored calibration          */
    int  set_deg;             /* calibrate --set N                      */
    int  have_set;
    int  duration;            /* calibrate: seconds per candidate        */
    int  orient;              /* 1 = clockwise, 0 = counter-clockwise     */
    int  have_orient;         /* --orient was given                       */
    int  chunk;               /* 0 = raw passthrough for JPEG files    */
    char *out;                /* dump the wire payload here            */
    char *dump;               /* dump the decoded image here (png)     */
} opts_t;

static int g_fps;

static opts_t g = {
    .vid = USBLCD_VID, .pid = USBLCD_PID, .serial = NULL,
    .brightness = 100, .quality = 95, .fit = 1, .max_size = 450000,
    .duration = 20,
    .out = NULL, .dump = NULL,
};

static const char *fit_name(int f)
{
    return f == 1 ? "fill" : f == 2 ? "stretch" : "fit";
}

static void usage(FILE *fp)
{
    fprintf(fp,
"usblcd — headless CLI for the Thermalright/ChiZhu USB LCD panel (87ad:70db)\n"
"\n"
"usage: usblcd [global flags] <command> [command args]\n"
"\n"
"commands\n"
"  list                       list the LCD/cooler panels on this machine\n"
"  info                       handshake and print the panel's geometry\n"
"  image FILE                 show a picture once\n"
"  color RRGGBB               fill the screen with a solid colour\n"
"  pattern                    colour bars + grey ramp + grid\n"
"  marker                     orientation marker (white top / yellow left)\n"
"  off                        black frame\n"
"  watch FILE                 hold a picture: re-send every 150 ms (Ctrl-C stops)\n"
"  feed                       raw RGBA8888 frames on stdin, encoded live\n"
"  cmd NUM [HEX]              send a raw 64-byte header packet (dev/experiment)\n"
"  raw FILE [W H]             send an already-encoded JPEG payload as-is\n"
"\n"
"global flags\n"
"  --vid NNNN --pid NNNN      target a specific USB id (default 87ad:70db)\n"
"  --serial STR               when several panels are plugged in\n"
"  --rotate N                 user orientation 0/90/180/270 (default 0)\n"
"  --brightness N             0..100 (software dimming; the panel has no HW dim)\n"
"  --quality N                JPEG quality, default 95\n"
"  --fit fit|fill|stretch     how a picture fills the panel (default fill)\n"
"  --fps N                    feed: frame cap (0 = as fast as the bus allows)\n"
"  --no-rotate                skip the panel's built-in wire rotation\n"
"  --base N                   measured rotation offset in degrees (see `calibrate`)\n"
"  --no-calib                 ignore the stored per-panel calibration\n"
"                             (file: $USBLCD_CALIB, ~/.config/usblcd/calibration,\n"
"                              then /etc/usblcd/calibration)\n"
"  --orient cw|ccw            sign of --rotate: cw = your picture turns clockwise\n"
"  --max-size N               JPEG byte ceiling, default 450000 (firmware)\n"
"  --out FILE                 write the wire payload to FILE instead of the panel\n"
"  --verbose -v               protocol chatter\n"
"  -h                         this help\n"
"\n"
"examples\n"
"  usblcd info\n"
"  usblcd image logo.png\n"
"  usblcd --rotate 90 --fit fill image dashboard.png\n"
"  usblcd --brightness 40 color 00ff88\n"
"  usblcd watch temp.png\n"
"  ffmpeg -f x11grab -i :0.0 -vf scale=540:960 -f rawvideo -pix_fmt rgba - \\\n"
"      | usblcd feed\n"
"\n"
"The panel fades back to its boot logo ~2-3 s after the last frame.\n"
);
}

/* ── shared pipeline ────────────────────────────────────────────────── */

/* Transmit an already-encoded payload.  Split out of send_image so `feed` can
 * reuse a payload when consecutive frames are byte-identical: a static
 * dashboard is re-sent once a second (to beat the ~2-3 s logo timeout), and
 * re-encoding the same picture every time is pure waste. */
static int send_payload(lcd_t *d, const profile_t *p, const uint8_t *payload, size_t plen)
{
    if (g.out) {
        FILE *fp = fopen(g.out, "wb");
        if (!fp) {
            fprintf(stderr, "usblcd: cannot write %s\n", g.out);
            return -1;
        }
        fwrite(payload, 1, plen, fp);
        fclose(fp);
        printf("wrote %zu payload bytes to %s\n", plen, g.out);
        return 0;
    }

    uint8_t *buf = malloc(USBLCD_HDR_SIZE + plen);
    if (!buf)
        return -1;
    build_header(buf, p->jpeg ? SSCRM_PICTURE : SSCRM_LOGO,
                 (uint32_t)p->width, (uint32_t)p->height, (uint32_t)plen);
    memcpy(buf + USBLCD_HDR_SIZE, payload, plen);

    if (g.verbose)
        printf("frame: %s header declares %dx%d cmd=%s payload=%zu bytes\n",
               p->jpeg ? "JPEG" : "RGB565", p->width, p->height,
               p->jpeg ? "2" : "3", plen);

    int r = lcd_write_frame(d, buf, USBLCD_HDR_SIZE + plen);
    free(buf);
    return r;
}

static int send_image(lcd_t *d, const profile_t *p, img_t *img)
{
    int angle = g.no_rotate ? 0 : wire_angle(p, g.orientation);
    img_t *frame = img_rotate(img, angle);
    if (!frame)
        return -1;
    if (p->encode_baseline) {
        img_t *mounted = img_rotate(frame, p->encode_baseline);
        img_free(frame);
        frame = mounted;
        if (!frame)
            return -1;
    }

    size_t plen = 0;
    uint8_t *payload = NULL;
    if (p->jpeg) {
        payload = encode_jpeg(frame, g.quality, g.max_size, &plen);
    } else {
        payload = encode_rgb565(frame, p->big_endian, p->wire_row_px, &plen);
    }
    img_free(frame);
    if (!payload)
        return -1;

    if (plen == 0)
        return -1;

    int r = send_payload(d, p, payload, plen);
    free(payload);
    return r;
}


/* Fit a source image to the panel canvas for the current orientation. */
static img_t *prepare_canvas_img(const profile_t *p, img_t *src)
{
    int cw, ch;
    oriented_canvas(p, g.orientation, &cw, &ch);
    if (!src)
        return img_fill(cw, ch, 0, 0, 0);
    img_t *fitted = img_resize(src, cw, ch, g.fit);
    if (!fitted)
        return NULL;
    img_brightness(fitted, g.brightness);
    return fitted;
}

static void print_profile(const profile_t *p, const lcd_t *d)
{
    printf("panel     : %04x:%04x  %s%s%s\n", d->vid, d->pid,
           d->product[0] ? d->product : "USBDISPLAY",
           d->serial[0] ? "  serial " : "", d->serial);
    printf("handshake : PM=%d SUB=%d → FBL %d%s\n", p->pm, p->sub, p->fbl,
           p->name[0] ? "" : "");
    if (p->name[0])
        printf("tag       : %s\n", p->name);
    printf("geometry  : %dx%d %s%s%s\n", p->width, p->height,
           p->jpeg ? "JPEG (cmd 2)" : "RGB565 (cmd 3)",
           p->big_endian ? ", big-endian" : "",
           p->wire_row_px ? ", padded rows" : "");
    printf("rotation  : base %d° invert=%s mount %d°%s\n", p->encode_base,
           p->encode_invert ? "yes" : "no", p->encode_baseline,
           p->portrait_mounted ? "  (portrait-mounted)" : "");
    printf("wire angle: %d° at orientation %d\n", wire_angle(p, g.orientation),
           g.orientation);
    if (p->calib_deg)
        printf("calibrate : %d° measured offset applied (usblcd calibrate)\n",
               p->calib_deg);
}

/* ── commands ───────────────────────────────────────────────────────── */

static int cmd_list(void)
{
    static const struct { uint16_t vid, pid; const char *what; } known[] = {
        { 0x87ad, 0x70db, "Thermalright / GrandVision LCD (raw bulk)  ← supported" },
        { 0x87cd, 0x70db, "Thermalright LCD (SCSI pass-through)        not this tool" },
        { 0x0416, 0x5302, "0416:5302 HID LCD (Type 2)                  not this tool" },
        { 0x0416, 0x5406, "ALi Corp LCD (SCSI-like)                    not this tool" },
        { 0x0416, 0x5408, "LY LCD (chunked bulk, EP09)                 not this tool" },
        { 0x0416, 0x5409, "LY1 LCD (chunked bulk, EP02)                not this tool" },
    };
    libusb_context *ctx;
    if (libusb_init(&ctx) < 0) {
        fprintf(stderr, "usblcd: libusb_init failed\n");
        return 1;
    }
    libusb_device **list = NULL;
    ssize_t n = libusb_get_device_list(ctx, &list);
    int found = 0;
    for (ssize_t i = 0; i < n; i++) {
        struct libusb_device_descriptor dd;
        if (libusb_get_device_descriptor(list[i], &dd) < 0)
            continue;
        for (size_t k = 0; k < sizeof(known) / sizeof(known[0]); k++) {
            if (dd.idVendor == known[k].vid && dd.idProduct == known[k].pid) {
                printf("%04x:%04x  bus%03d dev%03d  %s\n", dd.idVendor,
                       dd.idProduct, libusb_get_bus_number(list[i]),
                       libusb_get_device_address(list[i]), known[k].what);
                found++;
            }
        }
    }
    libusb_free_device_list(list, 1);
    libusb_exit(ctx);
    if (!found)
        printf("no known USB LCD panel found (lsusb: %s)\n",
               "run `lsusb` to see everything attached");
    return found ? 0 : 1;
}

static int cmd_info(lcd_t *d, const profile_t *calibrated)
{
    uint8_t resp[1024];
    profile_t p;
    memset(&p, 0, sizeof(p));
    int len = lcd_handshake(d, resp, sizeof(resp), &p);
    if (len < 0)
        return 1;
    /* Carry the measured calibration onto the freshly-handshaked profile: the
     * angles `info` prints are the ones the panel is actually driven at. */
    p.calib_deg = calibrated->calib_deg;
    p.encode_invert = calibrated->encode_invert;
    print_profile(&p, d);
    printf("reply     : %d bytes\n", len);
    hexdump(resp, len, 64);
    return 0;
}

static int cmd_image(lcd_t *d, const profile_t *p, const char *path)
{
    char err[128] = {0};
    img_t *src = img_load(path, err, sizeof(err));
    if (!src) {
        fprintf(stderr, "usblcd: cannot load %s (%s)\n", path, err);
        return 1;
    }
    if (g.verbose)
        printf("loaded %s: %dx%d\n", path, src->w, src->h);
    img_t *canvas = prepare_canvas_img(p, src);
    img_free(src);
    if (!canvas)
        return 1;
    int r = send_image(d, p, canvas);
    if (r == 0)
        printf("sent %dx%d canvas as %s (%s) to %04x:%04x\n", canvas->w, canvas->h,
               p->jpeg ? "JPEG" : "RGB565", fit_name(g.fit), d->vid, d->pid);
    img_free(canvas);
    return r;
}

static int cmd_fill(lcd_t *d, const profile_t *p, uint8_t rgb[3])
{
    int cw, ch;
    oriented_canvas(p, g.orientation, &cw, &ch);
    img_t *img = img_fill(cw, ch, rgb[0], rgb[1], rgb[2]);
    img_brightness(img, g.brightness);
    int r = send_image(d, p, img);
    if (r == 0)
        printf("sent #%02x%02x%02x fill on %dx%d %s\n", rgb[0], rgb[1], rgb[2],
               cw, ch, p->jpeg ? "JPEG" : "RGB565");
    img_free(img);
    return r;
}

static int cmd_watch(lcd_t *d, const profile_t *p, const char *path, int interval_ms)
{
    char err[128] = {0};
    img_t *src = img_load(path, err, sizeof(err));
    if (!src) {
        fprintf(stderr, "usblcd: cannot load %s (%s)\n", path, err);
        return 1;
    }
    img_t *canvas = prepare_canvas_img(p, src);
    img_free(src);
    if (!canvas)
        return 1;

    signal(SIGINT, on_signal);
    signal(SIGTERM, on_signal);
    printf("holding %s (%dx%d) at %d ms; Ctrl-C to release the panel\n",
           path, canvas->w, canvas->h, interval_ms);
    int fails = 0;
    while (!g_stop) {
        if (send_image(d, p, canvas) != 0) {
            if (++fails >= 3) {
                fprintf(stderr, "usblcd: giving up after %d failed frames\n", fails);
                img_free(canvas);
                return 1;
            }
        } else {
            fails = 0;
        }
        struct timespec ts = { interval_ms / 1000, (long)(interval_ms % 1000) * 1000000 };
        nanosleep(&ts, NULL);
    }
    printf("released; the panel reverts to its logo\n");
    img_free(canvas);
    return 0;
}

static int cmd_feed(lcd_t *d, const profile_t *p, int fps)
{
    int cw, ch;
    oriented_canvas(p, g.orientation, &cw, &ch);
    size_t framesz = (size_t)cw * ch * 4;
    uint8_t *raw = malloc(framesz);
    uint8_t *prev_raw = malloc(framesz);
    uint8_t *cache = NULL;          /* encoded payload of the previous frame */
    size_t cache_len = 0;
    int have_prev = 0, cached = 0, reused = 0;
    if (!raw || !prev_raw)
        return 1;
    signal(SIGINT, on_signal);
    printf("reading RGBA %dx%d frames from stdin (%d fps cap)\n", cw, ch, fps);
    long frame = 0;
    while (!g_stop) {
        size_t got = 0;
        while (got < framesz) {
            ssize_t r = read(0, raw + got, framesz - got);
            if (r <= 0)
                goto done;
            got += (size_t)r;
        }
        if (have_prev && memcmp(prev_raw, raw, framesz) == 0 && cached) {
            /* identical picture: reuse the payload, skip rotate + JPEG encode */
            if (send_payload(d, p, cache, cache_len) != 0)
                break;
            reused++;
        } else {
            img_t src = { raw, cw, ch };
            img_t *canvas = prepare_canvas_img(p, &src);
            if (!canvas)
                break;
            int angle = g.no_rotate ? 0 : wire_angle(p, g.orientation);
            img_t *fimg = img_rotate(canvas, angle);
            img_free(canvas);
            if (!fimg)
                break;
            if (p->encode_baseline) {
                img_t *mounted = img_rotate(fimg, p->encode_baseline);
                img_free(fimg);
                fimg = mounted;
                if (!fimg)
                    break;
            }
            size_t plen = 0;
            uint8_t *payload = p->jpeg ? encode_jpeg(fimg, g.quality, g.max_size, &plen)
                                       : encode_rgb565(fimg, p->big_endian,
                                                       p->wire_row_px, &plen);
            img_free(fimg);
            if (!payload || plen == 0) {
                free(payload);
                break;
            }
            if (send_payload(d, p, payload, plen) != 0) {
                free(payload);
                break;
            }
            free(cache);
            cache = payload;              /* keep it for the next identical frame */
            cache_len = plen;
            cached = 1;
            memcpy(prev_raw, raw, framesz);
            have_prev = 1;
        }
        frame++;
        if (fps > 0) {
            int ms = 1000 / fps;
            struct timespec ts = { ms / 1000, (long)(ms % 1000) * 1000000 };
            nanosleep(&ts, NULL);
        }
    }
done:
    free(raw);
    free(prev_raw);
    free(cache);
    printf("done, %ld frames (%d reused cached payload)\n", frame, reused);
    return 0;
}

static int cmd_raw(lcd_t *d, const profile_t *p, const char *path, int w, int h)
{
    FILE *fp = fopen(path, "rb");
    if (!fp) {
        fprintf(stderr, "usblcd: cannot read %s\n", path);
        return 1;
    }
    fseek(fp, 0, SEEK_END);
    long sz = ftell(fp);
    fseek(fp, 0, SEEK_SET);
    if (sz <= 0 || sz > 8 * 1024 * 1024) {
        fprintf(stderr, "usblcd: %s is %ld bytes, refusing\n", path, sz);
        fclose(fp);
        return 1;
    }
    uint8_t *payload = malloc((size_t)sz);
    if (!payload || fread(payload, 1, (size_t)sz, fp) != (size_t)sz) {
        fprintf(stderr, "usblcd: short read of %s\n", path);
        free(payload);
        fclose(fp);
        return 1;
    }
    fclose(fp);

    uint8_t *buf = malloc(USBLCD_HDR_SIZE + (size_t)sz);
    build_header(buf, p->jpeg ? SSCRM_PICTURE : SSCRM_LOGO,
                 (uint32_t)(w ? w : p->width), (uint32_t)(h ? h : p->height),
                 (uint32_t)sz);
    memcpy(buf + USBLCD_HDR_SIZE, payload, (size_t)sz);
    int r = lcd_write_frame(d, buf, USBLCD_HDR_SIZE + (size_t)sz);
    free(buf);
    free(payload);
    return r;
}

static int cmd_command(lcd_t *d, uint32_t code, const char *hex)
{
    uint8_t payload[512];
    size_t len = 0;
    if (hex && *hex) {
        size_t n = strlen(hex);
        if (n % 2 || n / 2 > sizeof(payload)) {
            fprintf(stderr, "usblcd: payload must be an even number of hex digits "
                            "(max %zu bytes)\n", sizeof(payload));
            return 1;
        }
        for (size_t i = 0; i < n; i += 2) {
            unsigned v;
            if (sscanf(hex + i, "%2x", &v) != 1) {
                fprintf(stderr, "usblcd: bad hex at offset %zu\n", i);
                return 1;
            }
            payload[len++] = (uint8_t)v;
        }
    }
    uint8_t resp[1024];
    int got = lcd_command(d, code, payload, len, resp, sizeof(resp));
    if (got < 0)
        return 1;
    if (got == 0) {
        printf("sent command %u (%zu payload bytes), device did not answer\n",
               code, len);
        return 0;
    }
    printf("command %u: %d byte reply\n", code, got);
    hexdump(resp, got, 64);
    return 0;
}

/* Cycle the candidate rotation offsets so the owner can pick the one that is
 * upright on their own glass, then store it.  The catalogued base angle is the
 * vendor app's behaviour, and it is 180° off on some panels. */
static int cmd_calibrate(lcd_t *d, profile_t *p, uint16_t vid, uint16_t pid,
                         const char *serial)
{
    if (g.have_set) {
        p->calib_deg = g.set_deg;
        if (g.have_orient)
            p->encode_invert = !g.orient;
        if (calib_save(vid, pid, serial, p->pm, p->sub, g.set_deg,
                       g.have_orient ? g.orient : -1) != 0)
            return 1;
        printf("stored: %04x:%04x serial %s PM=%d SUB=%d base offset %d\u00b0%s\n",
               vid, pid, serial, p->pm, p->sub, g.set_deg,
               g.have_orient ? (g.orient ? " orient=cw" : " orient=ccw") : "");
        printf("file    : %s\n", calib_path());
        printf("result  : wire angle %d\u00b0 at orientation %d\n",
               wire_angle(p, g.orientation), g.orientation);
        printf("showing the marker at the stored setting for %d s\n", g.duration);
        int cw, ch;
        oriented_canvas(p, 0, &cw, &ch);
        img_t *marker = img_marker(cw, ch);
        signal(SIGINT, on_signal);
        g_stop = 0;
        time_t end = time(NULL) + g.duration;
        while (!g_stop && time(NULL) < end) {
            if (send_image(d, p, marker) != 0)
                break;
            struct timespec ts = { 0, 150 * 1000000 };
            nanosleep(&ts, NULL);
        }
        img_free(marker);
        return 0;
    }

    int cw, ch;
    oriented_canvas(p, 0, &cw, &ch);
    img_t *marker = img_marker(cw, ch);
    if (!marker)
        return 1;

    /* Candidate offsets; the orientation sign is measured separately with
     * --rotate 90 (see README) and stored with --orient. */
    static const int cands[] = { 0, 90, 180, 270 };
    signal(SIGINT, on_signal);
    printf("Calibrating %04x:%04x serial %s (PM=%d SUB=%d, catalogued base %d\u00b0).\n",
           vid, pid, serial, p->pm, p->sub, p->encode_base);
    printf("Four candidates, %d seconds each.  UPRIGHT means:\n"
           "  WHITE bar along the TOP edge, YELLOW bar on the LEFT,\n"
           "  magenta square in the BOTTOM-RIGHT corner, ramp dark\u2192light left\u2192right.\n"
           "Ctrl-C skips to the next candidate.\n\n",
           g.duration);

    for (size_t i = 0; i < sizeof(cands) / sizeof(cands[0]) && !g_stop; i++) {
        profile_t q = *p;
        q.calib_deg = cands[i];
        g_stop = 0;
        signal(SIGINT, on_signal);
        printf("== offset %d\u00b0 (wire angle %d\u00b0 at orientation 0)\n",
               cands[i], wire_angle(&q, 0));
        time_t end = time(NULL) + g.duration;
        while (!g_stop && time(NULL) < end) {
            if (send_image(d, &q, marker) != 0) {
                img_free(marker);
                return 1;
            }
            struct timespec ts = { 0, 150 * 1000000 };
            nanosleep(&ts, NULL);
        }
        printf("   ... %d\u00b0 done\n", cands[i]);
    }
    img_free(marker);

    printf("\nWhich candidate was upright?  Store it with, e.g.:\n"
           "    usblcd calibrate --set 180\n"
           "and every later `usblcd image ...` uses it automatically.\n"
           "Calibration file: %s\n", calib_path());
    return 0;
}

/* ── option parsing ─────────────────────────────────────────────────── */

enum {
    OPT_VID = 256, OPT_PID, OPT_SERIAL, OPT_QUALITY, OPT_FIT, OPT_MAXSIZE,
    OPT_NOROTATE, OPT_OUT, OPT_INTERVAL, OPT_FPS, OPT_BASE, OPT_NOCALIB,
    OPT_SET, OPT_DURATION, OPT_ORIENT
};

static const struct option longopts[] = {
    { "vid",        required_argument, 0, OPT_VID },
    { "pid",        required_argument, 0, OPT_PID },
    { "serial",     required_argument, 0, OPT_SERIAL },
    { "rotate",     required_argument, 0, 'R' },
    { "brightness", required_argument, 0, 'B' },
    { "quality",    required_argument, 0, OPT_QUALITY },
    { "fit",        required_argument, 0, OPT_FIT },
    { "fps",        required_argument, 0, OPT_FPS },
    { "max-size",   required_argument, 0, OPT_MAXSIZE },
    { "no-rotate",  no_argument,       0, OPT_NOROTATE },
    { "base",       required_argument, 0, OPT_BASE },
    { "no-calib",   no_argument,       0, OPT_NOCALIB },
    { "set",        required_argument, 0, OPT_SET },
    { "duration",   required_argument, 0, OPT_DURATION },
    { "orient",     required_argument, 0, OPT_ORIENT },
    { "out",        required_argument, 0, OPT_OUT },
    { "interval",   required_argument, 0, OPT_INTERVAL },
    { "verbose",    no_argument,       0, 'v' },
    { "help",       no_argument,       0, 'h' },
    { 0, 0, 0, 0 }
};

int main(int argc, char **argv)
{
    int interval = 150;
    int c;
    while ((c = getopt_long(argc, argv, "hR:B:v", longopts, NULL)) != -1) {
        switch (c) {
        case OPT_VID:      g.vid = (uint16_t)strtol(optarg, NULL, 16); break;
        case OPT_PID:      g.pid = (uint16_t)strtol(optarg, NULL, 16); break;
        case OPT_SERIAL:   g.serial = optarg; break;
        case 'R':          g.orientation = atoi(optarg) % 360; break;
        case 'B':          g.brightness = atoi(optarg); break;
        case OPT_QUALITY:  g.quality = atoi(optarg); break;
        case OPT_FIT:
            g.fit = !strcmp(optarg, "fill") ? 1 : !strcmp(optarg, "stretch") ? 2 : 0;
            break;
        case OPT_MAXSIZE:  g.max_size = atoi(optarg); break;
        case OPT_FPS:      g_fps = atoi(optarg); break;
        case OPT_NOROTATE: g.no_rotate = 1; break;
        case OPT_BASE:     g.calib = atoi(optarg) % 360; g.have_calib = 1; break;
        case OPT_NOCALIB:  g.no_calib = 1; break;
        case OPT_SET:      g.set_deg = atoi(optarg) % 360; g.have_set = 1; break;
        case OPT_DURATION: g.duration = atoi(optarg); break;
        case OPT_ORIENT:
            if (!strcmp(optarg, "cw"))        { g.orient = 1; g.have_orient = 1; }
            else if (!strcmp(optarg, "ccw"))  { g.orient = 0; g.have_orient = 1; }
            else { fprintf(stderr, "usblcd: --orient is cw or ccw\n"); return 2; }
            break;
        case OPT_OUT:      g.out = optarg; break;
        case OPT_INTERVAL: interval = atoi(optarg); break;
        case 'v':          g.verbose++; break;
        case 'h':
        default:
            usage(stdout);
            return c == 'h' ? 0 : 2;
        }
    }
    if (optind >= argc) {
        usage(stderr);
        return 2;
    }
    if (g.duration <= 0)
        g.duration = 20;
    if (g.orientation % 90) {
        fprintf(stderr, "usblcd: --rotate must be 0, 90, 180 or 270\n");
        return 2;
    }
    if (g.brightness < 0 || g.brightness > 100) {
        fprintf(stderr, "usblcd: --brightness is 0..100\n");
        return 2;
    }

    const char *cmd = argv[optind++];

    if (!strcmp(cmd, "list"))
        return cmd_list() == 0 ? 0 : 1;

    if (!strcmp(cmd, "help"))
        usage(stdout), exit(0);

    /* Everything below talks to the panel. */
    lcd_t dev;
    if (lcd_open(&dev, g.vid, g.pid, g.serial, g.verbose) != 0)
        return 1;

    uint8_t resp[1024];
    profile_t prof;
    memset(&prof, 0, sizeof(prof));
    if (lcd_handshake(&dev, resp, sizeof(resp), &prof) < 0) {
        lcd_close(&dev);
        return 1;
    }
    /* Per-panel rotation offset: measured on this panel's glass, if the
     * owner ever measured it.  --base wins over the stored value. */
    if (!g.no_calib) {
        int deg = 0, orient = -1;
        if (calib_lookup(g.vid, g.pid, dev.serial, prof.pm, prof.sub,
                         &deg, &orient)) {
            prof.calib_deg = deg;
            if (orient >= 0)
                prof.encode_invert = !orient;      /* cw = count up */
            if (g.verbose)
                printf("calibration: base offset %d\u00b0, orient=%s from %s\n",
                       deg, orient >= 0 ? (orient ? "cw" : "ccw") : "catalogued",
                       calib_path());
        }
    }
    if (g.have_calib)
        prof.calib_deg = g.calib;
    if (g.have_orient)
        prof.encode_invert = !g.orient;
    if (g.verbose)
        print_profile(&prof, &dev);

    int r = 1;
    if (!strcmp(cmd, "info")) {
        r = cmd_info(&dev, &prof);
    } else if (!strcmp(cmd, "image") || !strcmp(cmd, "watch")) {
        if (optind >= argc) {
            fprintf(stderr, "usblcd: %s needs a file\n", cmd);
            r = 2;
        } else if (!strcmp(cmd, "image")) {
            r = cmd_image(&dev, &prof, argv[optind]);
        } else {
            r = cmd_watch(&dev, &prof, argv[optind], interval);
        }
    } else if (!strcmp(cmd, "color") || !strcmp(cmd, "fill")) {
        uint8_t rgb[3] = { 255, 255, 255 };
        if (optind < argc) {
            if (parse_color(argv[optind], rgb) != 0) {
                fprintf(stderr, "usblcd: bad colour '%s' (want RRGGBB)\n", argv[optind]);
                r = 2;
            } else {
                r = cmd_fill(&dev, &prof, rgb);
            }
        } else {
            r = cmd_fill(&dev, &prof, rgb);
        }
    } else if (!strcmp(cmd, "pattern")) {
        int cw, ch;
        oriented_canvas(&prof, g.orientation, &cw, &ch);
        img_t *img = img_test_pattern(cw, ch);
        img_brightness(img, g.brightness);
        r = send_image(&dev, &prof, img);
        img_free(img);
    } else if (!strcmp(cmd, "marker")) {
        int cw, ch;
        oriented_canvas(&prof, g.orientation, &cw, &ch);
        img_t *img = img_marker(cw, ch);
        img_brightness(img, g.brightness);
        r = send_image(&dev, &prof, img);
        if (r == 0)
            printf("sent orientation marker (white=TOP, yellow=LEFT, magenta square=bottom-right)\n");
        img_free(img);
    } else if (!strcmp(cmd, "off") || !strcmp(cmd, "black")) {
        uint8_t rgb[3] = { 0, 0, 0 };
        r = cmd_fill(&dev, &prof, rgb);
    } else if (!strcmp(cmd, "feed")) {
        r = cmd_feed(&dev, &prof, g_fps);
    } else if (!strcmp(cmd, "raw")) {
        if (optind >= argc) {
            fprintf(stderr, "usblcd: raw needs a file\n");
            r = 2;
        } else {
            int w = 0, h = 0;
            if (optind + 2 < argc) {
                w = atoi(argv[optind + 1]);
                h = atoi(argv[optind + 2]);
            }
            r = cmd_raw(&dev, &prof, argv[optind], w, h);
        }
    } else if (!strcmp(cmd, "calibrate")) {
        r = cmd_calibrate(&dev, &prof, g.vid, g.pid, dev.serial);
    } else if (!strcmp(cmd, "cmd")) {
        if (optind >= argc) {
            fprintf(stderr, "usblcd: cmd needs a command number\n");
            r = 2;
        } else {
            r = cmd_command(&dev, (uint32_t)strtoul(argv[optind], NULL, 10),
                            optind + 1 < argc ? argv[optind + 1] : NULL);
        }
    } else {
        fprintf(stderr, "usblcd: unknown command '%s'\n\n", cmd);
        usage(stderr);
        r = 2;
    }

    if (r == 0 && !strcmp(cmd, "pattern"))
        printf("sent test pattern (%dx%d, %s)\n", prof.width, prof.height,
               prof.jpeg ? "JPEG" : "RGB565");
    lcd_close(&dev);
    return r;
}
