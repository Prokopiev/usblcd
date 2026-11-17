/* SPDX-License-Identifier: Apache-2.0
 * Copyright 2026 Prokopiev; see LICENSE and NOTICE in the repository root. */
/* profile.c — handshake bytes → panel geometry/encoder/rotation.
 *
 * Ported from thermalright-trcc-linux src/trcc/core/protocol.py
 * (FBL_PROFILES, ENCODE_ROTATIONS, the PM ladder) and
 * src/trcc/adapters/device/bulk_lcd.py (bulk_profile).
 */
#include <stdio.h>
#include <string.h>

#include "usblcd.h"

#define ARRAY_LEN(a) ((int)(sizeof(a) / sizeof((a)[0])))

/* ── FBL table ──────────────────────────────────────────────────────── */

typedef struct {
    int fbl, w, h;
    int jpeg, big_endian, rotate, widescreen;
    int wire_row_px;
    int rgb565_subs;                 /* bitmask of SUB bytes forced to RGB565 */
    int pm_base_pm, pm_base_deg;     /* fixed hardware-mount rotation by PM   */
} fbl_entry_t;

static const fbl_entry_t FBL_PROFILES[] = {
    /*   fbl   w     h   jpeg BE  rot  wide  row  subs  pm  deg */
    {   36,  240, 240,   0,  0,  0,   0,   0,   0,  -1,   0 },
    {   37,  240, 240,   0,  0,  0,   0,   0,   0,  -1,   0 },
    {   49,  320, 240,   0,  1,  1,   0,   0,   0,  -1,   0 },
    {   50,  320, 240,   0,  0,  1,   0,   0,   0,  -1,   0 },
    {   51,  320, 240,   0,  0,  1,   0,   0,   0,  -1,   0 },
    {   52,  320, 240,   0,  0,  1,   0,   0,   0,  -1,   0 },
    {   53,  320, 240,   0,  0,  1,   0,   0,   0,  -1,   0 },
    {   54,  360, 360,   1,  0,  0,   0,   0,   1<<4, -1,  0 },  /* CORE VISION SUB 4 */
    {   58,  320, 240,   0,  0,  1,   0,   0,   0,  -1,   0 },
    {   59,  640, 172,   0,  0,  1,   1, 176,   0,  -1,   0 },  /* 640x172, RGB565 */
    {   60,  320, 176,   0,  0,  1,   0,   0,   0,  -1,   0 },
    {   64,  640, 480,   0,  0,  1,   0,   0,   0,  -1,   0 },
    {   72,  480, 480,   0,  0,  0,   0,   0,   0,   6, 180 },  /* FW360 Ultra */
    {  100,  320, 320,   0,  1,  0,   0,   0,   0,  -1,   0 },
    {  101,  320, 320,   0,  1,  0,   0,   0,   0,  -1,   0 },
    {  102,  320, 320,   0,  1,  0,   0,   0,   0,  -1,   0 },
    {  114, 1600, 720,   1,  0,  1,   1,   0,   0,  -1,   0 },
    {  128, 1280, 480,   1,  0,  1,   1,   0,   0,  -1,   0 },
    {  129,  480, 480,   0,  0,  0,   0,   0,   0,   6, 180 },  /* alias of 72 */
    {  192, 1920, 462,   1,  0,  1,   1,   0,   0,  -1,   0 },
    {  224,  854, 480,   1,  0,  1,   1,   0,   0,  -1,   0 },
};

/* Unknown FBL → DEFAULT_PROFILE (320x320, big-endian). */
static const fbl_entry_t DEFAULT_FBL = { 0, 320, 320, 0, 1, 0, 0, 0, 0, -1, 0 };

/* ── PM ladder ──────────────────────────────────────────────────────── */

static const struct { int pm, fbl; } PM_TO_FBL[] = {
    {   5,  50 }, {   7,  64 }, {   9, 224 }, {  10, 224 },
    {  11, 224 }, {  12, 224 }, {  13, 224 }, {  14,  64 },
    {  15, 224 }, {  16, 224 }, {  17, 224 }, {  18, 224 },
    {  20, 224 }, {  32, 100 }, {  50,  50 }, {  63, 114 },
    {  64, 114 }, {  65, 192 }, {  66, 192 }, {  68, 192 },
    {  69, 192 },
};

/* (PM, SUB) pairs that change the FBL mapping. */
static const struct { int pm, sub, fbl; } PM_SUB_TO_FBL[] = {
    { 1, 48, 114 },   /* 1600x720  */
    { 1, 49, 192 },   /* 1920x462  */
};

/* FBL 224 is shared by five resolutions — PM disambiguates. */
static const struct { int pm, w, h; } FBL_224_BY_PM[] = {
    {  9,  854, 480 }, { 10,  960, 540 }, { 11,  854, 480 },
    { 12,  800, 480 }, { 13,  960, 320 }, { 15,  640, 172 },
    { 16,  960, 540 }, { 17,  960, 320 }, { 18,  960, 320 },
    { 20,  854, 480 },
};
#define FBL_224_FALLBACK_W 854
#define FBL_224_FALLBACK_H 480

