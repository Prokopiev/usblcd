/* SPDX-License-Identifier: Apache-2.0
 * Copyright 2026 Prokopiev; see LICENSE and NOTICE in the repository root. */
/* test_profile.c — the handshake-byte → geometry tables, checked offline.
 *
 * Every case below is read off the reference project's own tables
 * (thermalright-trcc-linux src/trcc/core/protocol.py), so a drift in this
 * port shows up as a failing assertion, not as a sideways picture.
 */
#include <stdio.h>
#include <string.h>

#include "usblcd.h"

static int fails;

static void check(const char *what, int got, int want)
{
    if (got != want) {
        printf("FAIL %s: got %d want %d\n", what, got, want);
        fails++;
    }
}

static void check_profile(const char *label, int pm, int sub,
                          int w, int h, int jpeg, int base, int invert,
                          int baseline, int portrait, int fbl)
{
    profile_t p;
    char name[96];
    memset(&p, 0, sizeof(p));
    bulk_profile(pm, sub, &p);
#define CK(field, want) do { \
        snprintf(name, sizeof(name), "pm%d/sub%d %s: %s", pm, sub, label, #field); \
        check(name, p.field, want); \
    } while (0)
    CK(fbl, fbl);
    CK(width, w);
    CK(height, h);
    CK(jpeg, jpeg);
    CK(encode_base, base);
    CK(encode_invert, invert);
    CK(encode_baseline, baseline);
    CK(portrait_mounted, portrait);
#undef CK
}

int main(void)
{
    /* The live panel on this host: PM=10 SUB=7 → 960x540, JPEG, base 180
     * (SUB 7 is in the alt arm), portrait-mounted. */
    check_profile("pm10sub7", 10, 7, 960, 540, 1, 180, 1, 0, 1, 224);
    check_profile("pm10sub1", 10, 1, 960, 540, 1, 0, 1, 0, 0, 224);
    /* SUB is a byte, not a bit index: 49 must not index a 32-bit mask
     * (PM=1 SUB=49 is a catalogued fingerprint, so this happens for real). */
    check_profile("pm10sub49", 10, 49, 960, 540, 1, 0, 1, 0, 1, 224);
    check_profile("pm1sub49b", 1, 49, 1920, 462, 1, 180, 1, 0, 1, 192);
    check_profile("pm9sub0",   9, 0, 854, 480, 1, 0, 0, 0, 0, 224);
    check_profile("pm11sub5",  11, 5, 854, 480, 1, 0, 0, 0, 1, 224);
    check_profile("pm12sub0",  12, 0, 800, 480, 1, 0, 0, 0, 0, 224);
    check_profile("pm13sub5",  13, 5, 960, 320, 1, 180, 1, 0, 1, 224);
    check_profile("pm13sub2",  13, 2, 960, 320, 1, 0, 1, 0, 0, 224);
    check_profile("pm15sub0",  15, 0, 640, 172, 1, 0, 1, 0, 0, 224);
    check_profile("pm7sub0",   7, 0, 640, 480, 1, 0, 1, 0, 0, 64);
    check_profile("pm5sub0",   5, 0, 320, 240, 1, 0, 1, 0, 0, 50);
    check_profile("pm50sub0",  50, 0, 320, 240, 0, 90, 1, 0, 0, 50);
    check_profile("pm32sub0",  32, 0, 320, 320, 0, 0, 1, 0, 0, 100);
    check_profile("pm63sub3",  63, 3, 1600, 720, 1, 0, 1, 0, 0, 114);
    check_profile("pm63sub0",  63, 0, 1600, 720, 1, 180, 1, 0, 0, 114);
    check_profile("pm1sub49",  1, 49, 1920, 462, 1, 180, 1, 0, 1, 192);
    check_profile("pm1sub48",  1, 48, 1600, 720, 1, 180, 1, 0, 0, 114);
    check_profile("pm68sub2",  68, 2, 1280, 480, 1, 90, 1, 0, 0, 192);
    check_profile("pm69sub0",  69, 0, 1920, 440, 1, 180, 1, 0, 0, 192);
    check_profile("pm66sub5",  66, 5, 1920, 462, 1, 180, 1, 0, 0, 192);
    /* PM 54 / PM 100 are NOT in the mode-2 PM ladder, so a bulk panel that
     * reports them stays on the FBL 72 base (480x480) — FormCZTVInit leaves
     * them there; FBL 54/100 geometry is reached on the HID wires. */
    check_profile("pm54sub4",  54, 4, 480, 480, 1, 0, 1, 0, 0, 72);
    check_profile("pm54sub1",  54, 1, 480, 480, 1, 0, 1, 0, 0, 72);
    /* Mode-2 panels outside the PM ladder stay on the FBL 72 base (480x480). */
    check_profile("pm6sub0",   6, 0, 480, 480, 1, 0, 1, 180, 0, 72);
    check_profile("pm100sub0", 100, 0, 480, 480, 1, 0, 1, 0, 0, 72);
    check_profile("pm2sub0",   2, 0, 480, 480, 1, 0, 1, 0, 0, 72);

    /* Wire angle: base ± orientation, sign from invert. */
    profile_t p;
    memset(&p, 0, sizeof(p));
    bulk_profile(10, 7, &p);
    check("pm10sub7 @0",  wire_angle(&p, 0), 180);
    check("pm10sub7 @90", wire_angle(&p, 90), 90);
    check("pm10sub7 @180",wire_angle(&p, 180), 0);
    check("pm10sub7 @270",wire_angle(&p, 270), 270);

    memset(&p, 0, sizeof(p));
    bulk_profile(9, 0, &p);            /* 854x480, invert = no */
    check("pm9 @0",   wire_angle(&p, 0), 0);
    check("pm9 @90",  wire_angle(&p, 90), 90);
    check("pm9 @270", wire_angle(&p, 270), 270);

    memset(&p, 0, sizeof(p));
    bulk_profile(32, 0, &p);           /* 320x320, rotate = no */
    check("pm32 @90 (non-rotate panel)", wire_angle(&p, 90), 270);

    /* Canvas for a user picture: portrait at 90/270 on rotate panels. */
    int cw, ch;
    oriented_canvas(&p, 90, &cw, &ch);
    check("pm32 canvas @90 (square)", cw, 320);
    memset(&p, 0, sizeof(p));
    bulk_profile(10, 7, &p);
    oriented_canvas(&p, 0, &cw, &ch);
    check("pm10 canvas @0 w", cw, 960);
    check("pm10 canvas @0 h", ch, 540);
    oriented_canvas(&p, 90, &cw, &ch);
    check("pm10 canvas @90 w", cw, 540);
    check("pm10 canvas @90 h", ch, 960);

    /* Frame header layout: magic, cmd, w, h, 2, payload length. */
    uint8_t hdr[USBLCD_HDR_SIZE];
    build_header(hdr, 2, 960, 540, 0x6879);
    check("hdr magic", hdr[0] == 0x12 && hdr[1] == 0x34 && hdr[2] == 0x56 && hdr[3] == 0x78, 1);
    check("hdr cmd",  hdr[4], 2);
    check("hdr w",    hdr[8] | (hdr[9] << 8), 960);
    check("hdr h",    hdr[12] | (hdr[13] << 8), 540);
    check("hdr 56",   hdr[56], 2);
    check("hdr len0", hdr[60], 0x79);
    check("hdr len1", hdr[61], 0x68);

    if (fails) {
        printf("%d check(s) failed\n", fails);
        return 1;
    }
    printf("profile tables OK\n");
    return 0;
}
