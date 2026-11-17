/* SPDX-License-Identifier: Apache-2.0
 * Copyright 2026 Prokopiev; see LICENSE and NOTICE in the repository root. */
/* calib.c — measured-on-glass rotation offsets, stored per panel.
 *
 * Why this exists: the vendor app's rotation table (which we port) is known to
 * disagree with real panels by 180° — the reference project's own issues
 * #203/#169/#171 are that disagreement.  So the catalogued base angle is a
 * starting point, not a promise, and the answer that matters is the one you
 * measured on your own glass.
 *
 * File format (one panel per line, $XDG_CONFIG_HOME/usblcd/calibration):
 *
 *   87ad:70db <serial> pm=10 sub=7 base=180
 *
 * Where the file lives, in lookup order:
 *
 *   $USBLCD_CALIB                     explicit path, for services and images
 *   $XDG_CONFIG_HOME/usblcd/calibration
 *   $HOME/.config/usblcd/calibration
 *   /etc/usblcd/calibration           system-wide, for a daemon with no HOME
 *
 * The service case is the reason for the last two: a unit running as `nobody`
 * has no HOME, so per-user config is unreachable and the panel would silently
 * fall back to the catalogued angle - the exact disagreement this file exists
 * to correct.
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

#include "usblcd.h"

#define CALIB_MAGIC "# usblcd-calibration v1"

static char path_buf[512];

const char *calib_path(void)
{
    static const char *sys = "/etc/usblcd/calibration";
    const char *env, *xdg, *home;

    /* Recomputed on every call, not cached: the lookup order is short and a
     * cached answer makes the path untestable in one process, and makes a tool
     * that changes HOME (services, wrappers) report the wrong file. */
    env = getenv("USBLCD_CALIB");
    if (env && *env) {
        snprintf(path_buf, sizeof(path_buf), "%s", env);
        return path_buf;
    }
    xdg = getenv("XDG_CONFIG_HOME");
    if (xdg && *xdg)
        snprintf(path_buf, sizeof(path_buf), "%s/usblcd/calibration", xdg);
    else {
        home = getenv("HOME");
        if (home && *home)
            snprintf(path_buf, sizeof(path_buf), "%s/.config/usblcd/calibration",
                     home);
        else
            snprintf(path_buf, sizeof(path_buf), "%s", sys);
    }
    return path_buf;
}

/* Read one line's key + fields.  Returns 1 when the line is a panel entry. */
static int parse_line(const char *line, unsigned *vid, unsigned *pid, char *ser,
                      size_t serlen, int *pm, int *sub, int *deg, int *orient)
{
    (void)serlen;              /* the scanf field width is fixed at 127 */
    int n = sscanf(line, "%4x:%4x %127s pm=%d sub=%d base=%d",
                   vid, pid, ser, pm, sub, deg);
    if (n != 6)
        return 0;
    *orient = -1;
    const char *o = strstr(line, "orient=");
    if (o)
        *orient = !strncmp(o + 7, "cw", 2) ? 1 : !strncmp(o + 7, "ccw", 3) ? 0 : -1;
    return 1;
}

int calib_lookup(uint16_t vid, uint16_t pid, const char *serial,
                 int pm, int sub, int *deg, int *orient)
{
    FILE *fp = fopen(calib_path(), "r");
    if (!fp)
        return 0;
    char line[256];
    int found = 0;
    if (deg) *deg = 0;
    if (orient) *orient = -1;
    while (fgets(line, sizeof(line), fp)) {
        unsigned v = 0, p = 0;
        char ser[128] = {0};
        int lpm = -1, lsub = -1, lbase = 0, lorient = -1;
        if (!parse_line(line, &v, &p, ser, sizeof(ser), &lpm, &lsub,
                        &lbase, &lorient))
            continue;
        if (v == vid && p == pid && lpm == pm && lsub == sub &&
            (!ser[0] || !strcmp(ser, serial ? serial : ""))) {
            if (deg)    *deg = ((lbase % 360) + 360) % 360;
            if (orient) *orient = lorient;
            found = 1;
            break;
        }
    }
    fclose(fp);
    return found;
}

int calib_save(uint16_t vid, uint16_t pid, const char *serial,
               int pm, int sub, int deg, int orient)
{
    char dir[512];
    snprintf(dir, sizeof(dir), "%s", calib_path());
    char *slash = strrchr(dir, '/');
    if (slash) {
        *slash = 0;
        mkdir(dir, 0755);                    /* existing dirs are a no-op */
    }

    FILE *in = fopen(calib_path(), "r");
    char tmp[560];
    snprintf(tmp, sizeof(tmp), "%s.tmp%d", calib_path(), (int)getpid());
    FILE *out = fopen(tmp, "w");
    if (!out) {
        fprintf(stderr, "usblcd: cannot write %s\n", calib_path());
        if (in)
            fclose(in);
        return -1;
    }
    fprintf(out, "%s\n", CALIB_MAGIC);

    char line[256];
    int keep_deg = deg, keep_orient = orient;
    while (in && fgets(line, sizeof(line), in)) {
        unsigned v = 0, p = 0;
        char ser[128] = {0};
        int lpm = -1, lsub = -1, lbase = 0, lorient = -1;
        if (parse_line(line, &v, &p, ser, sizeof(ser), &lpm, &lsub,
                       &lbase, &lorient) &&
            v == vid && p == pid && lpm == pm && lsub == sub &&
            (!ser[0] || !strcmp(ser, serial ? serial : ""))) {
            /* This panel's existing line: carry over whatever we are not
             * changing, so `--orient` alone never loses a measured base. */
            if (keep_deg < 0)    keep_deg = lbase;
            if (keep_orient < 0) keep_orient = lorient;
            continue;
        }
        if (line[0] != '#')
            fputs(line, out);
    }
    if (in)
        fclose(in);
    if (keep_deg < 0) keep_deg = 0;

    fprintf(out, "%04x:%04x %s pm=%d sub=%d base=%d", vid, pid,
            serial && *serial ? serial : "-", pm, sub,
            ((keep_deg % 360) + 360) % 360);
    if (keep_orient == 1)  fputs(" orient=cw\n", out);
    else if (keep_orient == 0) fputs(" orient=ccw\n", out);
    else fputc('\n', out);
    fclose(out);
    chmod(tmp, 0644);
    if (rename(tmp, calib_path()) != 0) {
        fprintf(stderr, "usblcd: cannot move %s into place\n", tmp);
        return -1;
    }
    return 0;
}
