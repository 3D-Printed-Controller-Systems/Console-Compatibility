"""usbhost: the USB side of the mod simulator. MIT License.

- config_descriptor(): the configuration descriptor the firmware builds from the interfaces mods add (the reference
  for the firmware's own code: same order, same numbering).
- check_config(): walks a configuration descriptor the way a computer does and reports what a host would reject.
- HidReport: parses a HID report descriptor and decodes reports into readable names.
- midi_events(): decodes USB-MIDI event packets.

Written for the 3DPCS mod developer platform from the USB 2.0, HID 1.11 and USB MIDI 1.0 specs.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass, field

# Interfaces the controller itself has (docs/05_usb_mods.md): its game pad in Add mode, the settings interface always.
GAMEPAD_REPORT = bytes([0x05, 0x01, 0x09, 0x05, 0xA1, 0x01, 0x05, 0x09, 0x19, 0x01, 0x29, 0x10, 0x15, 0x00, 0x25,
                        0x01, 0x75, 0x01, 0x95, 0x10, 0x81, 0x02, 0xC0])      # stand-in: 16 buttons
SETTINGS_IFACE = (0xFF, 0x50, 0x01)
MAX_MOD_IFACES = 8
MAX_EP_NUM = 15


@dataclass
class Ep:
    addr: int
    type: int
    size: int
    interval: int
    extra: bytes = b""


@dataclass
class Iface:
    num: int
    cls: int
    subclass: int
    protocol: int
    eps: list[Ep]
    class_desc: bytes = b""
    group: int = 0
    name: str = ""
    hid_report: bytes = b""
    owner: str = "mod"            # "mod", "gamepad", "settings"


def iface_desc(i: Iface, string_index: int) -> bytes:
    out = b""
    if i.group:
        out += bytes([8, 0x0B, i.num, i.group, i.cls, i.subclass, i.protocol, 0])
    out += bytes([9, 0x04, i.num, 0, len(i.eps), i.cls, i.subclass, i.protocol, string_index])
    if i.cls == 0x03 and i.hid_report:
        out += bytes([9, 0x21, 0x11, 0x01, 0x00, 0x01, 0x22]) + struct.pack("<H", len(i.hid_report))
    out += i.class_desc
    for e in i.eps:
        out += bytes([7, 0x05, e.addr, e.type]) + struct.pack("<H", e.size) + bytes([e.interval]) + e.extra
    return out


def config_descriptor(ifaces: list[Iface]) -> tuple[bytes, list[str]]:
    """Configuration descriptor + the interface names. Mod interface names use string indexes 0x20 + n (as the
    firmware, core 0.11); the controller's own interfaces use their own (0x10 + n here, a stand-in)."""
    strings, body = [], b""
    for i in ifaces:
        idx = 0
        if i.name:
            strings.append(i.name)
            idx = (0x20 if i.owner == "mod" else 0x10) + len([j for j in ifaces[:ifaces.index(i)] if j.name and j.owner == i.owner])
        body += iface_desc(i, idx)
    head = bytes([9, 0x02]) + struct.pack("<H", 9 + len(body)) + bytes([len(ifaces), 1, 0, 0x80, 250])
    return head + body, strings


class UsbError(Exception):
    pass


def split(desc: bytes, where: str) -> list[bytes]:
    """Descriptors one by one; every bLength must fit exactly."""
    out, i = [], 0
    while i < len(desc):
        n = desc[i]
        if n < 2 or i + n > len(desc):
            raise UsbError(f"{where}: descriptor at byte {i} has length {n}, which doesn't fit")
        out.append(desc[i:i + n])
        i += n
    return out


