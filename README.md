# Takeout Metadata Merger

Restore takeout media metadata from JSON sidecars.

Release [v0.6.0-beta](https://github.com/Abdullah1728/takeout-metadata-merger/releases/tag/v0.6.0-beta)

Supports:
- photos and videos
- archives (.zip / .tar / .tgz)
- GPS metadata
- timestamps
- titles and descriptions
- batch processing

Preserves directory structure, existing files,  and content root. Writes or updates only selected metadata fields. 

Interactive in-app help (`?`)

## Status

Beta release tested with Google takeout photos.

Metadata sidecar formats may vary between export providers.

If metadata is not detected correctly, open an issue with:
- export source
- log files
- archive layout
- sample sidecar  (if possible)

Planned updates include additional features, a GUI edition (separate commercial license), improved compatibility, and broader format support.

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

ExifTool dependency: https://exiftool.org/

Windows: Extract ExifTool, rename `exiftool(-k).exe` to `exiftool.exe`, place it beside `metadata-merger.exe`, then run `metadata-merger.exe` from Terminal.

## License

MIT
