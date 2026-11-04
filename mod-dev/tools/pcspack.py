#!/usr/bin/env python3
"""pcspack: make and check 3DPCS packs. Python 3.9+, standard library only. MIT licensed (see LICENSE).

  pcspack.py new colors <folder> --id you.my-pack --name "My Pack" --author You
  pcspack.py check <folder or .3dpack>          checks everything the app checks, plus a few hints
  pcspack.py build <folder> [-o dist/]          checks, then writes "<Name> <version>.3dpack" (any name works)
  pcspack.py bundle <set.json>... --id ... --name ... --author ... [-o dist/]
                                                sets exported from the app -> one pack (what "Export as pack" does)
  pcspack.py inspect <.3dpack>                  what's inside
  pcspack.py hash <.3dpack>                     a pack's SHA-256 line, as on the official list

Every problem has a stable code (e.g. path-unsafe) so apps and tests can match on it. The app enforces the same rules,
in the same order, and tests/vectors/ holds a pack for each code.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import re
import struct
import sys
import zipfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

sys.path.insert(0, str(Path(__file__).resolve().parent))      # pcsmod / modsim for mod packs

FORMAT = "3dpcs-pack"
FORMAT_VERSION = 1
SET_FORMAT = "3dpcs-color-set"
EXT = ".3dpack"

# ---- pack limits (mod-dev/README.md) ----
MAX_PACK_BYTES = 2 * 1024 * 1024        # the .3dpack file itself
MAX_UNPACKED_BYTES = 4 * 1024 * 1024    # all files once unpacked
MAX_FILE_BYTES = 256 * 1024             # any one file
MAX_ENTRIES = 200
MAX_RATIO = 100                         # unpacked / packed per file (zip bombs)
MAX_ICON_BYTES = 64 * 1024
ICON_MIN, ICON_MAX = 64, 256
MAX_SETS = 5                            # color sets per pack: one presets tab, a controller holds 5 color sets
ALLOWED_EXT = {".json", ".png", ".md", ".txt", ".pcsmod"}

LIGHTS = ["Green", "Red", "Yellow", "Blue", "Orange", "Strum up", "Strum down", "Start", "Select"]
EFFECTS = {0: "None", 1: "Wave", 2: "Shift", 3: "Breathe", 4: "Sparkle"}
KNOWN_BOARDS = {"polybar", "monoboard", "hammeron", "riffstick", "polylink"}
MANIFEST_KEYS = {"$schema", "format", "format_version", "id", "name", "author", "version", "description", "icon",
                 "boards", "firmware", "min_app", "license", "homepage", "contents"}
CONTENT_KINDS = {"color_sets", "mods"}

# ---- native mods (mod-dev/README.md and pcs_mod.h) ----
MOD_API = (0, 5)
MOD_AREA = 640 * 1024 - 4096            # flash for mod images (after the 4 KB table)
MOD_RAM = 44 * 1024
SETTINGS_MAX = 256
MAX_MODS = 8
MOD_KEYS = {"file", "name", "description", "settings", "hardware", "usb", "usb_name", "usb_vid", "usb_pid", "role"}
RE_HEX16 = re.compile(r"^0x[0-9A-Fa-f]{4}$")
HW_KEYS = {"pins", "pio_sms", "dma", "alarms", "buses", "adc"}
BUSES = {"i2c0", "i2c1", "spi0", "spi1", "uart0", "uart1"}
USB_MODES = ("none", "add", "replace", "conditional")
FIELD_TYPES = {"range", "toggle", "select", "number", "color", "text", "hidden"}
FIELD_KEYS = {"id", "label", "type", "offset", "size", "default", "min", "max", "step", "unit", "options",
              "visible_if", "help", "signed"}
SECTIONS = {"main", "advanced"}
RE_MOD_PATH = re.compile(r"^[A-Za-z0-9_-]+(/[A-Za-z0-9_-]+)*\.pcsmod$")
RE_FIELD_ID = re.compile(r"^[a-z0-9_]{1,32}$")
RESERVED_IDS = ("3dpcs.",)
RESERVED_WORDS = {"3dpcs", "polybar", "polylink", "polypedal", "monoboard", "hammeron", "riffstick"}

RE_ID = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*\.[a-z0-9]+(-[a-z0-9]+)*$")
RE_VER = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")
_V = r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
RE_RANGE = re.compile(rf"^(>=|<=|<|>|=|\^|~)?{_V}( (>=|<=|<|>){_V})?$")
RE_JSON_PATH = re.compile(r"^[A-Za-z0-9_-]+(/[A-Za-z0-9_-]+)*\.json$")
RE_PNG_PATH = re.compile(r"^[A-Za-z0-9_-]+(/[A-Za-z0-9_-]+)*\.png$")
RE_BOARD = re.compile(r"^(\*|[a-z0-9_]{1,23})$")
RE_COLOR = re.compile(r"^#[0-9a-fA-F]{6}$")


@dataclass
class Report:
    errors: list[tuple[str, str]] = field(default_factory=list)     # (code, message)
    hints: list[str] = field(default_factory=list)

    def err(self, code: str, msg: str) -> None:
        self.errors.append((code, msg))

    @property
    def ok(self) -> bool:
        return not self.errors


# ---------------------------------------------------------------- manifest

def check_manifest(m: object, r: Report) -> None:
    if not isinstance(m, dict):
        r.err("manifest-invalid", "manifest.json must be a JSON object")
        return
    if m.get("format") != FORMAT:
        r.err("manifest-format", f'manifest "format" must be "{FORMAT}"')
        return
    if m.get("format_version") != FORMAT_VERSION:
        r.err("manifest-format-version", f"pack format_version {m.get('format_version')!r} is not one this tool knows ({FORMAT_VERSION})")
        return
    for k in ("id", "name", "author", "version", "contents"):
        if k not in m:
            r.err("manifest-missing-field", f'manifest needs "{k}"')
    for k in m:
        if k not in MANIFEST_KEYS:
            r.err("manifest-unknown-field", f'manifest has an unknown field "{k}"')
    s = lambda k: isinstance(m.get(k), str)
    if "id" in m and not (s("id") and RE_ID.match(m["id"]) and len(m["id"]) <= 64):
        r.err("manifest-id", 'id must look like author.pack-name: lowercase letters, digits and dashes, one dot, 64 characters max')
    for k, lim in (("name", 32), ("author", 32)):
        if k in m and not (s(k) and 1 <= len(m[k].strip()) and len(m[k]) <= lim):
            r.err(f"manifest-{k}", f"{k} must be 1 to {lim} characters")
    if "version" in m and not (s("version") and RE_VER.match(m["version"])):
        r.err("manifest-version", "version must be MAJOR.MINOR.PATCH, e.g. 1.0.0")
    if "description" in m:
        if not (s("description") and len(m["description"]) <= 120):
            r.err("manifest-description", "description must be text, 120 characters max")
        elif m["description"].rstrip().endswith("."):
            r.hints.append("description: leave off the period at the end (app style)")
    if "icon" in m and not (s("icon") and RE_PNG_PATH.match(m["icon"])):
        r.err("manifest-icon", "icon must be a .png path inside the pack")
    if "boards" in m:
        b = m["boards"]
        if not (isinstance(b, list) and b and all(isinstance(x, str) and RE_BOARD.match(x) for x in b) and len(set(b)) == len(b)):
            r.err("manifest-boards", 'boards must be a list of module ids, or ["*"]')
        else:
            for x in b:
                if x != "*" and x not in KNOWN_BOARDS:
                    r.hints.append(f'boards: "{x}" is not a 3DPCS module id (fine for your own kit)')
    if "firmware" in m and not (s("firmware") and RE_RANGE.match(m["firmware"])):
        r.err("manifest-firmware", 'firmware must be a version range like ">=0.9.80" or ">=0.9.80 <1.0.0"')
    if "min_app" in m and not (s("min_app") and RE_VER.match(m["min_app"])):
        r.err("manifest-min-app", "min_app must be MAJOR.MINOR.PATCH")
    if "license" in m and not (s("license") and 0 < len(m["license"]) <= 64):
        r.err("manifest-license", "license must be an SPDX id such as MIT")
    if "homepage" in m and not (s("homepage") and m["homepage"].startswith("https://") and len(m["homepage"]) <= 200):
        r.err("manifest-homepage", "homepage must be an https:// link")
    c = m.get("contents")
    if "contents" in m:
        if not (isinstance(c, dict) and c):
            r.err("manifest-contents", "contents must list at least one kind of content")
        else:
            for k in c:
                if k not in CONTENT_KINDS:
                    r.err("manifest-content-kind", f'"{k}" is not a content kind this tool knows (color_sets, mods)')
            cs = c.get("color_sets")
            if "color_sets" in c and not (isinstance(cs, list) and 1 <= len(cs) <= MAX_SETS and all(isinstance(p, str) and RE_JSON_PATH.match(p) and len(p) <= 128 for p in cs) and len(set(cs)) == len(cs)):
                r.err("manifest-color-sets", f"color_sets must list 1 to {MAX_SETS} different .json paths inside the pack")
            if "mods" in c:
                check_mod_entries(c["mods"], r)


def check_mod_entries(mods: object, r: Report) -> None:
    bad = lambda msg: r.err("manifest-mods", msg)
    if not (isinstance(mods, list) and 1 <= len(mods) <= MAX_MODS):
        bad(f"mods must list 1 to {MAX_MODS} mods")
        return
    files, names = set(), set()
    for i, e in enumerate(mods):
        w = f"mods[{i}]"
        if not isinstance(e, dict):
            bad(f"{w} must be an object")
            continue
        for k in e:
            if k not in MOD_KEYS:
                bad(f'{w} has an unknown field "{k}"')
        f = e.get("file")
        if not (isinstance(f, str) and RE_MOD_PATH.match(f) and len(f) <= 128) or f in files:
            bad(f"{w}: file must be a .pcsmod path inside the pack, each listed once")
        files.add(f)
        n = e.get("name")
        if not (isinstance(n, str) and n.strip() and len(n) <= 32) or str(n).lower() in names:
            bad(f"{w}: name must be 1 to 32 characters, different for each mod")
        names.add(str(n).lower())
        if "description" in e and not (isinstance(e["description"], str) and len(e["description"]) <= 120):
            bad(f"{w}: description must be text, 120 characters max")
        if "settings" in e and not (isinstance(e["settings"], str) and RE_JSON_PATH.match(e["settings"]) and len(e["settings"]) <= 128):
            bad(f"{w}: settings must be a .json path inside the pack")
        if e.get("usb", "none") not in USB_MODES:
            bad(f'{w}: usb must be "none", "add", "replace" or "conditional"')
        own_id = e.get("usb") in ("replace", "conditional")   # modes that are the whole device and may set its IDs/name
        if "usb_name" in e:
            u = e["usb_name"]
            if not own_id or not (isinstance(u, str) and u.isascii() and 1 <= len(u) <= 38):
                bad(f"{w}: usb_name is for \"replace\" / \"conditional\" mods, 1 to 38 ASCII characters")
        if ("usb_vid" in e) != ("usb_pid" in e) or any(k in e and (not own_id or not isinstance(e[k], str)
                                                                    or not RE_HEX16.match(e[k])) for k in ("usb_vid", "usb_pid")):
            bad(f'{w}: usb_vid and usb_pid go together, on \"replace\" / \"conditional\" mods, as "0x1234"')
        if e.get("role", "any") not in ("any", "hub", "node"):
            bad(f'{w}: role must be "any", "hub" or "node"')
        hw = e.get("hardware", {})
        if not isinstance(hw, dict) or set(hw) - HW_KEYS:
            bad(f"{w}: hardware may only have {', '.join(sorted(HW_KEYS))}")
            continue
        ints = lambda v, lo, hi: isinstance(v, list) and len(set(v)) == len(v) and all(type(x) is int and lo <= x <= hi for x in v)
        if "pins" in hw and not ints(hw["pins"], 0, 29):
            bad(f"{w}: hardware.pins must list different GPIO numbers 0 to 29")
        for k, hi in (("pio_sms", 8), ("dma", 8), ("alarms", 2)):
            if k in hw and not (type(hw[k]) is int and 0 <= hw[k] <= hi):
                bad(f"{w}: hardware.{k} must be 0 to {hi}")
        if "buses" in hw and not (isinstance(hw["buses"], list) and set(hw["buses"]) <= BUSES and len(set(hw["buses"])) == len(hw["buses"])):
            bad(f"{w}: hardware.buses must list some of {', '.join(sorted(BUSES))}")
        if "adc" in hw and not ints(hw["adc"], 0, 3):
            bad(f"{w}: hardware.adc must list ADC inputs 0 to 3")


def check_mod_settings(d: object, where: str, r: Report) -> int:
    """A mod's settings file: the same format product modules use (settings.schema.json), limited to one block of
    256 bytes and the field types a mod needs. Returns the block size it uses."""
    n0 = len(r.errors)
    bad = lambda msg: r.err("mod-settings", f"{where}: {msg}")
    if not isinstance(d, dict) or d.get("schema") != 1 or set(d) - {"$schema", "schema", "groups", "hidden"}:
        bad('must be {"schema": 1, "groups": [...], "hidden": [...]}')
        return 0
    fields = []
    groups = d.get("groups", [])
    if not isinstance(groups, list):
        bad("groups must be a list")
        return 0
    gids = set()
    for g in groups:
        if not (isinstance(g, dict) and set(g) <= {"id", "label", "section", "fields"} and isinstance(g.get("id"), str)
                and RE_FIELD_ID.match(g["id"]) and g["id"] not in gids and isinstance(g.get("label"), str)
                and 1 <= len(g["label"]) <= 40 and g.get("section", "main") in SECTIONS and isinstance(g.get("fields"), list)):
            bad('each group needs a unique "id", a "label" (40 max), "fields", and "section" main or advanced')
            return 0
        gids.add(g["id"])
        fields += [(f, False) for f in g["fields"]]
    hidden = d.get("hidden", [])
    if not isinstance(hidden, list):
        bad("hidden must be a list")
        return 0
    fields += [(f, True) for f in hidden]
    used = bytearray(SETTINGS_MAX)
    ids: dict[str, str] = {}
    end = 0
    for f, is_hidden in fields:
        if not isinstance(f, dict) or set(f) - FIELD_KEYS:
            bad(f"fields may only have {', '.join(sorted(FIELD_KEYS))}")
            continue
        fid, t = f.get("id"), f.get("type")
        w = f'field "{fid}"'
        if not (isinstance(fid, str) and RE_FIELD_ID.match(fid)) or fid in ids:
            bad(f"{w}: id must be unique, lowercase letters, digits and _")
            continue
        ids[fid] = t
        if t not in FIELD_TYPES:
            bad(f"{w}: type must be one of {', '.join(sorted(FIELD_TYPES))}")
            continue
        if not (isinstance(f.get("label"), str) and 1 <= len(f["label"]) <= 40):
            bad(f"{w}: label must be 1 to 40 characters")
        if "help" in f and not (isinstance(f["help"], str) and len(f["help"]) <= 200):
            bad(f"{w}: help must be text, 200 characters max")
        off, size = f.get("offset"), f.get("size")
        sizes = {"toggle": (1,), "select": (1, 2), "color": (3,), "text": tuple(range(2, 65))}.get(t, (1, 2, 4))
        if not (type(off) is int and off >= 0 and type(size) is int and size in sizes and off + size <= SETTINGS_MAX):
            bad(f"{w}: offset + size must fit in {SETTINGS_MAX} bytes (size for {t}: {sizes[0]}"
                f"{' to ' + str(sizes[-1]) if len(sizes) > 1 else ''})")
            continue
        if any(used[off:off + size]):
            bad(f"{w}: overlaps another field")
        used[off:off + size] = b"\x01" * size
        end = max(end, off + size)
        dv = f.get("default")
        if t in ("range", "number", "hidden", "toggle", "select"):
            signed = f.get("signed") is True
            lo, hi = (-(1 << (8 * size - 1)), (1 << (8 * size - 1)) - 1) if signed else (0, (1 << (8 * size)) - 1)
            if t == "range":
                if not (type(f.get("min")) is int and type(f.get("max")) is int and lo <= f["min"] < f["max"] <= hi):
                    bad(f"{w}: range needs whole-number min < max that fit {size} byte(s)")
                    continue
                lo, hi = f["min"], f["max"]
                if "step" in f and not (type(f["step"]) is int and 1 <= f["step"] <= hi - lo):
                    bad(f"{w}: step must be a whole number from 1 to max - min")
            if t == "toggle":
                lo, hi = 0, 1
            if t == "select":
                opts = f.get("options")
                if not (isinstance(opts, list) and 2 <= len(opts) <= 32 and all(
                        isinstance(o, dict) and set(o) == {"value", "label"} and type(o["value"]) is int and lo <= o["value"] <= hi
                        and isinstance(o["label"], str) and 1 <= len(o["label"]) <= 40 for o in opts)
                        and len({o["value"] for o in opts}) == len(opts)):
                    bad(f"{w}: select needs 2 to 32 options, each {{value, label}}, values different")
                    continue
                if type(dv) is not int or dv not in {o["value"] for o in opts}:
                    bad(f"{w}: default must be one of the option values")
                continue
            if type(dv) is not int or not lo <= dv <= hi:
                bad(f"{w}: default must be a whole number from {lo} to {hi}")
        elif t == "color":
            if not (isinstance(dv, str) and RE_COLOR.match(dv)):
                bad(f"{w}: default must be #RRGGBB")
        elif t == "text":
            if not (isinstance(dv, str) and len(dv.encode()) <= size - 1):
                bad(f"{w}: default must be text of at most {size - 1} bytes")
        if "unit" in f and not (isinstance(f["unit"], str) and len(f["unit"]) <= 8):
            bad(f"{w}: unit must be 8 characters max")
        if is_hidden is False and t == "hidden":
            bad(f'{w}: type "hidden" goes in the "hidden" list')
    for f, _ in fields:
        v = f.get("visible_if") if isinstance(f, dict) else None
        if v is not None and ids.get(v) not in ("toggle", "select"):
            bad(f'field "{f.get("id")}": visible_if must name a toggle or select field')
    return end if len(r.errors) == n0 else 0


def settings_defaults(d: dict) -> bytes:
    """The settings block a mod starts with: what the app writes when it installs the mod."""
    fields = [f for g in d.get("groups", []) for f in g["fields"]] + list(d.get("hidden", []))
    out = bytearray(max([f["offset"] + f["size"] for f in fields], default=0))
    for f in fields:
        o, n, v = f["offset"], f["size"], f["default"]
        if f["type"] == "color":
            out[o:o + 3] = bytes.fromhex(v[1:])
        elif f["type"] == "text":
            out[o:o + n] = v.encode().ljust(n, b"\0")
        else:
            out[o:o + n] = (v & ((1 << (8 * n)) - 1)).to_bytes(n, "little")
    return bytes(out)


def check_mod_file(b: bytes, e: dict, where: str, r: Report) -> tuple[int, int] | None:
    """Header checks the app can do without running anything. Returns (flash, RAM) the mod takes."""
    import pcsmod
    try:
        hd, _, _ = pcsmod.parse(b)
    except (ValueError, struct.error) as x:
        r.err("mod-invalid", f"{where}: not a valid .pcsmod ({x})")
        return None
    if (hd["api_major"], hd["api_minor"]) > MOD_API or hd["api_major"] != MOD_API[0]:
        r.err("mod-api", f"{where}: built for mod API {hd['api_major']}.{hd['api_minor']}; newest is {MOD_API[0]}.{MOD_API[1]}")
        return None
    usb = hd["flags"] & 3
    if USB_MODES[usb] != e.get("usb", "none"):
        r.err("mod-usb", f'{where}: the mod is built as USB "{USB_MODES[usb]}" but the manifest says '
                         f'"{e.get("usb", "none")}"')
    return (hd["image_size"] + 4095) & ~4095, (hd["data_size"] + hd["bss_size"] + 7) & ~7


def reserved_id(pid: str) -> bool:
    return pid.startswith(RESERVED_IDS) or bool(set(re.split(r"[.-]", pid)) & RESERVED_WORDS)


# ---------------------------------------------------------------- color set

def check_color_set(d: object, where: str, r: Report) -> str | None:
    """Checks one color set file. Returns its name when it's usable."""
    bad = lambda msg: r.err("set-invalid", f"{where}: {msg}")
    if not isinstance(d, dict) or d.get("format") != SET_FORMAT:
        r.err("set-format", f'{where}: not a color set (format must be "{SET_FORMAT}")')
        return None
    if d.get("version") != 1:
        r.err("set-version", f"{where}: color set version {d.get('version')!r} is not one this tool knows (1)")
        return None
    n0 = len(r.errors)
    name = d.get("name")
    if not (isinstance(name, str) and name.strip() and len(name) <= 15):
        bad("name must be 1 to 15 characters")
    lights = d.get("lights")
    if not (isinstance(lights, dict) and lights):
        bad("lights must name at least one light")
    else:
        for ln, l in lights.items():
            if ln not in LIGHTS:
                bad(f'"{ln}" is not a light name ({", ".join(LIGHTS)})')
            elif not (isinstance(l, dict) and l and set(l) <= {"idle", "pressed"} and all(isinstance(v, str) and RE_COLOR.match(v) for v in l.values())):
                bad(f'"{ln}" needs "idle" and/or "pressed" as #RRGGBB')
        if set(LIGHTS[:5]) - set(lights):
            r.hints.append(f"{where}: no colors for {', '.join(x for x in LIGHTS[:5] if x not in lights)}; those frets keep the starting colors")
    if "effect" in d and not (type(d["effect"]) is int and d["effect"] in EFFECTS):
        bad("effect must be 0 None, 1 Wave, 2 Shift, 3 Breathe or 4 Sparkle")
    if "speed" in d and not (type(d["speed"]) is int and 1 <= d["speed"] <= 10):
        bad("speed must be 1 to 10")
    if "brightness" in d:
        b = d["brightness"]
        if not (isinstance(b, dict) and set(b) <= {"idle", "pressed"} and all(type(v) is int and 0 <= v <= 100 for v in b.values())):
            bad("brightness must be {idle, pressed}, each 0 to 100")
    for k in ("tilt", "whammy", "joystick"):
        if k in d:
            f = d[k]
            keys = {"on", "color"} | ({"right"} if k == "joystick" else set())
            if not (isinstance(f, dict) and set(f) <= keys and ("on" not in f or type(f["on"]) is bool)
                    and all(isinstance(f[c], str) and RE_COLOR.match(f[c]) for c in ("color", "right") if c in f)):
                bad(f"{k} must be {{on: true/false, color: #RRGGBB{', right: #RRGGBB' if k == 'joystick' else ''}}}")
    return name.strip() if len(r.errors) == n0 else None


