#!/usr/bin/env python3
"""pcsmod: build a firmware mod into a .pcsmod file the app can place anywhere. MIT License.

STATUS: mod API 0.5. The app does not contain a compiler: the mod is linked twice with different
flash and RAM bases; every 32-bit word that moves with a base is an address, and the list of those words is all the
app needs to put the mod at any 4 KB slot in the mod area and any 8-byte-aligned spot in the mod RAM.

  pcsmod.py build <src.c>... -I include -o out.pcsmod      needs arm-none-eabi-gcc on the PATH
  pcsmod.py place <in.pcsmod> --flash 0x100A1000 --ram 0x20034000 -o placed.bin   (what the app does)
  pcsmod.py info <in.pcsmod>
  pcsmod.py sim <in.pcsmod> [--board polybar] [--ms 3000] [--press green@500-900]   run it on a simulated controller

.pcsmod layout (little endian): 64-byte header, image (as if linked at flash 0 / RAM 0), relocation words.
Relocation word: bit 31 = RAM address (else flash), bits 0..30 = byte offset into the image.
Header flags (written by build after a trial load in the simulator, so the app never has to run the mod):
  bits 0-1 USB mode (0 none, 1 add, 2 replace, 3 conditional), bits 8-19 hooks present in MOD_HOOKS order (init = bit 8).
"""
from __future__ import annotations

import argparse
import shutil
import struct
import subprocess
import sys
import tempfile
import zlib
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
PLATFORM = HERE.parent
MAGIC = 0x4D534350            # "PCSM"
FORMAT = 1
HDR = struct.Struct("<IHHHHIIIIIIIII16x")   # 64 bytes
assert HDR.size == 64

# Two links that differ in both bases by amounts no real constant would differ by.
LINK_A = (0x10000000, 0x20000000)
LINK_B = (0x10A31000, 0x20012A08)
LINK_C = (0x10140000, 0x20034000)   # third link: only used to prove place() == a real link
CFLAGS = ["-mcpu=cortex-m0plus", "-mthumb", "-Os", "-std=c11", "-ffreestanding", "-fno-common", "-fno-pic",
          "-ffunction-sections", "-fdata-sections", "-fno-builtin", "-fno-tree-loop-distribute-patterns", "-Wall", "-Wextra", "-Werror"]
RUNTIME = PLATFORM / "cmake" / "pcs_mod_rt.c"   # memset / memcpy / memmove the compiler may call


def sh(cmd):
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode:
        raise SystemExit(f"{' '.join(cmd[:3])}...: {p.stderr.strip()}")
    return p.stdout


def symbols(elf: Path) -> dict[str, int]:
    out = {}
    for line in sh(["arm-none-eabi-nm", str(elf)]).splitlines():
        parts = line.split()
        if len(parts) == 3:
            out[parts[2]] = int(parts[0], 16)
    return out


def link(objs, tmp: Path, bases, tag) -> tuple[bytes, dict]:
    elf = tmp / f"mod_{tag}.elf"
    sh(["arm-none-eabi-gcc", *CFLAGS, "-nostdlib", "-nostartfiles", f"-Wl,-T,{PLATFORM / 'cmake' / 'pcs_mod.ld'}",
        f"-Wl,--defsym=PCS_FLASH={bases[0]:#x}", f"-Wl,--defsym=PCS_RAM={bases[1]:#x}", "-Wl,--gc-sections",
        "-Wl,--no-warn-rwx-segments", "-o", str(elf), *map(str, objs), "-lgcc"])
    s = symbols(elf)
    binf = tmp / f"mod_{tag}.bin"
    sh(["arm-none-eabi-objcopy", "-O", "binary", "-j", ".text", "-j", ".data", str(elf), str(binf)])
    img = binf.read_bytes()
    need = s["__pcs_image_end"] - s["__pcs_image_start"]
    if len(img) != need:
        raise SystemExit(f"image is {len(img)} bytes, expected {need}: unexpected section in the mod")
    return img, s


