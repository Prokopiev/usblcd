#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Prokopiev; see LICENSE and NOTICE in the repository root.
"""AI page checks (offline: no strata server, no network, no model needed).

The AI page shows the strata monitor's Overview (view 1), read from the
server's /metrics.  Four things about it are worth pinning:

  1. the poller follows the GPU poller's rules -- a server that starts after
     this service (minutes later, always) must light the page up by itself, a
     failure only slows the polling down, and a box with nothing on disk to
     poll keeps looking because `strata launch` writes that state later
  2. the endpoint discovery pairs the port with the key that port's config
     owns, and the ready file (what the server actually bound) wins over the
     config it wrote, because a config is a wish and a ready file is a fact
  3. the Overview mapping reports a number the server did not report as None,
     never as an invented zero, and aggregates two cards the way the Overview
     does: busiest load, hottest temp, all the VRAM, all the watts
  4. every state of the page, including the empty and stale ones, draws
     inside its panels

Checks 1-2 are the poller, 3 the mapping, 4 the renderer.
"""
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, 'dash'))

spec = importlib.util.spec_from_file_location(
    'dash', os.path.join(ROOT, 'dash', 'dash.py'))
dash = importlib.util.module_from_spec(spec)
sys.modules['dash'] = dash
spec.loader.exec_module(dash)

NO_SERVER = 'no server answering ([Errno 111] Connection refused)'
BAD_KEY = 'HTTP 401 Unauthorized'


def fixture(state='idle', cards=2):
    """A /metrics reply shaped like the real one (field names included)."""
    gpus = [{'index': 1, 'name': 'NVIDIA GeForce RTX 3080', 'util': 19.0,
             'mem_used': 9314304000, 'mem_total': 10737418240, 'temp': 49,
             'power': 181.986}]
    if cards > 1:
        gpus.append({'index': 0, 'name': 'NVIDIA GeForce RTX 5080', 'util': 95.0,
                     'mem_used': 16624517120, 'mem_total': 17094934528,
                     'temp': 56, 'power': 166.586})
    live = {'state': state, 'queued': 0, 'phase': 'decode' if state == 'generating' else None,
            'prompt_tokens': 31741 if state in ('reading', 'generating') else None,
            'prompt_read': 291 if state == 'reading' else None,
            'prompt_total': 31741 if state == 'reading' else None,
            'generated': 244 if state == 'generating' else None,
            'max_tokens': 4096 if state == 'generating' else None,
            'tok_s': 163.1 if state == 'generating' else None,
            'tok_s_mean': 162.0 if state == 'generating' else None,
            'prefill_tok_s_mean': 35000.0 if state == 'reading' else None}
    return {
        'engine': {'model': 'swift-1.5-iq3_xxs', 'max_context': 262144,
                   'expert_slots': 10194, 'expert_cache_mib': 16356, 'spec': 6},
        'live': live,
        'requests': [{'id': 'fe8fa2fd5aae', 'time': 1795001501.05, 'duration_s': 2.4,
                      'finish': 'stop', 'prompt_tokens': 31741, 'reused': 31450,
                      'output_tokens': 244, 'prompt_ms': 873.0, 'decode_ms': 1495.8,
                      'decode_tok_s': 163.1, 'hit_rate': 0.931},
                     {'id': 'a1b2c3d4e5f6', 'time': 1795001498.0, 'duration_s': 3.1,
                      'finish': 'cancelled', 'prompt_tokens': 1200, 'reused': 0,
                      'output_tokens': 2, 'prompt_ms': 110.0, 'decode_ms': 900.0,
                      'hit_rate': 0.0},
                     {'id': '778899001122', 'time': 1795001490.0, 'duration_s': 0.9,
                      'finish': 'error', 'error': True, 'prompt_tokens': 40,
                      'reused': 0, 'output_tokens': 0, 'prompt_ms': 40.0,
                      'decode_ms': 0.0}],
        'totals': {'since': 1794994437.5, 'requests': 378, 'prompt_tokens': 30642383,
                   'reused': 30420339, 'output_tokens': 127127,
                   'prompt_ms': 404296.6, 'decode_ms': 1057221.8},
        'hardware': {'gpus': gpus, 'gpu_util': 49.5, 'gpu_mem_used': 25938821120,
                     'gpu_mem_total': 27832352768, 'gpu_temp': 56,
                     'gpu_power': 348.572, 'gpu_power_limit': 680.0,
                     'gpu_pcie_gen': 3, 'gpu_pcie_gen_max': 3, 'gpu_pcie_width': 4,
                     'gpu_pcie_rx_mb': 2777.19, 'cpu': 66.6,
                     'ram_used': 60323405824, 'ram_total': 67168075776,
                     'disk_read_mb': 5.5, 'disk_write_mb': 0.007, 'tok_s': 162.09},
        'hardware_static': {'gpu_name': 'RTX 3080 + RTX 5080', 'gpu_count': 2,
                            'cpu_name': 'i7-13700K', 'cores': 16, 'threads': 24},
        'history': {'gpu_util': [40.0 + i for i in range(60)],
                    'gpu_mem_used': [25900000000] * 60,
                    'gpu_temp': [50 + (i % 8) for i in range(60)],
                    'gpu_power': [300.0 + i for i in range(60)],
                    'gpu_pcie_rx_mb': [1000.0 + i * 30 for i in range(60)],
                    'cpu': [30.0 + i % 40 for i in range(60)],
                    'ram_used': [60000000000] * 60,
                    'disk_read_mb': [1.0, None] * 30,
                    'tok_s': [120.0 + (i % 40) for i in range(60)],
                    'prefill_tok_s_mean': [None] * 60},
        'time': time.time(),
    }