# ---------------------------------------------------------------- icon

def png_size(b: bytes) -> tuple[int, int] | None:
    if len(b) < 24 or b[:8] != b"\x89PNG\r\n\x1a\n" or b[12:16] != b"IHDR":
        return None
    return struct.unpack(">II", b[16:24])


def check_icon(b: bytes, where: str, r: Report) -> None:
    if len(b) > MAX_ICON_BYTES:
        r.err("icon-too-big", f"{where}: icon is {len(b) // 1024} KB (64 KB max)")
    wh = png_size(b)
    if not wh:
        r.err("icon-not-png", f"{where}: icon is not a PNG")
    elif wh[0] != wh[1] or not ICON_MIN <= wh[0] <= ICON_MAX:
        r.err("icon-size", f"{where}: icon is {wh[0]}x{wh[1]}; it must be square, {ICON_MIN} to {ICON_MAX} px")


# ---------------------------------------------------------------- files: folder or zip

def safe_path(name: str) -> bool:
    if not name or "\\" in name or name.startswith("/") or "\x00" in name or re.match(r"^[A-Za-z]:", name):
        return False
    parts = PurePosixPath(name).parts
    return all(p not in ("", ".", "..") for p in parts)


def read_zip(data: bytes, r: Report) -> dict[str, bytes] | None:
    """Opens a pack the way the app does: every entry checked before anything is used."""
    if len(data) > MAX_PACK_BYTES:
        r.err("pack-too-big", f"pack is {len(data) // 1024} KB (2 MB max)")
        return None
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        r.err("not-a-zip", "not a pack (not a ZIP file)")
        return None
    infos = z.infolist()
    if len(infos) > MAX_ENTRIES:
        r.err("too-many-files", f"pack has {len(infos)} entries ({MAX_ENTRIES} max)")
        return None
    files: dict[str, bytes] = {}
    seen: set[str] = set()
    total = 0
    for i in infos:
        if i.is_dir():
            if not safe_path(i.filename.rstrip("/")):
                r.err("path-unsafe", f'unsafe path "{i.filename}"')
                return None
            continue
        if not safe_path(i.filename):
            r.err("path-unsafe", f'unsafe path "{i.filename}"')
            return None
        if (i.external_attr >> 16) & 0o170000 == 0o120000:
            r.err("path-unsafe", f'"{i.filename}" is a link')
            return None
        if i.flag_bits & 0x1:
            r.err("encrypted", f'"{i.filename}" is password protected')
            return None
        if i.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
            r.err("compression", f'"{i.filename}" uses a compression other than deflate')
            return None
        low = i.filename.lower()
        if low in seen:
            r.err("duplicate-file", f'"{i.filename}" appears twice')
            return None
        seen.add(low)
        if i.file_size > MAX_FILE_BYTES:
            r.err("file-too-big", f'"{i.filename}" is {i.file_size // 1024} KB (256 KB max)')
            return None
        if i.compress_size and i.file_size / i.compress_size > MAX_RATIO and i.file_size > 64 * 1024:
            r.err("file-too-big", f'"{i.filename}" unpacks to far more than its packed size')
            return None
        total += i.file_size
        if total > MAX_UNPACKED_BYTES:
            r.err("pack-too-big", "pack unpacks to more than 4 MB")
            return None
        b = z.read(i)                       # zipfile checks the CRC
        if len(b) != i.file_size:
            r.err("file-too-big", f'"{i.filename}" is not the size the pack says')
            return None
        files[i.filename] = b
    return files