def api_version(includes) -> tuple[int, int]:
    """The API major/minor the mod is built against, read from pcs_mod.h on the include path."""
    import re
    for d in [*includes, str(PLATFORM / "include")]:
        h = Path(d) / "pcs_mod.h"
        if h.is_file():
            txt = h.read_text()
            mj = re.search(r"#define\s+PCS_MOD_API_MAJOR\s+(\d+)", txt)
            mn = re.search(r"#define\s+PCS_MOD_API_MINOR\s+(\d+)", txt)
            if mj and mn:
                return int(mj.group(1)), int(mn.group(1))
    return 0, 1


def build(sources, includes, out: Path, extra_cflags=()) -> bytes:
    if not shutil.which("arm-none-eabi-gcc"):
        raise SystemExit("arm-none-eabi-gcc not found (Arm GNU Toolchain)")
    out.parent.mkdir(parents=True, exist_ok=True)
    # Keep toolchain scratch files in the project/output tree. Some Windows app sandboxes expose the system temp
    # directory to Python but deny child compiler processes write access to it.
    with tempfile.TemporaryDirectory(prefix=".pcsmod-", dir=out.parent) as t:
        tmp = Path(t)
        objs = []
        for src in [*sources, RUNTIME]:
            o = tmp / (Path(src).stem + ".o")
            sh(["arm-none-eabi-gcc", *CFLAGS, *extra_cflags, *[f"-I{i}" for i in includes], "-c", str(src), "-o", str(o)])
            objs.append(o)
        a, sa = link(objs, tmp, LINK_A, "a")
        b, sb = link(objs, tmp, LINK_B, "b")
        c, sc = link(objs, tmp, LINK_C, "c")
    if len(a) != len(b):
        raise SystemExit("the two links differ in size: the mod can't be relocated")
    df, dr = LINK_B[0] - LINK_A[0], LINK_B[1] - LINK_A[1]
    canon = bytearray(a)
    relocs = []
    for off in range(0, len(a) - 3):
        wa, wb = struct.unpack_from("<I", a, off)[0], struct.unpack_from("<I", b, off)[0]
        if wa == wb:
            continue
        if off % 4:
            continue          # the bytes of a moved aligned word; checked below
        d = (wb - wa) & 0xFFFFFFFF
        if d == df:
            struct.pack_into("<I", canon, off, (wa - LINK_A[0]) & 0xFFFFFFFF)
            relocs.append(off)
        elif d == dr:
            struct.pack_into("<I", canon, off, (wa - LINK_A[1]) & 0xFFFFFFFF)
            relocs.append(off | 0x80000000)
        else:
            raise SystemExit(f"word at +{off:#x} moves by {d:#x}: not a plain address (computed pointer?)")
    moved = {o & 0x7FFFFFFF for o in relocs}
    for i in range(len(a)):
        if a[i] != b[i] and (i & ~3) not in moved:
            raise SystemExit(f"byte at +{i:#x} differs outside an aligned address word")
    s = sa
    rel = lambda k: s[k] - s["__pcs_image_start"]
    image_size = len(a)
    data_size = s["__pcs_data_end"] - s["__pcs_data_start"]
    bss_size = s["__pcs_bss_end"] - s["__pcs_bss_start"]
    entry = rel("pcs_mod_main") | 1
    api_major, api_minor = api_version(includes)
    hdr = HDR.pack(MAGIC, FORMAT, HDR.size, api_major, api_minor, image_size, data_size, bss_size, entry, rel("__pcs_data_load"),
                   len(relocs), zlib.crc32(bytes(canon)), 0, 0)
    blob = hdr + bytes(canon) + b"".join(struct.pack("<I", r) for r in relocs)
    # proof: placing at a third base gives exactly what the linker gives there
    placed = place(blob, *LINK_C)[0]
    if placed != c:
        raise SystemExit("self-check failed: place() differs from a real link")
    blob = with_flags(blob)
    out.write_bytes(blob)
    return blob


USB_MODES = ("none", "add", "replace", "conditional")


def with_flags(blob: bytes) -> bytes:
    """Trial-load the mod in the simulator (pcs_mod_main only) and record its USB mode and hooks in the header."""
    import modsim
    try:
        sim = modsim.ModSim(blob)
    except modsim.ModError as e:
        raise SystemExit(f"the mod doesn't load: {e}")
    if sim.usb_mode > 3:
        raise SystemExit(f"usb_mode {sim.usb_mode} is not PCS_USB_NONE / ADD / REPLACE / CONDITIONAL")
    flags = sim.usb_mode | sum(1 << (8 + i) for i, h in enumerate(modsim.MOD_HOOKS) if h in sim.hooks)
    out = bytearray(blob)
    struct.pack_into("<I", out, 40, flags)   # header field "flags"
    return bytes(out)


