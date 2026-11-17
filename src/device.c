/* SPDX-License-Identifier: Apache-2.0
 * Copyright 2026 Prokopiev; see LICENSE and NOTICE in the repository root. */
/* device.c — libusb transport for the raw-bulk USBLCDNew protocol. */
#define _POSIX_C_SOURCE 200809L        /* nanosleep() under -std=c11 */
#include <libusb-1.0/libusb.h>

#include <stdio.h>
#include <stdarg.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

#include "usblcd.h"

#define WRITE_TIMEOUT_MS 5000
#define HANDSHAKE_TIMEOUT_MS 1000
#define HANDSHAKE_TRIES 3

struct lcd_internal {
    libusb_context *ctx;
};

/* libusb context lives in a static: one device per process for a CLI. */
static libusb_context *g_ctx;

static void dlog(const lcd_t *d, const char *fmt, ...)
{
    if (!d->verbose)
        return;
    va_list ap;
    va_start(ap, fmt);
    fprintf(stderr, "usblcd: ");
    vfprintf(stderr, fmt, ap);
    fprintf(stderr, "\n");
    va_end(ap);
}

int lcd_open(lcd_t *d, uint16_t vid, uint16_t pid, const char *serial, int verbose)
{
    memset(d, 0, sizeof(*d));
    d->verbose = verbose;
    d->vid = vid;
    d->pid = pid;
    d->ep_in = USBLCD_EP_IN;
    d->ep_out = USBLCD_EP_OUT;

    if (libusb_init(&g_ctx) < 0) {
        fprintf(stderr, "usblcd: libusb_init failed\n");
        return -1;
    }

    libusb_device_handle *h = NULL;
    if (serial && *serial) {
        /* Enumerate to match a serial number (multiple panels on one host). */
        libusb_device **list = NULL;
        ssize_t n = libusb_get_device_list(g_ctx, &list);
        for (ssize_t i = 0; i < n && !h; i++) {
            libusb_device *dev = list[i];
            struct libusb_device_descriptor dd;
            if (libusb_get_device_descriptor(dev, &dd) < 0)
                continue;
            if (dd.idVendor != vid || dd.idProduct != pid)
                continue;
            libusb_device_handle *cand = NULL;
            if (libusb_open(dev, &cand) < 0 || !cand)
                continue;
            unsigned char buf[64] = {0};
            int ok = 0;
            if (dd.iSerialNumber &&
                libusb_get_string_descriptor_ascii(cand, dd.iSerialNumber,
                                                   buf, sizeof(buf) - 1) > 0)
                ok = !strcmp((char *)buf, serial);
            if (ok) {
                h = cand;
                break;
            }
            libusb_close(cand);
        }
        libusb_free_device_list(list, 1);
    } else {
        h = libusb_open_device_with_vid_pid(g_ctx, vid, pid);
    }

    if (!h) {
        fprintf(stderr, "usblcd: no %04x:%04x device openable", vid, pid);
        if (serial && *serial)
            fprintf(stderr, " with serial %s", serial);
        fprintf(stderr, "\n  (check `lsusb`, and permissions — see README/udev rule)\n");
        libusb_exit(g_ctx);
        return -1;
    }
    d->handle = h;

    libusb_device *dev = libusb_get_device(h);
    struct libusb_device_descriptor dd;
    libusb_get_device_descriptor(dev, &dd);
    if (dd.iSerialNumber) {
        int r = libusb_get_string_descriptor_ascii(h, dd.iSerialNumber,
            (unsigned char *)d->serial, sizeof(d->serial) - 1);
        if (r < 0) d->serial[0] = 0;
    }
    if (dd.iProduct) {
        int r = libusb_get_string_descriptor_ascii(h, dd.iProduct,
            (unsigned char *)d->product, sizeof(d->product) - 1);
        if (r < 0) d->product[0] = 0;
    }

    /* Interface 0, first bulk OUT / bulk IN pair. */
    struct libusb_config_descriptor *cd = NULL;
    if (libusb_get_config_descriptor(dev, 0, &cd) != 0) {
        fprintf(stderr, "usblcd: cannot read configuration descriptor\n");
        lcd_close(d);
        return -1;
    }
    if (cd->bNumInterfaces == 0) {
        libusb_free_config_descriptor(cd);
        lcd_close(d);
        fprintf(stderr, "usblcd: device exposes no interface\n");
        return -1;
    }
    const struct libusb_interface_descriptor *it = &cd->interface[0].altsetting[0];
    d->iface = it->bInterfaceNumber;
    int out_ep = -1, in_ep = -1;
    for (int i = 0; i < it->bNumEndpoints; i++) {
        uint8_t a = it->endpoint[i].bEndpointAddress;
        if ((it->endpoint[i].bmAttributes & LIBUSB_TRANSFER_TYPE_MASK)
            != LIBUSB_TRANSFER_TYPE_BULK)
            continue;
        if (a & 0x80) { if (in_ep < 0) in_ep = a; }
        else          { if (out_ep < 0) out_ep = a; }
    }
    libusb_free_config_descriptor(cd);

    if (out_ep < 0 || in_ep < 0) {
        fprintf(stderr, "usblcd: device has no bulk OUT/IN endpoint pair\n");
        lcd_close(d);
        return -1;
    }
    if (out_ep != USBLCD_EP_OUT || in_ep != USBLCD_EP_IN)
        dlog(d, "using endpoints OUT=0x%02x IN=0x%02x (protocol default 0x%02x/0x%02x)",
             out_ep, in_ep, USBLCD_EP_OUT, USBLCD_EP_IN);
    d->ep_out = (uint8_t)out_ep;
    d->ep_in = (uint8_t)in_ep;

    if (libusb_kernel_driver_active(h, d->iface) == 1) {
        dlog(d, "detaching kernel driver from interface %d", d->iface);
        libusb_detach_kernel_driver(h, d->iface);
    }
    int r = libusb_claim_interface(h, d->iface);
    if (r < 0) {
        fprintf(stderr, "usblcd: claim interface %d failed: %s\n",
                d->iface, libusb_strerror(r));
        fprintf(stderr, "  another process (trcc?) may own the device, or the\n"
                        "  udev rule/grant is missing\n");
        lcd_close(d);
        return -1;
    }
    dlog(d, "opened %04x:%04x iface=%d serial=%s", d->vid, d->pid, d->iface,
         d->serial[0] ? d->serial : "-");
    return 0;
}