def read_folder(root: Path, r: Report) -> dict[str, bytes]:
    files = {}
    for p in sorted(root.rglob("*")):
        rel = p.relative_to(root).parts
        if p.is_file() and not any(x.startswith(".") for x in rel) and rel[0] != "src":   # src/ = your source, not packed
            files[p.relative_to(root).as_posix()] = p.read_bytes()
    return files


def parse_json(b: bytes, where: str, r: Report, code: str) -> object:
    try:
        return json.loads(b.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        r.err(code, f"{where}: not valid JSON ({e})")
        return None


def check_files(files: dict[str, bytes], r: Report) -> dict | None:
    """The whole-pack check. Returns the manifest when the pack is usable."""
    if "manifest.json" not in files:
        tops = {n.split("/")[0] for n in files if "/" in n}
        extra = f' (it\'s inside "{next(iter(tops))}/": zip the files, not the folder)' if len(tops) == 1 and any(n.endswith("/manifest.json") for n in files) else ""
        r.err("manifest-missing", "no manifest.json at the top of the pack" + extra)
        return None
    m = parse_json(files["manifest.json"], "manifest.json", r, "manifest-invalid")
    if m is None:
        return None
    check_manifest(m, r)
    if not r.ok:
        return None
    for n in files:
        if PurePosixPath(n).suffix.lower() not in ALLOWED_EXT:
            r.err("file-type", f'"{n}": only .json, .png, .md, .txt and .pcsmod files go in a pack')
    if "icon" in m:
        if m["icon"] not in files:
            r.err("missing-file", f'icon "{m["icon"]}" is not in the pack')
        else:
            check_icon(files[m["icon"]], m["icon"], r)
    else:
        r.hints.append("no icon: the app shows a plain one")
    names: dict[str, str] = {}
    for p in m["contents"].get("color_sets", []):
        if p not in files:
            r.err("missing-file", f'"{p}" is listed but not in the pack')
            continue
        d = parse_json(files[p], p, r, "set-invalid")
        if d is None:
            continue
        nm = check_color_set(d, p, r)
        if nm:
            if nm.lower() in names:
                r.err("set-duplicate-name", f'"{p}" and "{names[nm.lower()]}" are both named "{nm}"')
            names[nm.lower()] = p
    flash = ram = 0
    mod_listed = set()
    for i, e in enumerate(m["contents"].get("mods", [])):
        for k in ("file", "settings"):
            if k in e:
                mod_listed.add(e[k])
                if e[k] not in files:
                    r.err("missing-file", f'"{e[k]}" is listed but not in the pack')
        if e["file"] not in files:
            continue
        got = check_mod_file(files[e["file"]], e, e["file"], r)
        if got:
            flash, ram = flash + got[0], ram + got[1]
        if "settings" in e and e["settings"] in files:
            d = parse_json(files[e["settings"]], e["settings"], r, "mod-settings")
            if d is not None:
                check_mod_settings(d, e["settings"], r)
    if flash > MOD_AREA or ram > MOD_RAM:
        r.err("mod-size", f"the mods need {flash // 1024} KB flash and {ram} B RAM; a controller has "
                          f"{MOD_AREA // 1024} KB and {MOD_RAM} B for all mods together")
    if m["contents"].get("mods") and reserved_id(m["id"]):
        r.hints.append(f'id "{m["id"]}" uses a reserved name: release it in packs/ on the packs branch of 3DPCS-Programmer-Releases (Pack Manager catalog); local builds use Developer installs')
    listed = set(m["contents"].get("color_sets", [])) | mod_listed | {"manifest.json", m.get("icon")}
    for n in files:
        if n.endswith((".json", ".pcsmod")) and n not in listed:
            r.hints.append(f'"{n}" is not listed in contents, so the app ignores it')
    return m if r.ok else None


def check_any(path: Path) -> tuple[Report, dict | None, dict[str, bytes] | None]:
    r = Report()
    if path.is_dir():
        files = read_folder(path, r)
    else:
        files = read_zip(path.read_bytes(), r)
        if files is None:
            return r, None, None
    m = check_files(files, r)
    return r, m, files


# ---------------------------------------------------------------- writing

def zip_bytes(files: dict[str, bytes]) -> bytes:
    """Deterministic ZIP: manifest first, then sorted paths, fixed time stamps, deflate."""
    out = io.BytesIO()
    order = ["manifest.json"] + sorted(n for n in files if n != "manifest.json")
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for n in order:
            zi = zipfile.ZipInfo(n, date_time=(2026, 1, 1, 0, 0, 0))
            zi.compress_type = zipfile.ZIP_DEFLATED
            zi.external_attr = 0o644 << 16
            z.writestr(zi, files[n])
    return out.getvalue()


def print_report(r: Report, label: str) -> None:
    for code, msg in r.errors:
        print(f"  error [{code}] {msg}")
    for h in r.hints:
        print(f"  hint  {h}")
    print(f"{label}: {'OK' if r.ok else f'{len(r.errors)} problem(s)'}")


def pack_filename(m: dict) -> str:
    """The name build gives a pack. Any name ending in .3dpack works ("My Colors.3dpack"): the app reads what's inside,
    never the file name, and the official list matches the file's bytes. Official 3DPCS packs:
    "3DPCS-Fret Keys 0.1.0.3dpack"; others: "<Name> <version>.3dpack"."""
    name = re.sub(r'[<>:"/\\|?*]', "", m["name"]).strip() or m["id"]
    return f"3DPCS-{name} {m['version']}{EXT}" if m["id"].startswith("3dpcs.") else f"{name} {m['version']}{EXT}"


def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-") or "set"


def sim_check(m: dict | None, files: dict | None, r: Report) -> None:
    """Developer tool: after the app's rules, trial-load each mod and run init once."""
    if not m or not files:
        return
    import modsim
    for e in m["contents"].get("mods", []):
        try:
            st = b""
            if "settings" in e:
                st = settings_defaults(json.loads(files[e["settings"]]))
            sim = modsim.ModSim(files[e["file"]], settings=st)
            sim.init()
            for _ in range(3):
                sim.task(); sim.on_inputs(); sim.on_leds(); sim.usb_poll(); sim.advance(10)
            for w in sim.warnings:
                r.hints.append(f'{e["file"]}: {w}')
        except modsim.ModError as x:
            r.err("mod-load", f'{e["file"]}: {x}')
        except Exception as x:          # simulator limits: report, don't crash
            r.hints.append(f"{e['file']}: the simulator could not run it ({x})")


def cmd_check(a) -> int:
    r, m, files = check_any(Path(a.path))
    sim_check(m, files, r)
    print_report(r, a.path)
    return 0 if r.ok else 1


def cmd_build(a) -> int:
    src = Path(a.folder)
    r, m, files = check_any(src)
    sim_check(m, files, r)
    if not r.ok:
        print_report(r, a.folder)
        return 1
    keep = {k: v for k, v in files.items() if not k.endswith(".md") or k == "README.md"}
    data = zip_bytes(keep)
    r2 = Report()
    if check_files(read_zip(data, r2) or {}, r2) is None:   # the built file must pass the app's own reading
        print_report(r2, "built pack")
        return 1
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    dest = out / pack_filename(m)
    dest.write_bytes(data)
    print_report(r, a.folder)
    print(f"wrote {dest} ({len(data)} bytes)")
    return 0


def cmd_bundle(a) -> int:
    files: dict[str, bytes] = {}
    paths = []
    r = Report()
    for f in a.sets:
        b = Path(f).read_bytes()
        d = parse_json(b, f, r, "set-invalid")
        nm = check_color_set(d, f, r) if d is not None else None
        if not nm:
            continue
        p = f"sets/{slug(nm)}.json"
        k = 2
        while p in files:
            p, k = f"sets/{slug(nm)}-{k}.json", k + 1
        d.pop("product", None)
        files[p] = (json.dumps(d, indent=2, ensure_ascii=False) + "\n").encode()
        paths.append(p)
    if not r.ok:
        print_report(r, "sets")
        return 1
    m = {"format": FORMAT, "format_version": FORMAT_VERSION, "id": a.id, "name": a.name, "author": a.author,
         "version": a.version, "boards": ["*"], "contents": {"color_sets": paths}}
    if a.description:
        m["description"] = a.description
    if a.icon:
        files["icon.png"] = Path(a.icon).read_bytes()
        m["icon"] = "icon.png"
    files["manifest.json"] = (json.dumps(m, indent=2, ensure_ascii=False) + "\n").encode()
    if check_files(files, r) is None:
        print_report(r, "pack")
        return 1
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    dest = out / pack_filename(m)
    dest.write_bytes(zip_bytes(files))
    print_report(r, "pack")
    print(f"wrote {dest}")
    return 0


def cmd_new(a) -> int:
    root = Path(a.folder)
    if root.exists() and any(root.iterdir()):
        print(f"{root} is not empty")
        return 1
    (root / "sets").mkdir(parents=True)
    m = {"$schema": "../../schema/manifest.schema.json", "format": FORMAT, "format_version": FORMAT_VERSION,
         "id": a.id, "name": a.name, "author": a.author, "version": "1.0.0",
         "description": "Describe it in one short line", "boards": ["*"], "license": "CC-BY-4.0",
         "contents": {"color_sets": ["sets/first.json"]}}
    s = {"format": SET_FORMAT, "version": 1, "name": "First",
         "lights": {n: {"idle": "#ff00ff", "pressed": "#ffffff"} for n in LIGHTS},
         "effect": 0, "speed": 5, "brightness": {"idle": 40, "pressed": 60},
         "tilt": {"on": True, "color": "#8000ff"}, "whammy": {"on": True, "color": "#00c8ff"},
         "joystick": {"on": False, "color": "#ff2000", "right": "#00ff40"}}
    (root / "manifest.json").write_text(json.dumps(m, indent=2) + "\n")
    (root / "sets" / "first.json").write_text(json.dumps(s, indent=2) + "\n")
    print(f"made {root}: edit manifest.json and sets/first.json, add icon.png (square PNG, 64 to 256 px), then build")
    return 0


def cmd_inspect(a) -> int:
    r, m, files = check_any(Path(a.path))
    if m:
        print(f"{m['name']} {m['version']} by {m['author']}  ({m['id']})")
        if m.get("description"):
            print(f"  {m['description']}")
        print(f"  boards: {', '.join(m.get('boards', ['*']))}")
        for p in m["contents"].get("color_sets", []):
            d = json.loads(files[p])
            fr = " ".join(d["lights"].get(n, {}).get("idle", "-------") for n in LIGHTS[:5])
            print(f"  color set {d['name']:<15} {fr}  effect {EFFECTS.get(d.get('effect', 0))}")
        import pcsmod
        for e in m["contents"].get("mods", []):
            hd, _, _ = pcsmod.parse(files[e["file"]])
            usb, hooks = pcsmod.describe(hd)
            print(f"  mod {e['name']}: {hd['image_size']} B flash, {hd['data_size'] + hd['bss_size']} B RAM, "
                  f"API {hd['api_major']}.{hd['api_minor']}, USB {usb}, hooks {', '.join(hooks) or 'none'}")
    print_report(r, a.path)
    return 0 if r.ok else 1


def cmd_hash(a) -> int:
    data = Path(a.path).read_bytes()
    r = Report()
    m = check_files(read_zip(data, r) or {}, r)
    if not m:
        print_report(r, a.path)
        return 1
    print(json.dumps({"id": m["id"], "version": m["version"], "sha256": hashlib.sha256(data).hexdigest()}))
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="pcspack", description="Make and check 3DPCS packs")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("check"); p.add_argument("path"); p.set_defaults(f=cmd_check)
    p = sub.add_parser("build"); p.add_argument("folder"); p.add_argument("-o", "--out", default="dist"); p.set_defaults(f=cmd_build)
    p = sub.add_parser("inspect"); p.add_argument("path"); p.set_defaults(f=cmd_inspect)
    p = sub.add_parser("hash"); p.add_argument("path"); p.set_defaults(f=cmd_hash)
    p = sub.add_parser("new"); p.add_argument("kind", choices=["colors"]); p.add_argument("folder")
    p.add_argument("--id", required=True); p.add_argument("--name", required=True); p.add_argument("--author", required=True)
    p.set_defaults(f=cmd_new)
    p = sub.add_parser("bundle"); p.add_argument("sets", nargs="+")
    for k in ("id", "name", "author"):
        p.add_argument(f"--{k}", required=True)
    p.add_argument("--version", default="1.0.0"); p.add_argument("--description"); p.add_argument("--icon")
    p.add_argument("-o", "--out", default="dist"); p.set_defaults(f=cmd_bundle)
    a = ap.parse_args(argv)
    return a.f(a)


if __name__ == "__main__":
    sys.exit(main())
