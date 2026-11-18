#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Prokopiev; see LICENSE and NOTICE in the repository root.
"""Print the two lines the dashboard service needs to show the AI page.

The service runs as the dedicated `usblcd` user, and a strata install writes its
state (the launch config with the port and the API key `/metrics` demands) 0600
by the login that runs the model -- so the poller in the unit cannot read it, and
the endpoint has to be given to it.  This is the shortest way to give it:

    sudo sh -c 'umask 077; python3 tools/strata_conf.py > /etc/usblcd/dashboard.conf'

which is exactly the line `tools/strata_conf.py` prints, ready to paste.  The
file it writes then holds a secret, so it is written 0600 and the shell above
does the umask part.

Discovery is dash.py's own (the same code the AI page uses), so the URL and the
key are the ones the panel would have found on this box; `--state DIR` points it
at another login's strata state, `--url`/`--key` override it entirely, and
`--check` proves the pair by asking the server for /metrics.  Run it under sudo
and it reads the sudo-ing login's state (root can read it), which is the case
here: the key has to land in a root-owned file the service user cannot read.
"""
import argparse
import importlib.util
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


def load_dash():
    spec = importlib.util.spec_from_file_location('dash', os.path.join(ROOT, 'dash', 'dash.py'))
    dash = importlib.util.module_from_spec(spec)
    sys.modules['dash'] = dash
    spec.loader.exec_module(dash)
    return dash


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--state', help='where to look for a strata install\'s state '
                    '(its launch configs and ready files).  Under sudo this is not '
                    'needed: the login that typed sudo is searched too, and root '
                    'can read its 0600 configs')
    ap.add_argument('--url', help='use this server root instead of the one found')
    ap.add_argument('--key', help='use this API key instead of the one found')
    ap.add_argument('--check', action='store_true',
                    help='GET /metrics once and report the model and state it answers with')
    args = ap.parse_args()

    dash = load_dash()
    if args.url:
        url, key, note = args.url.rstrip('/'), args.key or '', '--url'
    else:
        eps = dash.strata_endpoints(dash.strata_state_dirs(args.state))
        if not eps:
            print('no strata server found in:  %s' %
                  '  '.join(dash.strata_state_dirs(args.state)), file=sys.stderr)
            print('start the model (strata launch), or pass --url/--key', file=sys.stderr)
            return 1
        url, key, note = eps[0]

    if args.check:
        d, why = dash.query_strata(url, key, 10.0)
        if d is None:
            print('%s  %s' % (url, why), file=sys.stderr)
            return 1
        v = dash.strata_view(d) or {}
        print('%s  %s  model %s  state %s' %
              (url, note, v.get('model') or '?', v.get('state') or '?'), file=sys.stderr)

    # systemd's EnvironmentFile reads KEY=VALUE lines; a '#' line is a comment.
    # The key is a secret, so nothing here echoes it to stdout beyond this file.
    print('# the AI page: found by tools/strata_conf.py')
    print('STRATA_URL=%s' % url)
    print('STRATA_API_KEY=%s' % key)
    return 0


if __name__ == '__main__':
    sys.exit(main())