void lcd_close(lcd_t *d)
{
    libusb_device_handle *h = d->handle;
    if (h) {
        libusb_release_interface(h, d->iface);
        libusb_close(h);
        d->handle = NULL;
    }
    if (g_ctx) {
        libusb_exit(g_ctx);
        g_ctx = NULL;
    }
}

int lcd_handshake(lcd_t *d, uint8_t *resp, int cap, profile_t *prof)
{
    uint8_t req[USBLCD_HDR_SIZE] = {0};
    build_header(req, SSCRM_DEV_INFO, 0, 0, 0);
    req[56] = 0x01;                          /* device info query */

    int sent = 0, got = 0, r = -1;

    /* The firmware goes deaf on the IN endpoint every now and then - right
     * after a replug, after a port reset, or when the last session left a
     * stalled pipe.  Clear the endpoint and try a few times before failing,
     * so the CLI and the dashboard service ride out that window instead of
     * exiting on the first timeout and waiting for systemd's RestartSec. */
    for (int attempt = 1; attempt <= HANDSHAKE_TRIES; attempt++) {
        r = libusb_bulk_transfer(d->handle, d->ep_out, req, sizeof(req),
                                 &sent, HANDSHAKE_TIMEOUT_MS);
        if (r >= 0)
            r = libusb_bulk_transfer(d->handle, d->ep_in, resp, cap, &got,
                                     HANDSHAKE_TIMEOUT_MS);
        if (r >= 0 && got >= 41 && resp[24] != 0)
            break;
        if (attempt < HANDSHAKE_TRIES) {
            libusb_clear_halt(d->handle, d->ep_in);
            libusb_clear_halt(d->handle, d->ep_out);
            struct timespec ts = { 0, 400 * 1000 * 1000 };
            nanosleep(&ts, NULL);
        }
    }
    if (r < 0) {
        fprintf(stderr, "usblcd: handshake %s: %s\n",
                sent ? "read failed" : "write failed", libusb_strerror(r));
        return -1;
    }
    if (got < 41 || resp[24] == 0) {
        fprintf(stderr, "usblcd: handshake reply rejected (len=%d, resp[24]=%s)\n",
                got, got > 24 ? "0" : "missing");
        return -1;
    }

    if (prof) {
        bulk_profile(resp[24], got > 36 ? resp[36] : 0, prof);
        /* Product tag: resp[56] == 0x81 → 4 raw bytes (hex string). */
        if (got > 52)
            snprintf(prof->name, sizeof(prof->name), "%02x%02x%02x%02x",
                     resp[48], resp[49], resp[50], resp[51]);
    }
    return got;
}

