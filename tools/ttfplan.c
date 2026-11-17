/* SPDX-License-Identifier: Apache-2.0
 * Copyright 2026 Prokopiev; see LICENSE and NOTICE in the repository root. */
/*
 * ttfplan — FreeType glyph coverage dumper for the dashboard renderer.
 *
 * dash.py draws text as rectangles of quantized coverage, so it needs, for
 * every character and every font size it uses, the antialiased coverage grid
 * that FreeType computes out of the TTF outlines (true hinting, real 8-bit
 * anti-aliasing).  This tool prints those grids in a compact binary form once
 * at startup; dash.py quantizes them into rect plans and caches them.
 *
 * Output, for each requested character:
 *   int32 codepoint, int32 w, int32 h, int32 left, int32 top, int32 advance
 *   followed by w*h bytes of coverage (0..255), row major
 * A character FreeType cannot render is reported with w = h = 0.
 *
 * usage: ttfplan FONT PIXEL_SIZE [BOLD] "characters"
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include <ft2build.h>
#include FT_FREETYPE_H
#include FT_BITMAP_H
#include FT_MULTIPLE_MASTERS_H

static int w_i32(FILE *fp, int v)
{
    unsigned char b[4];
    b[0] = (unsigned char)(v & 0xff);
    b[1] = (unsigned char)((v >> 8) & 0xff);
    b[2] = (unsigned char)((v >> 16) & 0xff);
    b[3] = (unsigned char)((v >> 24) & 0xff);
    return fwrite(b, 1, 4, fp) == 4 ? 0 : -1;
}

int main(int argc, char **argv)
{
    if (argc < 4) {
        fprintf(stderr, "usage: ttfplan FONT PIXEL_SIZE [BOLD] \"characters\"\n");
        return 2;
    }
    const char *path = argv[1];
    int px = atoi(argv[2]);
    int argi = 3;
    const char *chars = NULL;
    double wght = -1, opsz = -1;
    for (int i = 3; i < argc; i++) {
        if (!strncmp(argv[i], "wght=", 5))
            wght = atof(argv[i] + 5);
        else if (!strncmp(argv[i], "opsz=", 5))
            opsz = atof(argv[i] + 5);
        else
            chars = argv[i];
    }
    if (!chars) {
        fprintf(stderr, "usage: ttfplan FONT PIXEL_SIZE [wght=N] [opsz=N] \"characters\"\n");
        return 2;
    }

    FT_Library lib;
    if (FT_Init_FreeType(&lib)) {
        fprintf(stderr, "ttfplan: freetype init failed\n");
        return 1;
    }
    FT_Face face;
    if (FT_New_Face(lib, path, 0, &face)) {
        fprintf(stderr, "ttfplan: cannot load %s\n", path);
        FT_Done_FreeType(lib);
        return 1;
    }
    if (FT_Set_Pixel_Sizes(face, 0, (FT_UInt)px)) {
        fprintf(stderr, "ttfplan: cannot set pixel size %d\n", px);
        FT_Done_Face(face);
        FT_Done_FreeType(lib);
        return 1;
    }

    /* Variable fonts: Inter on this box ships as one file with wght/opsz
     * axes, so pick the design instance the dashboard asked for.  opsz is the
     * optical size axis, in points: a face tuned for 10 px text has fatter
     * strokes and more spacing than the same outline at 40 px. */
    FT_MM_Var *mm = NULL;
    if ((wght >= 0 || opsz >= 0) && !FT_Get_MM_Var(face, &mm) && mm && mm->num_axis) {
        FT_Fixed *coords = malloc(sizeof(FT_Fixed) * mm->num_axis);
        if (coords) {
            for (unsigned i = 0; i < mm->num_axis; i++)
                coords[i] = mm->axis[i].def;
            for (unsigned i = 0; i < mm->num_axis; i++) {
                unsigned tag = mm->axis[i].tag;
                if (wght >= 0 && tag == FT_MAKE_TAG('w', 'g', 'h', 't'))
                    coords[i] = (FT_Fixed)(wght * 65536.0);
                if (opsz >= 0 && tag == FT_MAKE_TAG('o', 'p', 's', 'z'))
                    coords[i] = (FT_Fixed)(opsz * 65536.0);
            }
            if (FT_Set_Var_Design_Coordinates(face, mm->num_axis, coords))
                fprintf(stderr, "ttfplan: %s has no variable instance to set\n", path);
            free(coords);
        }
        FT_Done_MM_Var(lib, mm);
    }

    FILE *fp = stdout;
    for (const unsigned char *p = (const unsigned char *)chars; *p; p++) {
        int ch = *p;
        if (FT_Load_Char(face, (FT_ULong)ch, FT_LOAD_RENDER | FT_LOAD_TARGET_NORMAL)) {
            w_i32(fp, ch); w_i32(fp, 0); w_i32(fp, 0); w_i32(fp, 0); w_i32(fp, 0); w_i32(fp, 0);
            continue;
        }
        FT_GlyphSlot g = face->glyph;
        FT_Bitmap *bm = &g->bitmap;
        int w = (int)bm->width, h = (int)bm->rows;
        if (w_i32(fp, ch) || w_i32(fp, w) || w_i32(fp, h) ||
            w_i32(fp, (int)g->bitmap_left) || w_i32(fp, (int)g->bitmap_top) ||
            w_i32(fp, (int)((g->advance.x + 32) >> 6))     /* 26.6 fixed point -> px */) {
            FT_Done_Face(face);
            FT_Done_FreeType(lib);
            return 1;
        }
        unsigned char row[512];
        for (int y = 0; y < h; y++) {
            const unsigned char *src = bm->buffer + (size_t)y * bm->pitch;
            int n = w > 512 ? 512 : w;
            for (int x = 0; x < n; x++)
                row[x] = src[x];
            if (fwrite(row, 1, (size_t)w, fp) != (size_t)w) {
                FT_Done_Face(face);
                FT_Done_FreeType(lib);
                return 1;
            }
        }
    }
    fflush(stdout);
    FT_Done_Face(face);
    FT_Done_FreeType(lib);
    return 0;
}