def describe(hd: dict) -> tuple[str, list[str]]:
    """(usb mode, hooks) from a parsed header's flags."""
    import modsim
    f = hd["flags"]
    return USB_MODES[f & 3], [h for i, h in enumerate(modsim.MOD_HOOKS) if f >> (8 + i) & 1]


def parse(blob: bytes):
    h = HDR.unpack_from(blob)
    if h[0] != MAGIC or h[1] != FORMAT:
        raise ValueError("not a .pcsmod")
    keys = ["magic", "format", "header_size", "api_major", "api_minor", "image_size", "data_size", "bss_size",
            "entry", "data_load", "reloc_count", "crc", "flags", "stack"]
    hd = dict(zip(keys, h))
    img = blob[64:64 + hd["image_size"]]
    if zlib.crc32(img) != hd["crc"]:
        raise ValueError("image CRC mismatch")
    rel = struct.unpack_from(f"<{hd['reloc_count']}I", blob, 64 + hd["image_size"])
    return hd, img, rel


def place(blob: bytes, flash: int, ram: int) -> tuple[bytes, dict]:
    """What the app does: add the slot's bases to every address word. flash 4 KB aligned, ram 8 aligned."""
    if flash % 4096 or ram % 8:
        raise ValueError("flash must be 4 KB aligned and RAM 8-byte aligned")
    hd, img, rel = parse(blob)
    out = bytearray(img)
    for r in rel:
        off = r & 0x7FFFFFFF
        if off + 4 > len(out):
            raise ValueError("relocation outside the image")
        base = ram if r & 0x80000000 else flash
        struct.pack_into("<I", out, off, (struct.unpack_from("<I", out, off)[0] + base) & 0xFFFFFFFF)
    return bytes(out), hd


