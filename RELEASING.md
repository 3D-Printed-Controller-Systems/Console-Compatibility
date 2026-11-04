# Release checklist

1. Update the version in `pack/manifest.json`, `build.py`, and `pack/SOURCE.txt`.
2. Run `python build.py`.
3. Publish the `.3dpack`, source ZIP, and `SHA256SUMS.txt` under the matching Git tag.
4. Add the `.3dpack` to the `packs` branch of `3DPCS-Programmer-Releases`.
5. Confirm that Pack Manager can download and install it.
