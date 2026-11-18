#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Prokopiev; see LICENSE and NOTICE in the repository root.
"""GPU poller checks (offline, no NVIDIA hardware needed).

The GPU page used to give up after five failed `nvidia-smi` queries.  That is
exactly the boot case: the dashboard unit starts ~2s into userspace, the NVIDIA
module answers ~7s into the kernel's clock, so a box on which everything works
still showed "QUERY FAILED OR NO GPU" until the service was restarted.  These
checks pin the behaviour that replaces it -- poll forever with a backoff, heal
on the first good reply, and stop only when there is no nvidia-smi to poll:

  1. a driver that answers late must light the page up by itself
  2. a missing nvidia-smi binary is the one permanent stop
  3. failures back off, and a good reply resets the poll to normal speed
  4. every state of an empty GPU page, and a stale one, draws cleanly

Checks 1-3 are the poller; check 4 is the renderer, which is what the panel
actually shows for "no GPU after boot".
"""
import importlib.util
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, 'dash'))

spec = importlib.util.spec_from_file_location(
    'dash', os.path.join(ROOT, 'dash', 'dash.py'))
dash = importlib.util.module_from_spec(spec)
sys.modules['dash'] = dash
spec.loader.exec_module(dash)

LATE_DRIVER = "rc=1 NVIDIA-SMI has failed because it couldn't communicate with the NVIDIA driver"
NO_NVIDIA = 'rc=6 No devices were found'

def fake_query(state, fail_for, why=LATE_DRIVER, good=True):
    """Stand in for nvidia-smi: `fail_for` failures, then replies forever."""
    gpus = [{'name': 'NVIDIA GeForce RTX 5080', 'util': 0.0, 'mem_used': 1024.0,
             'mem_total': 16303.0, 'temp': 41.0, 'fan': 30.0, 'power': 120.0,
             'mem_util': 3.0}]

    def q(timeout=5.0):
        state['calls'] += 1
        if state['calls'] <= fail_for:
            return None, why
        return (gpus, '') if good else (None, NO_NVIDIA)
    return q


def poll(fail_for, why=LATE_DRIVER, period=0.02, deadline=6.0, good=True):
    """Run the real GPUThread against a fake nvidia-smi; return it once settled."""
    state = {'calls': 0}
    saved = dash.query_gpus
    dash.query_gpus = fake_query(state, fail_for, why, good)
    t = dash.GPUThread(period=period)
    t.start()
    try:
        t.join(timeout=deadline)          # the thread never returns, so this is the timeout
    finally:
        t.stop.set()                      # do not leave it hammering the real nvidia-smi
        dash.query_gpus = saved
    return t, state


def main():
    ok = True

    def check(name, cond, note=''):
        print('%-46s %s %s' % (name, 'OK' if cond else 'FAILED', note))
        return cond

    # 1. the boot race: 8 failures (more than the old give-up threshold), then
    #    the driver answers -- the page must come back with no help
    t, state = poll(fail_for=8)
    ok &= check('boot race heals (8 fails then replies)',
                bool(t.gpus) and t.fail == 0 and not t.disabled and t.stale() is not None,
                'gpus=%d  fail=%d  stale=%.2fs' % (len(t.gpus or []), t.fail, t.stale() or -1))
    ok &= check('poll backoff reset to base after success', t.period == t.base,
                'period=%.3fs' % t.period)
    ok &= check('more than the old 5-failure limit polled', state['calls'] > 5,
                'queries=%d' % state['calls'])

    # 2. a box with no nvidia-smi binary: stop polling, say why
    t2, state2 = poll(fail_for=10 ** 9, why='not installed')
    ok &= check('missing nvidia-smi is a permanent stop',
                t2.disabled and not t2.gpus and not t2.is_alive(),
                'calls=%d  why=%r' % (state2['calls'], t2.why))

    # 3. a driver that never answers here: keep polling, slowly
    t3, state3 = poll(fail_for=10 ** 9, why=NO_NVIDIA, period=0.02, deadline=1.0)
    ok &= check('a silent driver keeps being polled', not t3.disabled and not t3.gpus,
                'fails=%d  why=%r' % (t3.fail, t3.why))
    ok &= check('failures back off (period grew)', t3.period > t3.base,
                'period=%.3fs (base %.3fs)' % (t3.period, t3.base))
    ok &= check('reason is reported for the panel', t3.why == NO_NVIDIA, repr(t3.why))

    # 4. the three empty-page states, drawn for real
    m = dash.Metrics(gpu=False)
    m.sample()                      # the fleet strip reads the hwmon fields sample() fills
    c = dash.Canvas(960, 540, safe=(8, 30, 8, 8))

    class Stub:
        def __init__(self, fail=3, period=3.0, disabled=False, why=NO_NVIDIA, stale=None):
            self.gpus, self.fail, self.period = None, fail, period
            self.disabled, self.why = disabled, why
            self._stale = stale

        def stale(self):
            return self._stale

    def gpu_row(name, util):
        return {'name': name, 'util': util, 'mem_used': 1024.0, 'mem_total': 16303.0,
                'temp': 41.0, 'fan': 30.0, 'power': 120.0, 'mem_util': 3.0}

    for name, th, rows in (('queries off (--no-gpu)', None, None),
                           ('no nvidia-smi binary', Stub(disabled=True, why='not installed'), None),
                           ('probing, driver not answering', Stub(), None),
                           ('query failed after good replies', Stub(stale=42.0), None),
                           ('two GPUs, replies gone stale', Stub(stale=42.0),
                            [gpu_row('NVIDIA GeForce RTX 5080', 5.0),
                             gpu_row('NVIDIA GeForce RTX 3080', 99.0)])):
        m.gpu_thread = th
        m.gpu = rows
        c.over, c.panels = [], []
        try:
            dash.body_gpu(c, m, None, 30, 540 - 30 - 8)
            # the empty states draw one 'GPU' panel, the populated one 'GPU n'
            # plus 'FLEET'; an empty list means the page drew nothing at all
            drawn = any(p[4] == 'GPU' or p[4].startswith('GPU ') or p[4] == 'FLEET'
                        for p in c.panels)
            good, note = drawn and not c.over, '; '.join(c.over)
        except Exception as e:                                  # noqa: BLE001
            good, note = False, '%s: %s' % (e.__class__.__name__, e)
        ok &= check('render: %s' % name, good, note)

    print()
    print('GPU poller checks %s' % ('passed' if ok else 'FAILED'))
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