def fake_query(state, fail_for, why=NO_SERVER, good=True):
    """Stand in for GET /metrics: `fail_for` failures, then replies forever."""
    def q(url, key, timeout=3.0):
        state['calls'] += 1
        state.setdefault('urls', []).append((url, key))
        if state['calls'] <= fail_for:
            return None, why
        return (fixture(), '') if good else (None, why)
    return q


def poll(fail_for, why=NO_SERVER, period=0.02, deadline=4.0, good=True, endpoints=None):
    """Run the real StrataThread against a fake /metrics; return it once settled."""
    state = {'calls': 0}
    saved = dash.query_strata
    dash.query_strata = fake_query(state, fail_for, why, good)
    t = dash.StrataThread(period=period)
    if endpoints is not None:
        t.endpoints = endpoints
        t.pinned = None
        t.i = 0
    t.start()
    try:
        t.join(timeout=deadline)          # the thread never returns, so this is the timeout
    finally:
        t.stop.set()                      # do not leave it hammering a real server
        dash.query_strata = saved
    return t, state


def fake_state_tree(root, ready=None, configs=()):
    """A directory that looks like a strata install wrote it."""
    os.makedirs(os.path.join(root, 'launch'), exist_ok=True)
    os.makedirs(os.path.join(root, 'runtime'), exist_ok=True)
    for name, port, key, model in configs:
        with open(os.path.join(root, 'launch', '%s.json' % name), 'w') as f:
            json.dump({'port': port, 'api_key': key, 'model_name': model}, f)
    for name, url, port in (ready or ()):
        with open(os.path.join(root, 'runtime', '%s.ready.json' % name), 'w') as f:
            json.dump({'api_url': url, 'bound_port': port}, f)
    return root