int lcd_write_frame(lcd_t *d, const uint8_t *frame, size_t len)
{
    for (size_t off = 0; off < len; off += USBLCD_CHUNK_SIZE) {
        size_t n = len - off;
        if (n > USBLCD_CHUNK_SIZE)
            n = USBLCD_CHUNK_SIZE;
        int sent = 0;
        int r = libusb_bulk_transfer(d->handle, d->ep_out,
                                     (unsigned char *)frame + off, (int)n, &sent,
                                     WRITE_TIMEOUT_MS);
        if (r < 0) {
            fprintf(stderr, "usblcd: bulk write at offset %zu failed: %s\n",
                    off, libusb_strerror(r));
            return -1;
        }
        if ((size_t)sent < n) {
            fprintf(stderr, "usblcd: SHORT write at offset %zu of %zu "
                    "(%d of %zu bytes moved) — panel shows the previous frame\n",
                    off, len, sent, n);
            return -1;
        }
    }
    /* Zero-length packet delimiter when the frame is 512-byte aligned. */
    if (len % USBLCD_ALIGN == 0) {
        int sent = 0;
        libusb_bulk_transfer(d->handle, d->ep_out, NULL, 0, &sent, WRITE_TIMEOUT_MS);
    }
    dlog(d, "wrote %zu byte frame", len);
    return 0;
}

int lcd_command(lcd_t *d, uint32_t cmd, const uint8_t *payload, size_t len,
                uint8_t *resp, int cap)
{
    uint8_t hdr[USBLCD_HDR_SIZE];
    build_header(hdr, cmd, 0, 0, (uint32_t)len);

    uint8_t *buf = malloc(USBLCD_HDR_SIZE + len);
    if (!buf)
        return -1;
    memcpy(buf, hdr, USBLCD_HDR_SIZE);
    if (len)
        memcpy(buf + USBLCD_HDR_SIZE, payload, len);

    int sent = 0;
    int r = libusb_bulk_transfer(d->handle, d->ep_out, buf,
                                 (int)(USBLCD_HDR_SIZE + len), &sent,
                                 WRITE_TIMEOUT_MS);
    free(buf);
    if (r < 0) {
        fprintf(stderr, "usblcd: command %u write failed: %s\n", cmd,
                libusb_strerror(r));
        return -1;
    }
    if (!resp || cap <= 0)
        return 0;

    int got = 0;
    r = libusb_bulk_transfer(d->handle, d->ep_in, resp, cap, &got,
                             HANDSHAKE_TIMEOUT_MS);
    if (r < 0) {
        fprintf(stderr, "usblcd: command %u read failed: %s\n", cmd,
                libusb_strerror(r));
        return -1;
    }
    return got;
}
