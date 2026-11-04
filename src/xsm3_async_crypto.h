/* Resumable wrappers around libxsm3's DES helpers.
 * Copyright (C) 2026 3D Printed Controller Systems
 * SPDX-License-Identifier: LGPL-2.1-or-later
 */
#pragma once
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

bool xsm3_crypto_busy(void);
bool xsm3_crypto_begin_crypt(const uint8_t *key, const uint8_t *input, size_t length, uint8_t *output, bool encrypt);
bool xsm3_crypto_begin_mac(const uint8_t *key, uint8_t *salt, const uint8_t *input, size_t length, uint8_t *output);
bool xsm3_crypto_step(void); /* true when the active operation completed */
