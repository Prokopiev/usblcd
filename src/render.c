/* SPDX-License-Identifier: Apache-2.0
 * Copyright 2026 Prokopiev; see LICENSE and NOTICE in the repository root. */
/* render.c — image decode, geometry, rotation, brightness, wire encoders.
 *
 * Pixels are RGBA8888 internally.  Wire payloads are JPEG (libjpeg, 4:4:4
 * chroma like the reference's TurboJPEG path) or raw RGB565.
 */
#define STB_IMAGE_IMPLEMENTATION
#define STBI_ONLY_JPEG
#define STBI_ONLY_PNG
#define STBI_ONLY_BMP
#define STBI_ONLY_TGA
#define STBI_ONLY_PNM
#include "stb_image.h"

#include <jpeglib.h>

#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "usblcd.h"

#define JPEG_INITIAL_CAP 65536

/* ── buffers ────────────────────────────────────────────────────────── */

img_t *img_new(int w, int h)
{
    if (w <= 0 || h <= 0)
        return NULL;
    img_t *im = calloc(1, sizeof(*im));
    if (!im)
        return NULL;
    im->px = calloc((size_t)w * h, 4);
    if (!im->px) {
        free(im);
        return NULL;
    }
    im->w = w;
    im->h = h;
    return im;
}

void img_free(img_t *img)
{
    if (!img)
        return;
    free(img->px);
    free(img);
}

img_t *img_fill(int w, int h, uint8_t r, uint8_t g, uint8_t b)
{
    img_t *im = img_new(w, h);
    if (!im)
        return NULL;
    for (size_t i = 0; i < (size_t)w * h; i++) {
        im->px[i * 4 + 0] = r;
        im->px[i * 4 + 1] = g;
        im->px[i * 4 + 2] = b;
        im->px[i * 4 + 3] = 255;
    }
    return im;
}

img_t *img_marker(int w, int h)
{
    /* Orientation marker with no rotational symmetry, so four edges and a
     * corner are unambiguous: TOP white, BOTTOM blue, LEFT yellow, RIGHT
     * cyan, magenta square in the bottom-right corner, bars + ramp inside. */
    img_t *im = img_new(w, h);
    if (!im)
        return NULL;
    int bar = h / 15;  if (bar < 8) bar = 8;
    int sq  = h / 4;   if (sq > w / 4) sq = w / 4;

    uint8_t white[3] = {255,255,255}, blue[3] = {0,0,255};
    uint8_t yellow[3]= {255,255,0},  cyan[3] = {0,255,255};
    uint8_t mag[3]   = {255,0,255};

    for (int y = 0; y < h; y++) {
        for (int x = 0; x < w; x++) {
            uint8_t *p = im->px + ((size_t)y * w + x) * 4;
            p[3] = 255;
            int in_top = y < bar, in_bot = y >= h - bar;
            int in_left = x < bar, in_right = x >= w - bar;
            int in_sq = (y >= h - bar - sq && y < h - bar &&
                         x >= w - bar - sq && x < w - bar);
            if (in_top)       { p[0]=white[0]; p[1]=white[1]; p[2]=white[2]; }
            else if (in_bot)  { p[0]=blue[0];  p[1]=blue[1];  p[2]=blue[2];  }
            else if (in_left) { p[0]=yellow[0];p[1]=yellow[1];p[2]=yellow[2];}
            else if (in_right){ p[0]=cyan[0];  p[1]=cyan[1];  p[2]=cyan[2];  }
            else if (in_sq)   { p[0]=mag[0];   p[1]=mag[1];   p[2]=mag[2];   }
            else {
                /* 8 bars in the upper middle, grey ramp below them */
                int inner_h = h - 2*bar, inner_w = w - 2*bar;
                int rel_y = y - bar, rel_x = x - bar;
                if (rel_y * 2 < inner_h) {
                    int i = rel_x * 8 / inner_w;
                    p[0] = (i & 1) ? 255 : 0;
                    p[1] = (i & 2) ? 255 : 0;
                    p[2] = (i & 4) ? 255 : 0;
                } else {
                    uint8_t v = (uint8_t)(rel_x * 255 / (inner_w - 1 > 0 ? inner_w - 1 : 1));
                    p[0] = p[1] = p[2] = v;
                }
            }
        }
    }
    return im;
}