/* FBL 192 is shared by three resolutions. */
static const struct { int pm, w, h; } FBL_192_BY_PM[] = {
    {  1, 1920, 462 }, { 65, 1920, 462 }, { 66, 1920, 462 },
    { 68, 1280, 480 }, { 69, 1920, 440 },
};
#define FBL_192_FALLBACK_W 1920
#define FBL_192_FALLBACK_H 462

/* ── Encode rotations (the C# directionB switch) ────────────────────── */

typedef struct {
    int w, h, jpeg;
    int base, invert;
    int alt_base;                  /* -1 = no SUB arm */
    int alt_subs;                  /* bitmask of SUB bytes */
} encode_rot_t;

static const encode_rot_t ENCODE_ROTATIONS[] = {
    /* JPEG */
    {  320,  320, 1,   0, 1, -1, 0 },
    {  480,  480, 1,   0, 1, -1, 0 },
    {  320,  240, 1,   0, 1, -1, 0 },
    { 1600,  720, 1, 180, 1,  0, (1<<3) },
    {  854,  480, 1,   0, 0, -1, 0 },      /* counts UP with the angle */
    {  800,  480, 1,   0, 0, -1, 0 },
    { 1280,  480, 1,   0, 1, 90, (1<<2) },
    {  960,  320, 1, 180, 1,  0, (1<<0)|(1<<1)|(1<<2)|(1<<3)|(1<<4) },
    {  960,  540, 1,   0, 1, 180, (1<<5)|(1<<7) },
    { 1920,  462, 1, 180, 1,  0, (1<<2)|(1<<3)|(1<<4) },
    { 1920,  440, 1, 180, 1,  0, (1<<2)|(1<<3)|(1<<4) },
    {  640,  480, 1,   0, 1, -1, 0 },
    {  360,  360, 1,   0, 1, -1, 0 },
    {  640,  172, 1,   0, 1, -1, 0 },
    /* RGB565 */
    {  240,  240, 0,   0, 1, -1, 0 },
    {  320,  320, 0,   0, 1, -1, 0 },
    {  360,  360, 0,   0, 1, -1, 0 },
    {  480,  480, 0,   0, 1, -1, 0 },
    {  640,  172, 0, 270, 1, 90, (1<<5) },
};
#define DEFAULT_ENCODE_BASE   90
#define DEFAULT_ENCODE_INVERT 1

/* ── Portrait physical mount (SUB byte) ─────────────────────────────── */

static const struct { int w, h, min_sub; } PORTRAIT_MOUNT[] = {
    {  320,  176, 5 }, {  640,  172, 5 }, {  640,  480, 5 },
    {  800,  480, 5 }, {  854,  480, 5 }, {  960,  320, 5 },
    {  960,  540, 5 }, { 1920,  440, 5 }, { 1920,  462, 6 },
};

/* ── Lookups ────────────────────────────────────────────────────────── */

static const fbl_entry_t *fbl_entry(int fbl)
{
    for (int i = 0; i < ARRAY_LEN(FBL_PROFILES); i++)
        if (FBL_PROFILES[i].fbl == fbl)
            return &FBL_PROFILES[i];
    return &DEFAULT_FBL;
}

static int in_pm_ladder(int pm, int sub)
{
    for (int i = 0; i < ARRAY_LEN(PM_SUB_TO_FBL); i++)
        if (PM_SUB_TO_FBL[i].pm == pm && PM_SUB_TO_FBL[i].sub == sub)
            return 1;
    for (int i = 0; i < ARRAY_LEN(PM_TO_FBL); i++)
        if (PM_TO_FBL[i].pm == pm)
            return 1;
    return 0;
}

static int pm_to_fbl(int pm, int sub)
{
    for (int i = 0; i < ARRAY_LEN(PM_SUB_TO_FBL); i++)
        if (PM_SUB_TO_FBL[i].pm == pm && PM_SUB_TO_FBL[i].sub == sub)
            return PM_SUB_TO_FBL[i].fbl;
    for (int i = 0; i < ARRAY_LEN(PM_TO_FBL); i++)
        if (PM_TO_FBL[i].pm == pm)
            return PM_TO_FBL[i].fbl;
    return pm;                          /* PM == FBL by convention */
}

/* SUB-set membership.  Every set the C# names (alt_subs, rgb565_subs) lives
 * in SUB 0..7 — {4}, {2,3,4}, {5}, {3}, {5,7}, {0..4} — so a byte outside
 * 0..31 is simply in none of them, and `1 << sub` must never be asked to
 * shift by 49 (PM=1 SUB=49 is a real, catalogued fingerprint). */
static int sub_in(int mask, int sub)
{
    return sub >= 0 && sub < 32 && (mask & (1 << sub)) != 0;
}

