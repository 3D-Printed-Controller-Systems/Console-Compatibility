"""thumb: a small ARMv6-M (Cortex-M0+) Thumb interpreter for running mods on a PC. MIT License.

Written from the ARMv6-M Architecture Reference Manual for the in-repo mod simulator (`pcsmod.py sim`). It runs the
instructions gcc emits for `-mcpu=cortex-m0plus -mthumb` plus libgcc (division, 64-bit math). It is not a chip model:
no exceptions, no peripherals, no cycle timing. Memory is a list of regions; anything outside them is a fault with
a plain message, which is the point: a mod that reads or writes where it shouldn't is caught here, not on a controller.

Calls into the host: give a function pointer inside a "trap" region; when the PC lands there, the handler runs in
Python, sets r0 (and r1), and returns to LR.
"""
from __future__ import annotations

import struct


class Fault(Exception):
    """The program did something a controller would fault on (or the simulator can't do)."""


class Region:
    def __init__(self, name: str, base: int, size: int, writable: bool = True, data: bytes | None = None):
        self.name, self.base, self.size, self.writable = name, base, size, writable
        self.mem = bytearray(size)
        if data:
            self.mem[:len(data)] = data
        self.end = base + size


class Cpu:
    def __init__(self):
        self.r = [0] * 16              # r13 = SP, r14 = LR, r15 = PC (address of the current instruction)
        self.n = self.z = self.c = self.v = False
        self.regions: list[Region] = []
        self.traps: dict[int, object] = {}      # address (even) -> handler(cpu) -> None | int | (lo, hi)
        self.trap_base = self.trap_end = 0
        self.steps = 0
        self.lowest_sp = 0xFFFFFFFF

    # ------------------------------------------------------------------ memory
    def add(self, region: Region) -> Region:
        for o in self.regions:
            if region.base < o.end and o.base < region.end:
                raise ValueError(f"region {region.name} overlaps {o.name}")
        self.regions.append(region)
        return region

    def region_at(self, addr: int, size: int, write: bool) -> Region:
        for g in self.regions:
            if g.base <= addr and addr + size <= g.end:
                if write and not g.writable:
                    raise Fault(f"write to {g.name} at {addr:#010x} (read-only)")
                return g
        what = "write to" if write else "read from"
        if 0x40000000 <= addr < 0x60000000 or addr >= 0xD0000000:
            raise Fault(f"{what} hardware register {addr:#010x}: hardware isn't simulated")
        raise Fault(f"{what} {addr:#010x}, outside the mod's flash and RAM")

    def read(self, addr: int, size: int) -> int:
        if addr % size:
            raise Fault(f"unaligned {size}-byte read at {addr:#010x}")
        g = self.region_at(addr, size, False)
        return int.from_bytes(g.mem[addr - g.base:addr - g.base + size], "little")

    def write(self, addr: int, size: int, value: int):
        if addr % size:
            raise Fault(f"unaligned {size}-byte write at {addr:#010x}")
        g = self.region_at(addr, size, True)
        g.mem[addr - g.base:addr - g.base + size] = (value & ((1 << (8 * size)) - 1)).to_bytes(size, "little")

    def read_bytes(self, addr: int, n: int) -> bytes:
        if n == 0:
            return b""
        g = self.region_at(addr, n, False)
        return bytes(g.mem[addr - g.base:addr - g.base + n])

    def write_bytes(self, addr: int, data: bytes):
        if not data:
            return
        g = self.region_at(addr, len(data), True)
        g.mem[addr - g.base:addr - g.base + len(data)] = data

    def cstr(self, addr: int, limit: int = 256) -> str:
        out = bytearray()
        while len(out) < limit:
            b = self.read(addr + len(out), 1)
            if b == 0:
                break
            out.append(b)
        return out.decode("utf-8", "replace")

    # ------------------------------------------------------------------ calls
    def call(self, fn: int, args=(), ret_addr: int = 0x0EFFFFF0, max_steps: int = 2_000_000) -> int:
        """Call a Thumb function with up to 4 word arguments; returns r0. Stack args are not supported here."""
        if len(args) > 4:
            raise ValueError("at most 4 arguments")
        for i, a in enumerate(args):
            self.r[i] = a & 0xFFFFFFFF
        self.r[14] = ret_addr | 1
        self.branch(fn)
        start = self.steps
        while self.r[15] != ret_addr:
            if self.steps - start > max_steps:
                raise Fault(f"still running after {max_steps} instructions (stuck in a loop?)")
            self.step()
        return self.r[0]

    def branch(self, target: int):
        if not target & 1:
            raise Fault(f"branch to {target:#010x} without the Thumb bit (would fault on a Cortex-M0+)")
        self.r[15] = target & ~1

    # ------------------------------------------------------------------ flags helpers
    def nz(self, v: int) -> int:
        v &= 0xFFFFFFFF
        self.n, self.z = bool(v >> 31), v == 0
        return v

    def add_flags(self, a: int, b: int, carry: int) -> int:
        u = a + b + carry
        s = _s32(a) + _s32(b) + carry
        r = u & 0xFFFFFFFF
        self.n, self.z, self.c, self.v = bool(r >> 31), r == 0, u > 0xFFFFFFFF, s != _s32(r)
        return r

    def cond(self, c: int) -> bool:
        n, z, cy, v = self.n, self.z, self.c, self.v
        return [z, not z, cy, not cy, n, not n, v, not v, cy and not z, not cy or z, n == v, n != v,
                not z and n == v, z or n != v, True][c]

    # ------------------------------------------------------------------ one instruction
    def step(self):
        pc = self.r[15]
        if self.trap_base <= pc < self.trap_end:
            h = self.traps.get(pc)
            if h is None:
                raise Fault(f"call to an unknown host function at {pc:#010x}")
            res = h(self)
            if isinstance(res, tuple):
                self.r[0], self.r[1] = res[0] & 0xFFFFFFFF, res[1] & 0xFFFFFFFF
            elif res is not None:
                self.r[0] = res & 0xFFFFFFFF
            self.branch(self.r[14])
            return
        self.steps += 1
        ins = self.read(pc, 2)
        r = self.r
        nxt = pc + 2
        top = ins >> 11

        if top < 3:                                     # LSLS / LSRS / ASRS (immediate)
            imm, rm, rd = (ins >> 6) & 31, (ins >> 3) & 7, ins & 7
            v = r[rm]
            if top == 0:
                if imm:
                    self.c = bool((v >> (32 - imm)) & 1)
                    v = (v << imm) & 0xFFFFFFFF
            elif top == 1:
                imm = imm or 32
                self.c = bool((v >> (imm - 1)) & 1)
                v = 0 if imm == 32 else v >> imm
            else:
                imm = imm or 32
                self.c = bool((v >> (imm - 1)) & 1) if imm < 32 else bool(v >> 31)
                v = (_s32(v) >> min(imm, 31)) & 0xFFFFFFFF
            r[rd] = self.nz(v)
        elif top == 3:                                  # ADDS / SUBS (register or imm3)
            op, x, rn, rd = (ins >> 9) & 3, (ins >> 6) & 7, (ins >> 3) & 7, ins & 7
            b = x if op & 2 else r[x]
            r[rd] = self.add_flags(r[rn], b, 0) if not op & 1 else self.add_flags(r[rn], ~b & 0xFFFFFFFF, 1)
        elif top < 8:                                   # MOVS / CMP / ADDS / SUBS imm8
            rd, imm = (ins >> 8) & 7, ins & 0xFF
            if top == 4:
                r[rd] = self.nz(imm)
            elif top == 5:
                self.add_flags(r[rd], ~imm & 0xFFFFFFFF, 1)
            elif top == 6:
                r[rd] = self.add_flags(r[rd], imm, 0)
            else:
                r[rd] = self.add_flags(r[rd], ~imm & 0xFFFFFFFF, 1)
        elif ins >> 10 == 0b010000:                     # data processing
            op, rm, rd = (ins >> 6) & 15, (ins >> 3) & 7, ins & 7
            a, b = r[rd], r[rm]
            if op == 0:
                r[rd] = self.nz(a & b)
            elif op == 1:
                r[rd] = self.nz(a ^ b)
            elif op in (2, 3, 4, 7):                    # LSL / LSR / ASR / ROR by register
                s = b & 0xFF
                if op == 2:
                    if s:
                        self.c = bool((a >> (32 - s)) & 1) if s <= 32 else False
                        a = (a << s) & 0xFFFFFFFF if s < 32 else 0
                elif op == 3:
                    if s:
                        self.c = bool((a >> (s - 1)) & 1) if s <= 32 else False
                        a = a >> s if s < 32 else 0
                elif op == 4:
                    if s:
                        self.c = bool((a >> (s - 1)) & 1) if s < 32 else bool(a >> 31)
                        a = (_s32(a) >> min(s, 31)) & 0xFFFFFFFF
                else:
                    if s:
                        s5 = s & 31
                        if s5:
                            a = ((a >> s5) | (a << (32 - s5))) & 0xFFFFFFFF
                        self.c = bool(a >> 31)
                r[rd] = self.nz(a)
            elif op == 5:
                r[rd] = self.add_flags(a, b, int(self.c))
            elif op == 6:
                r[rd] = self.add_flags(a, ~b & 0xFFFFFFFF, int(self.c))
            elif op == 8:
                self.nz(a & b)
            elif op == 9:                               # RSBS rd, rm, #0
                r[rd] = self.add_flags(0, ~b & 0xFFFFFFFF, 1)
            elif op == 10:
                self.add_flags(a, ~b & 0xFFFFFFFF, 1)
            elif op == 11:
                self.add_flags(a, b, 0)
            elif op == 12:
                r[rd] = self.nz(a | b)
            elif op == 13:
                r[rd] = self.nz(a * b)
            elif op == 14:
                r[rd] = self.nz(a & ~b)
            else:
                r[rd] = self.nz(~b)
        elif ins >> 10 == 0b010001:                     # hi-register ADD / CMP / MOV, BX / BLX
            op = (ins >> 8) & 3
            rm = (ins >> 3) & 15
            rd = (ins & 7) | ((ins >> 4) & 8)
            vm = nxt + 2 if rm == 15 else r[rm]
            if op == 3:
                if ins & 0x80:
                    r[14] = nxt | 1
                self.branch(vm)
                self._sp_check()
                return
            vd = nxt + 2 if rd == 15 else r[rd]
            if op == 1:
                self.add_flags(vd, ~vm & 0xFFFFFFFF, 1)
            else:
                v = (vd + vm) & 0xFFFFFFFF if op == 0 else vm
                if rd == 15:
                    self.r[15] = v & ~1
                    return
                r[rd] = v
                if rd == 13:
                    self._sp_check()
        elif top == 9:                                  # LDR rt, [pc, #imm]
            addr = ((pc + 4) & ~3) + (ins & 0xFF) * 4
            r[(ins >> 8) & 7] = self.read(addr, 4)
        elif ins >> 12 == 0b0101:                       # load / store, register offset
            op, rm, rn, rt = (ins >> 9) & 7, (ins >> 6) & 7, (ins >> 3) & 7, ins & 7
            addr = (r[rn] + r[rm]) & 0xFFFFFFFF
            if op == 0:
                self.write(addr, 4, r[rt])
            elif op == 1:
                self.write(addr, 2, r[rt])
            elif op == 2:
                self.write(addr, 1, r[rt])
            elif op == 3:
                r[rt] = _sx(self.read(addr, 1), 8)
            elif op == 4:
                r[rt] = self.read(addr, 4)
            elif op == 5:
                r[rt] = self.read(addr, 2)
            elif op == 6:
                r[rt] = self.read(addr, 1)
            else:
                r[rt] = _sx(self.read(addr, 2), 16)
        elif ins >> 13 == 0b011:                        # STR / LDR / STRB / LDRB immediate
            byte, load = (ins >> 12) & 1, (ins >> 11) & 1
            imm, rn, rt = (ins >> 6) & 31, (ins >> 3) & 7, ins & 7
            size = 1 if byte else 4
            addr = (r[rn] + imm * size) & 0xFFFFFFFF
            if load:
                r[rt] = self.read(addr, size)
            else:
                self.write(addr, size, r[rt])
        elif ins >> 12 == 0b1000:                       # STRH / LDRH immediate
            imm, rn, rt = (ins >> 6) & 31, (ins >> 3) & 7, ins & 7
            addr = (r[rn] + imm * 2) & 0xFFFFFFFF
            if ins & 0x800:
                r[rt] = self.read(addr, 2)
            else:
                self.write(addr, 2, r[rt])
        elif ins >> 12 == 0b1001:                       # STR / LDR sp-relative
            rt, addr = (ins >> 8) & 7, (r[13] + (ins & 0xFF) * 4) & 0xFFFFFFFF
            if ins & 0x800:
                r[rt] = self.read(addr, 4)
            else:
                self.write(addr, 4, r[rt])
        elif ins >> 12 == 0b1010:                       # ADR / ADD rd, sp, #imm
            rd, imm = (ins >> 8) & 7, (ins & 0xFF) * 4
            r[rd] = (r[13] + imm) & 0xFFFFFFFF if ins & 0x800 else ((pc + 4) & ~3) + imm
        elif ins >> 12 == 0b1011:
            self._misc(ins, pc)
            if self.r[15] != pc:
                return
        elif ins >> 12 == 0b1100:                       # STM / LDM (increment after, writeback)
            rn, lst = (ins >> 8) & 7, ins & 0xFF
            addr = r[rn]
            if not lst:
                raise Fault(f"empty register list at {pc:#010x}")
            if ins & 0x800:
                for i in range(8):
                    if lst >> i & 1:
                        r[i] = self.read(addr, 4)
                        addr += 4
                if not lst >> rn & 1:
                    r[rn] = addr
            else:
                for i in range(8):
                    if lst >> i & 1:
                        self.write(addr, 4, r[i])
                        addr += 4
                r[rn] = addr
        elif ins >> 12 == 0b1101:                       # B<cond> / SVC / UDF
            c = (ins >> 8) & 15
            if c == 15:
                raise Fault(f"SVC at {pc:#010x}: mods can't make system calls")
            if c == 14:
                raise Fault(f"UDF (undefined instruction) at {pc:#010x}")
            if self.cond(c):
                self.r[15] = (pc + 4 + _sx(ins & 0xFF, 8) * 2) & 0xFFFFFFFF
                return
        elif top == 0b11100:                            # B
            self.r[15] = (pc + 4 + _sx(ins & 0x7FF, 11) * 2) & 0xFFFFFFFF
            return
        elif top in (0b11101, 0b11110, 0b11111):        # 32-bit instructions
            lo = self.read(pc + 2, 2)
            nxt = pc + 4
            if top == 0b11110 and (lo >> 14) == 0b11 and lo & 0x1000:   # BL
                s = (ins >> 10) & 1
                j1, j2 = (lo >> 13) & 1, (lo >> 11) & 1
                i1, i2 = 1 - (j1 ^ s), 1 - (j2 ^ s)
                off = (s << 24) | (i1 << 23) | (i2 << 22) | ((ins & 0x3FF) << 12) | ((lo & 0x7FF) << 1)
                r[14] = nxt | 1
                self.r[15] = (nxt + _sx(off, 25)) & 0xFFFFFFFF
                return
            if ins == 0xF3BF and (lo & 0xFF00) == 0x8F00:                # DSB / DMB / ISB
                pass
            else:
                raise Fault(f"32-bit instruction {ins:04x} {lo:04x} at {pc:#010x} isn't supported "
                            "(MRS / MSR and friends are off limits for mods)")
        else:
            raise Fault(f"unknown instruction {ins:04x} at {pc:#010x}")
        self.r[15] = nxt

    def _misc(self, ins: int, pc: int):
        r = self.r
        if ins >> 8 == 0b10110000:                      # ADD / SUB sp, #imm7*4
            imm = (ins & 0x7F) * 4
            r[13] = (r[13] - imm if ins & 0x80 else r[13] + imm) & 0xFFFFFFFF
            self._sp_check()
        elif ins >> 8 == 0b10110010:                    # SXTH / SXTB / UXTH / UXTB
            op, rm, rd = (ins >> 6) & 3, (ins >> 3) & 7, ins & 7
            v = r[rm]
            r[rd] = [_sx(v & 0xFFFF, 16), _sx(v & 0xFF, 8), v & 0xFFFF, v & 0xFF][op]
        elif ins >> 9 == 0b1011010:                     # PUSH
            lst = (ins & 0xFF) | ((ins & 0x100) << 6)
            regs = [i for i in range(15) if lst >> i & 1]
            sp = r[13] - 4 * len(regs)
            r[13] = sp & 0xFFFFFFFF
            self._sp_check()
            for i in regs:
                self.write(sp, 4, r[i])
                sp += 4
        elif ins >> 9 == 0b1011110:                     # POP
            sp = r[13]
            target = None
            for i in range(8):
                if ins >> i & 1:
                    r[i] = self.read(sp, 4)
                    sp += 4
            if ins & 0x100:
                target = self.read(sp, 4)
                sp += 4
            r[13] = sp & 0xFFFFFFFF
            if target is not None:
                self.branch(target)
                return
        elif ins >> 8 == 0b10111010:                    # REV / REV16 / REVSH
            op, rm, rd = (ins >> 6) & 3, (ins >> 3) & 7, ins & 7
            b = struct.pack("<I", r[rm])
            if op == 0:
                r[rd] = struct.unpack(">I", b)[0]
            elif op == 1:
                r[rd] = struct.unpack("<I", bytes([b[1], b[0], b[3], b[2]]))[0]
            elif op == 3:
                r[rd] = _sx((b[0] << 8) | b[1], 16)
            else:
                raise Fault(f"unknown instruction {ins:04x} at {pc:#010x}")
        elif ins & 0xFFEF == 0xB662:                    # CPSIE / CPSID i
            raise Fault(f"CPSID / CPSIE at {pc:#010x}: mods can't turn interrupts off")
        elif ins >> 8 == 0b10111110:
            raise Fault(f"BKPT at {pc:#010x}")
        elif ins >> 8 == 0b10111111:                    # NOP, YIELD, WFE, WFI, SEV
            if ins & 0xFF in (0x20, 0x30):
                raise Fault(f"WFE / WFI at {pc:#010x}: hooks must not wait")
        else:
            raise Fault(f"unknown instruction {ins:04x} at {pc:#010x}")
        self.r[15] = pc + 2

    def _sp_check(self):
        if self.r[13] < self.lowest_sp:
            self.lowest_sp = self.r[13]


def _s32(v: int) -> int:
    v &= 0xFFFFFFFF
    return v - (1 << 32) if v >> 31 else v


def _sx(v: int, bits: int) -> int:
    if v >> (bits - 1) & 1:
        v -= 1 << bits
    return v & 0xFFFFFFFF
