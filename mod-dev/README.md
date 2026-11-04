# Mod build tools

This folder contains the API header and build tools needed to rebuild the mod without any other 3DPCS source code.

- `include/`: public mod API header
- `cmake/`: runtime and linker files
- `tools/`: mod builder, pack builder, and simulator
- `schema/`: package formats
- `check.py`: tool checks

Most users should run `python build.py` from the repository root instead of using these tools separately.