void bulk_profile(int pm, int sub, profile_t *out)
{
    memset(out, 0, sizeof(*out));
    out->pm = pm;
    out->sub = sub;

    int fbl = in_pm_ladder(pm, sub) ? pm_to_fbl(pm, sub) : 72;
    out->fbl = fbl;

    const fbl_entry_t *e = fbl_entry(fbl);
    int w = e->w, h = e->h;

    /* FBL 192/224 are shared by several resolutions — PM disambiguates. */
    if (fbl == 224 || fbl == 192) {
        const struct { int pm, w, h; } *tab = fbl == 224 ? (void *)FBL_224_BY_PM
                                                        : (void *)FBL_192_BY_PM;
        int n = fbl == 224 ? ARRAY_LEN(FBL_224_BY_PM) : ARRAY_LEN(FBL_192_BY_PM);
        int found = 0;
        for (int i = 0; i < n; i++) {
            if (tab[i].pm == pm) { w = tab[i].w; h = tab[i].h; found = 1; break; }
        }
        if (!found) {
            w = fbl == 224 ? FBL_224_FALLBACK_W : FBL_192_FALLBACK_W;
            h = fbl == 224 ? FBL_224_FALLBACK_H : FBL_192_FALLBACK_H;
            fprintf(stderr, "usblcd: PM=%d not catalogued under FBL %d; "
                    "assuming %dx%d\n", pm, fbl, w, h);
        }
    }

    out->width = w;
    out->height = h;
    out->big_endian = e->big_endian;
    out->rotate = e->rotate;
    out->widescreen = e->widescreen;
    out->wire_row_px = e->wire_row_px;

    /* The bulk wire picks the encoder from the PM byte: JPEG for every PM
     * except 32 and 50, which take RGB565 (TRCC.exe ImageTo565). */
    out->jpeg = (pm != 32 && pm != 50);
    if (sub_in(e->rgb565_subs, sub))
        out->jpeg = 0;
    if (pm == 50)
        out->big_endian = 1;

    /* Encode rotation, keyed on resolution + encoder (+ SUB arm). */
    int base = DEFAULT_ENCODE_BASE, invert = DEFAULT_ENCODE_INVERT;
    int found = 0;
    for (int i = 0; i < ARRAY_LEN(ENCODE_ROTATIONS); i++) {
        const encode_rot_t *r = &ENCODE_ROTATIONS[i];
        if (r->w == w && r->h == h && r->jpeg == out->jpeg) {
            base = r->base;
            invert = r->invert;
            if (r->alt_base >= 0 && sub_in(r->alt_subs, sub))
                base = r->alt_base;
            found = 1;
            break;
        }
    }
    if (!found)
        fprintf(stderr, "usblcd: %dx%d jpeg=%d is not in the C# rotation "
                "switch; using default base %d\u00b0\n", w, h, out->jpeg,
                DEFAULT_ENCODE_BASE);
    out->encode_base = base;
    out->encode_invert = invert;

    /* Fixed hardware-mount baseline, keyed on PM (FW360 Ultra PM=6 → 180°). */
    out->encode_baseline = (e->pm_base_pm == pm) ? e->pm_base_deg : 0;

    out->portrait_mounted = 0;
    for (int i = 0; i < ARRAY_LEN(PORTRAIT_MOUNT); i++) {
        if (PORTRAIT_MOUNT[i].w == w && PORTRAIT_MOUNT[i].h == h &&
            sub >= PORTRAIT_MOUNT[i].min_sub) {
            out->portrait_mounted = 1;
            break;
        }
    }
    out->max_frame_bytes = 450000;
}

int wire_angle(const profile_t *p, int orientation)
{
    /* calib_deg is measured on the glass; the table base is the vendor app's
     * behaviour, which is 180° off on some panels (see calib.c). */
    int calib = ((p->calib_deg % 360) + 360) % 360;
    if (p->rotate) {
        int signed_term = p->encode_invert ? -orientation : orientation;
        return ((p->encode_base + calib + signed_term) % 360 + 360) % 360;
    }
    if (orientation && !p->portrait_mounted)
        return (360 - orientation + calib) % 360;
    return calib;
}

void oriented_canvas(const profile_t *p, int orientation, int *w, int *h)
{
    /* Landscape is (long, short), portrait (short, long) — the C# canvas for
     * a user picture at 90/270 on a non-square rotate panel is portrait. */
    int long_ = p->width > p->height ? p->width : p->height;
    int short_ = p->width > p->height ? p->height : p->width;

    if (p->rotate && p->width != p->height && (orientation == 90 || orientation == 270)) {
        *w = short_;
        *h = long_;
        return;
    }
    if (orientation == 180) {
        *w = p->width;
        *h = p->height;
        return;
    }
    *w = p->width;
    *h = p->height;
}

void build_header(uint8_t *hdr, uint32_t cmd, uint32_t w, uint32_t h,
                  uint32_t payload_len)
{
    memset(hdr, 0, USBLCD_HDR_SIZE);
    hdr[0] = 0x12; hdr[1] = 0x34; hdr[2] = 0x56; hdr[3] = 0x78;
    uint32_t v;
    v = cmd;         memcpy(hdr + 4, &v, 4);
    v = w;           memcpy(hdr + 8, &v, 4);
    v = h;           memcpy(hdr + 12, &v, 4);
    v = 2;           memcpy(hdr + 56, &v, 4);
    v = payload_len; memcpy(hdr + 60, &v, 4);
}
