#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Prokopiev; see LICENSE and NOTICE in the repository root.
"""usblcd dashboard — render a server dashboard to the USB LCD panel.

Stdlib only (no PIL / numpy / matplotlib: this box has none), rendered through
the 5x7 bitmap font in font57.py, so the whole toolchain is this repo plus
libusb.

    dash.py --once                      # one frame to /tmp/usblcd-dashboard.ppm
    dash.py --sensors                   # what was discovered on this box (debug)
    dash.py --page CPU                  # pin a page; default rotates CPU/GPU/PROC/AI
    dash.py --page AI                   # the inference server's Overview
    dash.py --exec ./usblcd             # live: one frame/s onto the panel
    dash.py --raw | usblcd feed         # same, with a pipeline you own
    dash.py --font-sample --out s.ppm   # draw the whole font sheet

Sensors come from hwmon, /proc, statvfs, nvidia-smi and the strata inference
server's /metrics.  nvidia-smi runs in a background thread so a slow query can
never stall a frame, and it is retried with a backoff for as long as the
dashboard runs: a driver that boots after this service, a GPU mid-reset and a
driver mid-upgrade all heal on their own, so the GPU page fills itself in.  The
AI page polls the same /metrics the strata monitor's Overview reads, with the
same poll rules.  Every section is guarded on its own: a missing sensor drops
its panel, it does not stop the frame.
"""
from __future__ import annotations

import argparse
import glob
import json
import math
import os
import pwd
import re
import shutil
import socket
import struct
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from array import array

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from font57 import FONT, glyph                                # noqa: E402

# ── palette ────────────────────────────────────────────────────────────
def merge_levels(C):
    """Turn a grid of coverage levels into rectangles: horizontal runs, then
    merged downwards while the row below matches exactly.  Text is then a few
    dozen rect calls per line, which is what keeps a 1 fps frame cheap."""
    plan = []
    h = len(C)
    for r in range(h):
        row = C[r]
        c = 0
        w = len(row)
        while c < w:
            lv = row[c]
            if lv == 0:
                c += 1
                continue
            run = 1
            while c + run < w and row[c + run] == lv:
                run += 1
            height = 1
            while r + height < h:
                below = C[r + height]
                if not all(below[c + k] == lv for k in range(run)):
                    break
                if (c > 0 and below[c - 1] != 0) or (c + run < w and below[c + run] != 0):
                    break
                height += 1
            plan.append((c, r, run, height, lv))
            for rr in range(r + 1, r + height):
                for k in range(run):
                    C[rr][c + k] = 0
            c += run
    return plan


def blend(a, b, f):
    """b blended over a at coverage f (0 = a, 1 = b)."""
    return tuple(int(a[i] + (b[i] - a[i]) * f) for i in range(3))


BG      = (11, 15, 20)
PANEL   = (18, 25, 34)
PANEL2  = (26, 36, 48)
TEXT    = (226, 232, 240)
DIM     = (104, 124, 148)
ACCENT  = (79, 209, 197)
OK      = (72, 187, 120)
WARN    = (246, 173, 85)
CRIT    = (252, 129, 129)
GRID    = (46, 60, 78)

TEMP_WARN, TEMP_CRIT = 70.0, 85.0

_DROP_ALPHA = re.compile(rb'(.)(.)(.)(.)', re.DOTALL)

# 50/50 pixel average done as SWAR on 8-byte words: (a&b) + ((a^b)&MASK)/2,
# with the per-byte LSB masked so the shift cannot carry between channels.
# 35 ms for a 960x540 frame, versus ~1000 ms for a naive per-byte generator
# (whose generator `file.write` coerces byte by byte).
_FADE_MASK = 0xFEFEFEFEFEFEFEFE


def mix_frames(a, b):
    A = array('Q')
    A.frombytes(a)
    B = array('Q')
    B.frombytes(b)
    return array('Q', ((x & y) + ((x ^ y) & _FADE_MASK) // 2
                       for x, y in zip(A, B))).tobytes()


# ── canvas ─────────────────────────────────────────────────────────────
_RGBA_CACHE = {}


def rgba(c):
    """4-byte pixel for a colour, cached: the framebuffer is RGBA8888 so a
    frame can be handed to `usblcd feed` as one buffer write."""
    b = _RGBA_CACHE.get(c)
    if b is None:
        b = bytes(c) + b'\xff'
        _RGBA_CACHE[c] = b
    return b


class Canvas:
    _aa_plan = {}   # (char, scale) -> coverage rect plan, shared by all canvases

    """Framebuffer plus a safe area.

    The physical panel crops its own edges (rounded corner, bezel), so content
    laid out to the framebuffer edge is partly lost on glass.  Everything is
    drawn inside `safe`, and the margin is filled with the background colour.
    """

    def __init__(self, w, h, safe=(8, 8, 8, 8)):
        self.W, self.H = w, h
        top, bottom, left, right = safe
        self.ox, self.oy = left, top
        self.w, self.h = max(16, w - left - right), max(16, h - top - bottom)
        self.px = bytearray(rgba(BG) * (w * h))
        # clip region of the panel currently being drawn, and a log of strings
        # that ran past its right edge: text is measured, so clipping is a
        # reportable bug instead of something you only see on the glass.
        self.clip = None
        self.over = []
        self.info = []      # informational: a list was capped to fit its panel
        # every drawn string, as a box: catches strings that collide with each
        # other, which a panel-edge clip check cannot see
        self.boxes = []
        self.panels = []      # (x, y, w, h, title) — for --debug-grid overlays
        self.text_mask = False    # when True, text() is a no-op: panels only
        self.trace = False
        self.aa = True            # soft edge strips around glyph strokes
        self.aa_bg = BG           # colour the strokes sit on, for the blends
        self.aa_min = 2           # soft edges from this font scale up
        self.font = None          # FreeType rect plans, when a TTF is loaded

    def clear(self, c=BG):
        self.px = bytearray(rgba(c) * (self.W * self.H))

    def set(self, x, y, c):
        if 0 <= x < self.w and 0 <= y < self.h:
            i = ((y + self.oy) * self.W + (x + self.ox)) * 4
            self.px[i:i + 4] = rgba(c)

    def rect(self, x0, y0, x1, y1, c):
        x0, x1 = max(0, min(self.w, int(x0))), max(0, min(self.w, int(x1)))
        y0, y1 = max(0, min(self.h, int(y0))), max(0, min(self.h, int(y1)))
        if x1 <= x0 or y1 <= y0:
            return
        row = rgba(c) * (x1 - x0)
        n = len(row)
        for y in range(y0, y1):
            i = ((y + self.oy) * self.W + (x0 + self.ox)) * 4
            self.px[i:i + n] = row

    def outline(self, x0, y0, x1, y1, c):
        self.rect(x0, y0, x1, y0 + 1, c)
        self.rect(x0, y1 - 1, x1, y1, c)
        self.rect(x0, y0, x0 + 1, y1, c)
        self.rect(x1 - 1, y0, x1, y1, c)

    def hline(self, x0, x1, y, c):
        self.rect(x0, y, x1, y + 1, c)

    def text(self, x, y, s, scale=2, c=TEXT, spacing=1):
        """Draw a string in the built-in bitmap font.

        With self.aa on (default) the glyph mask is blurred 1 px and quantized
        into coverage levels, and each level is painted in a colour blended
        from self.aa_bg: partial-coverage pixels *inside* the glyph box, exactly
        where a true antialiased raster puts them.  Hard bitmap edges read as
        serration on the panel, most of all on the big numbers."""
        if self.text_mask:
            return
        box_h = self.text_h(scale)
        if self.trace:
            self.boxes.append([x, y, x + self.text_w(s, scale), y + box_h, s, scale])
        if self.clip is not None:
            if y + box_h > self.clip[3] - 6:
                self.over.append('OVERFALL %r  by %dpx past panel bottom  (scale %d)' %
                                 (repr(s), y + box_h - (self.clip[3] - 6), scale))
                return
            room = self.clip[2] - 6 - x
            w = self.text_w(s, scale)
            if w > room:
                n = len(s)
                while n > 0 and self.text_w(s[:n], scale) > room:
                    n -= 1
                self.over.append('TRUNC %s  ->  %s  (scale %d, %dpx room, dropped %d chars)' %
                                 (repr(s), repr(s[:n]), scale, room, len(s) - n))
                s = s[:n]

        f = self.font.get(scale) if self.font else None
        if f is not None:
            cols = {1: blend(self.aa_bg, c, 0.15), 2: blend(self.aa_bg, c, 0.32),
                    3: blend(self.aa_bg, c, 0.50), 4: blend(self.aa_bg, c, 0.68),
                    5: blend(self.aa_bg, c, 0.85), 6: c}
            pen = x
            for ch in s:
                g = f['glyphs'].get(ch)
                if g is None:
                    pen += f['advance']
                    continue
                dx, dy, rects = g[0], g[1], g[3]
                for rx, ry, rw, rh, lv in rects:
                    self.rect(pen + dx + rx, y + dy + ry, pen + dx + rx + rw,
                              y + dy + ry + rh, cols[lv])
                pen += self.advance(ch, scale, f) + spacing
            return pen - x - spacing

        aa = self.aa and scale >= self.aa_min
        if aa:
            # one colour per coverage level; interior pixels stay the pure colour
            cols = {4: c, 3: blend(self.aa_bg, c, 0.75), 2: blend(self.aa_bg, c, 0.5),
                    1: blend(self.aa_bg, c, 0.25)}
        cx = x
        for ch in s:
            g = glyph(ch)
            if aa:
                for dx, dy, w, h, lv in self._aa_glyph(ch, scale):
                    self.rect(cx + dx, y + dy, cx + dx + w, y + dy + h, cols[lv])
            else:
                for row in range(7):
                    bits = g[row]
                    col = 0
                    while col < 5:
                        if bits[col] != '#':
                            col += 1
                            continue
                        run = 1
                        while col + run < 5 and bits[col + run] == '#':
                            run += 1
                        self.rect(cx + col * scale, y + row * scale,
                                  cx + (col + run) * scale, y + (row + 1) * scale, c)
                        col += run
            cx += 6 * scale + spacing
        return cx - x

    @classmethod
    def _aa_glyph(cls, ch, scale):
        """Rect plan for one glyph at one size: (dx, dy, w, h, coverage 1..4).

        The 5x7 mask is treated as a continuous image and resampled onto the
        target grid (bilinear, pixel centres), so a diagonal or a curve gets a
        real ramp of coverage instead of a 6-px staircase.  A contrast curve
        keeps that ramp short so the strokes stay crisp, and coverage is
        quantized to four levels, painted as merged rects.  Built once per
        (char, size): only the levels know the colours, so the plan serves
        every colour of text.
        """
        key = (ch, scale)
        plan = cls._aa_plan.get(key)
        if plan is not None:
            return plan
        g = glyph(ch)
        w, h = 5 * scale, 7 * scale
        ink = [[1 if c == '#' else 0 for c in row] for row in g]
        C = []
        for py in range(h):
            row = []
            v = (py + 0.5) / scale - 0.5
            r0, fr = int(math.floor(v)), 0.0
            fr = v - r0
            for px in range(w):
                u = (px + 0.5) / scale - 0.5
                c0 = int(math.floor(u))
                fc = u - c0
                a0 = max(0, min(4, c0)); a1 = min(4, max(0, c0 + 1))
                b0 = max(0, min(6, r0)); b1 = min(6, max(0, r0 + 1))
                cov = (ink[b0][a0] * (1 - fc) * (1 - fr) + ink[b0][a1] * fc * (1 - fr) +
                       ink[b1][a0] * (1 - fc) * fr + ink[b1][a1] * fc * fr)
                if ink[b0][a0] and ink[b1][a1] and 0 < fc < 1 and 0 < fr < 1:
                    cov = 1.0                      # inside a stroke, stay solid
                cov = max(0.0, min(1.0, (cov - 0.20) / 0.45))   # contrast curve
                row.append(0 if cov < 0.18 else (1 if cov < 0.42 else
                           (2 if cov < 0.72 else 3 if cov < 0.95 else 4)))
            C.append(row)
        plan = merge_levels(C)
        cls._aa_plan[key] = plan
        return plan

    def text_right(self, x1, y, s, scale=2, c=TEXT):
        return self.text(x1 - self.text_w(s, scale), y, s, scale, c)

    def advance(self, ch, scale, f=None):
        """Cell width of one glyph.  Digits all get the widest digit's width:
        a proportional face like Inter writes '1' narrower than '0', so every
        counter on the panel would change width as its value changed."""
        if f is None:
            f = self.font.get(scale) if self.font else None
        if f is None:
            return 6 * scale + 1
        if ch.isdigit() and f['digit_adv']:
            return f['digit_adv']
        g = f['glyphs'].get(ch)
        return g[2] if g else f['advance']

    def text_w(self, s, scale=2, spacing=1):
        """Width a string occupies at this scale (TTF advances, or the bitmap cell)."""
        f = self.font.get(scale) if self.font else None
        if f is not None:
            return sum(self.advance(ch, scale, f) for ch in s) + spacing * max(0, len(s) - 1)
        return len(s) * (6 * scale + spacing) - spacing

    def text_h(self, scale=2):
        """Ink box height of one line at this scale (descenders included)."""
        f = self.font.get(scale) if self.font else None
        return f['box_h'] if f else 7 * scale

    def ink_h(self, s, scale=2):
        """Height of the glyphs themselves, descenders excluded.

        Digits and capitals have no descenders, so this is the height they
        actually paint - what to match a circle or a rule against, where
        text_h() would include space the glyphs never occupy."""
        f = self.font.get(scale) if self.font else None
        if f is None:
            return 7 * scale
        lo, hi = 1 << 30, -(1 << 30)
        for ch in s:
            g = f['glyphs'].get(ch)
            if not g:
                continue
            for rx, ry, rw, rh, lv in g[3]:
                lo, hi = min(lo, ry), max(hi, ry + rh)
        return (hi - lo) if hi > lo else 7 * scale

    def bar(self, x, y, w, h, frac, c=ACCENT, bg=PANEL2):
        self.rect(x, y, x + w, y + h, bg)
        n = int(max(0.0, min(1.0, frac)) * (w - 2))
        if n:
            self.rect(x + 1, y + 1, x + 1 + n, y + h - 1, c)

    def vbar(self, x, y, w, h, frac, c=ACCENT, bg=PANEL2):
        self.rect(x, y, x + w, y + h, bg)
        n = int(max(0.0, min(1.0, frac)) * h)
        if n:
            self.rect(x, y + h - n, x + w, y + h, c)

    def ring(self, cx, cy, r, total, filled, c=ACCENT, bg=PANEL2, gap_deg=6):
        """Countdown dial: `total` sectors around a ring, `filled` of them lit.

        Every sector is one unit of time and is dropped as that unit passes, so
        the dial empties clockwise from the top with no number to read. Pixels
        are classified by angle, then painted as horizontal runs, which keeps it
        at a few dozen rects per frame instead of a thousand plot calls.
        """
        total = max(1, int(total))
        step = 360.0 / total
        inner = int(r * 0.42) ** 2
        r2 = r * r
        span = range(cx - r, cx + r + 1)
        for py in range(cy - r, cy + r + 1):
            row = []
            for px in span:
                dx = px - cx + 0.5
                dy = py - cy + 0.5
                d2 = dx * dx + dy * dy
                col = None
                if r2 >= d2 >= inner:
                    ang = (math.degrees(math.atan2(dx, -dy)) + 360.0) % 360.0
                    idx = int(ang / step)
                    off = ang - idx * step
                    if off > gap_deg and step - off > gap_deg:
                        col = c if idx < filled else bg
                row.append(col)
            i = 0
            n = len(row)
            while i < n:
                col = row[i]
                j = i + 1
                while j < n and row[j] is col:
                    j += 1
                if col is not None:
                    self.rect(cx - r + i, py, cx - r + j, py + 1, col)
                i = j

    def spark(self, x, y, w, h, vals, lo, hi, c=ACCENT):
        for frac in (0.5, 1.0):
            self.hline(x, x + w, y + int((h - 1) * (1 - frac)), GRID)
        self.hline(x, x + w, y + h - 1, GRID)
        if len(vals) < 2 or hi <= lo:
            return
        n = len(vals)
        span = max(1, n - 1)
        for i, v in enumerate(vals):
            yy = y + h - 1 - int((v - lo) / (hi - lo) * (h - 1))
            xx = x + int(i * (w - 1) / span)
            self.rect(xx, max(y, yy), xx + 1, min(y + h, yy + 2), c)

    def to_ppm(self, path):
        """RGB PPM for previewing: drop alpha with one C-level regex pass."""
        tmp = path + '.tmp'
        with open(tmp, 'wb') as f:
            f.write(b'P6\n%d %d\n255\n' % (self.w, self.h))
            f.write(_DROP_ALPHA.sub(rb'\1\2\3', bytes(self.px)))
        os.replace(tmp, path)

    def to_rgba(self, out):
        """Hand the frame to `usblcd feed`: the framebuffer *is* RGBA8888, so
        this is a single buffer write (it used to be a per-pixel Python loop,
        which was 72% of the frame cost)."""
        out.write(self.px)


