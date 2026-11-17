/* SPDX-License-Identifier: Apache-2.0
 * Copyright 2026 Prokopiev; see LICENSE and NOTICE in the repository root. */
/* test_encode.c — wire encoders, checked offline (no panel needed). */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "usblcd.h"

static int fails;

static void check(const char *what, long got, long want)
{
    if (got != want) {
        printf("FAIL %s: got %ld want %ld\n", what, got, want);
        fails++;
    }
}

static img_t *solid(int w, int h, uint8_t r, uint8_t g, uint8_t b)
{
    return img_fill(w, h, r, g, b);
}

int main(void)
{
    /* ── RGB565 packing ─────────────────────────────────────────────── */
    size_t len = 0;
    img_t *red = solid(1, 1, 255, 0, 0);
    uint8_t *p = encode_rgb565(red, 0, 0, &len);
    check("rgb565 len", (long)len, 2);
    check("rgb565 red LE lo", p[0], 0x00);
    check("rgb565 red LE hi", p[1], 0xf8);
    free(p);

    img_t *green = solid(1, 1, 0, 255, 0);
    p = encode_rgb565(green, 0, 0, &len);
    check("rgb565 green LE lo", p[0], 0xE0);
    check("rgb565 green LE hi", p[1], 0x07);
    free(p);

    img_t *blue = solid(1, 1, 0, 0, 255);
    p = encode_rgb565(blue, 1, 0, &len);
    check("rgb565 blue BE hi", p[0], 0x00);
    check("rgb565 blue BE lo", p[1], 0x1F);
    free(p);
    img_free(red); img_free(green); img_free(blue);

    /* Wider framebuffer row: 2px frame centred in a 4px row (640x172 panel). */
    img_t *two = solid(2, 1, 255, 255, 255);
    p = encode_rgb565(two, 0, 4, &len);
    check("rgb565 padded len", (long)len, 8);
    check("rgb565 pad left zeros", p[0], 0x00);
    check("rgb565 pad left zeros2", p[1], 0x00);
    check("rgb565 padded pixel", p[2], 0xFF);
    free(p);
    img_free(two);

    /* ── JPEG: 4:4:4 sampling, and the firmware byte ceiling ─────────── */
    img_t *img = solid(64, 64, 10, 200, 30);
    p = encode_jpeg(img, 95, 0, &len);
    if (!p) {
        printf("FAIL jpeg: encoder returned NULL\n");
        fails++;
    } else {
        check("jpeg soi", p[0] == 0xff && p[1] == 0xd8, 1);
        /* Walk the markers to SOF0 and read the sampling factors. */
        int sof = -1;
        for (size_t i = 2; i + 9 < len; ) {
            if (p[i] != 0xff) { i++; continue; }
            uint8_t m = p[i + 1];
            if (m >= 0xc0 && m <= 0xcf && m != 0xc4 && m != 0xc8 && m != 0xcc) {
                sof = (int)i;
                break;
            }
            if (m == 0xd8 || m == 0x01 || (m >= 0xd0 && m <= 0xd7)) { i += 2; continue; }
            i += 2 + ((p[i + 2] << 8) | p[i + 3]);
        }
        if (sof < 0) {
            printf("FAIL jpeg: no SOF segment\n");
            fails++;
        } else {
            check("jpeg sof height", (p[sof + 5] << 8) | p[sof + 6], 64);
            check("jpeg sof width",  (p[sof + 7] << 8) | p[sof + 8], 64);
            int ncomp = p[sof + 9];
            check("jpeg sof components", ncomp, 3);
            for (int c = 0; c < ncomp; c++)
                check("jpeg sof 4:4:4 sampling", p[sof + 11 + c * 3], 0x11);
        }
        free(p);
    }
    img_free(img);

    /* Worst case: high-entropy noise at the live panel's size must shrink to
     * under the firmware ceiling (the frame the panel silently drops, #251). */
    img_t *n = calloc(1, sizeof(img_t));
    n->w = 960;
    n->h = 540;
    n->px = malloc((size_t)n->w * n->h * 4);
    srandom(1234);
    for (int i = 0; i < n->w * n->h * 4; i += 4) {
        n->px[i + 0] = (uint8_t)(random() & 0xff);
        n->px[i + 1] = (uint8_t)(random() & 0xff);
        n->px[i + 2] = (uint8_t)(random() & 0xff);
        n->px[i + 3] = 255;
    }
    img_t *noise = n;
    p = encode_jpeg(noise, 95, 450000, &len);
    if (!p) {
        printf("FAIL jpeg ceiling: encoder returned NULL\n");
        fails++;
    } else {
        printf("jpeg noise payload at 960x540: %zu bytes (ceiling 450000)\n", len);
        check("jpeg ceiling respected", (long)(len <= 450000), 1);
        free(p);
    }
    free(noise->px);
    free(noise);

    /* ── rotation ───────────────────────────────────────────────────── */
    /* Pixel-exact rotation on a NON-square image: the 270° path used to read
     * row/col the other way round, which runs off the source buffer as soon
     * as width != height (the garbled picture that `calibrate` measured). */
    enum { RED = 0, GREEN, BLUE, YELLOW, CYAN, MAGENTA };
    static const uint8_t cols[6][3] = {
        {255,0,0}, {0,255,0}, {0,0,255}, {255,255,0}, {0,255,255}, {255,0,255}
    };
    img_t *grid = img_new(3, 2);
    for (int y = 0; y < 2; y++)
        for (int x = 0; x < 3; x++) {
            int id = y * 3 + x;
            memcpy(grid->px + ((size_t)y * 3 + x) * 4, cols[id], 3);
            grid->px[((size_t)y * 3 + x) * 4 + 3] = 255;
        }

    /* 90 CW canonical: source (sx,sy) -> dst(H-1-sy, sx), i.e.
     * dst(nx,ny) = src(ny, H-1-nx).  Grid is 3x2, dst is 2x3. */
    img_t *g90 = img_rotate(grid, 90);
    check("90cw w", g90->w, 2);
    check("90cw h", g90->h, 3);
    const int want90[6] = { YELLOW, RED, CYAN, GREEN, MAGENTA, BLUE };
    /* dst(0,0)=YELLOW  dst(1,0)=RED    dst(0,1)=CYAN
       dst(1,1)=GREEN  dst(0,2)=MAGENTA dst(1,2)=BLUE */
    for (int i = 0; i < 6; i++) {
        int x = i % 2, y = i / 2;
        const uint8_t *c = cols[want90[i]];
        const uint8_t *p = g90->px + ((size_t)y * g90->w + x) * 4;
        char name[64];
        snprintf(name, sizeof(name), "90cw pixel(%d,%d) r", x, y);
        check(name, p[0], c[0]);
        snprintf(name, sizeof(name), "90cw pixel(%d,%d) g", x, y);
        check(name, p[1], c[1]);
        snprintf(name, sizeof(name), "90cw pixel(%d,%d) b", x, y);
        check(name, p[2], c[2]);
    }
    img_free(g90);

    /* 270 CW canonical: source (sx,sy) -> dst(sy, W-1-sx), i.e.
     * dst(nx,ny) = src(W-1-ny, nx). */
    img_t *g270 = img_rotate(grid, 270);
    check("270cw w", g270->w, 2);
    check("270cw h", g270->h, 3);
    const int want270[6] = { BLUE, MAGENTA, GREEN, CYAN, RED, YELLOW };
    /* dst(0,0)=BLUE   dst(1,0)=MAGENTA dst(0,1)=GREEN
       dst(1,1)=CYAN   dst(0,2)=RED     dst(1,2)=YELLOW */
    for (int i = 0; i < 6; i++) {
        int x = i % 2, y = i / 2;
        const uint8_t *c = cols[want270[i]];
        const uint8_t *p = g270->px + ((size_t)y * g270->w + x) * 4;
        char name[64];
        snprintf(name, sizeof(name), "270cw pixel(%d,%d) r", x, y);
        check(name, p[0], c[0]);
        snprintf(name, sizeof(name), "270cw pixel(%d,%d) g", x, y);
        check(name, p[1], c[1]);
        snprintf(name, sizeof(name), "270cw pixel(%d,%d) b", x, y);
        check(name, p[2], c[2]);
    }
    img_free(g270);

    /* 90 then 270 must be the identity (catches a swapped row/col copy). */
    img_t *a = img_rotate(grid, 90);
    img_t *b = img_rotate(a, 270);
    check("90+270 w", b->w, 3);
    check("90+270 h", b->h, 2);
    for (int y = 0; y < 2; y++)
        for (int x = 0; x < 3; x++) {
            const uint8_t *p = b->px + ((size_t)y * 3 + x) * 4;
            const uint8_t *q = grid->px + ((size_t)y * 3 + x) * 4;
            char name[64];
            snprintf(name, sizeof(name), "90+270 identity(%d,%d)", x, y);
            check(name, p[0] * 65536 + p[1] * 256 + p[2],
                  q[0] * 65536 + q[1] * 256 + q[2]);
        }
    img_free(b); img_free(a);
    img_free(grid);

    img_t *strip = img_new(3, 1);
    strip->px[0] = 255; strip->px[1] = 0;  strip->px[2] = 0;  strip->px[3] = 255;
    strip->px[4] = 0;   strip->px[5] = 255;strip->px[6] = 0;  strip->px[7] = 255;
    strip->px[8] = 0;   strip->px[9] = 0;  strip->px[10] = 255;strip->px[11] = 255;

    img_t *r90 = img_rotate(strip, 90);
    check("rot90 w", r90->w, 1);
    check("rot90 h", r90->h, 3);
    check("rot90 top pixel is red", r90->px[0], 255);
    check("rot90 top pixel green", r90->px[1], 0);
    check("rot90 bottom pixel is blue", r90->px[10], 255);
    img_free(r90);

    img_t *r180 = img_rotate(strip, 180);
    check("rot180 w", r180->w, 3);
    check("rot180 first pixel is blue", r180->px[8], 255);
    img_free(r180);

    img_t *r270 = img_rotate(strip, 270);
    check("rot270 w", r270->w, 1);
    check("rot270 top pixel is blue", r270->px[2], 255);
    img_free(r270);
    img_free(strip);

    /* ── resize modes ───────────────────────────────────────────────── */
    img_t *src = solid(200, 100, 255, 255, 255);
    img_t *fit = img_resize(src, 100, 100, 0);
    check("fit keeps aspect (w)", fit->w, 100);
    check("fit keeps aspect (h)", fit->h, 100);
    check("fit letterboxes top row", fit->px[0], 0);
    check("fit keeps the picture centre", fit->px[((size_t)50 * 100 + 50) * 4], 255);
    img_free(fit);
    img_t *fill = img_resize(src, 100, 100, 1);
    check("fill covers centre", fill->px[((size_t)50 * 100 + 50) * 4], 255);
    img_free(fill);
    img_free(src);

    /* ── brightness ─────────────────────────────────────────────────── */
    img_t *half = solid(1, 1, 200, 100, 50);
    img_brightness(half, 50);
    check("brightness 50% red", half->px[0], 100);
    check("brightness 50% green", half->px[1], 50);
    img_free(half);

    if (fails) {
        printf("%d check(s) failed\n", fails);
        return 1;
    }
    printf("encoders OK\n");
    return 0;
}
