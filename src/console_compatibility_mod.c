/* Console Compatibility device-exchange provider for 3DPCS mod API 0.5.
 * Copyright (C) 2026 3D Printed Controller Systems
 * SPDX-License-Identifier: LGPL-2.1-or-later
 */
#include "pcs_mod.h"
#include "xsm3.h"
#include "xsm3_port.h"
#include <stddef.h>
#include <stdint.h>

enum { AUTH_IDLE = 0, AUTH_BUSY = 1, AUTH_READY = 2 };

/* XSM3 identification packet derived from the upstream wired-controller sample, with the
 * advertised USB identity changed to match the firmware's RedOctane X-plorer personality
 * (VID 0x1430, PID 0x4748). The final byte is the XOR of payload bytes 5..27. */
static const uint8_t xplorer_identification[0x1D] = {
    0x49, 0x4B, 0x00, 0x00, 0x17, 0x41, 0x41, 0x41,
    0x41, 0x41, 0x41, 0x41, 0x41, 0x41, 0x41, 0x41,
    0x00, 0x00, 0x80, 0x02, 0x30, 0x14, 0x48, 0x47,
    0x03, 0x00, 0x01, 0x01, 0xEB,
};

static const pcs_api_t *api;
static uint8_t pending_request;
static uint8_t pending_data[0x22];
static uint8_t auth_state;
static uint8_t response_len;
static uint8_t active_request;

static void reset_auth(void) {
    pending_request = 0;
    response_len = 0;
    auth_state = AUTH_IDLE;
    active_request = 0;
    xsm3_initialise_state();
    xsm3_set_identification_data(xplorer_identification);
}

static int32_t copy_reply(const uint8_t *data, uint16_t len, uint16_t capacity, uint8_t *reply) {
    if (len > capacity) len = capacity;
    if (len) api->memcpy(reply, data, len);
    return len;
}

static int32_t security_request(const pcs_service_request_t *r, uint8_t *reply) {
    if (r->event == PCS_SERVICE_RESET) {
        reset_auth();
        return 0;
    }
    if (r->event != PCS_SERVICE_CONTROL || (r->setup.wIndex & 0xFFu) != 3u) return -1;

    /* Retail-console captures include zero-length 0x84 keepalives in both control
     * directions. They carry no authentication state or payload; ACK either form. */
    if (r->setup.bRequest == 0x84u &&
        (r->setup.bmRequestType == 0x41u || r->setup.bmRequestType == 0xC1u)) {
        return r->setup.wLength == 0u && r->length == 0u ? 0 : -1;
    }

    if (r->setup.bmRequestType == 0x41u) {
        uint16_t need = r->setup.bRequest == 0x82u ? 0x22u :
                        r->setup.bRequest == 0x87u ? 0x16u : 0u;
        if (!need || r->length != need || r->setup.wLength != need || auth_state == AUTH_BUSY) return -1;
        api->memcpy(pending_data, r->data, need);
        xsm3_port_mix_entropy(r->data, need);
        pending_request = r->setup.bRequest;
        auth_state = AUTH_BUSY;
        response_len = 0;
        return 0;
    }

    if (r->setup.bmRequestType != 0xC1u) return -1;
    switch (r->setup.bRequest) {
    case 0x81:
        reset_auth();
        return copy_reply(xplorer_identification, sizeof xplorer_identification, r->capacity, reply);
    case 0x83:
        if (auth_state != AUTH_READY || !response_len) return -1;
        return copy_reply(xsm3_challenge_response, response_len, r->capacity, reply);
    case 0x86: {
        uint8_t status[2] = { auth_state == AUTH_BUSY ? 1u : 2u, 0u };
        if (r->capacity < sizeof status) return -1;
        return copy_reply(status, sizeof status, r->capacity, reply);
    }
    default:
        return -1;
    }
}

static const pcs_usb_service_t service = {
    .size = sizeof(service),
    .service = PCS_SERVICE_DEVICE_EXCHANGE,
    .handle = security_request,
};

static bool init(void) {
    size_t need = offsetof(pcs_api_t, random_fill) + sizeof(api->random_fill);
    if (api->major != 0 || api->minor < 5 || api->size < need || !api->usb_service_claim || !api->random_fill) {
        api->fail("Needs mod API 0.5");
        return false;
    }
    xsm3_port_set_api(api);
    reset_auth();
    if (!api->usb_service_claim(&service)) {
        api->fail("Device service is already in use");
        return false;
    }
    api->log("Console Compatibility ready\n");
    return true;
}

static void task(void) {
#ifdef XSM3_REFERENCE_SYNC
    uint8_t request = pending_request;
    if (!request) return;
    pending_request = 0;
    if (request == 0x82u) {
        xsm3_do_challenge_init(pending_data);
        response_len = 46;
    } else {
        xsm3_do_challenge_verify(pending_data);
        response_len = 22;
    }
    auth_state = AUTH_READY;
#else
    if (pending_request) {
        active_request = pending_request;
        pending_request = 0;
        bool started = active_request == 0x82u ? xsm3_begin_challenge_init(pending_data) : xsm3_begin_challenge_verify(pending_data);
        if (!started) {
            auth_state = AUTH_IDLE;
            active_request = 0;
        }
        return;
    }
    if (!active_request || !xsm3_challenge_step()) return;
    response_len = active_request == 0x82u ? 46 : 22;
    active_request = 0;
    auth_state = AUTH_READY;
#endif
}

static const pcs_mod_t mod = {
    .api_major = PCS_MOD_API_MAJOR,
    .api_minor = PCS_MOD_API_MINOR,
    .size = sizeof(mod),
    .usb_mode = PCS_USB_NONE,
    .init = init,
    .task = task,
};

const pcs_mod_t *pcs_mod_main(const pcs_api_t *a) {
    api = a;
    return &mod;
}
