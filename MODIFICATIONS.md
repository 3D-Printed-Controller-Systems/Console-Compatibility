# Modifications to libxsm3

This project uses libxsm3 commit `a085709cce5fe0da19f4a5213f7b6f325d3aff7a`.

Modified by 3DPCS on October 27 and November 4, 2026.

Changes:

- `excrypt_sha.c`: removed an unused variable that prevented the file from compiling.
- `usbdsec.c`: changed one loop counter so the file builds cleanly for RP2040.
- `xsm3.c` and `xsm3.h`: fixed the disabled debug macro and added step-by-step processing so the controller stays responsive.
- `README.md`: shortened the upstream description for this repository.
