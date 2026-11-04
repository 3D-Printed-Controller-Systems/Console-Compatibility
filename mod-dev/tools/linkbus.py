"""linkbus: reference model of the 3DPCS linked-board bus. MIT License.

Pure logic, no hardware: a Hub and Nodes talk through a Bus that behaves like I2C with open-drain wires (two targets
answering one address garble each other's bytes). The firmware's C code and this model must agree on every frame;
tests/vectors/linkbus.json holds worked examples for anyone implementing the bus.

Bus v1 grows the neck link that runs on double / triple neck guitars today, and stays compatible with it: a v0 neck
answers polls, shows the lights it's sent, moves when told, and takes firmware updates; a v1 node also carries mod
messages (channels 8-15) and its full inputs (channel 1).
"""
from __future__ import annotations

from dataclasses import dataclass, field

ADDR_BASE = 0x50
ADDRS = 16
ANSWER_LEN = 8
MAGIC_ANSWER = 0x4E
MSG_MAX = 60
READ_MAX = 64                     # a fetched message is read as 64 bytes, padded with 0xFF
POLL_US = 1000
SCAN_US = 10000
LOST_US = 300_000

CMD_LIGHTS = 0x4C                 # 'L'
CMD_MOVE = 0x4D                   # 'M'
CMD_MSG = 0x58                    # 'X' (v1): a message to the node / a fetched message from it
CMD_FETCH = 0x47                  # 'G' (v1): the next read is the node's waiting message
# firmware updates (unchanged from v0): 'U' size crc32, 'D' chunk, 'W' sector crc32, 'C' finish, 'A' abort
CMD_UPDATE = {0x55: "start", 0x44: "data", 0x57: "write", 0x43: "finish", 0x41: "abort"}

IDX_WAITING = 0x80                # answer byte 3: a v1 node has a message waiting. Until a hub has fetched once, a node
                                  # sets it only on its index-7 answer: older hubs skip that one, so they still learn
                                  # every identity byte
CAP_V1 = 0x01                     # info byte 7 (capabilities)

CH_HELLO, CH_INPUTS, CH_LIGHTS, CH_UPDATE = 0, 1, 2, 3
CH_MOD0, CH_MAX = 8, 15


def check(b: bytes) -> int:
    x = 0xA5
    for v in b:
        x ^= v
    return x


def framed(b: bytes) -> bytes:
    return b + bytes([check(b)])


def serial_hash(unique_id: bytes) -> int:
    """FNV-1a over the flash chip's 8-byte unique id; never 0."""
    h = 2166136261
    for v in unique_id:
        h = ((h ^ v) * 16777619) & 0xFFFFFFFF
    return h or 1


def addr_for(ident: int, seed: int) -> int:
    h = (ident ^ ((seed * 0x9E3779B1) & 0xFFFFFFFF)) & 0xFFFFFFFF
    h ^= h >> 15
    h = (h * 0x2C1B3C6D) & 0xFFFFFFFF
    h ^= h >> 12
    return ADDR_BASE + h % ADDRS


def answer(buttons: int, index: int, value: int, ustate: int = 0, uarg: int = 0, waiting: bool = False) -> bytes:
    return framed(bytes([MAGIC_ANSWER, buttons & 0xFF, (buttons >> 8) & 0xFF,
                         (index & 7) | (IDX_WAITING if waiting else 0), value & 0xFF, ustate & 0xFF, uarg & 0xFF]))


def lights_frame(px: list[tuple[int, int, int, int]]) -> bytes:
    if not 1 <= len(px) <= 32:
        raise ValueError("1 to 32 lights")
    return framed(bytes([CMD_LIGHTS, len(px)]) + b"".join(bytes([r, g, b, br & 31]) for r, g, b, br in px))


def msg_frame(channel: int, payload: bytes) -> bytes:
    if not 0 <= channel <= CH_MAX or len(payload) > MSG_MAX:
        raise ValueError("channel 0..15, payload up to 60 bytes")
    return framed(bytes([CMD_MSG, channel, len(payload)]) + payload)


def parse_msg(b: bytes) -> tuple[int, bytes] | None:
    if len(b) < 4 or b[0] != CMD_MSG or b[2] > MSG_MAX or len(b) < 4 + b[2]:
        return None
    n = 3 + b[2]
    if check(b[:n]) != b[n] or b[1] > CH_MAX:
        return None
    return b[1], bytes(b[3:n])


