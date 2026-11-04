"""modsim: run a .pcsmod's hooks on a PC, the way the controller firmware will. MIT License.

STATUS: follows mod API 0.5 in include/pcs_mod.h and the loader behavior in the proprietary firmware core.

The mod runs on tools/thumb.py (a Cortex-M0+ instruction interpreter), placed at a real mod-area slot with the same
relocation the app will do. The firmware side (the pcs_api_t table) is played by Python. What it checks:
  - the loader steps: CRC, data copy, bss zero, pcs_mod_main, API version
  - every memory access: only the mod's own flash (read-only), its RAM, its 4 KB stack, and what the API hands it
  - hook cost: instructions per call and stack use, against the budgets in pcs_mod.h
  - behavior: the lights and inputs after each hook, the log, settings, claims, fail() reasons
  - USB (usb_mode ADD / REPLACE): the interfaces it adds, the configuration descriptor a computer would get (checked the
    way a computer checks it), reports it sends (HID decoded by its report descriptor, MIDI as notes), control requests
    and OUT data sent to it

  - linked boards (LinkNet): a hub and its nodes on the link bus model (linkbus.py): link_nodes, link_send,
    on_link, on_node, with each board running its own mod (or none, like a plain neck)

Not simulated: hardware registers, interrupts, the boot ROM float table. A mod that
uses those runs until it touches them, then stops with a plain message.
"""
from __future__ import annotations

import struct

import pcsmod
import linkbus
import usbhost
from thumb import Cpu, Fault, Region

# Addresses match the current controller mod layout so failures are comparable with hardware logs.
FLASH_SLOT = 0x100A1000          # first slot after the 4 KB mod table
RAM_SLOT = 0x20034000            # start of mod RAM
STACK_TOP = 0x20040000           # mod stack: 0x2003F000..0x2003FFFF
STACK_SIZE = 4096
HOST = 0x30000000                # sim-only: the API table, board info, light frame, strings
TRAPS = 0x0F000000               # sim-only: host function addresses
RET = 0x0EFFFFF0

API_MAJOR, API_MINOR = 0, 5
TASK_BUDGET = 25_000             # instructions: ~200 us typical at 125 MHz (pcs_mod.h), generous for 1-2 cycle ops
HARD_BUDGET = 125_000            # ~1 ms worst case

LIGHTS9 = ["Green", "Red", "Yellow", "Blue", "Orange", "Strum up", "Strum down", "Start", "Select"]
BOARDS = {   # module id -> (profile shown to the mod, lights, has radio). Pin maps aren't modeled: use --pins-used
    "polybar": ("usk", LIGHTS9, True),
    "monoboard": ("monoboard_rp", LIGHTS9, False),
    "hammeron": ("hammeron", LIGHTS9, False),
    "riffstick": ("riffstick", [], False),
    "polylink": ("polylink_lite", ["Light"], True),
}
FW_VERSION = (0, 11, 0)
MAX_PERSONALITIES = 4           # conditional USB: personalities per mod (firmware 0.10.0 / Polylink 0.5.0)
SWITCH_WINDOW_MS = 10_000       # at most 4 usb_switch calls in this window

# pcs_api_t function order (offset 8 + 4 * index). tests/test_modsim.py checks this against the header with gcc.
API_FUNCS = ["board", "log", "fail", "time_us", "time_ms", "claim_pin", "claim_pio_sm", "claim_pio_program",
             "claim_dma", "claim_alarm", "claim_bus", "irq_add", "i2c_xfer", "spi_xfer", "adc_read", "settings_read",
             "settings_write", "usb_add", "usb_ready", "usb_send", "usb_control_reply", "link_nodes", "link_send",
             "rom_float", "memcpy", "memset",
             "usb_personality_add", "usb_host", "usb_switch", "usb_current",
             "usb_service_claim",                                                   # API 0.4
             "random_fill"]                                                        # API 0.5
API_SIZE = 8 + 4 * len(API_FUNCS)
MOD_HOOKS = ["init", "task", "on_inputs", "on_leds", "on_settings", "on_usb_setup", "on_usb_rx", "on_link",
             "on_node", "before_reboot", "on_usb_host", "on_usb_personality"]     # API 0.3
MOD_SIZE = 8 + 4 * len(MOD_HOOKS)
INPUTS_FMT = struct.Struct("<I8hBBH")       # pcs_inputs_t, 24 bytes
USB_IFACE_SIZE = 72                         # pcs_usb_iface_t (layout test checks it)
USB_EP_SIZE = 12                            # pcs_usb_ep_t
NODE_FMT = struct.Struct("<BB24s2xI")       # pcs_node_t, 32 bytes
SETTINGS_MAX = 256


class ModError(Exception):
    """The mod can't be loaded or broke a rule. The message is written for the mod's author."""


class HookStats:
    def __init__(self):
        self.calls = 0
        self.total = 0
        self.worst = 0

    def add(self, n: int):
        self.calls += 1
        self.total += n
        self.worst = max(self.worst, n)


