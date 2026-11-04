/* Embedded support glue for libxsm3. LGPL-2.1-or-later. */
#pragma once
#include <stddef.h>
#include <stdint.h>
#include "pcs_mod.h"

void xsm3_port_set_api(const pcs_api_t *api);
void xsm3_port_mix_entropy(const void *data, size_t len);
