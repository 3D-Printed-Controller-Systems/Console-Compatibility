# Rebuilding and modifying

Everything needed to rebuild this mod is included in the repository. You do not need the source for the 3DPCS app or firmware.

## 1. Install the tools

Install:

- Python 3.9 or newer
- Arm GNU Toolchain

Make sure these commands work:

```text
python --version
arm-none-eabi-gcc --version
arm-none-eabi-nm --version
arm-none-eabi-objcopy --version
```

Use `python3` instead of `python` if needed.

## 2. Get the complete source

```text
git clone --branch v0.1.5 https://github.com/3D-Printed-Controller-Systems/Console-Compatibility.git
cd Console-Compatibility
```

You can also download and extract the source ZIP from the release page.

## 3. Build

Run:

```text
python build.py
```

The script builds the mod, runs its checks, and creates `dist/Console-Compatibility-0.1.5.3dpack`.

## 4. Modify it

- Main mod code: `src/`
- Included libxsm3 source: `third_party/libxsm3/`
- Tests: `tests/test_provider.py`
- Package information: `pack/manifest.json`

Edit the source files and run `python build.py` again. Do not edit generated `.pcsmod` or `.3dpack` files.

If you change libxsm3, keep its license notices and update `MODIFICATIONS.md`.

## 5. Install your build

1. Open the new `.3dpack` from `dist/` with 3DPCS Controller Tool.
2. Enable **Developer installs** when asked.
3. Add **Console Compatibility** to the active profile.
4. Leave output mode on **Automatic** and reconnect the controller.

For wireless use, install the mod on the Polylink connected to the console.

## Sharing a modified build

Keep the LGPL and BSD notices, clearly describe your changes, and include the complete source and build files with your release. No private signing key is required to build or install a modified version.
