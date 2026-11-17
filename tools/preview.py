#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Prokopiev; see LICENSE and NOTICE in the repository root.
"""ASCII preview of a PPM frame — lets an agent/text-only session sanity-check
a rendered dashboard layout (panel bounds, overlap, empty regions).

    python3 tools/preview.py frame.ppm [cols] [rows]
"""
import sys


def preview(path, cols=104, rows=36):
    raw = open(path, 'rb').read()
    toks, i = [], 0
    while len(toks) < 4:
        j = raw.index(b'\n', i)
        toks += raw[i:j].split()
        i = j + 1
    w, h = int(toks[1]), int(toks[2])
    px = raw[i:]
    if len(px) < w * h * 3:
        raise SystemExit('%s: short pixel data (%d < %d)' % (path, len(px), w * h * 3))
    chars = ' .:-=+*#%@'
    sx, sy = max(1, w // cols), max(1, h // rows)
    out = []
    for r in range(rows):
        line = []
        for cc in range(cols):
            s = n = 0
            for dy in range(0, sy, 2):
                for dx in range(0, sx, 2):
                    k = ((r * sy + dy) * w + (cc * sx + dx)) * 3
                    s += (px[k] + px[k + 1] + px[k + 2]) // 3
                    n += 1
            line.append(chars[min(len(chars) - 1, (s // max(1, n)) // 26)])
        out.append(''.join(line))
    print('%s  %dx%d' % (path, w, h))
    print('\n'.join(out))


if __name__ == '__main__':
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    preview(sys.argv[1],
            int(sys.argv[2]) if len(sys.argv) > 2 else 104,
            int(sys.argv[3]) if len(sys.argv) > 3 else 36)
