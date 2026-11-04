/* Resumable wrappers around libxsm3's DES helpers.
 * Copyright (C) 2026 3D Printed Controller Systems
 * SPDX-License-Identifier: LGPL-2.1-or-later
 */
#include "xsm3_async_crypto.h"
#include "excrypt.h"
#include <string.h>

enum { CRYPTO_NONE = 0, CRYPTO_CRYPT = 1, CRYPTO_MAC = 2 };

typedef struct {
    uint8_t kind, stage, phase, encrypt;
    const uint8_t *key;
    uint8_t *salt;
    const uint8_t *input;
    size_t length, offset;
    uint8_t *output;
    uint64_t sk[3];
    EXCRYPT_DES_STATE des;
    EXCRYPT_DES3_STATE des3;
    uint8_t feed[8], block[8], temp[8];
} crypto_context_t;

static crypto_context_t ctx;

static void clear_context(void) { memset(&ctx, 0, sizeof(ctx)); }

bool xsm3_crypto_busy(void) { return ctx.kind != CRYPTO_NONE; }

bool xsm3_crypto_begin_crypt(const uint8_t *key, const uint8_t *input, size_t length, uint8_t *output, bool encrypt) {
    if (ctx.kind != CRYPTO_NONE || !key || !input || !output || !length || (length & 7u)) return false;
    clear_context();
    ctx.kind = CRYPTO_CRYPT;
    ctx.key = key;
    ctx.input = input;
    ctx.length = length;
    ctx.output = output;
    ctx.encrypt = encrypt;
    return true;
}

bool xsm3_crypto_begin_mac(const uint8_t *key, uint8_t *salt, const uint8_t *input, size_t length, uint8_t *output) {
    if (ctx.kind != CRYPTO_NONE || !key || !input || !output || !length || (length & 7u)) return false;
    clear_context();
    ctx.kind = CRYPTO_MAC;
    ctx.key = key;
    ctx.salt = salt;
    ctx.input = input;
    ctx.length = length;
    ctx.output = output;
    return true;
}

static void crypt_prepare_block(void) {
    memcpy(ctx.block, ctx.input + ctx.offset, 8);
    if (ctx.encrypt) {
        for (unsigned i = 0; i < 8; i++) ctx.block[i] ^= ctx.feed[i];
    } else {
        memcpy(ctx.temp, ctx.block, 8); /* ciphertext becomes the next feed */
    }
    ctx.phase = 1;
}

static bool crypt_step(void) {
    switch (ctx.stage) {
    case 0:
        ExCryptDesParity(ctx.key, 0x10, (uint8_t *)ctx.sk);
        ctx.sk[2] = ctx.sk[0];
        ctx.stage++;
        return false;
    case 1: ExCryptDesKey(&ctx.des3.des_state[0], &ctx.sk[0]); ctx.stage++; return false;
    case 2: ExCryptDesKey(&ctx.des3.des_state[1], &ctx.sk[1]); ctx.stage++; return false;
    case 3: ExCryptDesKey(&ctx.des3.des_state[2], &ctx.sk[2]); ctx.stage++; return false;
    default: break;
    }

    if (!ctx.phase) crypt_prepare_block();
    if (ctx.encrypt) {
        if (ctx.phase == 1) ExCryptDesEcb(&ctx.des3.des_state[0], ctx.block, ctx.block, 1);
        else if (ctx.phase == 2) ExCryptDesEcb(&ctx.des3.des_state[1], ctx.block, ctx.block, 0);
        else ExCryptDesEcb(&ctx.des3.des_state[2], ctx.block, ctx.block, 1);
    } else {
        if (ctx.phase == 1) ExCryptDesEcb(&ctx.des3.des_state[2], ctx.block, ctx.block, 0);
        else if (ctx.phase == 2) ExCryptDesEcb(&ctx.des3.des_state[1], ctx.block, ctx.block, 1);
        else ExCryptDesEcb(&ctx.des3.des_state[0], ctx.block, ctx.block, 0);
    }
    if (++ctx.phase <= 3) return false;

    if (ctx.encrypt) memcpy(ctx.feed, ctx.block, 8);
    else {
        for (unsigned i = 0; i < 8; i++) ctx.block[i] ^= ctx.feed[i];
        memcpy(ctx.feed, ctx.temp, 8);
    }
    memcpy(ctx.output + ctx.offset, ctx.block, 8);
    ctx.offset += 8;
    ctx.phase = 0;
    if (ctx.offset < ctx.length) return false;
    ctx.kind = CRYPTO_NONE;
    return true;
}

static bool mac_step(void) {
    switch (ctx.stage) {
    case 0:
        ExCryptDesParity(ctx.key, 0x10, (uint8_t *)ctx.sk);
        ctx.sk[2] = ctx.sk[0];
        ctx.stage++;
        return false;
    case 1:
        ExCryptDesKey(&ctx.des, &ctx.sk[0]);
        ctx.stage++;
        return false;
    case 2:
        if (ctx.salt) {
            uint64_t value;
            memcpy(&value, ctx.salt, 8);
            value = SWAP64(SWAP64(value) + 1);
            memcpy(ctx.salt, &value, 8);
            ExCryptDesEcb(&ctx.des, ctx.salt, ctx.temp, 1);
        }
        ctx.stage++;
        return false;
    case 3:
        memcpy(ctx.block, ctx.input + ctx.offset, 8);
        for (unsigned i = 0; i < 8; i++) ctx.temp[i] ^= ctx.block[i];
        ExCryptDesEcb(&ctx.des, ctx.temp, ctx.temp, 1);
        ctx.offset += 8;
        if (ctx.offset >= ctx.length) ctx.stage++;
        return false;
    case 4:
        ctx.temp[0] ^= 0x80;
        ExCryptDesKey(&ctx.des3.des_state[0], &ctx.sk[0]);
        ctx.stage++;
        return false;
    case 5: ExCryptDesKey(&ctx.des3.des_state[1], &ctx.sk[1]); ctx.stage++; return false;
    case 6: ExCryptDesKey(&ctx.des3.des_state[2], &ctx.sk[2]); ctx.stage++; return false;
    case 7: ExCryptDesEcb(&ctx.des3.des_state[0], ctx.temp, ctx.temp, 1); ctx.stage++; return false;
    case 8: ExCryptDesEcb(&ctx.des3.des_state[1], ctx.temp, ctx.temp, 0); ctx.stage++; return false;
    default:
        ExCryptDesEcb(&ctx.des3.des_state[2], ctx.temp, ctx.output, 1);
        ctx.kind = CRYPTO_NONE;
        return true;
    }
}

bool xsm3_crypto_step(void) {
    if (ctx.kind == CRYPTO_NONE) return true;
    return ctx.kind == CRYPTO_CRYPT ? crypt_step() : mac_step();
}