def check_config(cfg: bytes, ifaces: list[Iface]) -> list[str]:
    """What a computer checks while setting the device up. Returns warnings; raises UsbError for hard failures."""
    warn = []
    if struct.unpack_from("<H", cfg, 2)[0] != len(cfg):
        raise UsbError("configuration total length is wrong")
    descs = split(cfg, "configuration")
    n_if, eps, cur = 0, set(), None
    for d in descs[1:]:
        t = d[1]
        if t == 0x04:
            if len(d) != 9:
                raise UsbError("interface descriptor must be 9 bytes")
            n_if += 1
            cur = d
        elif t == 0x05:
            if len(d) != 7:
                raise UsbError("endpoint descriptor must be 7 bytes")
            if d[2] in eps:
                raise UsbError(f"endpoint {d[2]:#04x} is used twice")
            eps.add(d[2])
            size = struct.unpack_from("<H", d, 4)[0]
            if d[3] & 3 == 3 and not 1 <= d[6] <= 255:
                raise UsbError(f"interrupt endpoint {d[2]:#04x} needs an interval of 1 to 255 ms")
            if size > 64 or size < 8:
                raise UsbError(f"endpoint {d[2]:#04x}: max packet {size} (full speed: 8 to 64)")
        elif t == 0x0B:
            if len(d) != 8:
                raise UsbError("interface association descriptor must be 8 bytes")
        elif t in (0x21, 0x24, 0x25):
            if cur is None:
                raise UsbError(f"class descriptor {t:#04x} before any interface")
        else:
            warn.append(f"unusual descriptor type {t:#04x} in the configuration")
    if n_if != cfg[4]:
        raise UsbError(f"configuration says {cfg[4]} interfaces but has {n_if}")
    if len(eps) > 30:
        raise UsbError("more endpoints than the RP2040 has")
    for i in ifaces:
        if i.cls == 0x03:
            if not i.hid_report:
                raise UsbError(f"interface {i.num} is HID but has no report descriptor")
            HidReport(i.hid_report)               # raises on a bad report descriptor
            if not any(e.addr & 0x80 and e.type == 3 for e in i.eps):
                raise UsbError(f"HID interface {i.num} needs an interrupt IN endpoint")
        if i.class_desc:
            split(i.class_desc, f"interface {i.num} class descriptors")
        for e in i.eps:
            if e.extra:
                split(e.extra, f"endpoint {e.addr:#04x} extra descriptors")
        if (i.cls, i.subclass) == (0x01, 0x01):            # MIDI audio control: names its streaming interface
            hdr = i.class_desc
            if len(hdr) < 9 or hdr[1] != 0x24 or hdr[2] != 0x01:
                raise UsbError(f"audio control interface {i.num} needs its class header")
            for k in range(hdr[7]):
                ms = hdr[8 + k]
                if not any(j.num == ms and (j.cls, j.subclass) == (0x01, 0x03) for j in ifaces):
                    raise UsbError(f"audio control interface {i.num} names interface {ms}, which isn't MIDI streaming "
                                   "(fill in the number usb_add gave you)")
        if (i.cls, i.subclass) == (0x01, 0x03):
            hdr = i.class_desc
            if len(hdr) < 7 or hdr[1] != 0x24 or hdr[2] != 0x01:
                raise UsbError(f"MIDI streaming interface {i.num} needs its class header")
            total = len(hdr) + sum(7 + len(e.extra) for e in i.eps)
            if struct.unpack_from("<H", hdr, 5)[0] != total:
                warn.append(f"MIDI streaming interface {i.num}: header total {struct.unpack_from('<H', hdr, 5)[0]}, "
                            f"descriptors add up to {total}")
            for e in i.eps:
                if not e.extra or e.extra[1] != 0x25:
                    raise UsbError(f"MIDI endpoint {e.addr:#04x} needs its jack descriptor (extra)")
    return warn


# ---------------------------------------------------------------- HID report descriptors

KEY_NAMES = {**{0x04 + i: chr(ord("A") + i) for i in range(26)}, **{0x1E + i: str((i + 1) % 10) for i in range(10)},
             0x28: "Enter", 0x29: "Esc", 0x2A: "Backspace", 0x2B: "Tab", 0x2C: "Space", 0x4F: "Right", 0x50: "Left",
             0x51: "Down", 0x52: "Up", **{0x3A + i: f"F{i + 1}" for i in range(12)},
             0xE0: "LCtrl", 0xE1: "LShift", 0xE2: "LAlt", 0xE3: "LGui", 0xE4: "RCtrl", 0xE5: "RShift", 0xE6: "RAlt",
             0xE7: "RGui"}
DESKTOP_NAMES = {0x30: "X", 0x31: "Y", 0x32: "Z", 0x33: "Rx", 0x34: "Ry", 0x35: "Rz", 0x36: "Slider", 0x39: "Hat"}


def usage_name(page: int, u: int) -> str:
    if page == 0x07:
        return KEY_NAMES.get(u, f"key {u:#04x}")
    if page == 0x09:
        return f"Button {u}"
    if page == 0x01:
        return DESKTOP_NAMES.get(u, f"desktop {u:#04x}")
    return f"{page:#04x}:{u:#04x}"


@dataclass
class Field:
    kind: str                 # input / output / feature
    flags: int
    page: int
    usages: list[int]
    umin: int | None
    umax: int | None
    lmin: int
    lmax: int
    size: int
    count: int
    offset: int               # bit offset in the report (after the report id byte)


