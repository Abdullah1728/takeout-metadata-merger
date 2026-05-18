# Takeout Metadata Merger

Restore takeout media metadata from JSON sidecars.

Supports:
- photos and videos
- archives (.zip / .tar / .tgz)
- GPS metadata
- timestamps
- titles and descriptions
- batch processing

Preserves directory structure, existing files, and content root.

## Status

Beta release.

Metadata sidecar formats may vary between export providers.

If metadata is not detected correctly, open an issue with:
- export source
- log files
- archive layout
- sample sidecar  (if possible)

## Build

```bash
pip install pyinstaller tqdm
pyinstaller --onefile --name metadata-merger main.py
```

## Run

```bash
./metadata-merger
```

ExifTool must be available:
- beside the executable
- or in PATH

Link: https://exiftool.org/

## License

MIT