def hello_payload(version: tuple[int, int, int], module_id: str, role_flags: int = 0) -> bytes:
    """Channel 0: firmware version, flags, module id (zero padded to 24)."""
    m = module_id.encode()[:23]
    return bytes([*version, role_flags]) + m + bytes(24 - len(m))


# ---------------------------------------------------------------- the wires

class Bus:
    """I2C with open-drain wires: every target on an address drives the same read; bytes AND together."""

    def __init__(self):
        self.nodes: list[Node] = []
        self.reads = self.writes = 0

    def write(self, addr: int, data: bytes) -> bool:
        self.writes += 1
        hit = [n for n in self.nodes if n.addr == addr]
        for n in hit:
            n.on_write(bytes(data))
        return bool(hit)

    def read(self, addr: int, n: int) -> bytes | None:
        self.reads += 1
        hit = [nd for nd in self.nodes if nd.addr == addr]
        if not hit:
            return None
        out = bytearray(b"\xFF" * n)
        for nd in hit:
            b = nd.on_read(n)
            for i in range(n):
                out[i] &= b[i]
        return bytes(out)


# ---------------------------------------------------------------- a node (neck or any linked board)

@dataclass
class Node:
    unique_id: bytes
    version: tuple[int, int, int] = (0, 10, 0)
    module_id: str = "hammeron"
    v1: bool = True                           # False = a neck on the older firmware (no messages)
    buttons: int = 0
    seed: int = 0
    lights: list = field(default_factory=list)
    inbox: list[tuple[int, bytes]] = field(default_factory=list)      # messages from the hub
    outbox: list[tuple[int, bytes]] = field(default_factory=list)     # messages waiting for the hub
    updates: list[tuple[str, bytes]] = field(default_factory=list)

    def __post_init__(self):
        self.ident = serial_hash(self.unique_id)
        self.addr = addr_for(self.ident, 0)
        self.info = list(self.ident.to_bytes(4, "little")) + list(self.version) + [CAP_V1 if self.v1 else 0]
        self.info_i = 0
        self.fetch = False
        self.fetched_once = False             # until a hub has fetched, "waiting" rides only on the index-7 answer
        if self.v1:
            self.outbox.append((CH_HELLO, hello_payload(self.version, self.module_id)))

    def send(self, channel: int, payload: bytes) -> bool:
        if not self.v1 or len(self.outbox) >= 4:
            return False
        self.outbox.append((channel, bytes(payload)))
        return True

    def on_read(self, n: int) -> bytes:
        if self.fetch and self.outbox:
            self.fetch = False
            ch, p = self.outbox.pop(0)
            return (msg_frame(ch, p) + b"\xFF" * READ_MAX)[:n]
        self.fetch = False
        waiting = self.v1 and bool(self.outbox) and (self.fetched_once or self.info_i == 7)
        a = answer(self.buttons, self.info_i, self.info[self.info_i], waiting=waiting)
        self.info_i = (self.info_i + 1) & 7
        return (a + b"\xFF" * n)[:n]

    def on_write(self, b: bytes):
        if len(b) < 2 or check(b[:-1]) != b[-1]:
            return
        c = b[0]
        if c == CMD_MOVE and len(b) == 3:
            self.seed = b[1]
            self.addr = addr_for(self.ident, self.seed)
        elif c == CMD_LIGHTS and len(b) == 3 + 4 * b[1]:
            self.lights = [tuple(b[2 + 4 * i:6 + 4 * i]) for i in range(b[1])]
        elif c in CMD_UPDATE:
            self.updates.append((CMD_UPDATE[c], b))
        elif self.v1 and c == CMD_MSG:
            m = parse_msg(b)
            if m:
                self.inbox.append(m)
        elif self.v1 and c == CMD_FETCH and len(b) == 2:
            self.fetch = True
            self.fetched_once = True


# ---------------------------------------------------------------- the hub (the board the computer set up)

@dataclass
class Peer:
    addr: int
    last_us: int
    ok_us: int
    buttons: int = 0
    info: list = field(default_factory=lambda: [0] * 8)
    have: int = 0
    bad: int = 0
    hello: bytes = b""
    outq: list = field(default_factory=list)

    @property
    def ready(self) -> bool:
        return self.have == 0xFF

    @property
    def ident(self) -> int:
        return int.from_bytes(bytes(self.info[:4]), "little")

    @property
    def v1(self) -> bool:
        return bool(self.info[7] & CAP_V1)

    @property
    def module_id(self) -> str:
        return self.hello[4:28].split(b"\0")[0].decode(errors="replace") if self.hello else ""