img_t *img_test_pattern(int w, int h)
{
    img_t *im = img_new(w, h);
    if (!im)
        return NULL;
    for (int y = 0; y < h; y++) {
        for (int x = 0; x < w; x++) {
            uint8_t *p = im->px + ((size_t)y * w + x) * 4;
            p[3] = 255;
            /* 8-way colour bars on top, grayscale ramp below. */
            if (y * 2 < h) {
                int bar = x * 8 / w;
                p[0] = (bar & 1) ? 255 : 0;
                p[1] = (bar & 2) ? 255 : 0;
                p[2] = (bar & 4) ? 255 : 0;
                if (bar == 7) { p[0] = p[1] = p[2] = 255; }
            } else {
                uint8_t v = (uint8_t)(x * 255 / (w > 1 ? w - 1 : 1));
                p[0] = p[1] = p[2] = v;
            }
            /* 1px crosshair + border, to show the frame edges */
            if (x == 0 || y == 0 || x == w - 1 || y == h - 1)
                p[0] = p[1] = p[2] = 255;
            if (x == w / 2 || y == h / 2) {
                p[0] = (uint8_t)(255 - p[0]);
                p[1] = (uint8_t)(255 - p[1]);
                p[2] = (uint8_t)(255 - p[2]);
            }
        }
    }
    return im;
}

img_t *img_load(const char *path, char *err, size_t errlen)
{
    int w = 0, h = 0, n = 0;
    stbi_set_flip_vertically_on_load(0);
    unsigned char *px = stbi_load(path, &w, &h, &n, 4);
    if (!px) {
        if (errlen)
            snprintf(err, errlen, "%s", stbi_failure_reason() ?
                     stbi_failure_reason() : "decode failed");
        return NULL;
    }
    img_t *im = img_new(w, h);
    if (!im) {
        stbi_image_free(px);
        return NULL;
    }
    memcpy(im->px, px, (size_t)w * h * 4);
    stbi_image_free(px);
    if (errlen)
        err[0] = 0;
    return im;
}

/* Bilinear sample of src at fractional (fx, fy). */
static void sample(const img_t *s, double fx, double fy, uint8_t out[4])
{
    double px0 = fx - 0.5, py0 = fy - 0.5;
    if (px0 < 0) px0 = 0;
    if (py0 < 0) py0 = 0;
    int x0 = (int)px0, y0 = (int)py0;
    int x1 = x0 + 1 < s->w ? x0 + 1 : s->w - 1;
    int y1 = y0 + 1 < s->h ? y0 + 1 : s->h - 1;
    double tx = px0 - x0, ty = py0 - y0;
    const uint8_t *a = s->px + ((size_t)y0 * s->w + x0) * 4;
    const uint8_t *b = s->px + ((size_t)y0 * s->w + x1) * 4;
    const uint8_t *c = s->px + ((size_t)y1 * s->w + x0) * 4;
    const uint8_t *d = s->px + ((size_t)y1 * s->w + x1) * 4;
    for (int i = 0; i < 4; i++) {
        double top = a[i] + (b[i] - a[i]) * tx;
        double bot = c[i] + (d[i] - c[i]) * tx;
        double v = top + (bot - top) * ty;
        if (v < 0) v = 0;
        if (v > 255) v = 255;
        out[i] = (uint8_t)(v + 0.5);
    }
}

