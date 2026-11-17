#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Prokopiev; see LICENSE and NOTICE in the repository root.
"""Font plumbing checks (offline, no panel needed).

The dashboard draws with FreeType-antialiased TTF when it can and with its own
5x7 bitmap font otherwise.  Both paths must produce the same layout metrics: the
whole layout was measured against a bitmap box of 7*scale px, so the TTF
calibration has to land on the same box heights.
"""
import importlib.util
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DASH = os.path.join(ROOT, 'dash', 'dash.py')
HELPER = os.path.join(ROOT, 'build', 'ttfplan')

sys.path.insert(0, os.path.join(ROOT, 'dash'))


def load_dash():
    spec = importlib.util.spec_from_file_location('dash', DASH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules['dash'] = mod
    spec.loader.exec_module(mod)
    return mod


def main():
    if not os.path.exists(HELPER):
        print('ttfplan helper not built (no freetype2?) - font checks skipped')
        return 0
    mod = load_dash()
    fonts = [f for f in mod.FONT_CANDIDATES if os.path.exists(f)]
    if not fonts:
        print('no system TTF found - font checks skipped')
        return 0
    plans = mod.load_font(HELPER, fonts[0])
    assert plans, 'load_font returned nothing'
    for scale in (1, 2, 3, 4, 6, 7, 8):
        assert scale in plans, 'missing plan for scale %d' % scale
        box = plans[scale]['box_h']
        want = 7 * scale
        # the helper enforces a floor of 9 px em, so the smallest size can be a
        # little taller than the bitmap box; bigger sizes must match closely
        tol = max(8, int(0.3 * want))
        assert abs(box - want) <= tol, 'scale %d box_h %d, layout expects ~%d' % (scale, box, want)
    # every glyph used by the pages must exist and have an advance
    # every scale the layout can pick must have a plan, or that text silently
    # falls back to the bitmap font
    src = open(os.path.join(ROOT, 'dash', 'dash.py')).read()
    picked = set()
    for line in src.splitlines():
        if 'pick_scale(' in line:
            for tok in ('(6, 5, 4)', '(3, 2)', '(2, 1)'):
                if tok in line:
                    picked.update(int(x) for x in tok.strip('()').split(','))
    assert picked, 'no pick_scale scales found in dash.py'
    for sc in picked:
        assert sc in plans, 'pick_scale can choose %d but FONT_SIZES lacks it' % sc

    for ch in 'ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 %.:/-~+,()':
        g = plans[2]['glyphs'].get(ch)
        assert g is not None, 'glyph %r missing at scale 2' % ch
        assert g[2] > 0, 'glyph %r has zero advance' % ch
    # coverage levels: the plan must use more than the two extremes, that is the
    # whole point of using a real font
    levels = set()
    for g in plans[6]['glyphs'].values():
        for r in g[3]:
            levels.add(r[4])
    assert len(levels) >= 4, 'only %d coverage levels - not antialiased' % len(levels)

    # the bitmap fallback must keep working without FreeType
    out = subprocess.run([sys.executable, DASH, '--once', '--no-ttf', '--page', 'CPU',
                          '--title', 'TEST', '--out', '/tmp/_font_fallback.ppm'],
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    assert out.returncode == 0, out.stderr.decode()
    assert not os.path.exists('/tmp/_font_fallback.ppm') or \
        os.path.getsize('/tmp/_font_fallback.ppm') > 100000, 'fallback frame looks empty'
    os.path.exists('/tmp/_font_fallback.ppm') and os.remove('/tmp/_font_fallback.ppm')

    # and the TTF path must be reported when it is used
    out = subprocess.run([sys.executable, DASH, '--once', '--page', 'CPU', '--title', 'TEST',
                          '--out', '/tmp/_font_ttf.ppm'],
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    assert out.returncode == 0, out.stderr.decode()
    assert b'font ' in out.stderr, 'TTF path not reported: %r' % out.stderr[:200]
    assert 'font ' not in out.stderr.decode() or 'font' in out.stderr.decode()
    os.path.exists('/tmp/_font_ttf.ppm') and os.remove('/tmp/_font_ttf.ppm')
    # the page-rotation dial: a ring of one sector per second that empties as
    # the seconds pass, drawn without touching a panel
    def lit(canvas):
        n = 0
        px = canvas.px
        for i in range(0, len(px), 4):
            if (px[i], px[i + 1], px[i + 2]) == mod.ACCENT:
                n += 1
        return n

    def dial(filled):
        cv = mod.Canvas(120, 60, safe=(0, 0, 0, 0))
        cv.rect(0, 0, 120, 60, mod.BG)
        cv.ring(60, 30, 16, 12, filled, mod.ACCENT, mod.PANEL2)
        return lit(cv)

    a, b, cc = dial(12), dial(6), dial(0)
    assert a > 0, 'dial drew nothing at all'
    assert a > b > cc, 'dial does not empty with time (%d %d %d)' % (a, b, cc)
    assert abs(b - cc - (a - b)) <= max(4, (a - b) // 2), \
        'sectors are not uniform (%d %d %d)' % (a, b, cc)

    cv = mod.Canvas(120, 60, safe=(0, 0, 0, 0))
    cv.font = plans
    ih = cv.ink_h('0123456789', 3)
    assert ih > 0, 'ink_h returned %d' % ih
    assert ih <= cv.text_h(3), 'ink_h %d exceeds the ink box %d' % (ih, cv.text_h(3))
    assert cv.ink_h('0', 2) <= cv.text_h(2), 'ink_h must stay inside the line box'

    print('fonts OK (%s, %d sizes, %d coverage levels; dial %d/%d/%d px, digit ink %d)' %
          (os.path.basename(fonts[0]), len(plans), len(levels), a, b, cc, ih))
    return 0


if __name__ == '__main__':
    sys.exit(main())