def main():
    ok = True

    def check(name, cond, note=''):
        print('%-52s %s %s' % (name, 'OK' if cond else 'FAILED', note))
        return cond

    # 1. the boot race: 12 failures (well past any give-up threshold), then the
    #    server answers -- the page must come back with no help
    t, state = poll(fail_for=12, good=True)
    ok &= check('server that answers late lights the page',
                t.metrics is not None and t.fail == 0 and t.stale() is not None,
                'fails=%d  stale=%.2fs' % (t.fail, t.stale() or -1))
    ok &= check('poll period reset to base after a good reply', t.period == t.base,
                'period=%.3fs' % t.period)
    ok &= check('polled past any old give-up limit', state['calls'] > 5,
                'queries=%d' % state['calls'])

    # 2. a server that never answers: keep polling, slowly, and say why
    t2, state2 = poll(fail_for=10 ** 9, why=NO_SERVER, period=0.02, deadline=0.8, good=False)
    ok &= check('a silent server keeps being polled',
                t2.metrics is None and t2.fail > 3 and state2['calls'] > 3,
                'fails=%d queries=%d' % (t2.fail, state2['calls']))
    ok &= check('failures back off (period grew)', t2.period > t2.base,
                'period=%.3fs (base %.3fs)' % (t2.period, t2.base))
    ok &= check('the refusal reason reaches the panel', t2.why == NO_SERVER, repr(t2.why))

    # a wrong key is its own case: 401, and it heals when the config is re-read
    t3, _ = poll(fail_for=10 ** 9, why=BAD_KEY, period=0.02, deadline=0.4, good=False)
    ok &= check('a 401 is reported as a key problem', '401' in t3.why, repr(t3.why))

    # 3. nothing on disk to poll: not a permanent stop, the search is re-run, and the
    #    page's reason says 'nothing configured' instead of pretending it was a network error
    saved_eps = dash.strata_endpoints
    dash.strata_endpoints = lambda dirs=None: []
    try:
        t4, state4 = poll(fail_for=10 ** 9, period=0.02, deadline=0.4, good=False,
                          endpoints=[])
    finally:
        dash.strata_endpoints = saved_eps
    ok &= check('no endpoints keeps looking (never stops)',
                t4.metrics is None and state4['calls'] == 0 and
                'no local strata server' in t4.why, repr(t4.why))

    # 5. discovery: the ready file's URL paired with the config's key
    with tempfile.TemporaryDirectory() as tmp:
        fake_state_tree(tmp, ready=[('launch-swift', 'http://127.0.0.1:47832', 47832)],
                        configs=[('swift', 47832, 'k:secret', 'swift-1.5-iq3_xxs'),
                                 ('ud', 47900, 'k:other', 'ud-1.0-iq3_xxs')])
        eps = dash.strata_endpoints([tmp])
        ok &= check('ready URL is paired with its port key',
                    eps and eps[0][0] == 'http://127.0.0.1:47832' and eps[0][1] == 'k:secret',
                    str(eps[:1]))
        ok &= check('a config with no ready file is still polled',
                    any(u.endswith(':47900') for u, _, _ in eps), str(len(eps)))
        saved = os.environ.get('STRATA_URL')
        os.environ['STRATA_URL'] = 'http://10.0.0.5:8095'
        os.environ['STRATA_API_KEY'] = 'k:env'
        try:
            eps = dash.strata_endpoints([tmp])
            ok &= check('STRATA_URL wins over local configs',
                        eps[0][:2] == ('http://10.0.0.5:8095', 'k:env'), str(eps[:1]))
        finally:
            os.environ.pop('STRATA_URL', None)
            os.environ.pop('STRATA_API_KEY', None)
            if saved:
                os.environ['STRATA_URL'] = saved
        ok &= check('a box with no strata state finds nothing',
                    dash.strata_endpoints([os.path.join(tmp, 'nope')]) == [])

        # `sudo python3 tools/strata_conf.py` runs as root, and root has no strata
        # install: the state belongs to the login that typed sudo
        saved_su = os.environ.get('SUDO_USER')
        os.environ['SUDO_USER'] = 'definitely-not-a-user'
        try:
            dirs = dash.strata_state_dirs()
        finally:
            os.environ.pop('SUDO_USER', None)
            if saved_su:
                os.environ['SUDO_USER'] = saved_su
        ok &= check('sudo searches the sudo-ing login\'s state',
                    '/home/definitely-not-a-user/.local/state/strata' in dirs, str(dirs[:2]))
        ok &= check('without sudo, no phantom login is searched',
                    '/home/definitely-not-a-user/.local/state/strata' not in
                    dash.strata_state_dirs())

        # tools/strata_conf.py is what a service box uses to hand the endpoint to
        # the unit; it prints EnvironmentFile lines, and it says so when it found
        # nothing rather than writing a half-config
        env = dict(os.environ, HOME=tmp, XDG_STATE_HOME=os.path.join(tmp, 'none'),
                   STRATA_STATE='', STRATA_URL='', STRATA_API_KEY='')
        r = subprocess.run([sys.executable, os.path.join(ROOT, 'tools', 'strata_conf.py'),
                            '--state', tmp], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           env=env, timeout=60)
        out = r.stdout.decode()
        ok &= check('strata_conf.py prints the endpoint as EnvironmentFile lines',
                    r.returncode == 0 and 'STRATA_URL=http://127.0.0.1:47832' in out and
                    'STRATA_API_KEY=k:secret' in out,
                    out.replace('\n', '  ')[:96])
        empty = os.path.join(tmp, 'empty')
        os.makedirs(empty, exist_ok=True)
        r = subprocess.run([sys.executable, os.path.join(ROOT, 'tools', 'strata_conf.py'),
                            '--state', empty], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           env=env, timeout=60)
        ok &= check('strata_conf.py refuses to write a half-config',
                    r.returncode == 1 and b'STRATA' not in r.stdout and b'no strata server' in
                    r.stderr, r.stderr.decode().splitlines()[:1][0][:60] if r.stderr else '')

    # 5. the Overview mapping
    v = dash.strata_view(fixture('idle'))
    ok &= check('idle speed is the run average, not the last request',
                abs(v['speed'][0] - 120.3) < 0.5 and v['speed'][1] == 'run average',
                '%s (%s)' % v['speed'])
    ok &= check('idle context fill is the last request', v['ctx'][0] == 31741 + 244,
                str(v['ctx']))
    ok &= check('two cards: busiest load, summed VRAM, hottest temp',
                v['util'] == 95.0 and abs(v['vram'][1] - (10737418240 + 17094934528) / 2**30) < 0.01
                and v['temp'] == 56,
                'util=%s vram_total=%.1f temp=%s' % (v['util'], v['vram'][1], v['temp']))
    ok &= check('power limit falls back to the whole-computer reading',
                v['power'][1] == 680.0, str(v['power']))
    ok &= check('vram history is flat, not one card summed',
                len(set(v['vram_hist'])) == 1, str(len(v['vram_hist'])))
    ok &= check('a missing number stays None, never a zero',
                dash.snum(None) is None and dash.snum('3') is None and
                dash.sfmt(None) == '--' and dash.fmt_tok(None) == '--')
    ok &= check('a single token has no decode span, so no speed',
                dash.tok_s({'output_tokens': 1, 'decode_ms': 900.0}) is None and
                abs(dash.tok_s({'output_tokens': 244, 'decode_ms': 1495.8}) - 163.1) < 0.1,
                str(dash.tok_s({'output_tokens': 244, 'decode_ms': 1495.8})))
    gen = dash.strata_view(fixture('generating'))
    ok &= check('generating speed is the live rate', gen['speed'][0] == 163.1 and
                gen['speed'][1] == 'generating now', str(gen['speed']))
    ok &= check('generating progress is generated/max', gen['progress'] == (244.0, 4096.0),
                str(gen['progress']))
    ok &= check('generating state is Generating', gen['state'] == 'Generating', gen['state'])
    read = dash.strata_view(fixture('reading'))
    ok &= check('reading state reports prefill speed', read['state'] == 'Reading' and
                read['speed'][1] == 'reading the prompt', str(read['speed']))
    ok &= check('reply without a live block maps to nothing',
                dash.strata_view({'engine': {}}) is None)

    # 6. the renderer, in every state the page can be in
    c = dash.Canvas(960, 540, safe=(8, 30, 8, 8))

    wide = fixture('idle')
    # the widest rows the server can report: 8-digit token counts push the request
    # list past the panel's width even at the smallest font the dashboard draws
    wide['requests'][0].update({'prompt_tokens': 98765432, 'output_tokens': 9876543,
                                'decode_tok_s': 1234.5, 'duration_s': 9876.5,
                                'hit_rate': 0.9995, 'finish': 'tool_calls'})

    class Stub:
        def __init__(self, fail=3, period=3.0, why=NO_SERVER, stale=None, endpoints=True,
                     server='http://127.0.0.1:47832', source='swift-1.5-iq3_xxs'):
            self.metrics, self.fail, self.period, self.why = None, fail, period, why
            self.endpoints = [('http://127.0.0.1:47832', 'k', 'swift')] if endpoints else []
            self.server, self.source, self.pinned = server, source, None
            self._stale = stale

        def stale(self):
            return self._stale

    m = dash.Metrics(gpu=False, strata=False)
    m.sample()

    cases = [('queries off (--no-strata)', None, None),
             ('nothing on disk to poll', Stub(endpoints=False), None),
             ('probing, server not up yet', Stub(), None),
             ('reply stopped, numbers gone stale', Stub(stale=42.0), None),
             ('a reply with no live block', Stub(stale=1.0), {'engine': {}}),
             ('idle, two cards', Stub(stale=1.0), fixture('idle')),
             ('a request list too wide for the panel', Stub(stale=1.0), wide),
             ('generating, one card', Stub(stale=1.0), fixture('generating', cards=1)),
             ('reading, full live block', Stub(stale=1.0), fixture('reading'))]
    for name, th, d in cases:
        m.strata_thread = th
        m.strata = d if d is not None else (th.metrics if th is not None else None)
        if th is not None and d is not None:
            th.metrics = d
        c.clear()
        c.over, c.panels, c.info, c.boxes, c.clip = [], [], [], [], None
        c.trace = True
        try:
            dash.draw_header(c, m, type('A', (), {'title': 'TEST', 'page_list': ['AI'],
                                                  'page_seconds': 12, 'ring': 0,
                                                  'page_t0': 0})(), 'AI', 0)
            dash.body_ai(c, m, None, 50, c.h - 50 - 4)
            titles = [p[4] for p in c.panels]
            need = ('AI',) if d is None or dash.strata_view(d) is None else \
                   ('MODEL', 'SPEED', 'GPU LOAD', 'VRAM', 'GPU TEMP', 'POWER',
                    'PCIE RX', 'CPU', 'DISK READ', 'FILL', 'RECENT')
            good = all(any(t == p for p in titles) for t in need)
            note = '; '.join(c.over + dash.overlaps(c.boxes))
            good = good and not c.over and not dash.overlaps(c.boxes)
            if name == 'a request list too wide for the panel':
                # dropping columns is allowed (and reported); drawing past the
                # panel edge is not
                good = good and any('RECENT drops' in s for s in c.info)
        except Exception as e:                                  # noqa: BLE001
            good, note = False, '%s: %s' % (e.__class__.__name__, e)
        ok &= check('render: %s' % name, good, note or '(%d panels)' % len(c.panels))
    c.trace = False

    print()
    print('AI page checks %s' % ('passed' if ok else 'FAILED'))
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