img_t *img_resize(img_t *src, int w, int h, int mode)
{
    if (src->w == w && src->h == h) {
        img_t *copy = img_new(w, h);
        if (copy)
            memcpy(copy->px, src->px, (size_t)w * h * 4);
        return copy;
    }

    double sx, sy, ox = 0, oy = 0;
    int src_w = src->w, src_h = src->h;

    if (mode == 2) {                                   /* stretch */
        sx = (double)src->w / w;
        sy = (double)src->h / h;
    } else if (mode == 1) {                            /* cover + crop */
        double k = (double)w / h - (double)src->w / src->h;
        if (k > 0) {            /* source is taller: crop vertically */
            int vis_h = (int)(src->w * h / (double)w + 0.5);
            oy = (src->h - vis_h) / 2.0;
            sx = (double)src->w / w;
            sy = (double)vis_h / h;
        } else {                /* crop horizontally */
            int vis_w = (int)(src->h * w / (double)h + 0.5);
            ox = (src->w - vis_w) / 2.0;
            sx = (double)vis_w / w;
            sy = (double)src->h / h;
        }
        if (sx <= 0) sx = 1;
        if (sy <= 0) sy = 1;
    } else {                                           /* fit + letterbox */
        double k = (double)w / h - (double)src->w / src->h;
        int cw, ch;
        if (k > 0) { ch = h;  cw = (int)(src->w * h / (double)src->h + 0.5); }
        else       { cw = w;  ch = (int)(src->h * w / (double)src->w + 0.5); }
        sx = (double)src->w / cw;
        sy = (double)src->h / ch;
        ox = (w - cw) / 2.0;
        oy = (h - ch) / 2.0;
        img_t *dst = img_new(w, h);
        if (!dst)
            return NULL;
        for (int y = 0; y < h; y++) {
            for (int x = 0; x < w; x++) {
                uint8_t c[4];
                int inside = x >= ox && x < ox + cw && y >= oy && y < oy + ch;
                if (inside)
                    sample(src, (x - ox) * sx + 0.5, (y - oy) * sy + 0.5, c);
                else
                    c[0] = c[1] = c[2] = 0, c[3] = 255;
                memcpy(dst->px + ((size_t)y * w + x) * 4, c, 4);
            }
        }
        (void)src_w; (void)src_h;
        return dst;
    }

    img_t *dst = img_new(w, h);
    if (!dst)
        return NULL;
    for (int y = 0; y < h; y++) {
        for (int x = 0; x < w; x++) {
            uint8_t c[4];
            sample(src, ox + x * sx + 0.5, oy + y * sy + 0.5, c);
            memcpy(dst->px + ((size_t)y * w + x) * 4, c, 4);
        }
    }
    return dst;
}

img_t *img_rotate(img_t *src, int deg)
{
    switch ((deg % 360 + 360) % 360) {
    case 0:
        return img_resize(src, src->w, src->h, 2);
    case 180: {
        img_t *d = img_new(src->w, src->h);
        if (!d) return NULL;
        for (int y = 0; y < src->h; y++)
            for (int x = 0; x < src->w; x++)
                memcpy(d->px + ((size_t)y * src->w + x) * 4,
                       src->px + ((size_t)(src->h - 1 - y) * src->w
                                   + (src->w - 1 - x)) * 4, 4);
        return d;
    }
    case 90: {
        img_t *d = img_new(src->h, src->w);
        if (!d) return NULL;
        for (int y = 0; y < d->h; y++)
            for (int x = 0; x < d->w; x++)
                memcpy(d->px + ((size_t)y * d->w + x) * 4,
                       src->px + ((size_t)(d->w - 1 - x) * src->w + y) * 4, 4);
        return d;
    }
    case 270:
    default: {
        /* 270 CW == 90 CCW: dst(nx,ny) = src(sy = nx, sx = src_w-1-ny).
         * Getting row/col the other way round walks off the end of the
         * source buffer as soon as the image is not square. */
        img_t *d = img_new(src->h, src->w);
        if (!d) return NULL;
        for (int y = 0; y < d->h; y++)
            for (int x = 0; x < d->w; x++)
                memcpy(d->px + ((size_t)y * d->w + x) * 4,
                       src->px + ((size_t)x * src->w + (src->w - 1 - y)) * 4, 4);
        return d;
    }
    }
}

