#!/bin/sh
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Prokopiev; see LICENSE and NOTICE in the repository root.
# tests/run.sh — offline checks.  Nothing here needs the panel attached;
# the hardware path is exercised by `make live`.
set -e
cd "$(dirname "$0")/.."

PKGS="libusb-1.0 libjpeg"
FLAGS="-O2 -Wall -Wextra -std=c11 -D_DEFAULT_SOURCE -Isrc -Ivendor"
LIBS="$(pkg-config --libs $PKGS) -lm"

mkdir -p build/tests

echo "building"
make -s >/dev/null

echo "compiling tests"
cc $FLAGS -o build/tests/test_profile  tests/test_profile.c src/profile.c -lm
cc $FLAGS -o build/tests/test_encode  tests/test_encode.c src/render.c \
   $(pkg-config --cflags --libs libjpeg) -lm
cc $FLAGS -o build/tests/test_calib   tests/test_calib.c src/calib.c src/profile.c -lm

echo
echo "== profile / geometry tables"
./build/tests/test_profile

echo
echo "== encoders (RGB565, JPEG 4:4:4, rotation, fit, brightness)"
./build/tests/test_encode

echo
echo "== dashboard renderer (stdlib python + 5x7 font)"
for pg in CPU GPU PROC AI; do
	python3 dash/dash.py --once --page $pg --title TEST --out build/tests/dash_$pg.ppm >/dev/null
	test -s build/tests/dash_$pg.ppm && echo "dash.py page $pg OK ($(stat -c%s build/tests/dash_$pg.ppm) bytes)"
	# header (15) + 960*540 RGB: catches a stride/format regression
	test "$(stat -c%s build/tests/dash_$pg.ppm)" = "1555215"
	python3 dash/dash.py --once --audit --page $pg --title TEST --out build/tests/dash_$pg.ppm > build/tests/dash_$pg.audit 2>&1
	grep -q "CLIP" build/tests/dash_$pg.audit && { echo "page $pg layout audit FAILED:"; cat build/tests/dash_$pg.audit; exit 1; }
	rm -f build/tests/dash_$pg.audit
done
./build/ppm2png build/tests/dash.ppm build/tests/dash.png >/dev/null && echo "ppm2png OK"
python3 dash/dash.py --font-sample --out build/tests/font.ppm >/dev/null && echo "font sheet OK"
python3 dash/dash.py --once --page AI --no-strata --title TEST --out build/tests/dash_AI_off.ppm >/dev/null && echo "AI page with --no-strata OK"
python3 tests/test_font.py
python3 tests/test_gpu.py
python3 tests/test_strata.py

echo
echo "== calibration store (measured-on-glass offsets)"
./build/tests/test_calib

echo
echo "== CLI surface"
./usblcd --help >/dev/null && echo "usblcd --help OK"
./usblcd list || echo "(no panel attached — hardware commands skipped)"

echo
echo "all offline checks passed"
echo "with the panel plugged in, run:  ./usblcd info && make live"