# ── hwmon ──────────────────────────────────────────────────────────────
def _sortkey(path):
    m = re.search(r'(\d+)', os.path.basename(path))
    return int(m.group(1)) if m else 0


def scan_hwmon():
    """Every hwmon chip on this box, classified.  Name file lives in the hwmon
    directory itself (/sys/class/hwmon/hwmonN/name); `device` is the parent."""
    out = {'cpu': None, 'cpu_cores': [], 'thermal': [], 'gpu': [], 'nvme': [],
           'nic': [], 'chassis': [], 'fans': []}
    for path in sorted(glob.glob('/sys/class/hwmon/hwmon*')):
        chip = os.path.basename(path)
        try:
            name = open(os.path.join(path, 'name')).read().strip()
        except OSError:
            name = chip
        low = name.lower()

        for f in sorted(glob.glob(os.path.join(path, 'temp*_input')), key=_sortkey):
            try:
                v = int(open(f).read()) / 1000.0
            except (OSError, ValueError):
                continue
            lab = f[:-6] + '_label'
            try:
                label = open(lab).read().strip()
            except OSError:
                label = ''
            item = {'chip': chip, 'name': name, 'label': label, 'temp': v}
            if name == 'coretemp' or low in ('k10temp', 'zenpower', 'cpu_thermal'):
                if label.startswith('Core'):
                    out['cpu_cores'].append(item)
                elif 'package' in label.lower() or label in ('Tctl', 'Tdie'):
                    out['cpu'] = item
                else:
                    out['thermal'].append(item)
            elif name == 'nvidia' or 'gpu' in low:
                out['gpu'].append(item)
            elif name == 'nvme':
                out['nvme'].append(item)
            elif 'r81' in low or 'r812' in low or 'nic' in low or 'eth' in low:
                out['nic'].append(item)
            elif name == 'acpitz':
                out['thermal'].append(item)
            else:
                out['chassis'].append(item)

        for f in sorted(glob.glob(os.path.join(path, 'fan*_input')), key=_sortkey):
            try:
                v = int(open(f).read())
            except (OSError, ValueError):
                continue
            lab = f[:-6] + '_label'
            try:
                label = open(lab).read().strip()
            except OSError:
                label = ''
            out['fans'].append({'chip': chip, 'name': name,
                                'label': label or chip, 'rpm': v})
    out['cpu_cores'].sort(key=lambda i: int(re.search(r'\d+', i['label']).group(0)
                                            if re.search(r'\d+', i['label']) else 0))
    return out


# ── nvidia-smi (background thread, so a slow query never stalls a frame) ──
GPU_QUERY = ('utilization.gpu', 'memory.used', 'memory.total', 'temperature.gpu',
             'fan.speed', 'power.draw', 'utilization.memory', 'name')


def smi_reason(raw, limit=120):
    """nvidia-smi's first real stderr line, for the panel's status line.

    'NVIDIA-SMI has failed because it couldn't communicate with the NVIDIA
    driver' and 'No devices were found' are the two messages that matter on a
    dashboard: they say the driver is not answering yet, which heals, as
    opposed to this box having no NVIDIA at all, which does not.
    """
    for line in (raw or b'').decode(errors='replace').splitlines():
        line = ' '.join(line.split())
        if line and not line.startswith('['):
            return line[:limit]
    return ''


def query_gpus(timeout=5.0):
    """(gpus, why): a list of GPU dicts, or (None, one-line reason).

    `why` is what the GPU page shows when it has nothing to draw, so a
    boot-time race, a dead driver and a box with no NVIDIA look different.
    The reason 'not installed' is the only one the poller treats as final.
    """
    cmd = ['nvidia-smi', '--query-gpu=' + ','.join(GPU_QUERY),
           '--format=csv,noheader,nounits']
    try:
        r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           timeout=timeout)
    except FileNotFoundError:
        return None, 'not installed'
    except OSError as e:
        return None, 'spawn failed: %s' % (e.strerror or e.__class__.__name__)
    except subprocess.TimeoutExpired:
        return None, 'query timed out after %gs' % timeout
    except subprocess.SubprocessError as e:
        return None, e.__class__.__name__
    if r.returncode != 0:
        return None, ('rc=%d %s' % (r.returncode, smi_reason(r.stderr))).strip()
    gpus = []
    for line in r.stdout.decode(errors='replace').strip().splitlines():
        f = [x.strip() for x in line.split(',')]
        if len(f) < 7:
            continue

        def num(i):
            try:
                return float(f[i])
            except ValueError:
                return None
        gpus.append({'name': f[7] if len(f) > 7 else 'GPU', 'util': num(0),
                     'mem_used': num(1), 'mem_total': num(2), 'temp': num(3),
                     'fan': num(4), 'power': num(5), 'mem_util': num(6)})
    return (gpus or None), ('' if gpus else 'no devices answered')


class GPUThread(threading.Thread):
    """nvidia-smi in the background, polled for as long as we are running.

    A failed query is never fatal, and it is never the end of polling: this
    dashboard starts at boot, seconds before the driver answers nvidia-smi,
    and queries fail for other reasons that all heal (the module reloading,
    persistenced restarting, a GPU mid-reset, a driver mid-upgrade).  The old
    'give up after 5 failures' turned that race into a GPU page that stayed
    empty until the service was restarted.  So the poller backs off instead
    (3, 6, 12, 24, 30s, then 2min once it is clearly not a transient) and the
    first success lights the page back up by itself.  The one permanent stop
    is a missing nvidia-smi binary: there is nothing there to poll.

    `stale()` is seconds since the last good reply: the panel keeps painting
    the last numbers it had and marks them stale, which is far more useful
    than a page of zeros.
    """

    MAX_PERIOD = 30.0     # top of the normal backoff ladder
    SLOW_AFTER = 24       # consecutive failures (~10 min) before a 2-minute poll
    SLOW_PERIOD = 120.0

    def __init__(self, period=3.0):
        super().__init__(daemon=True)
        self.base = self.period = period
        self.stop = threading.Event()
        self.gpus = None
        self.fail = 0
        self.disabled = False
        self.why = 'probing'
        self.good_at = 0.0

    def stale(self):
        """Seconds since the last good query, or None if there never was one."""
        return (time.monotonic() - self.good_at) if self.good_at else None

    def run(self):
        while not self.stop.is_set():
            g, why = query_gpus()
            if g:
                self.gpus, self.fail, self.why = g, 0, ''
                self.good_at = time.monotonic()
                self.period = self.base
            else:
                self.fail += 1
                self.why = why
                if self.fail >= self.SLOW_AFTER:
                    self.period = self.SLOW_PERIOD
                else:
                    self.period = min(self.MAX_PERIOD, self.base * 2 ** min(self.fail - 1, 4))
                if why == 'not installed':
                    self.disabled = True      # nothing to poll, ever
                    return
            self.stop.wait(self.period)       # also the fastest way out


# ── strata inference server (/metrics, in a background thread) ─────────
def sudo_login_state():
    """The strata state of the login that typed `sudo`, when running under sudo.

    `sudo python3 tools/strata_conf.py` and `sudo dash.py` run as root, and root
    has no strata install: the model belongs to the login that typed sudo, and
    only root can read that login's 0600 launch configs.  Without this, the one
    command that writes the service's STRATA_URL/STRATA_API_KEY pair looks in
    /root, finds nothing, and refuses to write anything.
    """
    user = os.environ.get('SUDO_USER')
    if not user or user == 'root':
        return ''
    try:
        home = pwd.getpwnam(user).pw_dir
    except KeyError:
        home = '/home/%s' % user
    return os.path.join(home, '.local', 'state', 'strata')


def strata_state_dirs(extra=None):
    """Directories a strata install writes its runtime state into.

    `strata` writes its launch configs (port + the API key /metrics answers 401
    without) to $XDG_STATE_HOME/strata/launch and the URL a running server
    bound to to .../strata/runtime, defaulting under the user's home.  The
    dashboard service runs as a dedicated user with ProtectHome=read-only, so
    on a service box the state of *your* login is not under $HOME: STRATA_STATE
    (or --strata-state) is how you point the poller at it.
    """
    out = []
    for cand in ([extra] if extra else []):
        if cand:
            out.append(os.path.expanduser(cand))
    env = os.environ.get('STRATA_STATE')
    if env:
        out.append(os.path.expanduser(env))
    sudo = sudo_login_state()
    if sudo:
        out.append(sudo)
    base = os.environ.get('XDG_STATE_HOME')
    if not base:
        base = os.path.join(os.path.expanduser('~'), '.local', 'state')
    out.append(os.path.join(base, 'strata'))
    home = os.path.expanduser('~')
    if home not in ('/', ''):
        out.append(os.path.join(home, '.strata'))
    seen = []
    for p in out:
        if p not in seen:
            seen.append(p)
    return seen


def _read_json(path):
    try:
        with open(path, 'rb') as f:
            d = json.loads(f.read().decode('utf-8', 'replace'))
    except (OSError, ValueError):
        return {}
    return d if isinstance(d, dict) else {}


def strata_endpoints(state_dirs=None):
    """[(url, key, note)] for every local strata server this box describes.

    The launch config the setup writes carries the port and the API key, the
    ready file the running server writes carries the URL it actually bound; a
    config whose server is not up is still listed (the page then says the server
    is not answering, which is the truth, and heals when it starts).  Newest
    config first: a box that has launched two models keeps both files, and the
    model you started last is the one you want on the panel.
    """
    out = []
    for state in (state_dirs or strata_state_dirs()):
        ready = []
        for p in sorted(glob.glob(os.path.join(state, 'runtime', '*ready.json'))):
            d = _read_json(p)
            url, port = d.get('api_url'), d.get('bound_port')
            if url and port:
                ready.append((str(url).rstrip('/'), int(port), p))
        configs = []
        paths = glob.glob(os.path.join(state, 'launch', '*.json'))
        for p in sorted(paths, key=lambda q: os.path.getmtime(q) if os.path.exists(q) else 0,
                        reverse=True):
            d = _read_json(p)
            if d.get('api_key') and d.get('port'):
                configs.append((int(d['port']), str(d['api_key']),
                                str(d.get('model_name') or ''), p))
        for url, port, rp in ready:
            for cport, key, model, cp in configs:
                if cport == port:
                    out.append((url, key, model or os.path.basename(cp)))
                    break
        for port, key, model, cp in configs:
            # a config with no ready file: the server is not running right now,
            # so poll the address the config says it would use
            if not any(u.endswith(':%d' % port) for u, _, _ in out):
                host = '127.0.0.1'
                out.append(('http://%s:%d' % (host, port), key, model or 'launch/%s' %
                            os.path.basename(cp)))
    env_url = os.environ.get('STRATA_URL')
    if env_url:
        out.insert(0, (env_url.rstrip('/'), os.environ.get('STRATA_API_KEY') or '', 'STRATA_URL'))
    uniq = []
    for e in out:
        if e not in uniq:
            uniq.append(e)
    return uniq


def query_strata(url, key, timeout=3.0):
    """(metrics, '') on a good reply, (None, one-line reason) otherwise.

    The reason is what the AI page shows when it has nothing to draw, so the
    three cases stay distinguishable on the glass: a server that is not
    listening, a server that answers and refuses the key (401: the key file
    moved, which heals when the config is re-read), and a reply that is not the
    metrics document at all.
    """
    req = urllib.request.Request(url.rstrip('/') + '/metrics')
    if key:
        req.add_header('Authorization', 'Bearer %s' % key)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read()
    except urllib.error.HTTPError as e:
        # 401/403 is the key, 404 is an old server with no /metrics at all
        return None, 'HTTP %d %s' % (e.code, (e.reason or '')[:40])
    except urllib.error.URLError as e:
        why = e.reason
        why = (why.strerror if hasattr(why, 'strerror') else str(why)) or 'unreachable'
        return None, 'no server answering (%s)' % str(why)[:48]
    except (OSError, ValueError, TimeoutError) as e:
        return None, 'request failed (%s)' % (e.strerror if hasattr(e, 'strerror')
                                              else e.__class__.__name__)
    try:
        d = json.loads(body.decode('utf-8', 'replace'))
    except ValueError:
        return None, 'reply is not JSON'
    if not isinstance(d, dict) or not isinstance(d.get('live'), dict):
        return None, 'reply has no live block (not a strata metrics document)'
    return d, ''


class StrataThread(threading.Thread):
    """The strata server's /metrics, polled in the background.

    The same rules the GPU poller learned the hard way apply here, and for the
    same reason: this dashboard starts at boot, long before the inference
    server is up, and a model load takes minutes.  A failure is therefore never
    fatal and never the end of polling -- the poll backs off (3, 6, 12, 24, 30s,
    then 2min) and the first good reply lights the page back up by itself.  A
    box with no strata state on disk is the one case with nothing to poll, and
    even that is re-checked slowly: `strata launch` writes its config later.

    Nothing is started, installed or written here -- the poller only reads the
    state files a strata install already owns and issues GET /metrics.

    `stale()` is seconds since the last good reply: the page keeps painting the
    numbers it last had and marks them stale.
    """

    MAX_PERIOD = 30.0
    SLOW_AFTER = 24
    SLOW_PERIOD = 120.0

    def __init__(self, url=None, key=None, state=None, period=2.0, timeout=3.0):
        super().__init__(daemon=True)
        self.base = self.period = period
        self.timeout = timeout
        self.stop = threading.Event()
        self.metrics = None
        self.fail = 0
        self.why = 'probing'
        self.source = ''
        self.good_at = 0.0
        self.state = state
        self.endpoints = [(url, key or os.environ.get('STRATA_API_KEY') or '', '--strata-url')]
        if not url:
            self.endpoints = strata_endpoints(strata_state_dirs(state))
        self.pinned = None
        self.server = ''
        self.i = 0

    def stale(self):
        """Seconds since the last good reply, or None if there never was one."""
        return (time.monotonic() - self.good_at) if self.good_at else None

    def next_endpoint(self):
        if self.pinned is not None:
            return self.pinned
        if not self.endpoints:
            return None
        self.pinned = self.endpoints[self.i % len(self.endpoints)]
        return self.pinned

    def backoff(self):
        if self.fail >= self.SLOW_AFTER:
            self.period = self.SLOW_PERIOD
        else:
            self.period = min(self.MAX_PERIOD, self.base * 2 ** min(self.fail - 1, 4))

    def run(self):
        while not self.stop.is_set():
            ep = self.next_endpoint()
            if ep is None:
                # nothing on disk to ask: keep looking, a strata install writes
                # its config whenever the model is (re)launched
                self.fail += 1
                self.why = 'no local strata server configured'
                self.backoff()
                self.stop.wait(self.period)
                self.endpoints = strata_endpoints(strata_state_dirs(self.state))
                continue
            url, key, note = ep
            d, why = query_strata(url, key, self.timeout)
            if d is not None:
                self.metrics, self.fail, self.why, self.source = d, 0, '', note
                self.server = url
                self.good_at = time.monotonic()
                self.period = self.base
            else:
                self.fail += 1
                self.why = why
                self.backoff()
                if self.pinned is not None:
                    self.i += 1           # a box with two models: try the other one
                    self.pinned = None
            self.stop.wait(self.period)   # also the fastest way out