class ModSim:
    def __init__(self, blob: bytes, board: str = "polybar", settings: bytes = b"", pins_used: int = 0,
                 adc: dict[int, int] | None = None, i2c: dict[int, bytes] | None = None,
                 max_call_steps: int = HARD_BUDGET * 4):
        if board not in BOARDS:
            raise ModError(f"unknown board {board!r} (one of {', '.join(BOARDS)})")
        try:
            image, self.hdr = pcsmod.place(blob, FLASH_SLOT, RAM_SLOT)
        except ValueError as e:
            raise ModError(f"not a valid .pcsmod: {e}") from None
        h = self.hdr
        if h["api_major"] != API_MAJOR or h["api_minor"] > API_MINOR:
            raise ModError(f"built for mod API {h['api_major']}.{h['api_minor']}; this firmware has "
                           f"{API_MAJOR}.{API_MINOR}")
        self.board_id = board
        self.profile, self.lights, self.radio = BOARDS[board]
        self.pins_used = pins_used
        self.adc = adc or {}
        self.i2c = dict(i2c or {})                    # I2C devices: address -> what they answer
        self.i2c_log: list[tuple[int, bytes]] = []
        self.settings = bytearray(settings[:SETTINGS_MAX])
        self.settings_writes = 0
        self.now_us = 0
        self.logs: list[str] = []
        self.failed: str | None = None
        self.claims: dict[str, set] = {"pin": set(), "pio_sm": set(), "dma": set(), "alarm": set(), "bus": set()}
        self.irqs: list[int] = []
        self.stats: dict[str, HookStats] = {}
        self.max_stack = 0
        self.warnings: list[str] = []
        self.max_call_steps = max_call_steps           # profiling may raise this; report() still marks the real budget
        self.usb_ifaces: list[usbhost.Iface] = []
        self.usb_raw: list[tuple[int, list[int]]] = []     # (iface pointer, endpoint addresses) per usb_add
        self.usb_config = b""
        self.usb_ready = False
        self.usb_pending: dict[int, bytes] = {}
        self.hid: dict[int, usbhost.HidReport] = {}
        self.midi_eps: set[int] = set()
        self.personalities: list[dict] = []           # conditional USB: each {"id", "dev", "raw": [(ptr,(num,addrs))]}
        self.cur_build = 0                             # personality usb_add is filling during init (0 = none yet)
        self.live = 0                                  # the live personality (0 = stock)
        self.usb_session = 1
        self.host_addr = 0
        self.host_flags = 0                            # PCS_HOST_* the simulated computer has done (set via set_host)
        self.switch_times: list[int] = []              # ms of recent usb_switch calls (rate limit)
        self.empty_personalities: set[int] = set()     # declared with no interfaces: can't be switched to
        self.pending_switch: int | None = None         # a requested switch, applied from the main loop (not re-entrantly)
        self.service_provider = 0                      # API 0.4 core-owned device service callback
        self.service_req_addr = 0                     # reused: repeated status polls must not consume host memory
        self.service_reply_addr = 0
        self.random_state = 0xC001D00D             # deterministic per simulator; reproducible tests, unlike hardware
        self.in_init = False
        self.in_setup = False
        self.setup_reply: bytes | None = None
        self.link: LinkNet | None = None
        self.link_board: linkbus.Node | None = None   # set when this sim is a node on a LinkNet

        cpu = self.cpu = Cpu()
        ram_size = (h["data_size"] + h["bss_size"] + 7) & ~7
        cpu.add(Region("the mod's flash", FLASH_SLOT, (len(image) + 4095) & ~4095, writable=False, data=image))
        if ram_size:
            cpu.add(Region("the mod's RAM", RAM_SLOT, ram_size))
        cpu.add(Region("the mod stack", STACK_TOP - STACK_SIZE, STACK_SIZE))
        self.host = cpu.add(Region("controller memory", HOST, 0x4000))
        self._host_top = HOST
        cpu.trap_base, cpu.trap_end = TRAPS, TRAPS + 4 * len(API_FUNCS)

        # firmware loader steps: data copied from flash, bss already zero
        if h["data_size"]:
            cpu.write_bytes(RAM_SLOT, image[h["data_load"]:h["data_load"] + h["data_size"]])
        self._build_api()
        mod = self._call("pcs_mod_main", FLASH_SLOT + h["entry"], [self.api])
        if not mod:
            raise ModError("pcs_mod_main returned NULL")
        try:
            raw = cpu.read_bytes(mod, MOD_SIZE)
        except Fault as e:
            raise ModError(f"pcs_mod_main returned a bad pointer: {e}") from None
        major, minor, size, usb_mode = struct.unpack_from("<HHHB", raw)
        if (major, minor) != (h["api_major"], h["api_minor"]):
            raise ModError(f"pcs_mod_t says API {major}.{minor} but the header says {h['api_major']}.{h['api_minor']}")
        if size < 8 or size > MOD_SIZE:
            raise ModError(f"pcs_mod_t size {size} is wrong (expected {MOD_SIZE})")
        self.usb_mode = usb_mode
        self.hooks = {}
        for i, name in enumerate(MOD_HOOKS):
            if 8 + 4 * i + 4 <= size:
                p = struct.unpack_from("<I", raw, 8 + 4 * i)[0]
                if p:
                    self.hooks[name] = p

        self.inputs_addr = self._alloc(INPUTS_FMT.size)
        n = max(len(self.lights), 1)
        self.rgb_addr = self._alloc(3 * n)
        self.leds_addr = self._alloc(8)
        self.scratch = self._alloc(8 + 1024)          # control requests and OUT data from the "computer"
        self.initialized = False

    # ------------------------------------------------------------------ memory the "firmware" owns
    def _alloc(self, size: int, data: bytes | None = None) -> int:
        addr = (self._host_top + 7) & ~7
        if addr + size > self.host.end:
            raise RuntimeError("sim host memory full")
        self._host_top = addr + size
        if data:
            self.cpu.write_bytes(addr, data)
        return addr

    def _str(self, s: str) -> int:
        return self._alloc(len(s) + 1, s.encode() + b"\0")

    def _build_api(self):
        cpu = self.cpu
        names = [self._str(n) for n in self.lights]
        names_arr = self._alloc(4 * max(len(names), 1), b"".join(struct.pack("<I", a) for a in names))
        self.board_addr = self._alloc(24, struct.pack("<II3BBIIB3x", self._str(self.board_id), self._str(self.profile),
                                                      *FW_VERSION, len(self.lights), names_arr, self.pins_used,
                                                      int(self.radio)))
        table = struct.pack("<HHHH", API_MAJOR, API_MINOR, API_SIZE, 0)
        for i, name in enumerate(API_FUNCS):
            if name == "rom_float":
                table += struct.pack("<I", 0)         # not simulated: a mod that uses it faults on the NULL
                continue
            addr = TRAPS + 4 * i
            cpu.traps[addr] = getattr(self, "_api_" + name)
            table += struct.pack("<I", addr | 1)
        self.api = self._alloc(len(table), table)

    # ------------------------------------------------------------------ running hooks
    def _call(self, name: str, fn: int, args) -> int:
        cpu = self.cpu
        cpu.r[13] = STACK_TOP
        cpu.lowest_sp = STACK_TOP
        before = cpu.steps
        try:
            ret = cpu.call(fn, args, RET, max_steps=self.max_call_steps)
        except Fault as e:
            raise ModError(f"{name}: {e} (pc {cpu.r[15]:#010x})") from None
        self.stats.setdefault(name, HookStats()).add(cpu.steps - before)
        self.max_stack = max(self.max_stack, STACK_TOP - cpu.lowest_sp)
        if self.failed:
            raise ModError(f"the mod stopped itself: {self.failed}")
        return ret

    def hook(self, name: str, *args) -> int | None:
        fn = self.hooks.get(name)
        if fn is None:
            return None
        return self._call(name, fn, args)

    def init(self) -> bool:
        self.in_init = True
        try:
            ok = self.hook("init")
        finally:
            self.in_init = False
        self.initialized = ok is None or bool(ok & 0xFF)
        if not self.initialized:
            raise ModError("init returned false" + (f": {self.failed}" if self.failed else ""))
        if self.usb_mode == 3:                        # conditional: validate each declared personality (stays stock now)
            for p in self.personalities:
                if not p["raw"]:                      # a personality with no interfaces can't be switched to
                    self.empty_personalities.add(p["id"])
                    self.warnings.append(f"personality {p['id']} has no interfaces: it can't be switched to")
                    continue
                _, cfg, ifaces = self._build_personality(p["raw"])
                try:
                    self.warnings += usbhost.check_config(cfg, ifaces)
                except usbhost.UsbError as e:
                    raise ModError(f"USB: personality {p['id']}: {e}") from None
            if not any(p["raw"] for p in self.personalities):   # firmware: refuse the mod outright
                raise ModError("It declared no USB personality")
        elif self.usb_raw:
            self._enumerate()
        elif self.usb_mode == 2:                      # the firmware keeps the game pad then (core 0.11)
            self.warnings.append("usb_mode is PCS_USB_REPLACE but init added no USB interfaces: the game pad stays")
        return True

    # ------------------------------------------------------------------ USB: what a computer sees
    def _read_iface(self, ptr: int, num: int, addrs: list[int]) -> usbhost.Iface:
        cpu = self.cpu
        raw = cpu.read_bytes(ptr, USB_IFACE_SIZE)
        cls, sub, proto, n = raw[0:4]
        eps = []
        for k in range(n):
            e_in, e_type, size, interval, xlen, _, xptr = struct.unpack_from("<BBHBBHI", raw, 4 + USB_EP_SIZE * k)
            eps.append(usbhost.Ep(addrs[k], e_type, size, interval, cpu.read_bytes(xptr, xlen) if xlen else b""))
        cd_ptr, cd_len, group, _, name_ptr, hid_ptr, hid_len = struct.unpack_from("<IHBBIIH", raw, 52)
        return usbhost.Iface(num, cls, sub, proto, eps, cpu.read_bytes(cd_ptr, cd_len) if cd_len else b"", group,
                             cpu.cstr(name_ptr, 33) if name_ptr else "", cpu.read_bytes(hid_ptr, hid_len) if hid_len else b"")

    def _enumerate(self):
        try:
            mods = [self._read_iface(ptr, num, addrs) for ptr, (num, addrs) in self.usb_raw]
        except Fault as e:
            raise ModError(f"USB: a descriptor pointer is bad: {e}") from None
        ifaces = []
        if self.usb_mode == 1:     # Add: the controller's game pad stays as interface 0
            ifaces.append(usbhost.Iface(0, 0x03, 0, 0, [usbhost.Ep(0x81, 3, 64, 1)], hid_report=usbhost.GAMEPAD_REPORT,
                                        name="Game pad", owner="gamepad"))
        ifaces += mods
        top_ep = max([e.addr & 0x7F for i in ifaces for e in i.eps] + [1])
        ifaces.append(usbhost.Iface(len(ifaces), *usbhost.SETTINGS_IFACE,
                                    [usbhost.Ep(top_ep + 1, 2, 64, 0), usbhost.Ep(0x80 | (top_ep + 1), 2, 64, 0)],
                                    name="PCS Config", owner="settings"))
        for i in mods:
            if len(i.name) > 32 or not i.name.isascii():
                raise ModError(f"USB: interface {i.num} name must be ASCII, 32 characters max")
            if i.group and not all(any(j.num == i.num + k for j in mods) for k in range(i.group)):
                raise ModError(f"USB: interface {i.num} says its function has {i.group} interfaces; add them right after it")
        cfg, self.usb_strings = usbhost.config_descriptor(ifaces)
        try:
            self.warnings += usbhost.check_config(cfg, ifaces)
        except usbhost.UsbError as e:
            raise ModError(f"USB: {e}") from None
        self.usb_ifaces, self.usb_config = ifaces, cfg
        for i in mods:
            for e in i.eps:
                if i.cls == 0x03 and e.addr & 0x80:
                    self.hid[e.addr] = usbhost.HidReport(i.hid_report)
                if (i.cls, i.subclass) == (0x01, 0x03):
                    self.midi_eps.add(e.addr)
        self.usb_ready = True

    def _build_personality(self, raw):
        """Conditional USB: build one personality's interface list (its own interfaces + a trailing PCS Config)."""
        try:
            mods = [self._read_iface(ptr, num, addrs) for ptr, (num, addrs) in raw]
        except Fault as e:
            raise ModError(f"USB: a descriptor pointer is bad: {e}") from None
        for i in mods:
            if len(i.name) > 32 or not i.name.isascii():
                raise ModError(f"USB: interface {i.num} name must be ASCII, 32 characters max")
            if i.group and not all(any(j.num == i.num + k for j in mods) for k in range(i.group)):
                raise ModError(f"USB: interface {i.num} says its function has {i.group} interfaces; add them right after it")
        ifaces = list(mods)
        top_ep = max([e.addr & 0x7F for i in ifaces for e in i.eps] + [1])
        ifaces.append(usbhost.Iface(len(ifaces), *usbhost.SETTINGS_IFACE,
                                    [usbhost.Ep(top_ep + 1, 2, 64, 0), usbhost.Ep(0x80 | (top_ep + 1), 2, 64, 0)],
                                    name="PCS Config", owner="settings"))
        cfg, _ = usbhost.config_descriptor(ifaces)
        return mods, cfg, ifaces

    def _go_live(self, personality_id: int):
        """Make a personality (or stock, id 0) the live descriptor the computer talks to."""
        self.usb_pending.clear()
        self.hid, self.midi_eps = {}, set()
        self.live = personality_id
        p = next((x for x in self.personalities if x["id"] == personality_id), None)
        if p is None:                                 # stock: no mod interfaces to send on
            self.usb_ifaces, self.usb_config, self.usb_ready = [], b"", False
            return
        mods, cfg, ifaces = self._build_personality(p["raw"])
        self.usb_ifaces, self.usb_config = ifaces, cfg
        for i in mods:
            for e in i.eps:
                if i.cls == 0x03 and e.addr & 0x80:
                    self.hid[e.addr] = usbhost.HidReport(i.hid_report)
                if (i.cls, i.subclass) == (0x01, 0x03):
                    self.midi_eps.add(e.addr)
        self.usb_ready = True

    def _mod_ep(self, addr: int) -> usbhost.Ep | None:
        for i in self.usb_ifaces:
            if i.owner == "mod":
                for e in i.eps:
                    if e.addr == addr:
                        return e
        return None

    def usb_poll(self) -> list[tuple[int, bytes]]:
        """The computer reads every IN endpoint: what the mod queued since the last poll."""
        out = sorted(self.usb_pending.items())
        self.usb_pending.clear()
        return out

    def _host_struct(self) -> int:
        """pcs_usb_host_t in host memory. classification/confidence are always 0, as the firmware leaves them: a mod
        works out its own from flags and session."""
        if not self.host_addr:
            self.host_addr = self._alloc(8)
        self.cpu.write_bytes(self.host_addr, struct.pack("<HBBI", self.host_flags, 0, 0, self.usb_session))
        return self.host_addr

    def set_host(self, flags=None, bump_session=True):
        """Conditional USB: set what the simulated computer has asked for (PCS_HOST_* flags) and fire on_usb_host, so
        the mod can work out its own classification and usb_switch(). The firmware never fills classification itself."""
        if flags is not None:
            self.host_flags = flags
        if bump_session:
            self.usb_session += 1
        if "on_usb_host" in self.hooks:
            self.hook("on_usb_host", self._host_struct())
        self._apply_pending()                           # carry out a switch the hook asked for

    def usb_control(self, bm: int, req: int, value: int = 0, index: int = 0, length: int = 0,
                    data: bytes | None = None) -> bytes | None:
        """A class / vendor request from the computer. Returns the mod's reply, b"" for an accepted OUT request,
        None for STALL."""
        if data and len(data) > 1024:
            raise ValueError("control data stage over 1024 bytes")
        setup = self.scratch
        self.cpu.write_bytes(setup, struct.pack("<BBHHH", bm, req, value, index, length))
        dptr = 0
        if data:
            dptr = self.scratch + 8
            self.cpu.write_bytes(dptr, data)
        self.in_setup, self.setup_reply = True, None
        try:
            self.hook("on_usb_setup", setup, dptr)
        finally:
            self.in_setup = False
        if self.setup_reply is not None:
            return self.setup_reply
        return b"" if not bm & 0x80 and "on_usb_setup" in self.hooks else None

    def usb_out(self, ep: int, data: bytes):
        """The computer sends OUT data to one of the mod's endpoints."""
        e = self._mod_ep(ep)
        if e is None or ep & 0x80:
            raise ValueError(f"{ep:#04x} is not one of the mod's OUT endpoints")
        if len(data) > e.size:
            raise ValueError(f"{len(data)} bytes is more than the endpoint's {e.size}")
        self.cpu.write_bytes(self.scratch + 8, data)
        self.hook("on_usb_rx", ep, self.scratch + 8, len(data))

    def decode(self, ep: int, data: bytes) -> str:
        if ep in self.hid:
            return self.hid[ep].decode(data)
        if ep in self.midi_eps:
            return ", ".join(usbhost.midi_events(data)) or "(empty)"
        return data.hex()

    def advance(self, ms: float):
        self.now_us += int(ms * 1000)

    def task(self):
        self.hook("task")
        self._apply_pending()

    def _apply_pending(self):
        """Carry out a usb_switch requested during a hook. The firmware does transitions from the main loop, never
        inside a USB call or re-entrantly, so the ENTER/EXIT hooks run here (at top level), not inside usb_switch."""
        if self.pending_switch is None:
            return
        tid, self.pending_switch = self.pending_switch, None
        old = self.live
        if old and "on_usb_personality" in self.hooks:
            self.hook("on_usb_personality", old, 1)    # PCS_USBP_EXIT
        self.usb_session += 1
        self._go_live(tid)
        if tid and "on_usb_personality" in self.hooks:
            self.hook("on_usb_personality", tid, 0)    # PCS_USBP_ENTER (the computer has set it up)

    def on_inputs(self, buttons: int = 0, axis=(0,) * 8, hat: int = 8, flags: int = 0) -> dict:
        self.cpu.write_bytes(self.inputs_addr, INPUTS_FMT.pack(buttons, *axis, hat, flags, 0))
        self.hook("on_inputs", self.inputs_addr)
        v = INPUTS_FMT.unpack(self.cpu.read_bytes(self.inputs_addr, INPUTS_FMT.size))
        return {"buttons": v[0], "axis": list(v[1:9]), "hat": v[9], "flags": v[10]}

    def on_leds(self, rgb: list[tuple[int, int, int]] | None = None, brightness: int = 255) -> list[tuple[int, int, int]]:
        n = len(self.lights)
        rgb = list(rgb or [(0, 0, 0)] * n)
        self.cpu.write_bytes(self.rgb_addr, bytes(c for px in rgb[:n] for c in px))
        self.cpu.write_bytes(self.leds_addr, struct.pack("<BBHI", n, brightness, 0, self.rgb_addr))
        self.hook("on_leds", self.leds_addr)
        out = self.cpu.read_bytes(self.rgb_addr, 3 * n)
        return [tuple(out[i:i + 3]) for i in range(0, 3 * n, 3)]

    def set_settings(self, data: bytes):
        self.settings = bytearray(data[:SETTINGS_MAX])
        self.hook("on_settings")

    def report(self) -> list[str]:
        """Plain-text summary: hook costs against the budget, stack, claims."""
        lines = []
        for name, s in self.stats.items():
            avg = s.total // max(s.calls, 1)
            warn = "  OVER BUDGET" if s.worst > HARD_BUDGET else ("  slow" if avg > TASK_BUDGET else "")
            lines.append(f"{name:14} {s.calls:5} calls, avg {avg} / worst {s.worst} instructions{warn}")
        lines.append(f"stack          {self.max_stack} of {STACK_SIZE} bytes")
        ram = self.hdr["data_size"] + self.hdr["bss_size"]
        lines.append(f"size           {self.hdr['image_size']} B flash, {ram} B RAM")
        for i in self.usb_ifaces:
            eps = " ".join(f"{e.addr:#04x}" for e in i.eps)
            who = {"gamepad": " (the controller's game pad)", "settings": " (the app's settings link)"}.get(i.owner, "")
            lines.append(f"usb iface {i.num}    class {i.cls:02x}/{i.subclass:02x}/{i.protocol:02x} {i.name!r} {eps}{who}")
        if self.usb_config:
            lines.append(f"usb config     {len(self.usb_config)} bytes, checked like a computer does")
        for w in self.warnings:
            lines.append(f"warning        {w}")
        held = {k: sorted(v) for k, v in self.claims.items() if v}
        if held:
            lines.append("claims         " + ", ".join(f"{k} {v}" for k, v in held.items()))
        return lines

    # ------------------------------------------------------------------ the API, played by Python
    def _args(self, n: int) -> list[int]:
        """Word arguments n..: r0-r3, then the caller's stack."""
        cpu = self.cpu
        return [cpu.r[i] if i < 4 else cpu.read(cpu.r[13] + 4 * (i - 4), 4) for i in range(n)]

    def _api_board(self, cpu):
        return self.board_addr

    def _api_log(self, cpu):
        fmt = cpu.cstr(cpu.r[0])
        out, argi, i = [], 1, 0
        while i < len(fmt):
            ch = fmt[i]
            if ch != "%" or i + 1 == len(fmt):
                out.append(ch)
                i += 1
                continue
            spec = fmt[i + 1]
            i += 2
            if spec == "%":
                out.append("%")
                continue
            a = self._args(argi + 1)[argi]
            argi += 1
            if spec == "d":
                out.append(str(a - (1 << 32) if a >> 31 else a))
            elif spec == "u":
                out.append(str(a))
            elif spec == "x":
                out.append(f"{a:x}")
            elif spec == "c":
                out.append(chr(a & 0xFF))
            elif spec == "s":
                out.append(cpu.cstr(a))
            else:
                out.append("%" + spec)
        self.logs.append("".join(out).rstrip("\n"))

    def _api_fail(self, cpu):
        self.failed = cpu.cstr(cpu.r[0])

    def _api_time_us(self, cpu):
        return self.now_us & 0xFFFFFFFF, self.now_us >> 32

    def _api_time_ms(self, cpu):
        return (self.now_us // 1000) & 0xFFFFFFFF

    def _claim(self, kind, key) -> int:
        if key in self.claims[kind]:
            return 0
        self.claims[kind].add(key)
        return 1

    def _api_claim_pin(self, cpu):
        g = cpu.r[0] & 0xFF
        if g > 29 or self.pins_used >> g & 1:
            return 0
        return self._claim("pin", g)

    def _api_claim_pio_sm(self, cpu):
        return self._claim("pio_sm", (cpu.r[0] & 0xFF, cpu.r[1] & 0xFF)) if cpu.r[0] < 2 and cpu.r[1] < 4 else 0

    def _api_claim_pio_program(self, cpu):
        return 0 if (cpu.r[2] & 0xFF) <= 32 else 0xFFFFFFFF

    def _api_claim_dma(self, cpu):
        for ch in range(4, 12):                       # the firmware keeps 0..3
            if self._claim("dma", ch):
                return ch
        return 0xFFFFFFFF

    def _api_claim_alarm(self, cpu):
        for a in (2, 3):
            if self._claim("alarm", a):
                return a
        return 0xFFFFFFFF

    # Buses and interrupts (firmware 0.9.91, core 0.12): the same rules as the firmware. Devices on I2C are what
    # `i2c` holds (address -> bytes it answers with); anything else doesn't answer (NAK). SPI reads 0xFF.
    @staticmethod
    def _unit_pins_ok(kind: int, unit: int, a: int, b: int) -> bool:
        if a > 29 or b > 29 or a == b:
            return False
        if kind == 0:
            return (a % 4 >= 2) == bool(unit) and a % 4 == (2 if unit else 0) and b % 4 == (3 if unit else 1)
        if kind == 1:
            return (a // 8) % 2 == unit and (b // 8) % 2 == unit and a % 4 == 2 and b % 4 == 3
        if kind == 2:
            return ((a + 4) // 8) % 2 == unit and ((b + 4) // 8) % 2 == unit and a % 4 == 0 and b % 4 == 1
        return False

    def _api_claim_bus(self, cpu):
        kind, unit, a, b, hz = (x & 0xFF if i < 4 else x for i, x in enumerate(self._args(5)))
        if kind > 2 or unit > 1 or not self._unit_pins_ok(kind, unit, a, b):
            return 0
        for k, u, pa, pb, _ in self.claims["bus"]:
            if (k, u) == (kind, unit):
                return 1 if (pa, pb) == (a, b) else 0
        used = lambda g: bool(self.pins_used >> g & 1)
        if used(a) or used(b):                       # the controller's own pins: only its I2C can be shared
            if kind != 0 or not (used(a) and used(b)):
                return 0
            self.claims["bus"].add((kind, unit, a, b, "shared"))
            return 1
        for g in (a, b):
            if g not in self.claims["pin"]:
                self.claims["pin"].add(g)
        if not hz:
            return 0
        self.claims["bus"].add((kind, unit, a, b, hz))
        return 1

    def _has_bus(self, kind: int, unit: int) -> bool:
        return any((k, u) == (kind, unit) for k, u, *_ in self.claims["bus"])

    def _api_i2c_xfer(self, cpu):
        unit, addr, tx, tn, rx, rn = self._args(6)
        unit &= 0xFF; addr &= 0xFF
        if unit > 1 or addr > 0x7F or not self._has_bus(0, unit) or (not tn and not rn) or tn > 256 or rn > 256:
            return 0xFFFFFFFF
        if any((k, u, how) == (0, unit, "shared") for k, u, _, _, how in self.claims["bus"]):
            # the controller's own I2C (firmware 0.9.91 review fixes): refused while it isn't up or is a target
            if self.link_board is not None:
                self._warn_once("i2c_xfer on the controller's I2C on a linked node: refused (a neck board's bus is a "
                                "target, the hub drives it)")
                return 0xFFFFFFFF
            if self.in_init and self.board_id == "hammeron":
                self._warn_once("i2c_xfer on the controller's I2C in init: refused (Hammer-On starts its bus after the "
                                "mods' init; do I2C from task)")
                return 0xFFFFFFFF
        if addr not in self.i2c:
            return 0xFFFFFFFF                         # nothing answers there
        if tn:
            self.i2c_log.append((addr, bytes(cpu.read_bytes(tx, tn))))
        if not rn:
            return tn
        data = bytes(self.i2c[addr])
        cpu.write_bytes(rx, (data * (rn // max(1, len(data)) + 1))[:rn] if data else b"\xff" * rn)
        return rn

    def _api_spi_xfer(self, cpu):
        unit, cs, tx, rx, n = self._args(5)
        unit &= 0xFF; cs &= 0xFF
        if unit > 1 or not self._has_bus(1, unit) or not n or n > 4096 or (not tx and not rx):
            return 0xFFFFFFFF
        if cs != 0xFF and cs not in self.claims["pin"]:
            return 0xFFFFFFFF
        if rx:
            cpu.write_bytes(rx, b"\xff" * n)
        return n

    IRQ_NAMES = {0: "TIMER_IRQ_0", 1: "TIMER_IRQ_1", 2: "TIMER_IRQ_2", 3: "TIMER_IRQ_3", 7: "PIO0_IRQ_0", 8: "PIO0_IRQ_1",
                 9: "PIO1_IRQ_0", 10: "PIO1_IRQ_1", 11: "DMA_IRQ_0", 12: "DMA_IRQ_1", 13: "IO_IRQ_BANK0", 18: "SPI0_IRQ",
                 19: "SPI1_IRQ", 20: "UART0_IRQ", 21: "UART1_IRQ", 23: "I2C0_IRQ", 24: "I2C1_IRQ"}

    def _irq_allowed(self, irq: int) -> bool:
        if irq <= 3:
            return irq in self.claims["alarm"]
        if irq in (7, 8, 9, 10):
            return any(p == (0 if irq < 9 else 1) for p, _ in self.claims["pio_sm"])
        if irq in (11, 12):
            return bool(self.claims["dma"])
        if irq == 13:
            return bool(self.claims["pin"])
        kind_unit = {23: (0, 0), 24: (0, 1), 18: (1, 0), 19: (1, 1), 20: (2, 0), 21: (2, 1)}.get(irq)
        if kind_unit:
            return any((k, u) == kind_unit and how != "shared" for k, u, _, _, how in self.claims["bus"])
        return False

    def _warn_once(self, msg: str):
        if msg not in self.warnings:
            self.warnings.append(msg)

    def _api_irq_add(self, cpu):
        irq, handler = cpu.r[0] & 0xFF, cpu.r[1]
        if not self.in_init or not handler or len(self.irqs) >= 8 or not self._irq_allowed(irq):
            return 0
        self.irqs.append(irq)
        msg = "interrupt handlers aren't run by the simulator (irq_add was accepted)"
        if msg not in self.warnings:
            self.warnings.append(msg)
        return 1

    def _api_adc_read(self, cpu):
        inp = cpu.r[0] & 0xFF
        if inp == 4:                                  # the chip's temperature sensor: no claim needed
            return self.adc.get(4, 876)               # about 27 C
        if inp > 4 or 26 + inp not in self.claims["pin"]:
            return 0xFFFFFFFF
        return self.adc.get(inp, 2048)

    def _api_settings_read(self, cpu):
        out, mx = cpu.r[0], cpu.r[1]
        n = min(mx, len(self.settings))
        cpu.write_bytes(out, bytes(self.settings[:n]))
        return n

    def _api_settings_write(self, cpu):
        data, n = cpu.r[0], cpu.r[1]
        if n > SETTINGS_MAX:
            return 0
        self.settings = bytearray(cpu.read_bytes(data, n))
        self.settings_writes += 1
        return 1

    def _api_usb_add(self, cpu):
        ptr, num_out, eps_out = cpu.r[0], cpu.r[1], cpu.r[2]
        if self.usb_mode not in (1, 2, 3):
            self.warnings.append("usb_add called but the mod's usb_mode is PCS_USB_NONE: refused")
            return 0
        if not self.in_init:
            self.warnings.append("usb_add called outside init: refused")
            return 0
        if self.usb_mode == 3 and self.cur_build == 0:
            self.warnings.append("usb_add in conditional mode before usb_personality_add: refused")
            return 0
        raw_list = next(p["raw"] for p in self.personalities if p["id"] == self.cur_build) if self.usb_mode == 3 else self.usb_raw
        total = sum(len(p["raw"]) for p in self.personalities) if self.usb_mode == 3 else len(raw_list)
        if total >= usbhost.MAX_MOD_IFACES:            # conditional: 8 interfaces across all personalities
            self.warnings.append(f"more than {usbhost.MAX_MOD_IFACES} USB interfaces: refused")
            return 0
        raw = cpu.read_bytes(ptr, USB_IFACE_SIZE)
        n = raw[3]
        eps_raw = [struct.unpack_from("<BBHB", raw, 4 + USB_EP_SIZE * k) for k in range(min(n, 4))]
        why = ("ep_count is more than 4" if n > 4 else
               "an endpoint's max packet isn't 8 to 64" if any(not 8 <= sz <= 64 for _, _, sz, _ in eps_raw) else
               "an endpoint type isn't bulk or interrupt" if any(t not in (2, 3) for _, t, _, _ in eps_raw) else
               "a HID interface needs an interrupt IN endpoint" if raw[0] == 3 and not any(i and t == 3 for i, t, _, _ in eps_raw)
               else "")
        if why:                                      # the firmware refuses these at usb_add (core 0.11)
            self.warnings.append(f"usb_add refused: {why}")
            return 0
        first_if = 1 if self.usb_mode == 1 else 0      # Add keeps the game pad as interface 0; Replace/each personality own all
        first_ep = 3 if self.usb_mode == 1 else 1
        num = first_if + len(raw_list)
        used = sum(len(a) for _, (_, a) in raw_list)
        addrs = []
        for k in range(n):
            e_in = raw[4 + USB_EP_SIZE * k]
            epn = first_ep + used + k
            if epn > usbhost.MAX_EP_NUM - 1:            # keep one for the settings link
                self.warnings.append("out of USB endpoints: refused")
                return 0
            addrs.append((0x80 if e_in else 0) | epn)
        raw_list.append((ptr, (num, addrs)))
        if num_out:
            cpu.write(num_out, 1, num)
        if eps_out and addrs:
            cpu.write_bytes(eps_out, bytes(addrs))
        return 1

    def _api_usb_ready(self, cpu):
        return int(self.usb_ready)

    def _api_usb_send(self, cpu):
        ep, data, n = cpu.r[0] & 0xFF, cpu.r[1], cpu.r[2] & 0xFFFF
        e = self._mod_ep(ep)
        if not self.usb_ready or e is None or not ep & 0x80:
            if not self.usb_ready:
                return 0
            raise Fault(f"usb_send to {ep:#04x}, which isn't one of the mod's IN endpoints")
        if n > e.size:
            raise Fault(f"usb_send of {n} bytes on {ep:#04x}, whose max packet is {e.size}")
        if ep in self.usb_pending:
            return 0                                   # the computer hasn't read the last one yet
        payload = cpu.read_bytes(data, n)
        if ep in self.hid:
            want = self.hid[ep].input_len(payload[0] if n and 0 not in self.hid[ep].fields else 0)
            if n != want:
                msg = f"report on {ep:#04x} is {n} bytes; its report descriptor says {want}"
                if msg not in self.warnings:
                    self.warnings.append(msg)
        self.usb_pending[ep] = payload
        return 1

    def _api_usb_control_reply(self, cpu):
        if not self.in_setup:
            raise Fault("usb_control_reply outside on_usb_setup")
        self.setup_reply = cpu.read_bytes(cpu.r[0], cpu.r[1] & 0xFFFF)
        return 1

    # ------------------------------------------------------------------ conditional USB (API 0.3)
    def _api_usb_personality_add(self, cpu):
        if self.usb_mode != 3:
            self.warnings.append("usb_personality_add but usb_mode isn't PCS_USB_CONDITIONAL: refused")
            return 0xFFFFFFFF
        if not self.in_init:
            self.warnings.append("usb_personality_add outside init: refused")
            return 0xFFFFFFFF
        if len(self.personalities) >= MAX_PERSONALITIES:
            self.warnings.append(f"more than {MAX_PERSONALITIES} USB personalities: refused")
            return 0xFFFFFFFF
        dev = cpu.read_bytes(cpu.r[0], 24) if cpu.r[0] else b""   # pcs_usb_device_t (fields defaulted/validated firmware-side)
        pid = len(self.personalities) + 1
        self.personalities.append({"id": pid, "dev": dev, "raw": []})
        self.cur_build = pid
        return pid

    def _api_usb_host(self, cpu):
        return self._host_struct()

    def _api_usb_switch(self, cpu):
        tid = cpu.r[0]
        if self.in_init:                               # switch from on_usb_host or task, never init
            self.warnings.append("usb_switch from init: refused (switch from on_usb_host or task)")
            return 0
        if tid >= 0x80000000:                          # negative id
            return 0
        ids = {0} | {p["id"] for p in self.personalities}
        if self.pending_switch is not None:
            return 0                                    # a switch is already under way
        if tid not in ids or tid == self.live or tid in self.empty_personalities:
            return 0                                    # unknown, already live, or a personality with no interfaces
        now = self.now_us // 1000
        self.switch_times = [t for t in self.switch_times if now - t < SWITCH_WINDOW_MS]
        if len(self.switch_times) >= 4:                 # at most 4 switches per 10 s
            self.warnings.append("more than 4 usb_switch calls in 10 s: refused")
            return 0
        self.switch_times.append(now)
        self.pending_switch = tid                        # applied from the main loop (see _apply_pending)
        return 1

    def _api_usb_current(self, cpu):
        return self.live

    # --------------------------------------------------------- core-owned device services (API 0.4)
    def _api_usb_service_claim(self, cpu):
        ptr = cpu.r[0]
        if not self.in_init or self.usb_mode != 0 or self.service_provider or not ptr:
            return 0
        size, service, reserved, handler = struct.unpack("<HBBI", cpu.read_bytes(ptr, 8))
        if size != 8 or service != 1 or reserved or not handler:
            return 0
        self.service_provider = handler
        return 1

    # ------------------------------------------------------------------ entropy (API 0.5)
    def _api_random_fill(self, cpu):
        ptr, length = cpu.r[0], cpu.r[1]
        if not ptr or not length or length > 64:
            return 0
        out = bytearray()
        while len(out) < length:
            x = self.random_state
            x ^= (x << 13) & 0xFFFFFFFF
            x ^= x >> 17
            x ^= (x << 5) & 0xFFFFFFFF
            self.random_state = x & 0xFFFFFFFF
            out += struct.pack("<I", self.random_state)
        cpu.write_bytes(ptr, out[:length])
        return 1

    def service_control(self, bm_request_type: int, request: int, value: int, index: int,
                        data: bytes = b"", in_capacity: int = 0) -> bytes | None:
        """Call an API 0.4 device-service provider as the firmware would. None means STALL."""
        if not self.initialized:
            self.init()
        if not self.service_provider or max(len(data), in_capacity) > 64:
            return None
        is_in = bool(bm_request_type & 0x80)
        length, capacity = (0, in_capacity) if is_in else (len(data), 0)
        setup_len = in_capacity if is_in else len(data)
        req = struct.pack("<BBHHBBHHH64s", 0, 0, length, capacity, bm_request_type, request,
                          value, index, setup_len, data.ljust(64, b"\0"))
        if not self.service_req_addr:
            self.service_req_addr = self._alloc(len(req))
            self.service_reply_addr = self._alloc(64)
        req_addr, reply_addr = self.service_req_addr, self.service_reply_addr
        self.cpu.write_bytes(req_addr, req)
        self.cpu.write_bytes(reply_addr, b"\0" * 64)
        n = self._call("usb_service", self.service_provider, [req_addr, reply_addr])
        if n & 0x80000000 or n > capacity:
            return None
        return self.cpu.read_bytes(reply_addr, n) if is_in else (b"" if n == 0 else None)

    def service_reset(self) -> bool:
        """Deliver the device-service RESET event. False means no provider or provider failure."""
        if not self.initialized:
            self.init()
        if not self.service_provider:
            return False
        req = struct.pack("<BBHHBBHHH64s", 1, 0, 0, 0, 0, 0, 0, 0, 0, b"\0" * 64)
        if not self.service_req_addr:
            self.service_req_addr = self._alloc(len(req))
            self.service_reply_addr = self._alloc(64)
        req_addr, reply_addr = self.service_req_addr, self.service_reply_addr
        self.cpu.write_bytes(req_addr, req)
        self.cpu.write_bytes(reply_addr, b"\0" * 64)
        n = self._call("usb_service", self.service_provider, [req_addr, reply_addr])
        return n == 0

    def _api_link_nodes(self, cpu):
        out, mx = cpu.r[0], cpu.r[1] & 0xFF
        if not self.link:
            return 0
        rows = self.link.nodes_for(self)[:mx]
        for k, (num, role, mid, serial) in enumerate(rows):
            cpu.write_bytes(out + NODE_FMT.size * k, NODE_FMT.pack(num, role, mid.encode()[:23], serial))
        return len(rows)

    def _api_link_send(self, cpu):
        node, ch, data, n = cpu.r[0] & 0xFF, cpu.r[1] & 0xFF, cpu.r[2], cpu.r[3] & 0xFF
        if not linkbus.CH_MOD0 <= ch <= linkbus.CH_MAX or n > linkbus.MSG_MAX:
            msg = f"link_send on channel {ch} with {n} bytes refused (mods: channels 8-15, 60 bytes max)"
            if msg not in self.warnings:
                self.warnings.append(msg)
            return 0
        if not self.link:
            return 0
        return int(self.link.send_from(self, node, ch, cpu.read_bytes(data, n)))

    def _api_rom_float(self, cpu):                    # never called: the table holds NULL
        return 0

    def _api_memcpy(self, cpu):
        d, s, n = cpu.r[0], cpu.r[1], cpu.r[2]
        cpu.write_bytes(d, cpu.read_bytes(s, n))
        return d

    def _api_memset(self, cpu):
        d, c, n = cpu.r[0], cpu.r[1], cpu.r[2]
        cpu.write_bytes(d, bytes([c & 0xFF]) * n)
        return d


class LinkNet:
    """Linked boards: a hub board and nodes on the link bus, each with its own mod or none.

        net = LinkNet(hub_sim, [(node_sim, unique_id), (None, unique_id_of_a_plain_neck)])
        net.run(1000)
    """

    def __init__(self, hub: ModSim | None, nodes: list[tuple[ModSim | None, bytes]], v0: tuple[int, ...] = ()):
        self.bus = linkbus.Bus()
        self.hub_sim = hub
        self.hub = linkbus.Hub(self.bus)
        self.node_sims: list[tuple[ModSim | None, linkbus.Node]] = []
        for i, (sim, uid) in enumerate(nodes):
            module = sim.board_id if sim else "hammeron"
            node = linkbus.Node(uid, module_id=module, v1=i not in v0)
            self.bus.nodes.append(node)
            self.node_sims.append((sim, node))
            if sim:
                sim.link, sim.link_board = self, node
        if hub:
            hub.link = self
        self.t_us = 0
        self.log: list[str] = []
        self.polled = [False] * len(self.node_sims)   # each node: is a hub polling it (its on_node for node 0)

    def nodes_for(self, sim: ModSim) -> list[tuple[int, int, str, int]]:
        if sim is self.hub_sim:
            return [(num, 1, p.module_id, p.ident) for num, p in self.hub.nodes()]
        for s, node in self.node_sims:
            if s is sim and any(p.ident == node.ident for _, p in self.hub.nodes()):
                return [(0, 0, self.hub_sim.board_id if self.hub_sim else "hub", 0)]
        return []

    def send_from(self, sim: ModSim, node: int, ch: int, data: bytes) -> bool:
        if sim is self.hub_sim:
            return self.hub.send(node, ch, data)
        return node == 0 and sim.link_board is not None and sim.link_board.send(ch, data)

    def _deliver(self, sim: ModSim | None, node: int, ch: int, data: bytes, who: str):
        self.log.append(f"{self.t_us // 1000:6} ms  {who} got channel {ch} from {'hub' if node == 0 else f'node {node}'}: {data.hex()}")
        if sim and "on_link" in sim.hooks:
            sim.cpu.write_bytes(sim.scratch + 8, data)
            sim.hook("on_link", node, ch, sim.scratch + 8, len(data))

    def tick(self, ms: int = 1):
        for _ in range(ms):
            self.hub.poll(self.t_us)
            self.t_us += 1000
            for num, ch, data in self.hub.received:
                self._deliver(self.hub_sim, num, ch, data, "hub")
            self.hub.received.clear()
            for num, joined in self.hub.events:
                p = self.hub.peer(num)
                self.log.append(f"{self.t_us // 1000:6} ms  node {num} {'joined' if joined else 'left'}"
                                + (f" ({p.module_id or 'older neck firmware'})" if p else ""))
                if self.hub_sim and "on_node" in self.hub_sim.hooks:
                    mid = (p.module_id if p else "").encode()[:23]
                    self.hub_sim.cpu.write_bytes(self.hub_sim.scratch, NODE_FMT.pack(num, 1, mid, p.ident if p else 0))
                    self.hub_sim.hook("on_node", self.hub_sim.scratch, int(joined))
            self.hub.events.clear()
            known = {p.ident for _, p in self.hub.nodes()}
            for k, (sim, node) in enumerate(self.node_sims):
                now = node.ident in known
                if now != self.polled[k]:                     # a node's mod hears about the hub as node 0
                    self.polled[k] = now
                    if sim and "on_node" in sim.hooks:
                        hub_id = (self.hub_sim.board_id if self.hub_sim else "hub").encode()[:23]
                        sim.cpu.write_bytes(sim.scratch, NODE_FMT.pack(0, 0, hub_id, 0))
                        sim.hook("on_node", sim.scratch, int(now))
                for ch, data in node.inbox:
                    self._deliver(sim, 0, ch, data, f"node board {k + 1}")
                node.inbox.clear()

    def sims(self) -> list[ModSim]:
        return [s for s in [self.hub_sim] + [s for s, _ in self.node_sims] if s]

    def run(self, ms: int, frame_ms: int = 10):
        for s in self.sims():
            if not s.initialized:
                s.init()
        for _ in range(0, ms, frame_ms):
            for s in self.sims():
                s.task()
                s.on_inputs()
                s.on_leds()
                s.usb_poll()
                s.advance(frame_ms)
            self.tick(frame_ms)


class AirNet:
    """The Polylink link (firmware 0.9.95 / Polylink 0.3.37): a Polylink running one mod and up to 4 controllers
    connected to it, each with its own mod or none, using the same linked-board mod API:
    on the Polylink the controllers are nodes 16-19 (slots 1-4, role 2), on a controller the Polylink is node 20
    (role 3). Channels 8-15, 60 bytes, 4 messages waiting each way per controller; delivered on the next frame.

        net = AirNet(polylink_sim, [guitar_sim, None])
        net.run(1000)
    """
    QUEUE = 4

    def __init__(self, polylink: ModSim | None, controllers: list[ModSim | None], join_ms: int = 300):
        self.pl = polylink
        self.ctl = list(controllers)[:4]
        self.join_ms = join_ms                        # controllers connect a moment after start
        self.up = [False] * len(self.ctl)
        self.q: list[list[tuple[int, int, bytes]]] = [[] for _ in self.ctl]   # to deliver: (to_polylink, ch, data)
        self.t_ms = 0
        self.log: list[str] = []
        for s in [polylink] + self.ctl:
            if s:
                s.link = self

    def nodes_for(self, sim: ModSim) -> list[tuple[int, int, str, int]]:
        if sim is self.pl:
            return [(16 + i, 2, "controller", 0) for i, u in enumerate(self.up) if u]
        for i, s in enumerate(self.ctl):
            if s is sim and self.up[i]:
                return [(20, 3, "polylink", 0)]
        return []

    def send_from(self, sim: ModSim, node: int, ch: int, data: bytes) -> bool:
        if sim is self.pl:
            i = node - 16
            if not 0 <= i < len(self.ctl) or not self.up[i]:
                return False
            to_pl = False
        else:
            i = next((k for k, s in enumerate(self.ctl) if s is sim), -1)
            if i < 0 or node != 20 or not self.up[i]:
                return False
            to_pl = True
        if not data or sum(1 for t, _, _ in self.q[i] if t == to_pl) >= self.QUEUE:
            return False
        self.q[i].append((to_pl, ch, bytes(data)))
        return True

    def _node_event(self, sim: ModSim | None, num: int, role: int, mid: str, joined: bool):
        if sim and "on_node" in sim.hooks:
            sim.cpu.write_bytes(sim.scratch, NODE_FMT.pack(num, role, mid.encode()[:23], 0))
            sim.hook("on_node", sim.scratch, int(joined))

    def _deliver(self, sim: ModSim | None, node: int, ch: int, data: bytes, who: str):
        self.log.append(f"{self.t_ms:6} ms  {who} got channel {ch} from node {node}: {data.hex()}")
        if sim and "on_link" in sim.hooks:
            sim.cpu.write_bytes(sim.scratch + 8, data)
            sim.hook("on_link", node, ch, sim.scratch + 8, len(data))

    def tick(self, ms: int):
        self.t_ms += ms
        for i, s in enumerate(self.ctl):
            if not self.up[i] and self.t_ms >= self.join_ms * (i + 1):
                self.up[i] = True
                self.log.append(f"{self.t_ms:6} ms  slot {i + 1} connected ({s.board_id if s else 'no mod'})")
                self._node_event(self.pl, 16 + i, 2, "controller", True)
                self._node_event(s, 20, 3, "polylink", True)
        for i, s in enumerate(self.ctl):
            pending, self.q[i] = self.q[i], []
            for to_pl, ch, data in pending:
                if to_pl:
                    self._deliver(self.pl, 16 + i, ch, data, "polylink")
                else:
                    self._deliver(s, 20, ch, data, f"slot {i + 1}")

    def sims(self) -> list[ModSim]:
        return [s for s in [self.pl] + self.ctl if s]

    def run(self, ms: int, frame_ms: int = 10):
        for s in self.sims():
            if not s.initialized:
                s.init()
        for _ in range(0, ms, frame_ms):
            for s in self.sims():
                s.task()
                s.on_inputs()
                s.on_leds()
                s.usb_poll()
                s.advance(frame_ms)
            self.tick(frame_ms)


def parse_presses(spec: list[str]) -> list[tuple[int, int, int]]:
    """'green@500-900' -> (bit, start_ms, end_ms). Names as in pcs_mod.h without PCS_BTN_."""
    names = {"green": 0, "red": 1, "yellow": 2, "blue": 3, "orange": 4, "strum_up": 5, "strum_down": 6, "start": 7,
             "select": 8, "guide": 9, "stick": 10}
    out = []
    for s in spec:
        try:
            name, rng = s.lower().split("@")
            a, _, b = rng.partition("-")
            out.append((names[name], int(a), int(b) if b else int(a) + 100))
        except (ValueError, KeyError):
            raise ModError(f"bad --press {s!r}: use name@start-end in ms, e.g. green@500-900") from None
    return out


def run(blob: bytes, board="polybar", ms=3000, frame_ms=10, presses=(), settings=b"", pins_used=0,
        show_every=500, i2c: dict[int, bytes] | None = None, host: int | None = None) -> tuple[ModSim, list[str]]:
    """The `pcsmod.py sim` loop: init, then task / on_inputs / on_leds every frame."""
    sim = ModSim(blob, board, settings, pins_used, i2c=i2c)
    out = []
    sim.init()
    if host is not None:          # conditional USB: a computer with these PCS_HOST_* flags appears, so the mod can switch
        sim.set_host(flags=host)
        out.append(f"     0 ms  host flags {host:#06x} -> personality {sim.live}")
    held = parse_presses(list(presses))
    t = 0
    last: dict[int, str] = {}
    while t <= ms:
        buttons = sum(1 << bit for bit, a, b in held if a <= t < b)
        sim.task()
        sim.on_inputs(buttons)
        leds = sim.on_leds()
        if show_every and t % show_every == 0:
            px = " ".join(f"{r:02x}{g:02x}{b_:02x}" for r, g, b_ in leds) or "(no lights)"
            out.append(f"{t:6} ms  buttons {buttons:04x}  {px}")
        for ep, data in sim.usb_poll():
            text = sim.decode(ep, data)
            if ep in sim.midi_eps or last.get(ep) != text:     # HID: only changes; MIDI: every event
                out.append(f"{t:6} ms  usb {ep:#04x}  {text}")
                last[ep] = text
        t += frame_ms
        sim.advance(frame_ms)
    return sim, out
