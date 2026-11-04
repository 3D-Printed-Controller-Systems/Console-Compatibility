# Console Compatibility

Console Compatibility is an optional open-source mod that adds Xbox 360 support to compatible 3DPCS controllers and Polylink.

## Install

Download the `.3dpack` from the [latest release](https://github.com/3D-Printed-Controller-Systems/Console-Compatibility/releases/latest) and open it with 3DPCS Controller Tool. Add the mod to your active profile and leave the output mode on **Automatic**.

For a wireless controller, install the mod on the Polylink connected to the console.

## Build it yourself

You need Python 3.9 or newer and an Arm GNU Toolchain with `arm-none-eabi-gcc`, `arm-none-eabi-nm`, and `arm-none-eabi-objcopy`.

Clone the tagged release and run the complete build:

```text
git clone --branch v0.1.5 https://github.com/3D-Printed-Controller-Systems/Console-Compatibility.git
cd Console-Compatibility
python build.py
```

The finished package will be in `dist/`. Use `python3` instead of `python` if needed. See [REBUILDING.md](REBUILDING.md) for more help.

## Modify, compile, and run your version

Edit the files in `src/`, run `python build.py`, and open the new `.3dpack` with 3DPCS Controller Tool. The app will ask you to approve **Developer installs** for a modified build.

## Documentation

- [Rebuild, modify, and install the mod](REBUILDING.md)
- [Contributing](CONTRIBUTING.md)
- [Security](SECURITY.md)
- [Third-party notices](NOTICE.md)
- [Changes to libxsm3](MODIFICATIONS.md)
- [Upstream source](UPSTREAM.md)

## Licensing

The mod and libxsm3 changes use LGPL-2.1-or-later. ExCrypt files use BSD-3-Clause. The included mod build tools use MIT.
