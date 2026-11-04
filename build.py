#!/usr/bin/env python3
"""Build, test, and package Console Compatibility and its corresponding source."""
from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
MOD_DEV = ROOT / "mod-dev"
LIB = ROOT / "third_party" / "libxsm3"
PACK = ROOT / "pack"
MOD = PACK / "mods" / "console-compatibility.pcsmod"
BUILD = ROOT / "build"
DIST = ROOT / "dist"
REFERENCE = BUILD / "compatibility-reference.pcsmod"
VERSION = "0.1.5"
PACK_NAME = f"Console-Compatibility-{VERSION}.3dpack"
BUILT_PACK_NAME = f"3DPCS-Console Compatibility {VERSION}.3dpack"

SOURCES = [
    ROOT / "src" / "console_compatibility_mod.c",
    ROOT / "src" / "xsm3_port.c",
    ROOT / "src" / "xsm3_async_crypto.c",
    LIB / "xsm3.c",
    LIB / "usbdsec.c",
    LIB / "excrypt_des.c",
    LIB / "excrypt_parve.c",
    LIB / "excrypt_sha.c",
]


def run(*args: object) -> None:
    subprocess.run([str(x) for x in args], cwd=ROOT, check=True)


def source_archive() -> Path:
    out = DIST / f"Console-Compatibility-{VERSION}-source.zip"
    roots = [
        ".github", "README.md", "LICENSE", "NOTICE.md", "SECURITY.md",
        "CONTRIBUTING.md", "REBUILDING.md", "RELEASING.md", "UPSTREAM.md", "MODIFICATIONS.md",
        "build.py", "src", "tests", "third_party", "mod-dev", "pack",
    ]
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for name in roots:
            p = ROOT / name
            if p.is_file():
                z.write(p, p.relative_to(ROOT))
            elif p.is_dir():
                for f in sorted(p.rglob("*")):
                    if f.is_file() and f != MOD and "__pycache__" not in f.parts and f.suffix != ".pyc":
                        z.write(f, f.relative_to(ROOT))
    return out


def write_hashes(paths: list[Path]) -> None:
    lines = [f"{hashlib.sha256(p.read_bytes()).hexdigest()}  {p.name}" for p in paths]
    (DIST / "SHA256SUMS.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")


def check_release_identity() -> None:
    manifest = json.loads((PACK / "manifest.json").read_text(encoding="utf-8"))
    expected = {
        "id": "3dpcs.console-compatibility",
        "name": "Console Compatibility",
        "author": "3DPCS",
        "version": VERSION,
        "description": "Adds console compatibility through an open-source mod",
        "icon": "icon.png",
        "license": "LGPL-2.1-or-later",
        "homepage": "https://github.com/3D-Printed-Controller-Systems/Console-Compatibility",
    }
    for field, value in expected.items():
        if manifest.get(field) != value:
            raise SystemExit(f"pack/manifest.json {field!r} must be {value!r}")
    mods = manifest.get("contents", {}).get("mods", [])
    if (len(mods) != 1 or mods[0].get("file") != "mods/console-compatibility.pcsmod" or
            mods[0].get("name") != "Console Compatibility" or
            mods[0].get("description") != "Provides additional console compatibility framework"):
        raise SystemExit("pack manifest mod identity does not match Console Compatibility")
    source = (PACK / "SOURCE.txt").read_text(encoding="utf-8")
    if f"Console-Compatibility-{VERSION}-source.zip" not in source or f"/releases/tag/v{VERSION}" not in source:
        raise SystemExit("pack/SOURCE.txt does not match VERSION")


def main() -> int:
    missing = [tool for tool in ("arm-none-eabi-gcc", "arm-none-eabi-nm", "arm-none-eabi-objcopy") if not shutil.which(tool)]
    if missing:
        raise SystemExit("Arm GNU Toolchain is required on PATH; missing: " + ", ".join(missing))
    check_release_identity()
    MOD.parent.mkdir(parents=True, exist_ok=True)
    for old in MOD.parent.glob("*.pcsmod"):
        old.unlink()
    BUILD.mkdir(exist_ok=True)
    DIST.mkdir(exist_ok=True)
    for old in DIST.iterdir():
        if old.is_file():
            old.unlink()
    run(sys.executable, MOD_DEV / "tools" / "pcsmod.py", "build", *SOURCES,
        "-I", MOD_DEV / "include", "-I", ROOT / "src", "-I", LIB,
        "--cflag=-DXSM3_NO_DEBUGGING", "-o", MOD)
    run(sys.executable, MOD_DEV / "tools" / "pcsmod.py", "build", *SOURCES,
        "-I", MOD_DEV / "include", "-I", ROOT / "src", "-I", LIB,
        "--cflag=-DXSM3_NO_DEBUGGING", "--cflag=-DXSM3_REFERENCE_SYNC", "-o", REFERENCE)
    run(sys.executable, MOD_DEV / "tools" / "pcspack.py", "check", PACK)
    run(sys.executable, MOD_DEV / "tools" / "pcspack.py", "build", PACK, "-o", DIST)
    run(sys.executable, ROOT / "tests" / "test_provider.py")
    built_package = DIST / BUILT_PACK_NAME
    package = DIST / PACK_NAME
    if not built_package.is_file():
        raise SystemExit(f"pack builder did not create {built_package.name}")
    built_package.replace(package)
    source = source_archive()
    write_hashes([package, source])
    print(f"package: {package}")
    print(f"source:  {source}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