def snum(v):
    """float for a real number; None for anything the server reports as absent.

    The Overview's rule, copied: a number the server did not report is drawn as
    '--', never as an invented zero, because a zero on a dashboard reads as 'a
    zero was measured'.
    """
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    v = float(v)
    if v != v or v in (float('inf'), float('-inf')):
        return None
    return v


def sseries(history, key, scale=1.0, n=60):
    """One of the server's own minute-of-history series, as plain floats."""
    raw = history.get(key) if isinstance(history, dict) else None
    if not isinstance(raw, list):
        return []
    out = []
    for x in raw[-n:]:
        v = snum(x)
        out.append(None if v is None else v / scale)
    return out


def tok_s(r):
    """A request's decode speed, from its own reported rate or its token/ms."""
    if not isinstance(r, dict):
        return None
    v = snum(r.get('decode_tok_s'))
    if v is not None:
        return v
    out, ms = snum(r.get('output_tokens')), snum(r.get('decode_ms'))
    return round(out / (ms / 1000.0), 1) if out and out >= 2 and ms and ms > 0 else None


def strata_view(d):
    """The monitor Overview's numbers (view 1), from one /metrics reply.

    Field for field what `strata monitor`' first view shows: the model state and
    its progress, the eight tiles (speed, GPU load, VRAM, GPU temp, power, PCIe,
    CPU, disk) with the server's own history for the sparklines, the three bars
    (context fill, experts in VRAM, system RAM) and the recent requests.  With
    two cards, the tiles say what the Overview says: the busiest card's load, the
    hottest card's temp, all the memory and all the watts together.
    """
    if not isinstance(d, dict) or not isinstance(d.get('live'), dict):
        return None
    live = d['live']
    eng = d.get('engine') if isinstance(d.get('engine'), dict) else {}
    hw = d.get('hardware') if isinstance(d.get('hardware'), dict) else {}
    stat = d.get('hardware_static') if isinstance(d.get('hardware_static'), dict) else {}
    hist = d.get('history') if isinstance(d.get('history'), dict) else {}
    totals = d.get('totals') if isinstance(d.get('totals'), dict) else {}
    reqs = [r for r in (d.get('requests') or []) if isinstance(r, dict)]

    raw = str(live.get('state') or 'idle')
    queued = int(snum(live.get('queued')) or 0)
    if eng.get('error') or raw == 'error':
        state = 'Error'
    elif raw == 'reading':
        state = 'Reading'
    elif raw == 'generating':
        state = 'Generating'
    elif queued > 0 and raw not in ('reading', 'generating'):
        state = 'Queued'
    else:
        state = 'Idle'

    gen, maxtok = snum(live.get('generated')), snum(live.get('max_tokens'))
    pread, ptotal = snum(live.get('prompt_read')), snum(live.get('prompt_total'))
    last = reqs[0] if reqs else {}
    if state == 'Reading' and ptotal:
        done, total = pread or 0.0, ptotal
    elif state == 'Generating' and maxtok:
        done, total = gen or 0.0, maxtok
    else:
        done, total = None, None

    ctx_max = snum(eng.get('max_context'))
    pt, po = snum(live.get('prompt_tokens')), gen
    out_last, pr_last = snum(last.get('output_tokens')), snum(last.get('prompt_tokens'))
    if state in ('Reading', 'Generating') and pt is not None and po is not None:
        used = pt + po
    elif pr_last is not None and out_last is not None:
        used = pr_last + out_last            # idle: the last request's fill, like the Overview
    else:
        used = None

    if state == 'Generating' and snum(live.get('tok_s')) is not None:
        speed, note = snum(live.get('tok_s')), 'generating now'
    elif state == 'Reading' and snum(live.get('prefill_tok_s_mean')) is not None:
        speed, note = snum(live.get('prefill_tok_s_mean')), 'reading the prompt'
    else:
        # idle: the run's token-weighted average, not the last request's speed
        out, ms = snum(totals.get('output_tokens')), snum(totals.get('decode_ms'))
        speed = round(out / (ms / 1000.0), 1) if out and out >= 2 and ms and ms > 0 else None
        note = 'run average'
        if speed is None:
            speed, note = tok_s(last), 'last request'

    cards = [c for c in (hw.get('gpus') or []) if isinstance(c, dict)]

    def top(vals):
        vals = [v for v in vals if v is not None]
        return max(vals) if vals else None

    def summ(vals):
        vals = [v for v in vals if v is not None]
        return sum(vals) if vals else None

    util = top([c.get('util') for c in cards]) if cards else snum(hw.get('gpu_util'))
    v_used = summ([c.get('mem_used') for c in cards]) if cards else snum(hw.get('gpu_mem_used'))
    v_total = summ([c.get('mem_total') for c in cards]) if cards else snum(hw.get('gpu_mem_total'))
    temp = top([c.get('temp') for c in cards]) if cards else snum(hw.get('gpu_temp'))
    power = summ([c.get('power') for c in cards]) if cards else snum(hw.get('gpu_power'))
    # the per-card rows do not always carry a power limit, so the whole-computer
    # reading is the fallback (a limit of '--' would leave the tile with no scale)
    plimit = summ([c.get('power_limit') for c in cards])
    if plimit is None:
        plimit = snum(hw.get('gpu_power_limit'))
    vram_hist = sseries(hist, 'gpu_mem_used', 2 ** 30)
    if len(cards) > 1:
        # the server keeps a history of its first card only: a flat line for all
        # cards is honest, a summed history built from one card is not
        vram_hist = [v_used] * 60 if v_used is not None else []

    return {
        'model': eng.get('model') or stat.get('model') or '',
        'state': state, 'queued': queued, 'phase': live.get('phase') or '',
        'progress': (done, total), 'ctx': (used, ctx_max),
        'speed': (speed, note), 'speed_hist': sseries(hist, 'tok_s'),
        'gpus': len(cards) if cards else int(snum(stat.get('gpu_count')) or 0),
        'gpu_name': stat.get('gpu_name') or '',
        'util': util, 'util_hist': sseries(hist, 'gpu_util'),
        'vram': (v_used / 2 ** 30 if v_used is not None else None,
                 v_total / 2 ** 30 if v_total is not None else None),
        'vram_hist': vram_hist,
        'temp': temp, 'temp_hist': sseries(hist, 'gpu_temp'),
        'power': (power, plimit), 'power_hist': sseries(hist, 'gpu_power'),
        'pcie': (snum(hw.get('gpu_pcie_rx_mb')),
                 snum(hw.get('gpu_pcie_gen')) or snum(hw.get('gpu_pcie_gen_max')),
                 snum(hw.get('gpu_pcie_width'))),
        'pcie_hist': sseries(hist, 'gpu_pcie_rx_mb'),
        'cpu': snum(hw.get('cpu_pct')) if hw.get('cpu_pct') is not None else snum(hw.get('cpu')),
        'cpu_hist': sseries(hist, 'cpu'),
        'cores': int(snum(stat.get('cores')) or 0),
        'disk': (snum(hw.get('disk_read_mb')), snum(hw.get('disk_write_mb'))),
        'disk_hist': sseries(hist, 'disk_read_mb'),
        'ram': (snum(hw.get('ram_used')), snum(hw.get('ram_total'))),
        'experts': (snum(eng.get('expert_slots')), snum(eng.get('experts')),
                    snum(eng.get('expert_cache_mib'))),
        'totals': {k: snum(totals.get(k)) for k in ('requests', 'prompt_tokens',
                                                   'output_tokens', 'reused')},
        'recent': [{'tps': tok_s(r), 'prompt': snum(r.get('prompt_tokens')),
                    'reused': snum(r.get('reused')),
                    'hit': snum(r.get('hit_rate')),
                    'output': snum(r.get('output_tokens')),
                    'dur': snum(r.get('duration_s')),
                    'finish': str(r.get('finish') or r.get('finish_reason') or ''),
                    'error': bool(r.get('error'))} for r in reqs[:8]],
        'sampled_at': snum(d.get('time')),
    }


def fmt_tok(v):
    return '--' if v is None else '%s' % format(int(round(v)), ',d')


def fmt_k(v):
    """262144 -> '262K': the Overview's context label, in the panel's font."""
    return '--' if v is None else ('%dK' % int(round(v / 1024.0)) if v >= 1024
                                   else '%d' % int(round(v)))


# ── metrics ────────────────────────────────────────────────────────────
class Metrics:
    def __init__(self, gpu=True, history=180, interval=1.0, strata=True,
                 strata_url=None, strata_key=None, strata_state=None):
        self.interval = interval
        self.hist = history
        self.hist_cpu = [0.0] * history
        self.hist_temp = [0.0] * history
        self.hist_net = [0.0] * history
        self.gpu_thread = GPUThread() if gpu else None
        self.strata_thread = StrataThread(url=strata_url, key=strata_key,
                                          state=strata_state) if strata else None
        self.sensors = scan_hwmon()
        self.host = socket.gethostname()
        self.ip = ''
        self._ip_at = 0.0
        try:
            self.kernel = open('/proc/sys/kernel/osrelease').read().strip()
        except OSError:
            self.kernel = '?'
        self.nproc = os.cpu_count() or 1
        self._prev_cpu = self._read_cpu()
        self._prev_net = self._read_net()
        self._prev_procs = self._read_procs()[0]
        self._prev_stat = (0, 0)
        self._prev_t = time.time()
        self.gpu = None
        self.hist_gpu = []          # per-GPU {'util':[], 'temp':[]}
        self.strata = None          # last /metrics reply (the AI page's source)
        self.strata_age = None      # seconds since that reply, None: never had one
        self.net = {}
        self.disks = []
        self.mem = {'total': 0, 'used': 0, 'swap_total': 0, 'swap_used': 0}
        self.load = [0.0] * 3
        self.uptime = 0.0
        self.freqs = []
        self.top_cpu, self.top_mem = [], []
        self.proc_ewma = {}
        self.proc_period = 3.0        # seconds between /proc process walks
        self._last_procs = 0.0
        self.ctxt_rate = self.intr_rate = self.fork_rate = 0.0
        self.proc_total = self.tasks = self.kthreads = self.threads_total = 0
        self._prev_forks = 0
        self.cpu_all, self.cpu_temp, self.core_temps = 0.0, None, []

    @staticmethod
    def _read_cpu():
        out = {}
        try:
            with open('/proc/stat') as f:
                for line in f:
                    if not line.startswith('cpu'):
                        break
                    p = line.split()
                    v = [int(x) for x in p[1:]]
                    # user nice system idle iowait irq softirq steal
                    busy = v[0] + v[1] + v[2] + v[5] + v[6] + (v[7] if len(v) > 7 else 0)
                    out[p[0]] = (busy, sum(v))
        except (OSError, ValueError, IndexError):
            pass
        return out

    @staticmethod
    def _read_net():
        out = {}
        for p in glob.glob('/sys/class/net/*/statistics/rx_bytes'):
            iface = os.path.basename(os.path.dirname(os.path.dirname(p)))
            if iface == 'lo':
                continue
            try:
                rx = int(open(p).read())
                tx = int(open(p.replace('rx_bytes', 'tx_bytes')).read())
                oper = open(os.path.join(os.path.dirname(os.path.dirname(p)),
                                         'operstate')).read().strip()
            except (OSError, ValueError):
                continue
            out[iface] = (rx, tx, oper)
        return out

    @staticmethod
    def _read_procs():
        """pid -> (name, cpu ticks, rss).  Also returns parent map and thread
        counts, because `ps`-equivalent numbers need them: everything under
        kthreadd (pid 2) is a kernel thread, and a userspace process is many
        threads."""
        out, ppid, threads = {}, {}, {}
        page = os.sysconf('SC_PAGE_SIZE')
        try:
            entries = [e.name for e in os.scandir('/proc') if e.name.isdigit()]
        except OSError:
            return out, ppid, threads
        for pid in entries:
            base = '/proc/' + pid
            try:
                stat = open(base + '/stat').read().split()
                out[pid] = (stat[1].strip('()'), int(stat[13]) + int(stat[14]),
                            int(stat[23]) * page)
            except (OSError, IndexError, ValueError):
                continue
            try:
                for line in open(base + '/status'):
                    if line.startswith('PPid:'):
                        ppid[pid] = str(int(line.split()[1]))
                    elif line.startswith('Threads:'):
                        threads[pid] = int(line.split()[1])
                        break
            except (OSError, IndexError, ValueError):
                ppid[pid] = '0'
        return out, ppid, threads

    def sample(self):
        now = time.time()
        dt = max(0.001, now - self._prev_t)
        self._prev_t = now
        # the address is only worth re-reading slowly: it changes when a link
        # comes or goes, not every frame
        if now - self._ip_at > 30.0:
            self._ip_at = now
            ip = host_ip()
            if ip:
                self.ip = ip

        cpu = self._read_cpu()
        busy = {}
        for k, (b, t) in cpu.items():
            pb, pt = self._prev_cpu.get(k, (b, t))
            busy[k] = 100.0 * (b - pb) / (t - pt) if t > pt else 0.0
        self._prev_cpu = cpu
        self.cpu_all = busy.get('cpu', 0.0)
        self.cpu_cores = [busy.get('cpu%d' % i, 0.0) for i in range(self.nproc)]

        net = self._read_net()
        self.net = {}
        for iface, (rx, tx, oper) in net.items():
            prx, ptx, _ = self._prev_net.get(iface, (rx, tx, oper))
            self.net[iface] = (max(0.0, (rx - prx)) / dt * 8 / 1e6,
                               max(0.0, (tx - ptx)) / dt * 8 / 1e6, oper)
        self._prev_net = net

        # The /proc walk is the most expensive part of a sample and process
        # lists move slowly, so it runs every --proc-period seconds; between
        # rescans the frame reuses the last list (CPU%, RAM and net still
        # refresh every frame)
        if now - self._last_procs >= self.proc_period:
            procs, ppid, thread_count = self._read_procs()
            proc_dt = max(0.001, now - self._last_procs)
            prev = self._prev_procs
            self._prev_procs = procs
            self._last_procs = now
            # Smooth CPU% per pid: a 1 s sample of a bursting process is mostly
            # noise, and a list that reorders every frame is unreadable on glass.
            alpha, ew = 0.35, self.proc_ewma
            rows = {}
            for pid, (comm, t, rss) in procs.items():
                pcomm, pt = prev.get(pid, (comm, t, rss))[:2]
                if pcomm != comm:
                    # pid reuse: a dead process' tick total is not this
                    # process' baseline, so the first sample of a reused pid
                    # is not a delta
                    pt = t
                pct = 100.0 * (t - pt) / (100.0 * proc_dt)
                pct = max(0.0, min(pct, 100.0 * os.cpu_count()))
                old = ew.get(pid)
                rows[pid] = (comm, pct if old is None else
                             old[1] * (1 - alpha) + pct * alpha, rss)
            self.proc_ewma = rows
            # kernel threads = every task that is a descendant of kthreadd (pid 2)
            kt = set()
            for pid in ppid:
                cur, seen, hops = pid, set(), 0
                while cur and cur not in seen and hops < 24:
                    seen.add(cur)
                    if cur == '2':
                        kt.add(pid)
                        break
                    cur = ppid.get(cur, '0')
                    hops += 1
            self.tasks = len(ppid)
            self.kthreads = len(kt)
            self.proc_total = self.tasks - self.kthreads
            self.threads_total = sum(thread_count.values())
            self.top_cpu = sorted(rows.values(), key=lambda r: -r[1])[:14]
            self.top_mem = sorted(rows.values(), key=lambda r: -r[2])[:10]

        try:
            ct = it = pr = None
            for line in open('/proc/stat'):
                if line.startswith('ctxt'):
                    ct = int(line.split()[1])
                elif line.startswith('intr'):
                    it = int(line.split()[1])
                elif line.startswith('processes'):
                    pr = int(line.split()[1])
                if ct is not None and it is not None and pr is not None:
                    break
            if ct is not None:
                p = self._prev_stat
                self.ctxt_rate = (ct - p[0]) / dt if p[0] else 0.0
                self.intr_rate = (it - p[1]) / dt if p[1] else 0.0
                self._prev_stat = (ct, it)
            if pr is not None:
                # /proc/stat "processes" is a cumulative fork counter since boot.
                # It is never a process count; use it as a rate, and never let
                # the fallback path treat it as one.
                self.fork_rate = (pr - self._prev_forks) / dt if self._prev_forks else 0.0
                self._prev_forks = pr
                self._forks_seen = pr
        except (OSError, ValueError, IndexError):
            pass

        try:
            self.load = [float(v) for v in open('/proc/loadavg').read().split()[:3]]
        except (OSError, ValueError):
            pass

        try:
            mi = {}
            for line in open('/proc/meminfo'):
                k, v = line.split(':')[0], line.split(':')[1].split()[0]
                mi[k] = int(v) * 1024
            self.mem['total'] = mi.get('MemTotal', 0)
            self.mem['used'] = self.mem['total'] - mi.get('MemAvailable',
                                                          mi.get('MemFree', 0))
            self.mem['swap_total'] = mi.get('SwapTotal', 0)
            self.mem['swap_used'] = mi.get('SwapTotal', 0) - mi.get('SwapFree', 0)
        except (OSError, ValueError, IndexError):
            pass

        self.disks = []
        try:
            seen = set()
            for line in open('/proc/mounts'):
                p = line.split()
                if len(p) < 3:
                    continue
                dev, mp, fstype = p[0], p[1], p[2]
                if fstype not in ('ext4', 'xfs', 'btrfs', 'ext2', 'f2fs', 'zfs',
                                  'ntfs3', 'bcachefs', 'vfat') or mp.startswith(('/proc', '/sys', '/dev')):
                    continue
                seen.add((dev, mp))
            for dev, mp in sorted(seen):
                try:
                    du = shutil.disk_usage(mp)
                except OSError:
                    continue
                if du.total > 0:
                    self.disks.append((mp, du.used / du.total, du.total))
            self.disks.sort(key=lambda r: -r[1])
        except OSError:
            pass

        try:
            self.uptime = float(open('/proc/uptime').read().split()[0])
        except (OSError, ValueError):
            pass

        self.freqs = []
        for p in glob.glob('/sys/devices/system/cpu/cpu*/cpufreq/scaling_cur_freq'):
            try:
                self.freqs.append(int(open(p).read()) / 1000.0)
            except (OSError, ValueError):
                pass

        s = self.sensors
        self.core_temps = [c['temp'] for c in s['cpu_cores']]
        self.cpu_temp = s['cpu']['temp'] if s['cpu'] else (
            max(self.core_temps) if self.core_temps else
            (max(t['temp'] for t in s['thermal']) if s['thermal'] else None))
        self.nvme = {}
        for item in s['nvme']:
            dev = item['chip'].replace('hwmon', 'hw')
            cur = self.nvme.get(dev, {}).get('temp', -1e9)
            self.nvme[dev] = {'temp': max(cur, item['temp']),
                              'label': item['label'] or 'Composite'}
        self.chassis = []
        for i, item in enumerate(s['chassis']):
            lab = item['label'] or 'T%d' % (_sortkey(item['chip']) + 1)
            self.chassis.append(('%s %s' % (item['name'][:6], lab), item['temp']))
        self.nic = [(i['name'], i['temp']) for i in s['nic']]
        self.fans = [(f['label'], f['rpm']) for f in s['fans']]

        if self.gpu_thread is not None:
            self.gpu = self.gpu_thread.gpus
        if self.strata_thread is not None:
            # a failed poll leaves the last reply in place: the page keeps its
            # numbers and marks their age, exactly like the GPU page
            self.strata = self.strata_thread.metrics
            self.strata_age = self.strata_thread.stale()
        while len(self.hist_gpu) < len(self.gpu or []):
            self.hist_gpu.append({'util': [0.0] * self.hist, 'temp': [0.0] * self.hist})
        for i, g in enumerate(self.gpu or []):
            if i < len(self.hist_gpu):
                self.hist_gpu[i]['util'].append(g.get('util') or 0.0)
                self.hist_gpu[i]['temp'].append(g.get('temp') or 0.0)
                for k in ('util', 'temp'):
                    if len(self.hist_gpu[i][k]) > self.hist:
                        del self.hist_gpu[i][k][:-self.hist]

        self.hist_cpu.append(self.cpu_all)
        self.hist_temp.append(self.cpu_temp or 0.0)
        self.hist_net.append(sum(r + t for r, t, _ in self.net.values()))
        for h in (self.hist_cpu, self.hist_temp, self.hist_net):
            if len(h) > self.hist:
                del h[:-self.hist]

    def shutdown(self):
        if self.gpu_thread is not None:
            self.gpu_thread.stop.set()
        if self.strata_thread is not None:
            self.strata_thread.stop.set()