void img_brightness(img_t *img, int percent)
{
    if (percent >= 100)
        return;
    if (percent < 0)
        percent = 0;
    size_t n = (size_t)img->w * img->h;
    for (size_t i = 0; i < n; i++) {
        uint8_t *p = img->px + i * 4;
        p[0] = (uint8_t)(p[0] * percent / 100);
        p[1] = (uint8_t)(p[1] * percent / 100);
        p[2] = (uint8_t)(p[2] * percent / 100);
    }
}

/* ── JPEG ───────────────────────────────────────────────────────────── */

struct mem_dest {
    struct jpeg_destination_mgr mgr;
    uint8_t *buf;
    size_t   cap, len;
};

static struct mem_dest *to_mem(j_compress_ptr cinfo)
{
    return (struct mem_dest *)cinfo->dest;
}

static void mem_init(j_compress_ptr cinfo)
{
    struct mem_dest *md = to_mem(cinfo);
    if (!md->buf) {
        md->buf = malloc(JPEG_INITIAL_CAP);
        if (!md->buf) {
            md->cap = 0;
            md->mgr.next_output_byte = NULL;
            md->mgr.free_in_buffer = 0;
            return;
        }
        md->cap = JPEG_INITIAL_CAP;
    }
    md->len = 0;
    /* libjpeg decrements free_in_buffer BEFORE checking it, so it must
     * start non-zero and point at real memory. */
    md->mgr.next_output_byte = md->buf;
    md->mgr.free_in_buffer = md->cap;
}

static boolean mem_empty(j_compress_ptr cinfo)
{
    struct mem_dest *md = to_mem(cinfo);
    md->len = md->cap;                       /* the buffer is full */
    size_t need = md->cap * 2;
    uint8_t *b = realloc(md->buf, need);
    if (!b)
        return FALSE;
    md->buf = b;
    md->cap = need;
    md->mgr.next_output_byte = b + md->len;
    md->mgr.free_in_buffer = need - md->len;
    return TRUE;
}

static void mem_term(j_compress_ptr cinfo)
{
    struct mem_dest *md = to_mem(cinfo);
    md->len = md->cap - md->mgr.free_in_buffer;
}

/* Encode one image to malloc'd JPEG bytes.  samp: 1 = 4:4:4, 2 = 4:2:0. */
static uint8_t *jpeg_encode(const img_t *img, int quality, int samp, size_t *out_len)
{
    struct jpeg_compress_struct cinfo;
    struct jpeg_error_mgr err;
    struct mem_dest dest;

    cinfo.err = jpeg_std_error(&err);
    jpeg_create_compress(&cinfo);

    memset(&dest, 0, sizeof(dest));
    dest.mgr.init_destination    = mem_init;
    dest.mgr.empty_output_buffer = mem_empty;
    dest.mgr.term_destination    = mem_term;
    cinfo.dest = &dest.mgr;

    cinfo.image_width = img->w;
    cinfo.image_height = img->h;
    cinfo.input_components = 3;
    cinfo.in_color_space = JCS_RGB;
    jpeg_set_defaults(&cinfo);
    jpeg_set_quality(&cinfo, quality, TRUE);
    cinfo.optimize_coding = TRUE;
    cinfo.dct_method = JDCT_ISLOW;

    for (int i = 0; i < cinfo.num_components; i++) {
        int s = (i == 0 && samp == 2) ? 2 : 1;
        cinfo.comp_info[i].h_samp_factor = s;
        cinfo.comp_info[i].v_samp_factor = s;
    }
    cinfo.max_h_samp_factor = samp == 2 ? 2 : 1;
    cinfo.max_v_samp_factor = samp == 2 ? 2 : 1;

    jpeg_start_compress(&cinfo, TRUE);

    JSAMPROW row = malloc((size_t)img->w * 3);
    if (!row) {
        jpeg_destroy_compress(&cinfo);
        free(dest.buf);
        return NULL;
    }
    while (cinfo.next_scanline < cinfo.image_height) {
        const uint8_t *src = img->px + (size_t)cinfo.next_scanline * img->w * 4;
        uint8_t *dst = row;
        for (int x = 0; x < img->w; x++) {
            dst[0] = src[0];
            dst[1] = src[1];
            dst[2] = src[2];
            dst += 3;
            src += 4;
        }
        jpeg_write_scanlines(&cinfo, &row, 1);
    }
    free(row);
    jpeg_finish_compress(&cinfo);
    jpeg_destroy_compress(&cinfo);

    *out_len = dest.len;
    return dest.buf;
}

