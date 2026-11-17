/* SPDX-License-Identifier: Apache-2.0
 * Copyright 2026 Prokopiev; see LICENSE and NOTICE in the repository root. */
/* ppm2png — tiny PPM(P6)/RAWGA -> PNG converter for previewing on a headless box.
 *
 *   ./ppm2png in.ppm out.png
 *   ./ppm2png - W H in.raw out.png          (raw RGB24 on stdin)
 *
 * The dashboard renderer emits PPM; this makes it viewable in any image
 * viewer, and `usblcd image` reads the PPM directly, so nothing here is on
 * the panel path.
 */
#define STB_IMAGE_WRITE_IMPLEMENTATION
#include "stb_image_write.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static int read_header(FILE *fp, int *w, int *h)
{
    char magic[3] = {0};
    if (fread(magic, 1, 2, fp) != 2 || magic[0] != 'P' || magic[1] != '6') {
        fprintf(stderr, "ppm2png: not a P6 binary PPM\n");
        return -1;
    }
    int vals[3] = {0}, n = 0;
    int in_number = 0;
    while (n < 3) {
        int c = fgetc(fp);
        if (c == EOF)
            return -1;
        if (c == '#') { while ((c = fgetc(fp)) != '\n' && c != EOF) ; continue; }
        if (c >= '0' && c <= '9') {
            vals[n] = vals[n] * 10 + (c - '0');
            in_number = 1;
        } else if (in_number) {
            n++;
            in_number = 0;
        }
    }
    *w = vals[0];
    *h = vals[1];
    if (vals[2] != 255) {
        fprintf(stderr, "ppm2png: maxval %d (want 255)\n", vals[2]);
        return -1;
    }
    return 0;
}

int main(int argc, char **argv)
{
    int raw = 0, w = 0, h = 0;
    int argi = 1;
    if (argc > 1 && !strcmp(argv[1], "-")) {
        raw = 1;
        if (argc < 5) {
            fprintf(stderr, "usage: ppm2png - W H IN.png-target (reads raw RGB24 on stdin)\n");
            return 2;
        }
        w = atoi(argv[2]);
        h = atoi(argv[3]);
        argi = 4;
    }
    if (argc - argi < 2) {
        fprintf(stderr, "usage: ppm2png IN.ppm OUT.png\n"
                        "       ppm2png - W H IN OUT.png   (raw RGB24)\n");
        return 2;
    }
    const char *in = argv[argi], *out = argv[argi + 1];

    FILE *fp = !strcmp(in, "-") ? stdin : fopen(in, "rb");
    if (!fp) {
        fprintf(stderr, "ppm2png: cannot read %s\n", in);
        return 1;
    }
    unsigned char *px = NULL;
    if (raw) {
        size_t need = (size_t)w * h * 3;
        px = malloc(need);
        if (!px || fread(px, 1, need, fp) != need) {
            fprintf(stderr, "ppm2png: short raw read (%dx%d)\n", w, h);
            return 1;
        }
    } else {
        if (read_header(fp, &w, &h) != 0)
            return 1;
        size_t need = (size_t)w * h * 3;
        px = malloc(need);
        if (!px || fread(px, 1, need, fp) != need) {
            fprintf(stderr, "ppm2png: short PPM pixel read (%dx%d)\n", w, h);
            return 1;
        }
    }
    if (!strcmp(in, "-"))
        fclose(fp);

    if (!stbi_write_png(out, w, h, 3, px, w * 3)) {
        fprintf(stderr, "ppm2png: cannot write %s\n", out);
        return 1;
    }
    printf("%s -> %s  (%dx%d)\n", in, out, w, h);
    free(px);
    return 0;
}
