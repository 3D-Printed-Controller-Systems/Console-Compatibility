/* pcs_mod_rt.c: the few C library functions the compiler may call on its own (zeroing a local array, copying a
 * struct). Linked into every mod by pcsmod.py; unused ones are dropped. Small and plain on purpose: for bulk copies
 * use api->memcpy / api->memset, which use the firmware's fast versions. MIT License. */
#include <stddef.h>
#include <stdint.h>

void *memset(void *d, int c, size_t n) {
    uint8_t *p = d;
    while (n--) *p++ = (uint8_t)c;
    return d;
}

void *memcpy(void *restrict d, const void *restrict s, size_t n) {
    uint8_t *p = d;
    const uint8_t *q = s;
    while (n--) *p++ = *q++;
    return d;
}

void *memmove(void *d, const void *s, size_t n) {
    uint8_t *p = d;
    const uint8_t *q = s;
    if (p < q) { while (n--) *p++ = *q++; }
    else { p += n; q += n; while (n--) *--p = *--q; }
    return d;
}
