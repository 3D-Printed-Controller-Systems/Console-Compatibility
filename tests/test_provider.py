#!/usr/bin/env python3
"""Protocol-level simulator checks for Console Compatibility."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "mod-dev" / "tools"))

from modsim import ModSim  # noqa: E402


def _xor(data: bytes) -> int:
    value = 0
    for byte in data:
        value ^= byte
    return value


def finish(sim: ModSim, limit: int = 256) -> list[int]:
    steps: list[int] = []
    for _ in range(limit):
        before = sim.cpu.steps
        sim.task()
        steps.append(sim.cpu.steps - before)
        if sim.service_control(0xC1, 0x86, 0, 3, in_capacity=2) == b"\x02\x00":
            return steps
    raise AssertionError("provider did not finish its queued challenge")


def main() -> int:
    blob = (ROOT / "pack" / "mods" / "console-compatibility.pcsmod").read_bytes()
    reference_blob = (ROOT / "build" / "compatibility-reference.pcsmod").read_bytes()
    # A raised execution ceiling lets this test finish and measure the current upstream implementation. The report
    # below still compares it with the real 125,000-instruction hook budget.
    sim = ModSim(blob, max_call_steps=10_000_000)
    sim.init()
    reference = ModSim(reference_blob, max_call_steps=10_000_000)
    reference.init()
    assert sim.service_provider, "init did not claim the security service"

    identity = sim.service_control(0xC1, 0x81, 0x5B17, 3, in_capacity=29)
    assert identity is not None and len(identity) == 29
    assert identity[20:24] == bytes.fromhex("30144847"), "security identity must match USB VID/PID 1430:4748"
    assert identity[-1] == 0xEB and identity[-1] == _xor(identity[5:-1]), "security identity checksum"
    assert reference.service_control(0xC1, 0x81, 0x5B17, 3, in_capacity=29) == identity
    assert sim.service_control(0xC1, 0x86, 0, 3, in_capacity=2) == b"\x02\x00"
    assert sim.service_control(0xC1, 0x84, 0, 3, in_capacity=0) == b""
    assert sim.service_control(0x41, 0x84, 0, 3, data=b"") == b""
    assert reference.service_control(0xC1, 0x84, 0, 3, in_capacity=0) == b""
    assert reference.service_control(0x41, 0x84, 0, 3, data=b"") == b""
    assert sim.service_control(0xC1, 0x84, 0, 3, in_capacity=1) is None
    assert sim.service_control(0x41, 0x84, 0, 3, data=b"\0") is None
    assert sim.service_control(0xC1, 0x81, 0x5B17, 2, in_capacity=29) is None
    assert sim.service_control(0x41, 0x82, 0, 3, data=b"\0" * 33) is None

    challenge = bytes(range(34))
    assert sim.service_control(0x41, 0x82, 0, 3, data=challenge) == b""
    assert reference.service_control(0x41, 0x82, 0, 3, data=challenge) == b""
    assert sim.service_control(0xC1, 0x86, 0, 3, in_capacity=2) == b"\x01\x00"
    init_slices = finish(sim)
    reference.task()
    response = sim.service_control(0xC1, 0x83, 0, 3, in_capacity=46)
    assert response is not None and len(response) == 46
    assert reference.service_control(0xC1, 0x83, 0, 3, in_capacity=46) == response, "incremental init response differs from upstream one-shot output"

    verify = bytes(range(22))
    assert sim.service_control(0x41, 0x87, 0, 3, data=verify) == b""
    assert reference.service_control(0x41, 0x87, 0, 3, data=verify) == b""
    assert sim.service_control(0xC1, 0x86, 0, 3, in_capacity=2) == b"\x01\x00"
    verify_slices = finish(sim)
    reference.task()
    response = sim.service_control(0xC1, 0x83, 0, 3, in_capacity=22)
    assert response is not None and len(response) == 22
    assert reference.service_control(0xC1, 0x83, 0, 3, in_capacity=22) == response, "incremental verify response differs from upstream one-shot output"

    assert sim.service_reset()
    assert sim.service_control(0xC1, 0x83, 0, 3, in_capacity=46) is None
    worst = max(init_slices + verify_slices)
    assert worst <= 125_000, f"task slice exceeded the 125,000-instruction hard budget: {worst}"
    print(f"provider protocol: OK; init {len(init_slices)} slices, verify {len(verify_slices)} slices; worst {worst}; stack {sim.max_stack} bytes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
