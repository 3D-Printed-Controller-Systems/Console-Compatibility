#!/usr/bin/env python3
"""Check the in-repo mod developer platform against its firmware/app contracts."""
from __future__ import annotations

import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]


def version(text: str) -> tuple[int, int]:
    major = re.search(r"#define\s+PCS_MOD_API_MAJOR\s+(\d+)", text)
    minor = re.search(r"#define\s+PCS_MOD_API_MINOR\s+(\d+)", text)
    if not major or not minor:
        raise ValueError("missing PCS_MOD_API version")
    return int(major.group(1)), int(minor.group(1))


def main() -> int:
    public = (HERE / "include" / "pcs_mod.h").read_text(encoding="utf-8")
    firmware_path = ROOT / "firmware" / "core" / "include" / "pcs" / "pcs_mod.h"
    if firmware_path.is_file() and public != firmware_path.read_text(encoding="utf-8"):
        print("mod-dev/include/pcs_mod.h differs from the detected firmware API header", file=sys.stderr)
        return 1
    api = version(public)
    packer = (HERE / "tools" / "pcspack.py").read_text(encoding="utf-8")
    simulator = (HERE / "tools" / "modsim.py").read_text(encoding="utf-8")
    rust_path = ROOT / "tool" / "src-tauri" / "src" / "packs.rs"
    expected = f"({api[0]}, {api[1]})"
    checks = {
        "pcspack.py": f"MOD_API = {expected}" in packer,
        "modsim.py": f"API_MAJOR, API_MINOR = {api[0]}, {api[1]}" in simulator,
    }
    if rust_path.is_file():
        checks["packs.rs"] = f"pub const MOD_API: (u16, u16) = {expected}" in rust_path.read_text(encoding="utf-8")
    failed = [name for name, ok in checks.items() if not ok]
    if failed:
        print("mod API ceiling mismatch: " + ", ".join(failed), file=sys.stderr)
        return 1
    print(f"mod developer platform: API {api[0]}.{api[1]} contracts agree")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