class Hub:
    """Scans for nodes, polls them, sorts out address clashes, keeps node numbers by identity, moves messages."""

    def __init__(self, bus: Bus, max_nodes: int = 15, known: list[int] | None = None):
        self.bus, self.max = bus, max_nodes
        self.peers: list[Peer] = []
        self.known = list(known or [])          # identities in node-number order (saved like Neck 2 / Neck 3 today)
        self.scan_i, self.scan_us = 0, -SCAN_US
        self.garble = [0] * ADDRS
        self.received: list[tuple[int, int, bytes]] = []    # (node number, channel, payload)
        self.events: list[tuple[int, bool]] = []            # (node number, joined)
        self.seed_counter = 1

    def node_number(self, p: Peer) -> int:
        """1-based, by identity: a board keeps its number across restarts."""
        if p.ident not in self.known:
            self.known.append(p.ident)
        return self.known.index(p.ident) + 1

    def peer(self, number: int) -> Peer | None:
        for p in self.peers:
            if p.ready and self.node_number(p) == number:
                return p
        return None

    def send(self, number: int, channel: int, payload: bytes) -> bool:
        p = self.peer(number)
        if not p or not p.v1 or len(p.outq) >= 4 or len(payload) > MSG_MAX:
            return False
        p.outq.append(msg_frame(channel, payload))
        return True

    def send_lights(self, number: int, px) -> bool:
        p = self.peer(number)
        return bool(p) and self.bus.write(p.addr, lights_frame(px))

    def _move(self, addr: int):
        self.seed_counter = (self.seed_counter * 7 + 3) & 0xFF or 1
        self.bus.write(addr, framed(bytes([CMD_MOVE, self.seed_counter])))

    def _drop(self, p: Peer):
        self.peers.remove(p)
        if p.ready:
            self.events.append((self.node_number(p), False))

    def poll(self, now_us: int):
        for p in list(self.peers):
            if now_us - p.last_us < POLL_US:
                continue
            p.last_us = now_us
            if p.outq:                                   # one message to it per poll slot
                self.bus.write(p.addr, p.outq.pop(0))
            f = self.bus.read(p.addr, ANSWER_LEN)
            if f and f[0] == MAGIC_ANSWER and check(f[:7]) == f[7]:
                p.buttons, p.ok_us, p.bad = f[1] | f[2] << 8, now_us, 0
                i = f[3] & 7
                was = p.ready
                p.info[i], p.have = f[4], p.have | (1 << i)
                if p.ready and not was:
                    if not p.v1:
                        self.events.append((self.node_number(p), True))   # a v0 neck: no hello coming
                if f[3] & IDX_WAITING and p.ready:          # fetch only once it's known (first: its hello)
                    self.bus.write(p.addr, framed(bytes([CMD_FETCH])))
                    m = parse_msg(self.bus.read(p.addr, READ_MAX) or b"")
                    if m:
                        ch, data = m
                        if ch == CH_HELLO:
                            first = not p.hello
                            p.hello = data
                            if first:
                                self.events.append((self.node_number(p), True))
                        else:
                            self.received.append((self.node_number(p), ch, data))
            else:
                if f is not None:
                    p.bad += 1
                    if p.bad >= 4:                       # someone else answers here too
                        self._move(p.addr)
                        self._drop(p)
                        continue
                if now_us - p.ok_us > LOST_US:
                    self._drop(p)
        if now_us - self.scan_us >= SCAN_US:              # look at one more address
            self.scan_us = now_us
            i = self.scan_i
            self.scan_i = (self.scan_i + 1) % ADDRS
            a = ADDR_BASE + i
            if not any(p.addr == a for p in self.peers) and len(self.peers) < self.max:
                f = self.bus.read(a, ANSWER_LEN)
                if f and f[0] == MAGIC_ANSWER and check(f[:7]) == f[7]:
                    self.garble[i] = 0
                    self.peers.append(Peer(a, now_us, now_us))
                elif f is not None:
                    self.garble[i] += 1
                    if self.garble[i] >= 2:              # two nodes on one address: both pick again
                        self.garble[i] = 0
                        self._move(a)
                else:
                    self.garble[i] = 0

    def nodes(self) -> list[tuple[int, Peer]]:
        return sorted((self.node_number(p), p) for p in self.peers if p.ready and (p.hello or not p.v1))


def run(hub: Hub, ms: int, start_us: int = 0) -> int:
    t = start_us
    for _ in range(ms):
        hub.poll(t)
        t += 1000
    return t