uint8_t *encode_jpeg(const img_t *img, int quality, int max_size, size_t *len)
{
    static const int ladder[] = { 95, 85, 75, 60, 45, 30 };
    const int nladder = (int)(sizeof(ladder) / sizeof(ladder[0]));
    int start = 0;
    while (start < nladder && ladder[start] > quality)
        start++;

    for (int pass = 0; pass < 2; pass++) {
        int samp = pass == 0 ? 1 : 2;
        int from = pass == 0 ? start : 3;      /* 4:2:0 tries 60,45,30 */
        for (int i = from; i < nladder; i++) {
            if (pass == 1 && ladder[i] > 60)
                continue;
            size_t n = 0;
            uint8_t *j = jpeg_encode(img, ladder[i], samp, &n);
            if (!j)
                return NULL;
            if (!max_size || (int)n <= max_size) {
                *len = n;
                return j;
            }
            free(j);
        }
    }
    fprintf(stderr, "usblcd: JPEG frame exceeds %d bytes even at q=30; "
            "panel may drop it\n", max_size);
    *len = 0;
    return NULL;
}

/* ── RGB565 ─────────────────────────────────────────────────────────── */

uint8_t *encode_rgb565(const img_t *img, int big_endian, int wire_row_px, size_t *len)
{
    int w = img->w, h = img->h;
    int row = w * 2;
    int stride = wire_row_px > w ? wire_row_px * 2 : row;

    uint8_t *out = calloc((size_t)stride * h, 1);
    if (!out)
        return NULL;

    int off = (stride - row) / 2;
    for (int y = 0; y < h; y++) {
        const uint8_t *src = img->px + (size_t)y * w * 4;
        uint8_t *dst = out + (size_t)y * stride + off;
        for (int x = 0; x < w; x++) {
            int r = (src[0] * 31 + 127) / 255;
            int g = (src[1] * 63 + 127) / 255;
            int b = (src[2] * 31 + 127) / 255;
            uint16_t v = (uint16_t)((r << 11) | (g << 5) | b);
            if (big_endian) {
                dst[0] = (uint8_t)(v >> 8);
                dst[1] = (uint8_t)(v & 0xff);
            } else {
                dst[0] = (uint8_t)(v & 0xff);
                dst[1] = (uint8_t)(v >> 8);
            }
            dst += 2;
            src += 4;
        }
    }
    *len = (size_t)stride * h;
    return out;
}

/* ── small helpers ──────────────────────────────────────────────────── */

void hexdump(const uint8_t *p, size_t n, size_t show)
{
    size_t lim = n < show ? n : show;
    for (size_t i = 0; i < lim; i += 16) {
        printf("%04zx  ", i);
        for (size_t j = 0; j < 16; j++)
            printf(i + j < lim ? "%02x " : "   ", p[i + j]);
        printf(" |");
        for (size_t j = 0; j < 16 && i + j < lim; j++)
            putchar(p[i + j] >= 32 && p[i + j] < 127 ? p[i + j] : '.');
        printf("|\n");
    }
    if (n > lim)
        printf("... (%zu bytes total)\n", n);
}

int parse_color(const char *s, uint8_t out[3])
{
    unsigned r, g, b;
    if (s[0] == '#')
        s++;
    if (strlen(s) == 3) {
        if (sscanf(s, "%1x%1x%1x", &r, &g, &b) != 3)
            return -1;
        out[0] = (uint8_t)(r * 17);
        out[1] = (uint8_t)(g * 17);
        out[2] = (uint8_t)(b * 17);
        return 0;
    }
    if (sscanf(s, "%2x%2x%2x", &r, &g, &b) != 3)
        return -1;
    out[0] = (uint8_t)r;
    out[1] = (uint8_t)g;
    out[2] = (uint8_t)b;
    return 0;
}