def main(argv=None):
    ap = argparse.ArgumentParser(prog="pcsmod")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("build"); p.add_argument("sources", nargs="+"); p.add_argument("-I", action="append", default=[])
    p.add_argument("-o", "--out", required=True)
    p.add_argument("--cflag", action="append", default=[], help="extra compiler flag for this project (repeatable)")
    p = sub.add_parser("place"); p.add_argument("mod"); p.add_argument("--flash", type=lambda x: int(x, 0), required=True)
    p.add_argument("--ram", type=lambda x: int(x, 0), required=True); p.add_argument("-o", "--out", required=True)
    p = sub.add_parser("info"); p.add_argument("mod")
    p = sub.add_parser("sim", help="run the mod's hooks on a simulated controller")
    p.add_argument("mod"); p.add_argument("--board", default="polybar")
    p.add_argument("--ms", type=int, default=3000, help="how long to run (simulated)")
    p.add_argument("--frame", type=int, default=10, help="ms between frames")
    p.add_argument("--press", action="append", default=[], help="hold a button: green@500-900 (ms)")
    p.add_argument("--settings", default="", help="the mod's saved settings as hex bytes, e.g. 0a2801")
    p.add_argument("--schema", help="mod settings file (<name>-settings.json): start from its defaults")
    p.add_argument("--pins-used", type=lambda x: int(x, 0), default=0, help="GPIO mask the controller keeps")
    p.add_argument("--i2c", action="append", default=[], metavar="ADDR=HEX",
                   help="an I2C device that answers reads with these bytes, e.g. 0x48=0102 (anything else doesn't answer)")
    p.add_argument("--every", type=int, default=500, help="print the lights every N ms (0 = never)")
    p.add_argument("--host", type=lambda x: int(x, 0), metavar="FLAGS", help="conditional USB: a computer with these PCS_HOST_* flags appears (e.g. 0x17), so the mod can switch personality")
    p.add_argument("--node", action="append", default=[], metavar="MOD[@BOARD]",
                   help="add a linked board running MOD (- = no mod, old = a neck on older firmware); this mod is the hub")
    p.add_argument("--air", action="append", default=[], metavar="MOD[@BOARD]",
                   help="over the Polylink: with --board polylink, a controller connected to it running MOD (- = no mod); "
                        "otherwise MOD runs on the Polylink this controller is connected to")
    a = ap.parse_args(argv)
    if a.cmd == "build":
        blob = build(a.sources, a.I or [str(PLATFORM / "include")], Path(a.out), a.cflag)
        hd, _, rel = parse(blob)
        usb, hooks = describe(hd)
        print(f"hooks: {', '.join(hooks) or 'none'}; USB: {usb}")
        print(f"wrote {a.out}: {hd['image_size']} B flash, {hd['data_size'] + hd['bss_size']} B RAM, {len(rel)} relocations (self-check ok)")
    elif a.cmd == "place":
        img, hd = place(Path(a.mod).read_bytes(), a.flash, a.ram)
        Path(a.out).write_bytes(img)
        print(f"placed at flash {a.flash:#x}, RAM {a.ram:#x}; entry {a.flash + hd['entry']:#x}")
    elif a.cmd == "sim" and a.air:
        import modsim
        try:
            me = modsim.ModSim(Path(a.mod).read_bytes(), a.board)
            if a.board == "polylink":
                pl, ctl = me, []
                for spec in a.air[:4]:
                    f, _, board = spec.partition("@")
                    ctl.append(modsim.ModSim(Path(f).read_bytes(), board or "polybar") if f != "-" else None)
            else:
                f = a.air[0].partition("@")[0]
                pl, ctl = (modsim.ModSim(Path(f).read_bytes(), "polylink") if f != "-" else None), [me]
            net = modsim.AirNet(pl, ctl)
            net.run(a.ms, a.frame)
        except modsim.ModError as e:
            print(f"mod stopped: {e}")
            return 1
        print(*net.log, sep="\n")
        for name, sim in [("polylink", pl)] + [(f"slot {k + 1}", s) for k, s in enumerate(ctl)]:
            if not sim:
                continue
            for line in sim.logs:
                print(f"{name} log: {line}")
            for w in sim.warnings:
                print(f"{name} warning: {w}")
    elif a.cmd == "sim" and a.node:
        import modsim
        try:
            hub = modsim.ModSim(Path(a.mod).read_bytes(), a.board)
            nodes, v0 = [], []
            for k, spec in enumerate(a.node):
                f, _, board = spec.partition("@")
                if f == "old":
                    v0.append(k)
                sim = modsim.ModSim(Path(f).read_bytes(), board or a.board) if f not in ("-", "old") else None
                nodes.append((sim, bytes([k + 1] * 8)))
            net = modsim.LinkNet(hub, nodes, tuple(v0))
            net.run(a.ms, a.frame)
        except modsim.ModError as e:
            print(f"mod stopped: {e}")
            return 1
        print(*net.log, sep="\n")
        for name, sim in [("hub", hub)] + [(f"node board {k + 1}", s) for k, (s, _) in enumerate(nodes) if s]:
            for line in sim.logs:
                print(f"{name} log: {line}")
            for w in sim.warnings:
                print(f"{name} warning: {w}")
    elif a.cmd == "sim":
        import modsim
        try:
            settings = bytes.fromhex(a.settings)
            if a.schema and not settings:
                import json
                import pcspack
                settings = pcspack.settings_defaults(json.loads(Path(a.schema).read_text()))
            i2c = {}
            for d in a.i2c:
                addr, _, data = d.partition("=")
                i2c[int(addr, 0)] = bytes.fromhex(data)
            sim, lines = modsim.run(Path(a.mod).read_bytes(), a.board, a.ms, a.frame, a.press,
                                    settings, a.pins_used, a.every, i2c, a.host)
        except modsim.ModError as e:
            print(f"mod stopped: {e}")
            return 1
        for line in sim.logs:
            print(f"log: {line}")
        print(*lines, sep="\n")
        print(*sim.report(), sep="\n")
    else:
        hd, _, rel = parse(Path(a.mod).read_bytes())
        for k, v in hd.items():
            print(f"{k:12} {v:#x}" if k in ("magic", "entry", "crc", "flags") else f"{k:12} {v}")
        usb, hooks = describe(hd)
        print(f"{'usb':12} {usb}\n{'hooks':12} {', '.join(hooks) or 'none'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