@dataclass
class HidReport:
    desc: bytes
    fields: dict[int, list[Field]] = field(default_factory=dict)      # report id (0 = none) -> fields
    bits: dict[tuple[str, int], int] = field(default_factory=dict)

    def __post_init__(self):
        g = {"page": 0, "lmin": 0, "lmax": 0, "size": 0, "count": 0, "id": 0}
        stack, local, depth, i, d = [], {"usages": [], "umin": None, "umax": None}, 0, 0, self.desc
        if not d:
            raise UsbError("empty HID report descriptor")
        while i < len(d):
            b = d[i]
            if b == 0xFE:
                raise UsbError("long HID items aren't supported")
            n = (0, 1, 2, 4)[b & 3]
            if i + 1 + n > len(d):
                raise UsbError(f"HID report descriptor ends inside an item at byte {i}")
            raw = d[i + 1:i + 1 + n]
            u = int.from_bytes(raw, "little") if n else 0
            s = int.from_bytes(raw, "little", signed=True) if n else 0
            typ, tag = (b >> 2) & 3, b >> 4
            i += 1 + n
            if typ == 0:                                   # main
                if tag in (8, 9, 11):
                    kind = {8: "input", 9: "output", 11: "feature"}[tag]
                    if g["size"] == 0 or g["count"] == 0:
                        raise UsbError(f"HID {kind} item without report size / count")
                    if g["size"] * g["count"] > 8 * 64:
                        raise UsbError("HID report field larger than 64 bytes")
                    key = (kind, g["id"])
                    off = self.bits.get(key, 0)
                    self.fields.setdefault(g["id"], []).append(Field(kind, u, g["page"], list(local["usages"]),
                                                                     local["umin"], local["umax"], g["lmin"], g["lmax"],
                                                                     g["size"], g["count"], off))
                    self.bits[key] = off + g["size"] * g["count"]
                elif tag == 10:
                    depth += 1
                elif tag == 12:
                    depth -= 1
                    if depth < 0:
                        raise UsbError("HID END_COLLECTION without a COLLECTION")
                else:
                    raise UsbError(f"unknown HID main item {b:#04x}")
                local = {"usages": [], "umin": None, "umax": None}
            elif typ == 1:                                 # global
                if tag == 0: g["page"] = u
                elif tag == 1: g["lmin"] = s
                elif tag == 2: g["lmax"] = s if s >= g["lmin"] else u
                elif tag == 7: g["size"] = u
                elif tag == 8:
                    if not 1 <= u <= 255:
                        raise UsbError("HID report id must be 1 to 255")
                    g["id"] = u
                elif tag == 9: g["count"] = u
                elif tag == 10: stack.append(dict(g))
                elif tag == 11:
                    if not stack:
                        raise UsbError("HID POP without PUSH")
                    g = stack.pop()
                elif tag in (3, 4, 5, 6): pass             # physical min / max, unit exponent, unit
                else: raise UsbError(f"unknown HID global item {b:#04x}")
            elif typ == 2:                                 # local
                if tag == 0: local["usages"].append(u)
                elif tag == 1: local["umin"] = u
                elif tag == 2: local["umax"] = u
            else:
                raise UsbError(f"reserved HID item {b:#04x}")
        if depth:
            raise UsbError("HID report descriptor has an unclosed COLLECTION")
        if not any(f.kind == "input" for fs in self.fields.values() for f in fs) and not self.fields:
            raise UsbError("HID report descriptor has no reports")
        if 0 in self.fields and len(self.fields) > 1:
            raise UsbError("HID report descriptor mixes reports with and without ids")

    def input_len(self, rid: int = 0) -> int:
        return (self.bits.get(("input", rid), 0) + 7) // 8 + (1 if rid else 0)

    def decode(self, report: bytes) -> str:
        rid = report[0] if 0 not in self.fields else 0
        data = report[1:] if rid else report
        fs = [f for f in self.fields.get(rid, []) if f.kind == "input"]
        if not fs:
            return f"unknown report id {rid}"
        val = int.from_bytes(data, "little")
        parts = []
        for f in fs:
            if f.flags & 1:                                # constant
                continue
            vals = [(val >> (f.offset + k * f.size)) & ((1 << f.size) - 1) for k in range(f.count)]
            if f.flags & 2:                                # variable
                for k, v in enumerate(vals):
                    u = f.usages[k] if k < len(f.usages) else ((f.umin or 0) + k if f.umin is not None else None)
                    if u is None:
                        continue
                    if f.size == 1:
                        if v:
                            parts.append(usage_name(f.page, u))
                    else:
                        if f.lmin < 0 and v >> (f.size - 1):
                            v -= 1 << f.size
                        parts.append(f"{usage_name(f.page, u)}={v}")
            else:                                          # array: values are usages
                parts += [usage_name(f.page, v) for v in vals if v]
        return (f"[{rid}] " if rid else "") + (" ".join(parts) or "(nothing)")


# ---------------------------------------------------------------- MIDI

NOTE = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]


def note_name(n: int) -> str:
    return f"{NOTE[n % 12]}{n // 12 - 1}"


def midi_events(data: bytes) -> list[str]:
    out = []
    for k in range(0, len(data) - 3, 4):
        cin, st, a, b = data[k] & 15, data[k + 1], data[k + 2], data[k + 3]
        ch = (st & 15) + 1
        if cin == 0x9 and b:
            out.append(f"ch{ch} note on {note_name(a)} vel {b}")
        elif cin in (0x8, 0x9):
            out.append(f"ch{ch} note off {note_name(a)}")
        elif cin == 0xB:
            out.append(f"ch{ch} CC {a} = {b}")
        elif cin == 0xE:
            out.append(f"ch{ch} bend {a | b << 7}")
        elif cin == 0xC:
            out.append(f"ch{ch} program {a}")
        else:
            out.append(f"MIDI {data[k:k + 4].hex()}")
    return out