# ── formatting helpers ─────────────────────────────────────────────────
def ellipsize(c, s, scale, width):
    """Shorten s with a '~' tail until it fits `width` px at a fixed `scale`.

    Shrinking the font of one row is what made a long row (a 4-digit CPU%)
    render smaller than its neighbours: numbers stay at the same size and the
    name gives way instead.
    """
    if c.text_w(s, scale) <= width:
        return s
    keep = len(s)
    while keep > 1 and c.text_w(s[:keep] + '~', scale) > width:
        keep -= 1
    return s[:keep] + '~'


def pick_scale(c, text, scales, width):
    """Largest glyph scale at which `text` fits `width` pixels."""
    for sc in scales:
        if c.text_w(text, sc) <= width:
            return sc
    return scales[-1]


def cell_row(c, x, y, w, cells, scale=2, row_h=22, max_rows=1):
    """Flow label/value cells across a panel row, wrapping to new rows.

    A long joined string is what used to get cut at the panel edge, losing the
    tail; wrapping keeps every cell readable.  Returns the number of rows used
    so the caller can tell you when the content no longer fits.
    """
    rows = 1
    cx = x
    for label, value, color in cells:
        lw, vw = c.text_w(label, scale), c.text_w(value, scale)
        if cx > x and cx + lw + 6 + vw > x + w:
            rows += 1
            if rows > max_rows:
                c.over.append('CELLROW %d cells need %d rows, %d allowed (last %r)' %
                              (len(cells), rows, max_rows, label))
                return rows
            y += row_h
            cx = x
        c.text(cx, y, label, scale, DIM)
        cx += lw + 6
        c.text(cx, y, value, scale, color)
        cx += vw + 14
    return rows


def temp_color(t):
    if t is None:
        return DIM
    if t >= TEMP_CRIT:
        return CRIT
    if t >= TEMP_WARN:
        return WARN
    return OK


def fmt_bytes(n):
    for unit, div in (('T', 1e12), ('G', 1e9), ('M', 1e6), ('K', 1e3)):
        if n >= div:
            return '%3.0f%s' % (n / div, unit)
    return '  0 '


def host_ip():
    """Address this box uses for its default route, without sending anything.

    A connected UDP socket picks the source address the kernel would route
    from; it never transmits a packet, so it works with no internet.  Falls
    back to the address the hostname resolves to, then to nothing."""
    for family, target in ((socket.AF_INET, ('224.0.0.1', 1)),
                           (socket.AF_INET6, ('ff02::1', 1))):
        try:
            s = socket.socket(family, socket.SOCK_DGRAM)
            try:
                s.connect(target)
                ip = s.getsockname()[0]
                if family == socket.AF_INET:
                    return ip
                best = ip
            finally:
                s.close()
        except OSError:
            continue
    for cand in ('best', None):
        try:
            infos = socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)
            for i in infos:
                a = i[4][0]
                if not a.startswith('127.'):
                    return a
        except OSError:
            pass
    return ''


def fmt_dur(s):
    d, s = divmod(int(s), 86400)
    h, s = divmod(s, 3600)
    m, _ = divmod(s, 60)
    return '%dd %02d:%02d' % (d, h, m) if d else '%02d:%02d' % (h, m)


def short_iface(name):
    return name.replace('enp', 'e').replace('eno', 'e')


# A panel's title band is measured from the font: a gap above the title, the
# title itself, and padding below it, so a panel's first row never touches the
# caption at any font size.
TITLE_TOP = 6
TITLE_PAD = 6


def band(c):
    """Height of a panel's title band; every panel height includes it."""
    return TITLE_TOP + c.text_h(2) + TITLE_PAD


# ── layout: header + paged bodies ──────────────────────────────────────
PAGES = ('CPU', 'GPU', 'PROC', 'AI')   # GPU = all GPUs side by side
# GPU1..GPU9 pin one GPU to the whole screen (name fits, histories are huge)
# AI = the strata monitor's Overview (view 1), read from the server's /metrics


def debug_grid(c, args):
    c.clip = None
    for i, (x, y, w, h, title) in enumerate(c.panels):
        c.outline(x, y, x + w, y + h, CRIT)
        c.rect(x, y, x + 3, y + 14, CRIT)
        c.text(x + 5, y + 1, 'P%d' % i, 2, CRIT)
        c.text_right(x + w - 1, y + 1, '%dx%d' % (w, h), 2, CRIT)
    for x in range(0, c.w, 100):
        c.rect(x, 0, x + 1, 8, WARN)
        c.text(x, 9, '%d' % x, 1, WARN)
    for y in range(0, c.h, 50):
        c.rect(0, y, 8, y + 1, WARN)
        c.text(9, y, '%d' % y, 1, WARN)
    print('panels: ' + '  '.join('P%d %s %dx%d @%d,%d' %
                                 (i, t, w, h, x, y) for i, (x, y, w, h, t) in enumerate(c.panels)))


def overlaps(boxes, gap=2):
    """Report drawn strings that collide (axis-aligned box intersection)."""
    hits = []
    n = len(boxes)
    for i in range(n):
        ax0, ay0, ax1, ay1, a, asx = boxes[i]
        for j in range(i + 1, n):
            bx0, by0, bx1, by1, b, bsx = boxes[j]
            ox = min(ax1, bx1) - max(ax0, bx0) - gap
            oy = min(ay1, by1) - max(ay0, by0) - gap
            if ox > 0 and oy > 0:
                hits.append('%r x %r  overlap %dx%d px' % (a, b, ox, oy))
    return hits


def panel(c, x, y, w, h, title, sub=None):
    c.clip = (x, y, x + w, y + h)
    c.aa_bg = PANEL
    c.panels.append((x, y, w, h, title))
    c.rect(x, y, x + w, y + h, PANEL)
    c.rect(x, y, x + w, y + 2, PANEL2)
    c.text(x + 8, y + TITLE_TOP, title, 2, DIM)
    if sub:
        # the title is drawn first, so a subtitle long enough to reach it is a
        # real bug: measure, shrink, and report what had to go
        sw = c.text_w(sub, 2)
        room = w - 16 - 12 - c.text_w(title, 2)
        if sw > room:
            n = len(sub)
            while n > 0 and c.text_w(sub[:n], 2) > room:
                n -= 1
            c.over.append('TITLE-SUB %s + %s needs %dpx, %dpx room, kept %r' %
                          (repr(title), repr(sub), sw, room, sub[:n]))
            sub = sub[:n]
        c.text_right(x + w - 8, y + TITLE_TOP, sub, 2, DIM)
    return (x + 8, y + band(c))


def big_number(c, x, y, value, unit, color, scale=7):
    c.text(x, y, value, scale, color)
    w = c.text_w(value, scale)
    c.text(x + w + 5, y + scale, unit, 3, DIM)


