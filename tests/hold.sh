#!/bin/sh
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Prokopiev; see LICENSE and NOTICE in the repository root.
# tests/hold.sh SECONDS [usblcd args...] — keep a frame on the panel for a while.
# The firmware paints its logo ~2-3 s after the last frame, so a held frame
# needs re-sending; this is the same loop `usblcd watch` runs, for arbitrary
# commands (pattern, color, ...).
set -u
sec=${1:?usage: hold.sh SECONDS [usblcd args...]}
shift
cd "$(dirname "$0")/.." || exit 1
end=$(( $(date +%s) + sec ))
while [ "$(date +%s)" -lt "$end" ]; do
	./usblcd "$@" >/dev/null 2>&1 || { echo "usblcd failed"; exit 1; }
	sleep 2
done
