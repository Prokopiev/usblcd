# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Prokopiev; see LICENSE and NOTICE in the repository root.
# usblcd — headless CLI for the 87ad:70db USB LCD panel
CC      ?= cc
PKGS     = libusb-1.0 libjpeg

BASE_CFLAGS = -O2 -g -Wall -Wextra -std=c11
CPPFLAGS += -Ivendor $(shell pkg-config --cflags $(PKGS))
LDLIBS   += $(shell pkg-config --libs $(PKGS)) -lm

SRCS = src/usblcd.c src/device.c src/profile.c src/render.c src/calib.c
OBJS = $(SRCS:src/%.c=build/%.o)
DEPS = $(OBJS:.o=.d)

BIN  = usblcd
TOOLS = build/ppm2png

# True antialiased text for the dashboard: FreeType coverage grids for a TTF.
# Skipped silently when the FreeType development package is not installed --
# dash.py then keeps its built-in bitmap font.
TTF_CFLAGS := $(shell pkg-config --cflags freetype2 2>/dev/null)
TTF_LIBS   := $(shell pkg-config --libs freetype2 2>/dev/null)
ifneq ($(TTF_LIBS),)
TOOLS += build/ttfplan
build/ttfplan: tools/ttfplan.c | build
	$(CC) $(BASE_CFLAGS) $(TTF_CFLAGS) -Ivendor -o $@ tools/ttfplan.c $(TTF_LIBS)
endif

all: $(BIN) $(TOOLS)

$(BIN): $(OBJS)
	$(CC) $(CFLAGS) $(LDFLAGS) -o $@ $(OBJS) $(LDLIBS)

# PPM -> PNG preview (inspection only; `usblcd image` reads PPM directly)
build/ppm2png: tools/ppm2png.c vendor/stb_image_write.h | build
	$(CC) $(BASE_CFLAGS) -Ivendor -o $@ tools/ppm2png.c -lm

build/%.o: src/%.c | build
	$(CC) $(CPPFLAGS) $(BASE_CFLAGS) $(CFLAGS) -MMD -MP -c -o $@ $<

build:
	mkdir -p build

-include $(DEPS)

.PHONY: clean install install-udev install-sysusers install-service uninstall test live sanitize

install: $(BIN)
	install -Dm755 $(BIN) $(DESTDIR)/usr/local/bin/$(BIN)

# Create the service account.  The unit runs as `usblcd`, a nologin system user
# whose primary group is the group the udev rule puts the device node in — so
# the service needs no supplementary groups and `nobody` stays untouched.
install-sysusers:
	install -Dm644 contrib/sysusers.d/usblcd.conf \
		$(DESTDIR)/usr/lib/sysusers.d/usblcd.conf
	@command -v systemd-sysusers >/dev/null && systemd-sysusers /usr/lib/sysusers.d/usblcd.conf \
		&& echo "user/group 'usblcd' ready" || \
		echo "run as root: systemd-sysusers /usr/lib/sysusers.d/usblcd.conf"

# Let a normal user (or a systemd service) drive the panel without root.
install-udev:
	install -Dm644 rules.d/99-usblcd.rules \
		$(DESTDIR)/etc/udev/rules.d/99-usblcd.rules
	@echo "reload with: sudo udevadm control --reload && sudo udevadm trigger"

# Install the live dashboard as a boot service.  Run it as root, after `make
# install`: the project files go to /opt/usblcd (the unit's WorkingDirectory,
# and where dash.py finds build/ttfplan and the fonts) and the unit goes to
# systemd.  Enable it from the same sudo shell, while /opt/usblcd is world
# readable and before the umask-masked copy lands on the panel.
PREFIX ?= /opt/usblcd
install-service: $(BIN) $(TOOLS)
	install -d $(DESTDIR)$(PREFIX)/dash $(DESTDIR)$(PREFIX)/vendor \
		$(DESTDIR)$(PREFIX)/build $(DESTDIR)$(PREFIX)/fonts
	install -m644 dash/dash.py dash/font57.py $(DESTDIR)$(PREFIX)/dash/
	install -m644 vendor/stb_image.h vendor/stb_image_write.h $(DESTDIR)$(PREFIX)/vendor/
	install -m755 $(TOOLS) $(DESTDIR)$(PREFIX)/build/
	install -m644 fonts/Inter-var.ttf fonts/OFL.txt $(DESTDIR)$(PREFIX)/fonts/
	install -m644 README.md $(DESTDIR)$(PREFIX)/
	install -Dm644 contrib/usblcd-dashboard.service \
		$(DESTDIR)/etc/systemd/system/usblcd-dashboard.service
	@echo "then: sudo systemctl daemon-reload && sudo systemctl enable --now usblcd-dashboard.service"
	@echo "tune: sudo mkdir -p /etc/usblcd && sudo $${EDITOR:-nano} /etc/usblcd/dashboard.conf"

uninstall:
	rm -f $(DESTDIR)/usr/local/bin/$(BIN)

uninstall-service:
	rm -f $(DESTDIR)/etc/systemd/system/usblcd-dashboard.service
	rm -f $(DESTDIR)/usr/lib/sysusers.d/usblcd.conf
	rm -rf $(DESTDIR)$(PREFIX)

test: $(BIN)
	./tests/run.sh

# Everything the offline tests cannot prove: geometry + rotation + colour on
# glass.  Run it and watch the panel.
live: $(BIN)
	./usblcd info
	./usblcd marker
	@read -p "white bar on TOP, yellow on LEFT, magenta square bottom-right? Enter " _
	./usblcd --rotate 90 --fit fill marker
	@read -p "sideways: white bar on the RIGHT, magenta square bottom-left? Enter " _
	./usblcd --rotate 270 --fit fill marker
	@read -p "sideways the other way: white on LEFT, magenta square top-right? Enter " _
	./usblcd calibrate
	./usblcd image tests/sample.ppm
	@read -p "gradient upright, white band across the middle? Enter " _
	for b in 100 60 30 10 100; do ./usblcd --brightness $$b color ffffff >/dev/null; sleep 3; done
	./usblcd off
	@echo "the panel reverts to its logo ~3 s after the last frame"

# Address+UB sanitizer build, then run the offline suites under it.  Catches
# the class of bug a panel only shows as a garbled picture (the 270-degree
# rotation once read off the end of the source buffer on non-square images).
sanitize:
	@mkdir -p build/asan
	$(CC) -O1 -g -std=c11 -D_DEFAULT_SOURCE -fsanitize=address,undefined \
	    -fno-omit-frame-pointer -Isrc -Ivendor -o build/asan/usblcd $(SRCS) \
	    $$(pkg-config --cflags --libs $(PKGS)) -lm
	$(CC) -O1 -g -std=c11 -D_DEFAULT_SOURCE -fsanitize=address,undefined \
	    -Isrc -Ivendor -o build/asan/test_profile tests/test_profile.c src/profile.c -lm
	$(CC) -O1 -g -std=c11 -D_DEFAULT_SOURCE -fsanitize=address,undefined \
	    -Isrc -Ivendor -o build/asan/test_encode tests/test_encode.c src/render.c \
	    $$(pkg-config --cflags --libs libjpeg) -lm
	$(CC) -O1 -g -std=c11 -D_DEFAULT_SOURCE -fsanitize=address,undefined \
	    -Isrc -Ivendor -o build/asan/test_calib tests/test_calib.c src/calib.c src/profile.c -lm
	./build/asan/test_profile && ./build/asan/test_encode && ./build/asan/test_calib
	@echo "asan + ubsan clean"

clean:
	rm -rf build $(BIN) dash/__pycache__
