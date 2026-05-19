# Takeout Metadata Merger

Restore takeout media metadata from JSON sidecars.

Release [v0.7.0-beta](https://github.com/Abdullah1728/takeout-metadata-merger/releases/tag/v0.7.0-beta)

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

Cross-platform beta release, tested on Linux with 1,300+ Google Photos Takeout media files.

Metadata sidecar formats may vary between export providers.

If metadata is not detected correctly, open an issue with:
- export source
- log files
- archive layout
- sample sidecar  (if possible)

Upcoming updates may include more features, broader format support, improved compatibility, and a separate commercial GUI edition.

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

ExifTool: https://exiftool.org/

## License

MIT