def draw_header(c, m, args, page, idx):
    W = c.w
    now = time.localtime()
    c.clip = (0, 0, W, 46)          # header strings get measured like panels
    c.rect(0, 0, W, 46, PANEL2)
    c.aa_bg = PANEL2
    c.text(8, 5, args.title, 3, ACCENT)

    clock = time.strftime('%H:%M:%S', now)
    cw = c.text_w(clock, 3)
    # the countdown dial owns the top-right corner, so the clock and the
    # host/uptime line are measured out from its left edge
    dial_on = args.page_seconds > 0 and len(args.page_list) > 1
    # default size: exactly the clock's own digit height, so the dial reads as
    # part of the clock and not as an object of its own; --ring N overrides it
    clock_h = c.ink_h(clock, 3)
    r = args.ring if args.ring > 0 else max(6, clock_h // 2)
    dial_on = dial_on and r > 0
    edge = W - 8 - (2 * r + 12 if dial_on else 0)
    c.text_right(edge, 5, clock, 3, TEXT)

    # tabs: which page is up, and where the next one is in the cycle.
    # The box is built around the measured glyph box with equal padding top
    # and bottom, so a bigger font size moves the box, it does not spill out
    tab_pad = 4
    tab_h = c.text_h(2)
    tab_y = 8
    x = 8 + c.text_w(args.title, 3) + 20
    for i, name in enumerate(args.page_list):
        w = c.text_w(name, 2) + 14
        active = (i == idx)
        c.rect(x, tab_y, x + w, tab_y + tab_h + 2 * tab_pad, PANEL if active else BG)
        c.outline(x, tab_y, x + w, tab_y + tab_h + 2 * tab_pad, ACCENT if active else GRID)
        c.text(x + 7, tab_y + tab_pad, name, 2, ACCENT if active else DIM)
        x += w + 6
    info = 'IP %s  UP %s' % (m.ip or m.host, fmt_dur(m.uptime))
    iw = c.text_w(info, 2)
    c.text(edge - cw - 14 - iw, tab_y + tab_pad, info, 2, DIM)
    if dial_on:
        # page-rotation countdown as a dial: one sector per second, and a sector
        # goes dark as its second passes, so no text is needed to read the cycle
        left = args.page_seconds - (time.time() - args.page_t0) % args.page_seconds
        sectors = max(2, int(round(args.page_seconds)))
        c.ring(W - 8 - r, 5 + clock_h // 2, r, sectors, int(math.ceil(left)),
               ACCENT, PANEL2)


# ── page 1: CPU + system, histories, RAM/disks/network ─────────────────
def body_cpu(c, m, args, y0, h):
    W = c.w
    col = (W - 32) // 3
    x0, x1, x2 = 8, 8 + col + 8, 8 + 2 * (col + 8)

    # ── CPU + system ──────────────────────────────────────────────────
    ix, iy = panel(c, x0, y0, col, h, 'CPU', '%d threads' % m.nproc)
    # sizes are shared with the RAM panel so the two read as one system:
    # package temp at the used-RAM number size, total utilisation at the RAM
    # percentage size
    T, U = 6, 3
    big_number(c, ix, iy, '%d' % (m.cpu_temp or 0), 'C', temp_color(m.cpu_temp), T)
    utxt = '%3.0f%%' % m.cpu_all
    c.text_right(x0 + col - 8, iy + (c.text_h(T) - c.text_h(U)) // 2, utxt, U, TEXT)
    row = c.text_h(2)
    bar_y = iy + c.text_h(T) + 10
    c.bar(ix, bar_y, col - 16, 16, m.cpu_all / 100.0,
          ACCENT if m.cpu_all < 80 else WARN)
    yy = bar_y + 26
    ltxt = 'LOAD ' + ' '.join('%.2f' % v for v in m.load)
    c.text(ix, yy, ltxt, pick_scale(c, ltxt, (2, 1), col - 16), TEXT)
    yy += row + 6
    if m.freqs:
        ctxt = 'CLOCK %.2f / %.2f GHZ' % (min(m.freqs) / 1000.0, max(m.freqs) / 1000.0)
        c.text(ix, yy, ctxt, pick_scale(c, ctxt, (2, 1), col - 16), DIM)
        yy += row + 6
    c.text(ix, yy, 'TOP CPU', 2, DIM)
    top_top = yy + row + 4
    # One font size for the whole list.  Pick the scale from the widest
    # *number* column, then make an over-long row fit by shortening the name
    # with '~' -- shrinking the font of one row is what made a 4-digit CPU%
    # (a process with more than 999%% of one core) render smaller than its
    # neighbours.
    avail = col - 16
    # rows left after reserving the four counter rows that follow the list:
    # the panel's bottom clip, minus the counters, minus the list's own first row
    row_h = c.text_h(2)
    last_bottom = y0 + h - 8 - row_h
    room = int((last_bottom - 3 * (row_h + 2) - (yy + row_h + 4)) // (row_h + 4))
    room = max(1, min(6, room))
    tops = [(comm, pct) for comm, pct, rss in m.top_cpu[:room]]
    if room < 6:
        c.info.append('TOP CPU shows %d of 6 rows at this font size' % room)
    advance = lambda sc: 6 * sc + 1
    sc = 2
    while sc > 1 and tops and \
            c.text_w('XXXX', sc) + advance(sc) + \
            max(c.text_w('%5.1f%%' % pct, sc) for _, pct in tops) > avail:
        sc -= 1
    num_w = max((c.text_w('%5.1f%%' % pct, sc) for _, pct in tops), default=0)
    name_px = avail - num_w - advance(sc)
    for i, (comm, pct) in enumerate(tops):
        num = '%5.1f%%' % pct
        name = ellipsize(c, comm, sc, name_px)
        # names flush left, percentages flush right on the same edge the system
        # list uses, so the whole column lines up
        c.text(ix, top_top + i * (row + 4), name, sc, TEXT)
        c.text_right(x0 + col - 8, top_top + i * (row + 4), num, sc, TEXT)
    # the counter list starts under the last row *actually drawn*
    yy = top_top + len(tops) * (row + 4) + 4
    # just the process counters: the sensor rows (nic / fan / zone) live on the
    # PROC page with the rest of the SYSTEM list, and the CPU page spends its
    # vertical budget on the process table instead
    lines = [('THREADS', '%d' % m.nproc),
             ('PROCESSES', '%d of %d' % (m.proc_total, m.tasks)),
             ('CTX / SEC', '%d' % int(m.ctxt_rate))]
    row_box = c.text_h(2)
    lead = row_box + 2
    limit = y0 + h - 8 - row_box
    room = max(1, int((limit - yy) // lead))
    if room < len(lines):
        c.info.append('SYSLIST %d of %d rows fit in the panel at this font size' %
                      (room, len(lines)))
    for i, (k, v) in enumerate(lines[:room]):
        ry = yy + i * lead
        if ry + row_box > limit:
            break
        label = k
        c.text(ix, ry, label, 2, DIM)
        vx = ix + c.text_w(label, 2) + 6
        # values on the right edge, like the SYSTEM panel: the numbers then read
        # as a column instead of starting where the last label ended
        v = ellipsize(c, v, 2, x0 + col - 8 - vx)
        c.text_right(x0 + col - 8, ry, v, 2, TEXT)

    # ── histories ─────────────────────────────────────────────────────
    ix, iy = panel(c, x1, y0, col, h, 'CPU HISTORY')
    gh = (h - band(c) - 4 * 20 - 30) // 2
    c.spark(ix, iy, col - 16, gh, m.hist_cpu, 0, 100, ACCENT)
    c.outline(ix, iy, ix + col - 16, iy + gh + 1, GRID)
    c.text(ix, iy + gh + 6, 'UTIL %3.0f%%  PEAK %3.0f%%' %
           (m.cpu_all, max(m.hist_cpu) if m.hist_cpu else 0), 2, DIM)
    ty = iy + gh + 30
    hi = max(90.0, (max(m.hist_temp) if m.hist_temp else 0) + 5)
    c.spark(ix, ty, col - 16, gh, m.hist_temp, 0, hi, OK)
    c.outline(ix, ty, ix + col - 16, ty + gh + 1, GRID)
    c.text(ix, ty + gh + 6, 'TEMP %.0f C  PEAK %.0f C' %
           (m.cpu_temp or 0, max(m.hist_temp) if m.hist_temp else 0), 2, DIM)


    # ── RAM, disks, network stacked so they cannot overlap ────────────
    # right column heights come from the font: the RAM panel is a big number,
    # a bar, a swap row and a bar; disks are label+bar per mount; the network
    # panel takes what is left and sizes its spark from that
    big_h, row_h = c.text_h(6), c.text_h(2)
    head_band = band(c)
    h_ram = head_band + big_h + 26 + row_h + 32
    # three disk rows if the network panel still gets its interface line, its
    # rx/tx row, a spark and the peak label; two disk rows otherwise
    disk_n = 3
    h_disk = head_band + disk_n * (row_h + 10) + 8
    h_net = h - h_ram - h_disk - 20
    need_net = head_band + 6 + row_h + 6 + row_h + 8 + 24   # 24 px keeps the spark readable
    if h_net < need_net:
        disk_n = 2
        h_disk = head_band + disk_n * (row_h + 10) + 8
        h_net = h - h_ram - h_disk - 20
        c.info.append('DISKS shows 2 of 3 mounts so NETWORK keeps its graph')

    ix, iy = panel(c, x2, y0, col, h_ram, 'RAM',
                   '%.1f GB' % (m.mem['total'] / 2**30))
    frac = m.mem['used'] / max(1, m.mem['total'])
    val = '%.1f' % (m.mem['used'] / 2**30)
    c.text(ix, iy, val, 6, OK if frac < 0.8 else WARN)
    xu = ix + c.text_w(val, 6) + 8
    pct_txt = '%3.0f%%' % (frac * 100)
    # the unit only goes in when the percentage still fits beside it
    if xu + c.text_w('GB', 3) + 12 + c.text_w(pct_txt, 3) <= x2 + col - 8:
        c.text(xu, iy + 18, 'GB', 3, DIM)
    c.text_right(x2 + col - 8, iy + 18, pct_txt, 3,
                 OK if frac < 0.8 else WARN)
    bar_y = iy + big_h + 8
    c.bar(ix, bar_y, col - 16, 14, frac, OK if frac < 0.8 else WARN)
    swap_y = bar_y + 20
    if m.mem['swap_total']:
        sf = m.mem['swap_used'] / max(1, m.mem['swap_total'])
        c.text(ix, swap_y, 'SWAP %.1f/%.1f GB' %
               (m.mem['swap_used'] / 2**30, m.mem['swap_total'] / 2**30), 2, TEXT)
        c.text_right(x2 + col - 8, swap_y, '%3.0f%%' % (sf * 100), 2,
                     WARN if sf > 0.1 else OK)
        c.bar(ix, swap_y + row_h + 4, col - 16, 12, sf, WARN if sf > 0.1 else OK)
    else:
        c.text(ix, swap_y, 'SWAP NONE', 2, DIM)

    ix, iy = panel(c, x2, y0 + h_ram + 10, col, h_disk, 'DISKS', 'fill')
    disk_row = row_h + 10
    shown = m.disks[:disk_n]
    # one bar geometry for every mount: the bar column starts where the longest
    # name ends, so all bars share the same x and the same width and only their
    # fill differs.  Names stay flush left.
    name_w = max((c.text_w(mp, 2) for mp, _, _ in shown), default=0)
    bar_x = ix + name_w + 12
    bar_w = (x2 + col - 8) - c.text_w('100%', 2) - 10 - bar_x
    for i, (mp, used, total) in enumerate(shown):
        yy = iy + i * disk_row
        c.text(ix, yy, mp, 2, TEXT)
        lab2 = '%3.0f%%' % (used * 100)
        c.text_right(x2 + col - 8, yy, lab2, 2, OK if used < 0.8 else WARN)
        # the bar rides the same line as its name, centred on the glyph box
        c.bar(bar_x, yy + (row_h - 12) // 2, max(8, bar_w), 12, used,
              OK if used < 0.8 else WARN)

    ny = y0 + h_ram + h_disk + 20
    ix, iy = panel(c, x2, ny, col, h_net, 'NETWORK  Mb/s')
    nets = sorted(m.net.items(), key=lambda kv: -(kv[1][0] + kv[1][1]))[:1]
    if nets:
        iface, (rx, tx, oper) = nets[0]
        row = c.text_h(2)
        head = row                      # interface name is a normal-size row
        rx_txt, tx_txt = 'rx %6.1f' % rx, 'tx %6.1f' % tx
        state_txt = 'UP' if oper == 'up' else 'DOWN'
        peak_txt = 'PEAK %.1f' % max(m.hist_net)
        # the name gets whatever width is left of PEAK on the title line
        name = ellipsize(c, short_iface(iface), 2,
                         x2 + col - 8 - c.text_w(state_txt, 2) - 16 - c.text_w(peak_txt, 2) - 16 - ix)
        c.text(ix, iy, name, 2, TEXT if oper == 'up' else CRIT)
        c.text_right(x2 + col - 8, iy, state_txt, 2, OK if oper == 'up' else CRIT)
        rx_txt, tx_txt = 'rx %6.1f' % rx, 'tx %6.1f' % tx
        # peak rides the title line next to the link state: a label under the
        # graph would eat a row the graph needs at bigger font sizes
        y_rx = iy + head + 6
        c.text(ix, y_rx, rx_txt, 2, TEXT)
        c.text_right(x2 + col - 8, y_rx, tx_txt, 2, TEXT)
        c.text_right(x2 + col - 8 - c.text_w(state_txt, 2) - 16, iy, peak_txt, 2, DIM)
        sy = iy + head + 6 + row + 8
        sh = (h_net - band(c) - 6) - (head + 6 + row + 8) - 6
        if sh >= 14:
            c.spark(ix, sy, col - 16, sh, m.hist_net, 0,
                    max(10.0, max(m.hist_net) * 1.1), ACCENT)
            c.outline(ix, sy, ix + col - 16, sy + sh + 1, GRID)
        else:
            c.info.append('NETWORK dropped its history: panel holds %d px at this size' %
                          max(0, sh))
    else:
        c.text(ix, iy, 'no interfaces', 2, CRIT)


# ── page 2: GPU ────────────────────────────────────────────────────────
def gpu_title(name):
    """'NVIDIA GeForce RTX 5090' -> 'RTX 5090': panel titles are 309px wide."""
    n = (name or 'GPU').replace('NVIDIA ', '').replace('GeForce ', '').strip()
    return n[:16]


def body_gpu(c, m, args, y0, h, only=None):
    W = c.w
    th = m.gpu_thread
    stale = th.stale() if th is not None else None
    gpus = m.gpu or []
    if only is not None:
        gpus = gpus[only:only + 1]
    if not gpus:
        # an empty GPU page has three causes and they need three messages:
        # --no-gpu (asked for), no nvidia-smi at all (this box has no NVIDIA),
        # and a query failing right now (at boot that is the driver racing us,
        # and it heals on its own -- the poller never gives up on that)
        sub, head, col = 'no nvidia-smi result', 'QUERY FAILED OR NO GPU', CRIT
        rows = []
        if th is None:
            sub, head, col = 'queries disabled', 'GPU PAGE DISABLED', DIM
            rows = ['this run has --no-gpu']
        elif th.disabled:
            sub, head = 'nvidia-smi not installed', 'NO NVIDIA-SMI'
            rows = ['install the NVIDIA driver, then: nvidia-smi -L']
        elif stale is None:
            head, col = 'PROBING nvidia-smi', (WARN if th.fail < 20 else CRIT)
            rows = ['attempt %d, next in %.0fs -- a driver that boots after us'
                    % (th.fail, th.period), th.why]
        else:
            head = 'QUERY FAILED'
            col = CRIT if stale > 120 else WARN
            rows = ['last good reply %.0fs ago, next try in %.0fs' % (stale, th.period),
                    th.why]
        rows.append('nvidia-smi -L   /   dash.py --sensors')
        panel(c, 8, y0, W - 16, h, 'GPU', sub)
        c.text(16, y0 + 40, ellipsize(c, head, 3, W - 32), 3, col)
        y = y0 + 40 + c.text_h(3) + 12
        for s in rows:
            if y + c.text_h(2) > y0 + h - 8:
                break
            c.text(16, y, ellipsize(c, s, 2, W - 32), 2, DIM)
            y += c.text_h(2) + 8
        return
    n = min(len(gpus), 2)
    col = (W - 16 - 8 * (n - 1)) // n
    if col < 260:                       # two panels this narrow crush the text
        n, col = 1, W - 16
    h1 = h - 144
    for i, g in enumerate(gpus[:n]):
        x = 8 + i * (col + 8)
        # a query that failed while the page kept its last numbers says so: on
        # a dashboard an unlabelled stale number is worse than no number
        sub = gpu_title(g['name'])
        if stale is not None and stale > 8.0:
            sub = '%s STALE %.0fs' % (sub, stale)
        ix, iy = panel(c, x, y0, col, h1, 'GPU %d' % i, sub)
        t = g.get('temp')
        u = g.get('util') or 0
        big_number(c, ix, iy, '%d' % u, '%', ACCENT, 8)
        # everything below the big number is placed from the measured text box
        # heights, not from constants tuned for one font
        big_h, lab_h, row_h = c.text_h(8), c.text_h(6), c.text_h(2)
        # temperature sits after the big number, dropping to its own row when
        # the panel is too narrow to hold both
        tstr = '%3.0f C' % (t or 0)
        x_t = ix + c.text_w('%d' % u, 8) + c.text_w('%', 3) + 24
        y_t = iy + 10
        if x_t + c.text_w(tstr, 6) > x + col - 8:
            x_t, y_t = ix, iy + big_h + 2
        c.text(x_t, y_t, tstr, 6, temp_color(t))
        vram_y = max(iy + big_h + 2, y_t + lab_h + 4)
        vtxt = 'VRAM %4.1f / %4.1f GB' % ((g.get('mem_used') or 0) / 1024.0,
                                          (g.get('mem_total') or 0) / 1024.0)
        vsc = pick_scale(c, vtxt, (3, 2), col - 16)
        c.text(ix, vram_y, vtxt, vsc, TEXT)
        c.bar(ix, vram_y + c.text_h(3) + 6, col - 16, 16,
              (g.get('mem_used') or 0) / max(1.0, g.get('mem_total') or 1.0), ACCENT)
        # left/right labels share a row only when both fit, else the second wraps
        ltxt = 'FAN %3.0f%%  PWR %5.1f W' % (g.get('fan') or 0, g.get('power') or 0)
        rtxt = 'MEM-BUSY %3.0f%%' % (g.get('mem_util') or 0)
        ly = vram_y + c.text_h(3) + 30
        c.text(ix, ly, ltxt, 2, DIM)
        if c.text_w(ltxt, 2) + c.text_w(rtxt, 2) + 24 <= col - 16:
            c.text_right(x + col - 8, ly, rtxt, 2, DIM)
        elif ly + row_h + 6 + row_h <= y0 + h1 - 8:
            c.text(ix, ly + row_h + 6, rtxt, 2, DIM)
            ly += row_h + 6
        # the two histories get whatever vertical space is left in the panel
        top = ly + 24
        budget = (y0 + h1 - 8) - top
        gh = (budget - 2 * 22) // 2
        if i < len(m.hist_gpu) and gh >= 24:
            gw = col - 16
            c.spark(ix, top, gw, gh, m.hist_gpu[i]['util'], 0, 100, ACCENT)
            c.outline(ix, top, ix + gw, top + gh + 1, GRID)
            c.text(ix, top + gh + 5, 'UTIL 0  50  100%%', 2, DIM)
            hi = max(95.0, (max(m.hist_gpu[i]['temp']) if m.hist_gpu[i]['temp'] else 0) + 5)
            ty = top + gh + 26
            c.spark(ix, ty, gw, gh, m.hist_gpu[i]['temp'], 0, hi, OK)
            c.outline(ix, ty, ix + gw, ty + gh + 1, GRID)
            c.text(ix, ty + gh + 1, 'TEMP 0  %d  %d C' % (int(hi / 2), int(hi)), 2, DIM)

    # bottom strip: totals + board/enclosure temperatures, flowed so nothing
    # gets cut at the strip edge
    by = y0 + h1 + 10
    sh = h - h1 - 10
    ix, iy = panel(c, 8, by, W - 16, sh, 'FLEET',
                   '%d GPU%s' % (len(gpus), 's' if len(gpus) > 1 else ''))
    row_w = W - 16 - 16
    n = max(1, len(gpus))
    tot_u = sum(g.get('util') or 0 for g in gpus) / n
    tot_v = sum(g.get('mem_used') or 0 for g in gpus)
    tot_p = sum(g.get('power') or 0 for g in gpus)
    cells = [('SUM VRAM', '%4.1f GB' % (tot_v / 1024.0), TEXT),
             ('AVG UTIL', '%3.0f%%' % tot_u, TEXT),
             ('TOTAL PWR', '%5.1f W' % tot_p, TEXT),
             ('CPU TEMP', '%.0f C' % (m.cpu_temp or 0), temp_color(m.cpu_temp))]
    cells += [('MB%d' % (i + 1), '%.0f C' % t, temp_color(t))
              for i, (lab, t) in enumerate(m.chassis[:6])]
    cells += [('NIC', '%.0f C' % t, temp_color(t)) for _, t in m.nic[:2]]
    cells += [('ZONE %s' % t['name'][:6], '%.0f C' % t['temp'], temp_color(t['temp']))
              for t in m.sensors['thermal'][:3]]
    cells += [('FAN', '%s %d' % (lab[:5], rpm), TEXT) for lab, rpm in m.fans[:4]]
    if not m.fans:
        cells.append(('FANS', 'NONE', DIM))
    cell_row(c, ix, iy, row_w, cells, scale=2, row_h=22,
             max_rows=max(1, (sh - band(c)) // 22))

# ── page 3: processes ──────────────────────────────────────────────────
def body_proc(c, m, args, y0, h):
    W = c.w
    row_box = c.text_h(2)
    # the SYSTEM list is built first, because its height decides how the right
    # column is split: give it exactly the rows it needs and let TOP BY MEMORY
    # have the rest, so neither list is silently cut at bigger font sizes
    sys_lines = [
        ('LOAD 1/5/15', ' '.join('%.2f' % v for v in m.load)),
        ('TASKS', '%d  (%d kernel)' % (m.tasks, m.kthreads)),
        ('RAM', '%.1f / %.1f GB' % (m.mem['used'] / 2**30, m.mem['total'] / 2**30)),
        ('SWAP', '%.2f / %.2f GB' % (m.mem['swap_used'] / 2**30,
                                     m.mem['swap_total'] / 2**30)),
        ('CTX / SEC', '%d' % int(m.ctxt_rate)),
        ('IRQ / SEC', '%d' % int(m.intr_rate)),
        ('CPU TEMP', '%.0f C' % (m.cpu_temp or 0)),
    ]
    for lab, val in m.nic[:1]:
        sys_lines.append(('NIC TEMP', '%.0f C' % val))
    # fans and board/zone temperatures live on the GPU page's fleet strip, so the
    # SYSTEM list stays short enough that every row of it is visible
    pitch = row_box + 2
    need_sys = band(c) + 6 + (len(sys_lines) - 1) * pitch + row_box
    split = max(int(h * 0.35), h - 10 - need_sys)
    # right column: top-by-memory on top, system list under it -- one split,
    # so the two panels can never grow into each other
    lw = int((W - 24) * 0.58)
    ix, iy = panel(c, 8, y0, lw, h, 'TOP BY CPU',
                   '%d PROCS' % m.proc_total if lw < 520 else
                   '%d PROCS  %d THREADS' % (m.proc_total, m.threads_total))
    # how many rows the panel can actually hold at this font size, then the
    # list is cut to that: a longer list is a layout bug, not a scroll
    fit_cpu = max(1, (h - band(c) - 12) // (row_box + 2))
    rows = m.top_cpu[:min(14, fit_cpu)]
    if fit_cpu < 14:
        c.info.append('TOPCPU panel holds %d of 14 rows at this font size' % fit_cpu)
    peak = max([p for _, p, _ in rows] + [1.0])
    row_h = max(row_box + 2, (h - band(c) - 12) // max(1, len(rows)))
    name_w = c.text_w('X' * 16, 2)
    pct_w = c.text_w('100.0%', 2)
    rank_w = c.text_w('99', 2)
    gap = 26
    name_x = ix + rank_w + gap
    bx = name_x + name_w + 12
    bw = lw - 16 - rank_w - gap - name_w - 12 - pct_w - 8
    for i, (comm, pct, rss) in enumerate(rows):
        yy = iy + i * row_h
        rank = str(i + 1).ljust(2)   # flush left: '%2d' would indent single digits
        c.text(ix, yy, rank, 2, DIM)
        c.text(name_x, yy, '%-16s' % comm[:16], 2, TEXT)
        c.bar(bx, yy + 3, max(8, bw), 12, pct / peak,
              ACCENT if pct < peak * 0.8 else WARN)
        c.text_right(8 + lw - 14, yy, '%5.1f%%' % pct, 2, TEXT)
    rx = 8 + lw + 8
    rw = W - 16 - lw - 8
    ix, iy = panel(c, rx, y0, rw, split, 'TOP BY MEMORY')
    fit_mem = max(1, (split - band(c) - 12) // (row_box + 2))
    rows = m.top_mem[:min(8, fit_mem)]
    if fit_mem < 8:
        c.info.append('TOPMEM panel holds %d of 8 rows at this font size' % fit_mem)
    peak = max([r for _, _, r in rows] + [1])
    # the list fills its panel from the panel's own height; a fixed pitch ran
    # the last rows over the panel's bottom edge and into the panel below
    row_h = max(row_box + 2, (split - band(c) - 12) // max(1, len(rows)))
    name_w = c.text_w('X' * 14, 2)
    for i, (comm, pct, rss) in enumerate(rows):
        yy = iy + i * row_h
        c.text(ix, yy, '%-14s' % comm[:14], 2, TEXT)
        c.text_right(rx + rw - 14, yy, fmt_bytes(rss), 2, TEXT)
        c.bar(ix + name_w + 8, yy + 3,
              max(8, rw - 16 - name_w - c.text_w('9999G', 2) - 16),
              12, rss / peak, ACCENT)
    iy2 = y0 + split + 10
    panel_h = h - split - 10
    ix, iy = panel(c, rx, iy2, rw, panel_h, 'SYSTEM')
    lines = sys_lines
    # the panel's own clip bottom (y + panel_h - 6) is what text() checks
    # against; iy sits one title band below the panel top
    row_h = row_box
    limit = iy + panel_h - (band(c) + 6) - row_h
    room = max(1, int((limit - iy) // pitch) + 1)
    if room < len(lines):
        c.info.append('SYSTEM %d of %d rows fit in the panel at this font size' %
                      (room, len(lines)))
    for i, (k, v) in enumerate(lines[:room]):
        yy = iy + i * pitch
        if yy + row_h > limit:
            break
        label = k
        c.text(ix, yy, label, 2, DIM)
        vx = ix + c.text_w(label, 2) + 6
        # values are right-aligned on one edge, so the column of numbers reads
        # as a column; an over-long value gets '~', never a smaller font
        v = ellipsize(c, v, 2, rx + rw - 8 - vx)
        c.text_right(rx + rw - 8, yy, v, 2, TEXT)


# ── page 4: AI -- the strata monitor's Overview (view 1) ────────────────
AI_PHASE = {'Idle': 'IDLE, NO REQUEST', 'Reading': 'READING PROMPT',
            'Generating': 'GENERATING A REPLY', 'Queued': 'REQUESTS QUEUED',
            'Error': 'MODEL UNAVAILABLE'}
AI_STATE_COLOR = {'Idle': DIM, 'Reading': ACCENT, 'Generating': ACCENT,
                  'Queued': WARN, 'Error': CRIT}
AI_FINISH = {'stop': 'DONE', 'length': 'CAPPED', 'tool_calls': 'TOOLS',
             'cancelled': 'GONE', 'aborted': 'GONE', 'error': 'ERROR'}


def sfmt(v, digits=0):
    return '--' if v is None else '%.*f' % (digits, v)


def ai_hi(series, floor):
    """Top of a sparkline's range: the data's own max, or a floor when it is empty."""
    vals = [x for x in (series or []) if x is not None]
    return max(floor, (max(vals) if vals else 0.0) * 1.1)


def ai_tile(c, x, y, w, h, title, foot, big, unit, series, lo, hi, color):
    """One Overview tile: a big number, a caption, and the server's own history.

    The histories come in the /metrics reply (a minute per series), so the AI
    page draws the same curve `strata monitor` draws instead of building its own
    ring buffer out of a 1 fps poll."""
    ix, iy = panel(c, x, y, w, h, title, None)
    sc = pick_scale(c, big, (5, 4, 3), max(20, w - 16 - c.text_w(unit, 3) - 10))
    c.text(ix, iy, big, sc, color)
    c.text(ix + c.text_w(big, sc) + 8,
           iy + (c.text_h(sc) - c.text_h(3)) // 2, unit, 3, DIM)
    row = iy + max(c.text_h(sc), c.text_h(3)) + 5
    c.text(ix, row, ellipsize(c, foot, 1, w - 16), 1, DIM)
    top = row + c.text_h(1) + 6
    gw = w - 16
    sh = (y + h - 8) - top
    if sh < 12:
        return
    vals = [v for v in (series or []) if v is not None]
    if len(vals) >= 2 and hi > lo:
        c.spark(ix, top, gw, sh, vals, lo, hi, color)
    else:
        c.text(ix, top, ellipsize(c, 'no history in the reply', 1, gw), 1, DIM)


def body_ai(c, m, args, y0, h):
    W = c.w
    th = m.strata_thread
    d = m.strata
    stale = th.stale() if th is not None else None
    state_dir = getattr(args, 'strata_state', None)
    if d is None:
        # an empty AI page has the same four causes as an empty GPU page, and
        # they need four messages: --no-strata (asked for), nothing on disk to
        # poll (this box runs no strata server), a server that is not answering
        # (it boots after this service, and it heals), and a reply that stopped
        sub, head, col = 'no /metrics reply', 'SERVER NOT ANSWERING', CRIT
        rows = []
        if th is None:
            sub, head, col = 'queries disabled', 'AI PAGE DISABLED', DIM
            rows = ['this run has --no-strata']
        elif not th.endpoints:
            sub, head, col = 'nothing to poll', 'NO STRATA SERVER', DIM
            rows = ['looked in  ' + '  '.join(strata_state_dirs(state_dir)),
                    'strata launch,  STRATA_URL + STRATA_API_KEY,  dash.py --strata-url']
        elif stale is None:
            head, col = 'PROBING THE STRATA SERVER', WARN
            rows = ['attempt %d, next try in %.0fs -- the server boots after this service'
                    % (th.fail, th.period), th.why]
        else:
            rows = ['last good reply %.0fs ago, next try in %.0fs' % (stale, th.period),
                    th.why]
        rows.append('strata status --json   /   dash.py --sensors')
        panel(c, 8, y0, W - 16, h, 'AI', sub)
        c.text(16, y0 + 40, ellipsize(c, head, 3, W - 32), 3, col)
        y = y0 + 40 + c.text_h(3) + 12
        for s in rows:
            if y + c.text_h(2) > y0 + h - 8:
                break
            c.text(16, y, ellipsize(c, s, 2, W - 32), 2, DIM)
            y += c.text_h(2) + 8
        return

    v = strata_view(d)
    if v is None:
        panel(c, 8, y0, W - 16, h, 'AI', 'no live block in the reply')
        c.text(16, y0 + 40, ellipsize(c, 'NOT A STRATA METRICS REPLY', 3, W - 32), 3, CRIT)
        c.text(16, y0 + 40 + c.text_h(3) + 12,
               ellipsize(c, 'GET %s/metrics  ->  {live: ...}' %
                         (th.source if th is not None else 'server'), 2, W - 32), 2, DIM)
        return

    # ── model state bar (the Overview's heading + state strip) ────────────
    strip = 96
    state = v['state']
    scol = AI_STATE_COLOR[state]
    marker = 'LIVE' if stale is None or stale <= 6.0 else 'STALE %.0fs' % stale
    used, ctx_max = v['ctx']
    sub = '%s  %s  %s' % (v['model'] or 'model', fmt_k(ctx_max), marker)
    ix, iy = panel(c, 8, y0, W - 16, strip, 'MODEL', sub)
    x0, w0 = 8, W - 16
    done, total = v['progress']
    pct = int(round(100.0 * done / total)) if done is not None and total else None
    if pct is not None:
        right = '%s / %s TOK  %d%%' % (fmt_tok(done), fmt_tok(total), pct)
    elif v['queued']:
        right = '%d QUEUED' % v['queued']
    else:
        right = '%s TOK OUT  %s REQ' % (fmt_tok(v['totals'].get('output_tokens')),
                                        fmt_tok(v['totals'].get('requests')))
    phase = AI_PHASE[state] + ('  %s' % v['phase'].upper() if v['phase'] else '')
    sc = pick_scale(c, phase, (4, 3, 2),
                    max(40, w0 - 16 - c.text_w(right, 3) - 20))
    c.text(ix, iy, phase, sc, scol)
    c.text_right(x0 + w0 - 8, iy + (c.text_h(sc) - c.text_h(3)) // 2, right, 3, TEXT)
    yy = iy + c.text_h(sc) + 6
    url = (th.server if th is not None else '') or ''
    line2 = 'SERVER %s   CTX %s / %s   STATE %s   %s' % (
        ellipsize(c, url.replace('http://', ''), 1, 320), fmt_k(used), fmt_k(ctx_max),
        state, marker)
    c.text(ix, yy, ellipsize(c, line2, 1, w0 - 16), 1, DIM)

    # ── the Overview's eight tiles, with the server's own histories ──────
    ty = y0 + strip + 8
    tile_h = 110
    cols = 4
    col = (W - 16 - 8 * (cols - 1)) // cols
    vu, vt = v['vram']
    pw, pl = v['power']
    rx, pgen, pwidth = v['pcie']
    slots, slots_total, slots_mib = v['experts']
    speed, note = v['speed']
    note = {'run average': 'AVG OF THE RUN', 'generating now': 'GENERATING NOW',
            'reading the prompt': 'PREFILL NOW', 'last request': 'LAST REQUEST'}.get(note,
                                                                                       'NO RATE')
    cards_note = ('BUSIEST OF %d CARDS' % v['gpus']) if v['gpus'] > 1 else 'THE ONLY CARD'
    hot_note = ('HOTTEST OF %d CARDS' % v['gpus']) if v['gpus'] > 1 else 'THE ONLY CARD'
    tiles = [
        ('SPEED', sfmt(speed, 0), 'T/S', note, v['speed_hist'],
         0, ai_hi(v['speed_hist'], 50), ACCENT),
        ('GPU LOAD', sfmt(v['util']), '%', cards_note,
         v['util_hist'], 0, 100, ACCENT),
        ('VRAM', sfmt(vu, 1), 'GB',
         '%.0f%% OF %.1f GB' % (100.0 * vu / vt, vt) if vu is not None and vt else '-- OF -- GB',
         v['vram_hist'], 0, max(vt or 0.0, vu or 0.0) * 1.05, WARN),
        ('GPU TEMP', sfmt(v['temp']), 'C', hot_note, v['temp_hist'],
         0, ai_hi(v['temp_hist'], 95), temp_color(v['temp'])),
        ('POWER', sfmt(pw), 'W',
         '%.0f%% OF %.0f W' % (100.0 * pw / pl, pl) if pw is not None and pl else 'OF -- W',
         v['power_hist'], 0, max(pl or 0.0, ai_hi(v['power_hist'], 100)), OK),
        ('PCIE RX', sfmt(rx), 'MB/S',
         'GEN %s x%s' % (sfmt(pgen), sfmt(pwidth)), v['pcie_hist'],
         0, ai_hi(v['pcie_hist'], 100), OK),
        ('CPU', sfmt(v['cpu']), '%', 'HOST CPU, %d CORES' % max(0, v['cores']),
         v['cpu_hist'], 0, 100, OK),
        ('DISK READ', sfmt(v['disk'][0], 1), 'MB/S',
         'WRITE %s MB/S' % sfmt(v['disk'][1], 1), v['disk_hist'],
         0, ai_hi(v['disk_hist'], 10), TEXT),
    ]
    for i, (title, big, unit, foot, series, lo, hi, col_c) in enumerate(tiles):
        r, k = divmod(i, cols)
        ai_tile(c, 8 + k * (col + 8), ty + r * (tile_h + 8), col, tile_h,
                title, foot, big, unit, series, lo, hi, col_c)

    # ── the Overview's three bars (context, experts, RAM) + recent requests
    by = ty + 2 * (tile_h + 8)
    bh = (y0 + h) - by
    lw = 420
    ix, iy = panel(c, 8, by, lw, bh, 'FILL',
                   '%s REQ  %s TOK IN' % (fmt_tok(v['totals'].get('requests')),
                                          fmt_tok(v['totals'].get('prompt_tokens'))))
    ru, rt = v['ram']
    bars = [('CONTEXT', used, ctx_max, '%s / %s TOK' % (fmt_k(used), fmt_k(ctx_max)),
             ACCENT if used is None or not ctx_max or used / ctx_max < 0.85 else WARN),
            ('EXPERTS IN VRAM', slots, slots_total,
             '%s SLOTS  %.1f GB' % (fmt_tok(slots), (slots_mib or 0) / 1024.0), ACCENT),
            ('SYSTEM RAM', ru, rt, '%.1f / %.1f GB' % ((ru or 0) / 2 ** 30, (rt or 0) / 2 ** 30),
             WARN if ru is not None and rt and ru / rt >= 0.85 else OK)]
    pitch = (bh - band(c) - 10) // max(1, len(bars))
    for i, (lab, u, t, txt, colr) in enumerate(bars):
        yy = iy + i * pitch
        c.text(ix, yy, ellipsize(c, '%s  %s' % (lab, txt), 1, lw - 16), 1, DIM)
        frac = (u / t) if (u is not None and t) else 0.0
        c.bar(ix, yy + c.text_h(1) + 3, lw - 16, max(6, min(12, pitch - c.text_h(1) - 6)),
              frac, colr)

    rx0, rw = 8 + lw + 8, W - 16 - lw - 8
    pitch = max(12, c.text_h(1) + 10)
    fit = max(1, (by + bh - 8 - (by + band(c))) // pitch)
    ix, iy = panel(c, rx0, by, rw, bh, 'RECENT',
                   'LAST %d OF %d' % (fit, len(v['recent'])))
    pitch = max(12, c.text_h(1) + 10)
    shown = v['recent'][:fit]
    if len(v['recent']) > fit:
        c.info.append('RECENT shows %d of %d rows at this font size' % (fit, len(v['recent'])))
    for i, r in enumerate(shown):
        hit = r['hit']
        cells = [('IN', fmt_tok(r['prompt']), TEXT),
                 ('HIT', '--' if hit is None else '%3.0f%%' % (hit * 100.0),
                  OK if hit is not None and hit >= 0.5 else WARN),
                 ('OUT', fmt_tok(r['output']), TEXT),
                 ('T/S', sfmt(r['tps'], 1), ACCENT),
                 ('TIME', '%s S' % sfmt(r['dur'], 1), TEXT),
                 ('END', ('ERROR' if r['error'] else AI_FINISH.get(r['finish'], 'DONE')).upper(),
                  CRIT if r['error'] else DIM)]
        # the row is a fixed set of cells, not a joined string: drop the tail
        # cells that no longer fit (biggest prompt token count decides the row),
        # never the font size, and say what had to go
        room, dropped = rw - 16, 0
        while len(cells) > 2 and sum(c.text_w(lab, 1) + 6 + c.text_w(val, 1) + 14
                                    for lab, val, _ in cells) > room + 14:
            cells.pop()
            dropped += 1
        if dropped:
            c.info.append('RECENT drops %d of 6 columns at this font size' % dropped)
        cell_row(c, ix, iy + i * pitch, rw - 16, cells, scale=1, row_h=pitch, max_rows=1)


def draw(c, m, args, idx):
    # pages composite onto the previous page: the canvas has to start empty,
    # otherwise dense text from the last page (CPU's system list) stays visible
    # under a page that never paints that area
    c.clear()
    c.clip = None
    c.panels = []
    page = args.page_list[idx]
    gpu_only = None
    if page.startswith('GPU') and len(page) > 3 and page[3:].isdigit():
        gpu_only = int(page[3:]) - 1
        page = 'GPU' 
    draw_header(c, m, args, page, idx)
    y0, h = 50, c.h - 50 - 4
    if page == 'CPU':
        body_cpu(c, m, args, y0, h)
    elif page == 'GPU':
        body_gpu(c, m, args, y0, h, only=gpu_only)
    elif page == 'PROC':
        body_proc(c, m, args, y0, h)
    elif page == 'AI':
        body_ai(c, m, args, y0, h)


# ── font sheet / debug ─────────────────────────────────────────────────
def font_sample(c):
    c.clear()
    c.text(8, 6, '5X7 FONT SHEET  (USBLCD DASHBOARD)', 2, ACCENT)
    x, y = 8, 40
    for ch in sorted(FONT.keys()):
        c.text(x, y, ch, 3, TEXT)
        x += 20
        if x > c.w - 24:
            x, y = 8, y + 26
            if y > c.h - 30:
                break
    c.outline(4, 36, c.w - 4, c.h - 4, GRID)


def print_sensors(m):
    s = m.sensors
    print('host %s  kernel %s  cpus %d' % (m.host, m.kernel, m.nproc))
    print('cpu package   : %s' % ('%s %s = %.1f C' % (s['cpu']['name'], s['cpu']['label'],
                                                       s['cpu']['temp']) if s['cpu'] else 'NONE'))
    print('cpu cores     : %s' % (' '.join('%.0f' % c['temp'] for c in s['cpu_cores'])
                                  or 'NONE'))
    print('thermal zones : %s' % (', '.join('%s %s %.0f C' % (t['name'], t['label'] or '-',
                                                               t['temp'])
                                            for t in s['thermal']) or 'NONE'))
    print('nvme          : %s' % (', '.join('%s %s %.0f C' % (i['chip'], i['label'], i['temp'])
                                            for i in s['nvme']) or 'NONE'))
    print('nic temps     : %s' % (', '.join('%s %.0f C' % (i['name'], i['temp'])
                                            for i in s['nic']) or 'NONE'))
    print('chassis       : %s' % (', '.join('%s %s %.0f C' % (i['name'], i['label'] or '-',
                                                               i['temp'])
                                            for i in s['chassis']) or 'NONE'))
    print('fans          : %s' % (', '.join('%s %s %d RPM' % (f['chip'], f['label'], f['rpm'])
                                            for f in s['fans']) or 'NONE'))
    print('hwmon nvidia  : %s' % (', '.join('%s %.0f C' % (g['name'], g['temp'])
                                            for g in s['gpu']) or 'NONE'))
    print('nvidia-smi    : %s' % (', '.join('%s util %s%% mem %s/%s temp %s fan %s%%'
                                            % (g['name'], g['util'], g['mem_used'],
                                               g['mem_total'], g['temp'], g['fan'])
                                            for g in (m.gpu or [])) or 'NONE'))
    th = m.gpu_thread
    if th is not None and not (m.gpu or []):
        # why a GPU page is empty lives in the poller, not in the sensors
        print('gpu poll      : %s  (%d failures, next in %.0fs%s)'
              % (th.why, th.fail, th.period,
                 ', poller stopped' if th.disabled else ''))
    print('interfaces    : %s' % (', '.join(sorted(m.net) or ['NONE'])))
    ep = (m.strata_thread.endpoints if m.strata_thread is not None else [])
    print('strata server : %s' % (', '.join('%s (%s)' % (u, n) for u, k, n in ep[:3])
                                  or 'NONE -- no launch config, no STRATA_URL'))
    th = m.strata_thread
    if th is not None and th.metrics is not None:
        v = strata_view(th.metrics) or {}
        print('strata metrics: model %s  state %s  speed %s tok/s  %d gpu(s)  %d recent'
              % (v.get('model') or '?', v.get('state') or '?',
                 sfmt((v.get('speed') or (None,))[0], 1), v.get('gpus') or 0,
                 len(v.get('recent') or [])))
    if th is not None and th.metrics is None:
        print('strata poll   : %s  (%d failures, next in %.0fs%s)'
              % (th.why, th.fail, th.period, ', nothing to poll' if not th.endpoints else ''))
    if th is not None and th.pinned is not None:
        print('strata source : %s  (from %s, polled every %.0fs)' % (th.server, th.source,
                                                                      th.period))
    print('freqs         : %s' % ('%d' % len(m.freqs)))
    print('disks         : %s' % (', '.join('%s %.0f%%' % (d[0], d[1] * 100)
                                            for d in m.disks) or 'NONE'))


# ── entry ──────────────────────────────────────────────────────────────
FONT_SIZES = (1, 2, 3, 4, 5, 6, 7, 8, 9)
BIG_SIZES = 1        # calibrate on the digit height: caps match the bitmap font's box
FONT_CHARS = ''.join(chr(i) for i in range(32, 127))
# project-local first: fonts/ is where a chosen face lives (Inter ships as one
# variable file with wght/opsz axes)
FONT_CANDIDATES = (os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'fonts', 'Inter-var.ttf'),
                   '/usr/share/fonts/Adwaita/AdwaitaMono-Regular.ttf',
                   '/usr/share/fonts/TTF/DejaVuSansMono.ttf',
                   '/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf',
                   '/usr/share/fonts/Liberation/LiberationMono-Regular.ttf',
                   '/usr/share/fonts/truetype/liberation/LiberationMono-Regular.ttf')
# coverage (0..255) -> six painting levels; level 6 is the pure text colour
COV_STEPS = (24, 68, 118, 168, 216, 244)


def font_grab(helper, path, px, wght=None, opsz=None):
    """Glyph coverage grids for one pixel size, from build/ttfplan."""
    args = [helper, path, str(px)]
    if wght is not None:
        args.append('wght=%g' % wght)
    if opsz is not None:
        args.append('opsz=%g' % opsz)
    args.append(FONT_CHARS)
    out = subprocess.run(args, stdout=subprocess.PIPE).stdout
    glyphs, i = {}, 0
    while i + 24 <= len(out):
        cp, w, h, left, top, adv = struct.unpack('<6i', out[i:i + 24])
        i += 24
        glyphs[chr(cp)] = (w, h, left, top, adv, out[i:i + w * h])
        i += w * h
    return glyphs


def load_font(helper, path, scales=FONT_SIZES, wght=400, rise=1.0, big_rise=1.0):
    """Rect plans for every dashboard font size, rasterised by FreeType.

    The bitmap font this replaces draws its glyphs 7*scale px tall, so each
    size is calibrated to the pixel size whose digits come out that tall and
    the text keeps the layout it was measured for.  FreeType's 8-bit coverage
    is quantized to six levels and merged into rectangles, so drawing stays
    a few dozen rect calls per line instead of per-pixel blending."""
    probe = font_grab(helper, path, 100, wght, 75)
    rise = max(0.6, min(2.0, rise))
    # calibrate against the *whole* ink box (cap height plus descender), not the
    # cap height: the bitmap font this replaces occupies exactly 7*scale px, and
    # every panel pitch, bar and clip in the layout was measured against that
    # measure on letters and digits only: brackets, slashes and underscores
    # reach far above and below the text band, and calibrating on them would
    # shrink the font below the layout the panels were measured for
    probe_set = [g for ch, g in probe.items() if ch.isalnum() and g[1]]
    asc = max([g[3] for g in probe_set] + [0])                     # bitmap_top
    desc = max([max(0, g[1] - g[3]) for g in probe_set] + [0])     # rows below baseline
    full = max(1, asc + desc)
    plans = {}
    digits = max([g[1] for ch, g in probe.items() if ch.isdigit() and g[1]] + [1])
    for scale in scales:
        # Big numbers own their whole panel, so they are calibrated on the digit
        # height and use all of it; the dense label/row text is calibrated on the
        # full ink box (cap + descender) so descenders don't run into the row
        # below them.
        big = scale >= BIG_SIZES
        metric = digits if big else full
        # the big-number panels own their space, so they get their own enlargement
        px = max(9, int(round(100.0 * 7 * scale * (big_rise if big else rise) / metric)))
        # the optical-size axis is in points: the same outline is drawn with
        # fatter strokes and wider spacing at 9 pt than at 40 pt
        raw = font_grab(helper, path, px, wght, max(9, min(144, int(round(px * 0.75)))))
        if not raw:
            return None
        asc = [g[3] for ch, g in raw.items() if ch.isalnum()]
        ascent = max(asc) if asc else px
        desc = max([max(0, g[1] - g[3]) for ch, g in raw.items() if ch.isalnum()] + [0])
        glyphs = {}
        for ch, (w, h, left, top, adv, grid) in raw.items():
            if w == 0 or h == 0:
                glyphs[ch] = (0, 0, adv, [])
                continue
            C = []
            for y in range(h):
                base = y * w
                C.append([0 if grid[base + x] < COV_STEPS[0] else
                          1 if grid[base + x] < COV_STEPS[1] else
                          2 if grid[base + x] < COV_STEPS[2] else
                          3 if grid[base + x] < COV_STEPS[3] else
                          4 if grid[base + x] < COV_STEPS[4] else
                          5 if grid[base + x] < COV_STEPS[5] else 6
                          for x in range(w)])
            glyphs[ch] = (left, ascent - top, adv, merge_levels(C))   # dx, dy, advance, rects
        dadv = [g[4] for ch, g in raw.items() if ch.isdigit() and g[4]]   # (w,h,left,top,adv,grid)
        plans[scale] = {'glyphs': glyphs, 'box_h': ascent + desc,
                        'advance': max([g[4] for g in raw.values()] + [6 * scale + 1]),
                        'digit_adv': max(dadv) if dadv else 0}
    return plans


def parse_cpus(spec):
    """'16-23', '16,17,18', 'all' -> set of CPU ids."""
    if not spec or spec.lower() == 'all':
        return None
    cpus = set()
    n = os.cpu_count() or 1
    for part in spec.split(','):
        part = part.strip()
        if not part:
            continue
        if '-' in part:
            a, b = part.split('-', 1)
            cpus.update(range(int(a), int(b) + 1))
        else:
            cpus.add(int(part))
    return {c for c in cpus if 0 <= c < n}


def pin_self(spec, nice_level=0):
    """Run on the given CPUs.  Children (usblcd feed) inherit the affinity."""
    cpus = parse_cpus(spec)
    if cpus is not None:
        try:
            os.sched_setaffinity(0, cpus)
        except OSError as e:
            print('sched_setaffinity %s failed: %s' % (sorted(cpus), e), file=sys.stderr)
    if nice_level:
        try:
            os.nice(nice_level)
        except OSError:
            pass
    return cpus


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--width', type=int, default=960)
    ap.add_argument('--height', type=int, default=540)
    ap.add_argument('--safe-area', default='8,30,8,8', metavar='T,BOTTOM,LEFT,RIGHT',
                    help='pixels to keep clear of the panel bezel / rounded corners')
    ap.add_argument('--title', default='',
                    help='header title; empty means the machine hostname')
    ap.add_argument('--out', default='/tmp/usblcd-dashboard.ppm')
    ap.add_argument('--raw', action='store_true',
                    help='write RGBA frames to stdout (pipe into `usblcd feed`)')
    ap.add_argument('--exec', metavar='CMD',
                    help='spawn CMD and write frames to its stdin (e.g. ./usblcd)')
    ap.add_argument('--cpus', help='pin to these CPUs, e.g. --cpus 16-23 (Intel E-cores); '
                    'the usblcd child inherits the affinity')
    ap.add_argument('--nice', type=int, default=0, help='nice this process (and the child)')
    ap.add_argument('--no-aa', dest='aa', action='store_false',
                    help='hard-edged bitmap font: no soft edge strips on the text')
    ap.add_argument('--font', help='TTF to draw with, antialiased through FreeType '
                    '(default: first mono font installed)')
    ap.add_argument('--no-ttf', dest='ttf', action='store_true',
                    help='use the built-in 5x7 bitmap font even when FreeType is available')
    ap.add_argument('--ttfplan', help='helper that dumps FreeType coverage (default build/ttfplan)')
    ap.add_argument('--text-rise', type=float, default=112.0, metavar='PERCENT',
                    help='draw labels and rows this %% of the layout size; the audit reports any '
                    'string that no longer fits')
    ap.add_argument('--big-rise', type=float, default=118.0, metavar='PERCENT',
                    help='draw the big numbers (temps, utilisation, RAM) this %% of the layout size')
    ap.add_argument('--weight', type=float, default=400.0,
                    help='wght axis for variable fonts (Inter: 400 regular, 500 medium, 600 semibold)')
    ap.add_argument('--aa-min', type=int, default=2, metavar='SCALE',
                    help='soft edges only on text at this font scale and above (2 = everything, '
                    '4 = big numbers only, cheaper to draw)')
    ap.add_argument('--interval', type=float, default=1.0)
    ap.add_argument('--fps', type=float, default=1.0)
    ap.add_argument('--history', type=int, default=180,
                    help='samples kept for the graphs (window = history * interval)')
    ap.add_argument('--pages', default=','.join(PAGES),
                    help='comma separated pages to rotate through (%s)' % '/'.join(PAGES))
    ap.add_argument('--page', help='show only this page (no rotation)')
    ap.add_argument('--debug-grid', action='store_true',
                    help='draw panel outlines, ids, sizes and a pixel ruler (for photos)')
    ap.add_argument('--audit', action='store_true',
                    help='report any string drawn past its panel edge')
    ap.add_argument('--page-seconds', type=float, default=12.0,
                    help='seconds per page in rotation (0 = stay on one page)')
    ap.add_argument('--ring', type=int, default=0,
                    help='countdown dial radius in px (0 = match the clock digits)')
    ap.add_argument('--once', action='store_true')
    ap.add_argument('--clean-frames', type=int, default=2, metavar='N',
                    help='how many textless "bare panels" frames to flush before a new page; each one makes '
                    'the panel composite this page\'s colours again, so the previous page\'s text decays N times '
                    'faster (no black flash)')
    ap.add_argument('--resend', type=int, default=1, metavar='N',
                    help='write the first frame of a new page N times, so the panel converges on it faster '
                    'and any ghost of the previous page decays without a black flash')
    ap.add_argument('--stamp', action='store_true',
                    help='draw a giant PAGE + seconds + frame counter on every frame, so you can read the '
                    'display lag off the panel itself')
    ap.add_argument('--warm-seconds', type=float, default=0.0,
                    help='for the first N seconds of a page, re-send the ALREADY RENDERED frame at --warm-fps '
                    '(several complete copies of the new page, no CPU for re-rendering).  0 = off, which is '
                    'the default: a warm window repeats a frame verbatim, so the clock and the next-page '
                    'countdown freeze for those seconds')
    ap.add_argument('--warm-fps', type=float, default=2.0,
                    help='frame rate during --warm-seconds after a page change')
    ap.add_argument('--transition', choices=('none', 'clean', 'fade', 'black'), default='none',
                    help='clean: the first frame of a new page is drawn with its panels but no text, which '
                    'erases the ghost of the previous page using this page\'s own colours (no black flash).  '
                    'fade: 50/50 blend frame.  black: flush a black frame.  none: just switch.')
    ap.add_argument('--no-gpu', action='store_true')
    ap.add_argument('--no-strata', dest='strata', action='store_false',
                    help='skip the strata server: the AI page says the page is disabled')
    ap.add_argument('--strata-url', help='strata server root (default: found from the '
                    'launch configs strata writes, then STRATA_URL)')
    ap.add_argument('--strata-key', help='API key for /metrics (default: the key in the '
                    'launch config that owns that port, then STRATA_API_KEY)')
    ap.add_argument('--strata-state', metavar='DIR',
                    help='where to look for the state a strata install writes (its launch '
                    'configs and ready files); default $STRATA_STATE, then '
                    '$XDG_STATE_HOME/strata.  The dashboard service runs as a dedicated '
                    'user with no access to your home, so on a service box this is the '
                    'path to your login\'s ~/.local/state/strata')
    ap.add_argument('--sensors', action='store_true', help='print what was found and exit')
    ap.add_argument('--font-sample', action='store_true')
    args = ap.parse_args()

    args.page_list = [args.page.upper()] if args.page else \
        [p.strip().upper() for p in args.pages.split(',') if p.strip()]
    def is_page(p):
        if p in PAGES:
            return True
        return p.startswith('GPU') and p[3:].isdigit() and 1 <= int(p[3:]) <= 9
    bad = [p for p in args.page_list if not is_page(p)]
    if bad:
        print('unknown page(s) %s; pages are %s (+GPU1..GPU9)' %
              (bad, '/'.join(PAGES)), file=sys.stderr)
        return 2
    pinned = pin_self(args.cpus, args.nice)
    if pinned is not None:
        print('pinned to CPUs %s%s' % (sorted(pinned),
              '  nice %d' % args.nice if args.nice else ''), file=sys.stderr, flush=True)
    args.page_t0 = time.time()

    try:
        st, sb, sl, sr = [int(v) for v in args.safe_area.split(',')]
    except ValueError:
        st, sb, sl, sr = 8, 30, 8, 8
    c = Canvas(args.width, args.height, safe=(st, sb, sl, sr))
    c.aa = args.aa
    c.aa_min = args.aa_min
    if not getattr(args, 'ttf', False):
        here = os.path.dirname(os.path.abspath(__file__))
        helper = args.ttfplan or os.path.join(here, '..', 'build', 'ttfplan')
        if os.path.exists(helper):
            for cand in ([args.font] if args.font else list(FONT_CANDIDATES)):
                if not cand or not os.path.exists(cand):
                    continue
                plans = load_font(helper, cand, wght=args.weight, rise=args.text_rise / 100.0,
                                big_rise=args.big_rise / 100.0)
                if plans:
                    c.font = plans
                    print('font %s via %s (%d sizes)' %
                          (os.path.basename(cand), helper, len(plans)),
                          file=sys.stderr, flush=True)
                    break
    c.trace = args.audit
    if args.font_sample:
        font_sample(c)
        c.to_ppm(args.out)
        print('font sheet -> %s' % args.out)
        return 0

    m = Metrics(gpu=not args.no_gpu, history=args.history, interval=args.interval,
                strata=args.strata, strata_url=args.strata_url,
                strata_key=args.strata_key, strata_state=args.strata_state)
    # an empty title is the hostname: the systemd unit ships TITLE unset, so a
    # box identifies itself on the panel without a hard-coded name in the repo
    if not args.title.strip():
        args.title = m.host
    if m.gpu_thread is not None:
        m.gpu_thread.start()
    if m.strata_thread is not None:
        m.strata_thread.start()
    for _ in range(2):
        m.sample()
        time.sleep(0.25)

    if args.sensors:
        # nvidia-smi answers in a background thread: wait for it a little, so a
        # driver that is still loading shows up as a poll reason on this report
        # instead of a bare NONE
        for _ in range(40):
            if m.gpu_thread is None or m.gpu_thread.gpus:
                break
            time.sleep(0.25)
        for _ in range(20):
            if m.strata_thread is None or m.strata_thread.metrics:
                break
            time.sleep(0.25)
        m.sample()
        m.gpu = m.gpu_thread.gpus if m.gpu_thread else None
        m.strata = m.strata_thread.metrics if m.strata_thread else None
        print_sensors(m)
        m.shutdown()
        return 0

    child = None
    if args.exec:
        # the child's --fps is a *pacing cap*: if it equals our render rate, every
        # write blocks for a whole frame period, any extra frame (a fade, a
        # slow encode) builds a permanent queue, and the panel ends up showing
        # frames seconds behind.  Give the child headroom so writes complete
        # immediately and the panel is paced by us, not by a saturated pipe.
        child = subprocess.Popen([args.exec, '--fps', '%g' % max(args.fps * 8.0, 8.0), 'feed'],
                                 stdin=subprocess.PIPE,
                                 stdout=subprocess.DEVNULL)   # child stderr -> our log

    try:
        idx = -1
        drawn_idx = -1
        prev_px = None
        warm_until = 0.0
        warm_idx = -1
        fade_ms = 0.0
        write_ms = 0.0
        while True:
            t0 = time.time()
            if args.page_seconds > 0 and len(args.page_list) > 1:
                idx = int((t0 - args.page_t0) // args.page_seconds) % len(args.page_list)
            elif idx < 0:
                idx = 0
            page_changed = idx != drawn_idx
            if (warm_until and t0 < warm_until and idx == warm_idx
                    and prev_px is not None and child is not None):
                # warm-up after a page change: repeat the frame that is already
                # rendered.  No metrics sample and no re-render -- the panel
                # just wants several complete copies of the new page close
                # together, and re-rendering them is pure CPU burn
                wt = time.time()
                try:
                    child.stdin.write(prev_px)
                    child.stdin.flush()
                    write_ms = (time.time() - wt) * 1000.0
                except (BrokenPipeError, ValueError):
                    print('output closed', file=sys.stderr)
                    return 1
                if write_ms > 200.0:
                    print('write blocked %.0f ms (usblcd pipeline)' % write_ms,
                          file=sys.stderr, flush=True)
                nap = 1.0 / max(args.fps, args.warm_fps) - (time.time() - t0)
                if nap > 0:
                    time.sleep(nap)
                continue
            m.sample()
            if page_changed and args.transition == 'black' and child is not None:
                # the panel keeps displaying the last frame it fully received,
                # so a page change leaves the previous page on the glass until
                # this page's first frame lands: clear it first
                c.clear((0, 0, 0))
                try:
                    c.to_rgba(child.stdin)
                    child.stdin.flush()
                except (BrokenPipeError, ValueError):
                    pass
            drawn_idx = idx
            if page_changed:
                warm_until = time.time() + args.warm_seconds
                warm_idx = idx
            if page_changed and args.transition == 'clean' and child is not None:
                # the panel keeps the previous frame composited for a while, so
                # dense text from the old page ghosts over the new one.  Flush
                # this page's bare panels (same colours, no text) N times: each
                # composite fades the ghost further, with no black flash.
                c.text_mask = True
                draw(c, m, args, idx)
                c.text_mask = False
                bare = bytes(c.px)
                try:
                    for _ in range(max(1, args.clean_frames)):
                        child.stdin.write(bare)
                    child.stdin.flush()
                except (BrokenPipeError, ValueError):
                    pass
            draw(c, m, args, idx)
            if args.stamp:
                c.clip = None      # stamp lives outside the panels
                stamp = '%s %03d' % (args.page_list[idx], int(time.time() - args.page_t0) % 1000)
                c.rect(c.w - 262, 44, c.w - 8, 100, (0, 0, 0))
                c.text(c.w - 254, 50, stamp, 6, CRIT)
            if page_changed and args.transition == 'fade' and prev_px and child is not None:
                # half old page, half new page: the panel shows this mix, then
                # the real frame, so the switch reads as a cross-fade.  Costs
                # one ~0.2 s pass over the pixels per page change.
                tf = time.time()
                try:
                    child.stdin.write(mix_frames(prev_px, bytes(c.px)))
                    child.stdin.flush()
                    fade_ms = (time.time() - tf) * 1000.0
                    if fade_ms > 400.0:
                        print('fade: mix frame handed to usblcd in %.0f ms '
                          '(pipe throughput, not CPU)' % fade_ms, file=sys.stderr, flush=True)
                except (BrokenPipeError, ValueError):
                    pass
            if args.debug_grid:
                debug_grid(c, args)
            try:
                if child is not None:
                    wt = time.time()
                    c.to_rgba(child.stdin)
                    child.stdin.flush()
                    write_ms = (time.time() - wt) * 1000.0
                    if page_changed and args.resend > 1:
                        payload = bytes(c.px)
                        for _ in range(args.resend - 1):
                            child.stdin.write(payload)
                        child.stdin.flush()
                    prev_px = bytes(c.px)
                elif args.raw:
                    c.to_rgba(sys.stdout.buffer)
                    sys.stdout.buffer.flush()
                else:
                    c.to_ppm(args.out)
            except (BrokenPipeError, ValueError):
                print('output closed', file=sys.stderr)
                return 1
            if args.audit:
                for msg in overlaps(c.boxes):
                    print('OVERLAP %s' % msg)
                c.boxes = []
            if c.over and (args.audit or not args.once):
                # never lose text quietly: a truncated string is a layout bug,
                # so it goes to stderr (journal) even without --audit
                for line in c.over:
                    print('LAYOUT %s' % line, file=sys.stderr, flush=True)
                c.over = []
            if c.info:
                # not a bug: a list is capped to whatever its panel holds at the
                # current font size, so it is only worth mentioning on --audit
                if args.audit:
                    for line in c.info:
                        print('FIT %s' % line)
                c.info = []
            if args.once:
                print('frame -> %s' % args.out)
                return 0
            if write_ms > 200.0:
                print('write blocked %.0f ms (usblcd pipeline)' % write_ms,
                      file=sys.stderr, flush=True)
            # right after a page change, run faster for a moment: the panel
            # shows a *complete* frame only after it decodes one, so extra
            # complete copies of the new page settle the display quickly
            rate = args.interval
            if warm_until and time.time() < warm_until:
                rate = 1.0 / max(args.fps, args.warm_fps)
            nap = rate - (time.time() - t0)
            if nap > 0:
                time.sleep(nap)
    except KeyboardInterrupt:
        return 0
    finally:
        m.shutdown()
        if child is not None:
            try:
                child.stdin.close()
                child.terminate()
            except OSError:
                pass


if __name__ == '__main__':
    sys.exit(main())
