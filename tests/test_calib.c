/* SPDX-License-Identifier: Apache-2.0
 * Copyright 2026 Prokopiev; see LICENSE and NOTICE in the repository root. */
/* test_calib.c — the measured-on-glass calibration store. */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

#include "usblcd.h"

static int fails;

static void check(const char *what, long got, long want)
{
    if (got != want) {
        printf("FAIL %s: got %ld want %ld\n", what, got, want);
        fails++;
    }
}

int main(void)
{
    char root[256];
    snprintf(root, sizeof(root), "/tmp/usblcd-calib-test-%d", (int)getpid());

    /* Path lookup, checked first because calib_path() caches its answer:
     * an explicit USBLCD_CALIB wins over everything, and with no HOME and no
     * XDG_CONFIG_HOME the system-wide file is used - the case of a unit
     * running as nobody, which has no HOME and would otherwise lose the
     * measured rotation and fall back to the catalogued angle. */
    setenv("USBLCD_CALIB", "/etc/usblcd/calibration", 1);
    unsetenv("XDG_CONFIG_HOME");
    unsetenv("HOME");
    if (strcmp(calib_path(), "/etc/usblcd/calibration") != 0) {
        printf("FAIL USBLCD_CALIB ignored: got %s\n", calib_path());
        fails++;
    }
    unsetenv("USBLCD_CALIB");

    /* no HOME, no XDG_CONFIG_HOME: the system-wide file, which is what a unit
     * running as nobody lands on (it has no HOME at all) */
    if (strcmp(calib_path(), "/etc/usblcd/calibration") != 0) {
        printf("FAIL no-HOME fallback: got %s\n", calib_path());
        fails++;
    }

    setenv("XDG_CONFIG_HOME", root, 1);
    if (strncmp(calib_path(), root, strlen(root)) != 0) {
        printf("FAIL XDG lookup: got %s\n", calib_path());
        fails++;
    }
    mkdir(root, 0755);

    const char *ser = "testserial";

    /* No file yet -> not found, and nothing invented. */
    int deg = 12345, orient = 12345;
    check("absent file is not found",
          calib_lookup(0x87ad, 0x70db, ser, 10, 7, &deg, &orient), 0);
    check("absent leaves deg alone", deg, 12345);
    check("absent leaves orient alone", orient, 12345);

    /* Store the pair this machine's panel measured on glass. */
    check("save", calib_save(0x87ad, 0x70db, ser, 10, 7, 180, 1), 0);
    deg = -1; orient = -1;
    check("found after save",
          calib_lookup(0x87ad, 0x70db, ser, 10, 7, &deg, &orient), 1);
    check("base", deg, 180);
    check("orient cw", orient, 1);

    /* --orient alone must not lose the measured base. */
    check("save orient only", calib_save(0x87ad, 0x70db, ser, 10, 7, -1, 0), 0);
    deg = -1; orient = -1;
    calib_lookup(0x87ad, 0x70db, ser, 10, 7, &deg, &orient);
    check("base survives an orient-only write", deg, 180);
    check("orient ccw", orient, 0);

    /* Base-only write keeps the orient. */
    check("save base only", calib_save(0x87ad, 0x70db, ser, 10, 7, 90, -1), 0);
    deg = -1; orient = -1;
    calib_lookup(0x87ad, 0x70db, ser, 10, 7, &deg, &orient);
    check("base updated", deg, 90);
    check("orient survives a base-only write", orient, 0);

    /* One line per panel: a second panel does not clobber the first. */
    calib_save(0x87ad, 0x70db, "other", 32, 0, 270, 1);
    deg = -1; orient = -1;
    check("first panel still there",
          calib_lookup(0x87ad, 0x70db, ser, 10, 7, &deg, &orient), 1);
    check("first panel base", deg, 90);
    deg = -1; orient = -1;
    check("second panel found",
          calib_lookup(0x87ad, 0x70db, "other", 32, 0, &deg, &orient), 1);
    check("second panel base", deg, 270);
    check("second panel orient", orient, 1);

    /* A PM/SUB pair that was never stored is not answered by another row. */
    check("different PM/SUB is a miss",
          calib_lookup(0x87ad, 0x70db, "other", 10, 7, &deg, &orient), 0);

    /* The wire angle actually uses what we stored. */
    profile_t p;
    memset(&p, 0, sizeof(p));
    bulk_profile(10, 7, &p);              /* catalogued base 180, invert yes */
    check("catalogued angle @0", wire_angle(&p, 0), 180);
    p.calib_deg = 180;                    /* base=180 from the file */
    check("calibrated angle @0", wire_angle(&p, 0), 0);
    p.encode_invert = 0;                  /* orient=cw */
    check("calibrated angle @90", wire_angle(&p, 90), 90);
    check("calibrated angle @270", wire_angle(&p, 270), 270);

    unlink(calib_path());
    rmdir(root);

    if (fails) {
        printf("%d check(s) failed\n", fails);
        return 1;
    }
    printf("calibration store OK\n");
    return 0;
}
