/* Embedded libc/randomness support for libxsm3.
 * Copyright (C) 2026 3D Printed Controller Systems
 * SPDX-License-Identifier: LGPL-2.1-or-later
 *
 * libxsm3 uses srand(time(NULL))/rand(). API 0.5 supplies the random bytes from
 * the firmware entropy pool; these shims preserve the upstream call surface.
 */
#include "xsm3_port.h"
#include <stdlib.h>
#include <time.h>

static const pcs_api_t *port_api;
static uint32_t rng_state = 0x6D2B79F5u;
static uint32_t mixed = 0xA5C31E27u;
static uint8_t random_bytes[32];
static uint8_t random_pos = sizeof random_bytes;

void xsm3_port_set_api(const pcs_api_t *api) { port_api = api; }

void xsm3_port_mix_entropy(const void *data, size_t len) {
    const uint8_t *p = (const uint8_t *)data;
    while (len--) mixed = (mixed ^ *p++) * 16777619u;
    if (port_api) {
        uint64_t t = port_api->time_us();
        mixed ^= (uint32_t)t ^ (uint32_t)(t >> 32);
    }
    if (!mixed) mixed = 0x9E3779B9u;
}

time_t time(time_t *out) {
    uint64_t t = port_api ? port_api->time_us() : 0;
    time_t v = (time_t)(mixed ^ (uint32_t)t ^ (uint32_t)(t >> 32));
    if (out) *out = v;
    return v;
}

void srand(unsigned seed) {
    random_pos = sizeof random_bytes;
    if (port_api && port_api->random_fill && port_api->random_fill(random_bytes, sizeof random_bytes)) {
        random_pos = 0;
        return;
    }
    rng_state ^= (uint32_t)seed + mixed + 0x9E3779B9u;   /* unreachable on the required API; defensive fallback */
    if (!rng_state) rng_state = 0x6D2B79F5u;
}

int rand(void) {
    if (random_pos < sizeof random_bytes) return random_bytes[random_pos++];
    uint32_t x = rng_state;
    x ^= x << 13;
    x ^= x >> 17;
    x ^= x << 5;
    rng_state = x;
    return (int)(x & 0x7FFFFFFFu);
}

int memcmp(const void *a, const void *b, size_t len) {
    const uint8_t *p = (const uint8_t *)a;
    const uint8_t *q = (const uint8_t *)b;
    while (len--) {
        if (*p != *q) return (int)*p - (int)*q;
        p++; q++;
    }
    return 0;
}
