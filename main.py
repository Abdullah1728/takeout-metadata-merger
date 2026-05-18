#!/usr/bin/env python3
"""
Takeout Metadata Merger

Restore media metadata from Takeout JSON sidecars.

Principles:
- Non-destructive processing.
- In-memory intermediate handling.
- Minimal unnecessary disk I/O.
"""

import os, re, sys, json, shutil, zipfile, tarfile, atexit
import tempfile, subprocess, threading, multiprocessing
from pathlib import Path
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed
from tqdm import tqdm


__app_name__ = "Takeout Metadata Merger"
__version__  = "0.6.0-beta"

_ET_BIN = "exiftool"
_ET_ENV = None


# ── Colour support ─────────────────────────────────────────────────────────────
# Disabled when stdout is not a TTY, when NO_COLOR is set, or when TERM=dumb.
# On Windows, the VT-100 layer is activated via os.system("") so that ANSI
# sequences work in modern cmd / PowerShell without an external library.
def _setup_color() -> bool:
    if not sys.stdout.isatty():                      return False
    if os.environ.get("NO_COLOR") is not None:       return False
    if os.environ.get("TERM", "") == "dumb":         return False
    if os.name == "nt":  os.system("")               # enable VT-100 on Windows
    return True

_USE_COLOR = _setup_color()

def _green(s):  return f"\x1b[32m{s}\x1b[0m" if _USE_COLOR else s
def _red(s):    return f"\x1b[31m{s}\x1b[0m" if _USE_COLOR else s
def _yellow(s): return f"\x1b[33m{s}\x1b[0m" if _USE_COLOR else s


# ── Readline / input helpers ───────────────────────────────────────────────────

_readline_ready = False

def _init_readline() -> None:
    """Initialize readline support once for interactive input history."""
    global _readline_ready
    if _readline_ready:
        return
    _readline_ready = True
    try:
        import readline as _rl
        _rl.set_history_length(500)
    except (ImportError, AttributeError):
        pass


def _input_prefilled(prompt: str, prefill: str = "") -> str:
    return input(prompt)


# ── Path history and re-edit exception ────────────────────────────────────────

_path_history: list = []   # confirmed source and destination strings, shared

class _ReeditPaths(Exception):
    """Raised when path entry should restart."""
    def __init__(self, msg: str = "", raw_src: str = "", raw_dst: str = ""):
        super().__init__(msg)
        self.raw_src = raw_src
        self.raw_dst = raw_dst


def _resolve_path_refs(tokens: list) -> tuple:
    """Resolve !! and !N history references from _path_history."""
    resolved = []
    for tok in tokens:
        if tok == "!!":
            if not _path_history:
                return [], "!! used but no path history exists yet."
            resolved.append(_path_history[-1])
        else:
            m = re.match(r"^!(\d+)$", tok)
            if m:
                n = int(m.group(1))
                if n < 1 or n > len(_path_history):
                    return [], (f"!{n} is out of range "
                                f"(history has {len(_path_history)} entry/entries).")
                resolved.append(_path_history[n - 1])
            else:
                resolved.append(tok)
    return resolved, ""


# ── Persistent ExifTool processes ──────────────────────────────────────────────

_local         = threading.local()
_et_procs: list = []
_et_procs_lock  = threading.Lock()

def _close_et_procs() -> None:
    """Terminate all active ExifTool processes."""
    with _et_procs_lock:
        for proc in _et_procs:
            try:
                if proc.poll() is None:
                    proc.stdin.write("-stay_open\nFalse\n-execute\n")
                    proc.stdin.flush()
                    proc.stdin.close()
                    proc.wait(timeout=3)
            except Exception:
                try: proc.kill()
                except Exception: pass

atexit.register(_close_et_procs)


def get_et():
    """Return the thread-local persistent ExifTool process."""
    if not getattr(_local, "proc", None) or _local.proc.poll() is not None:
        proc = subprocess.Popen(
            [_ET_BIN, "-stay_open", "True", "-@", "-"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, bufsize=1,
            env=_ET_ENV,
        )
        _local.proc = proc
        with _et_procs_lock:
            _et_procs[:] = [p for p in _et_procs if p.poll() is None]
            _et_procs.append(proc)
    return _local.proc


_ET_TIMEOUT = 60   # seconds before the watchdog declares ExifTool hung


def et_run(*args) -> str:
    """Execute ExifTool commands through the persistent worker process."""
    for _attempt in range(2):
        proc = get_et()
        try:
            proc.stdin.write("\n".join(args) + "\n-execute\n")
            proc.stdin.flush()
        except (BrokenPipeError, OSError):
            _local.proc = None
            continue

        lines      = []
        _timed_out = False

        def _watchdog():
            nonlocal _timed_out
            _timed_out = True
            # Important: threading.local() is per-thread.  Setting
            # _local.proc here would affect the *timer thread's* local
            # storage, not the calling worker's.  The worker detects
            # the dead process when readline returns "" or raises, which
            # already sets _local.proc = None via the paths below.
            try: proc.kill()
            except Exception: pass

        timer = threading.Timer(_ET_TIMEOUT, _watchdog)
        timer.daemon = True
        timer.start()
        try:
            while True:
                line = proc.stdout.readline()
                if not line:
                    _local.proc = None; break
                if "{ready}" in line: break
                lines.append(line.rstrip())
        except (BrokenPipeError, OSError):
            _local.proc = None
        finally:
            timer.cancel()

        if not _timed_out:
            return "\n".join(lines).strip()
    return ""


# ── File classification ────────────────────────────────────────────────────────

MEDIA_EXT = {
    ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tiff", ".tif", ".heic", ".heif",
    ".webp", ".raw", ".arw", ".cr2", ".nef", ".dng", ".orf", ".rw2", ".pef", ".srw",
    ".mp4", ".mov", ".avi", ".mkv", ".wmv", ".flv", ".m4v", ".3gp", ".3g2",
    ".mts", ".m2ts", ".mpg", ".mpeg", ".webm",
}
VIDEO_EXT = {
    ".mp4", ".mov", ".avi", ".mkv", ".wmv", ".flv", ".m4v", ".3gp", ".3g2",
    ".mts", ".m2ts", ".mpg", ".mpeg", ".webm",
}
# Formats that use the ISO Base Media File Format / QuickTime container and
# therefore accept QuickTime:* timestamp tags via ExifTool.  Other VIDEO_EXT
# entries (MKV, AVI, WMV, FLV, WebM) use different metadata schemas; any
# failed QuickTime write is caught by the "0 image files updated" guard in
# process_file and reported as no_date rather than a hard failure.
_QT_VIDEO_EXT = {".mp4", ".mov", ".m4v", ".3gp", ".3g2", ".mts", ".m2ts"}

IGNORE = {".ds_store", "thumbs.db", "desktop.ini", "picasa.ini",
          ".nomedia", ".gitkeep", ".localized"}


def is_archive(p: Path) -> bool:
    n = p.name.lower()
    if p.suffix.lower() == ".zip": return True
    return any(n.endswith(s) for s in
               (".tgz", ".tar", ".tar.gz", ".tar.bz2", ".tar.xz", ".tar.z"))


def classify(files):
    jsons, media, archives = [], [], []
    for f in files:
        n, e = f.name.lower(), f.suffix.lower()
        if n in IGNORE:           continue
        elif e == ".json":        jsons.append(f)
        elif e in MEDIA_EXT:      media.append(f)
        elif is_archive(f):       archives.append(f)
    return jsons, media, archives


# ── Archive helpers ────────────────────────────────────────────────────────────

def _open_archive(path: Path):
    """Return (ZipFile, None) or (None, TarFile) for the given archive."""
    if path.suffix.lower() == ".zip":
        return zipfile.ZipFile(path, "r"), None
    return None, tarfile.open(path, "r:*")


def _safe_member(name: str) -> bool:
    """Return False for absolute paths and directory-traversal entries."""
    if os.path.isabs(name):               return False
    if len(name) >= 3 and name[1] == ":": return False   # Windows drive letter
    if ".." in name.split("/"):           return False
    return True


def _iter_members(z, t):
    """Yield safe archive members as (name, is_dir)."""
    if z:
        for info in z.infolist():
            if _safe_member(info.filename):
                yield info.filename, info.filename.endswith("/")
    else:
        for m in t.getmembers():
            if _safe_member(m.name):
                yield m.name, m.isdir()


def _read_member_bytes(z, t, name: str,
                       tar_idx: "dict | None" = None) -> bytes:
    """Read an archive member into memory."""
    if z:
        return z.read(name)
    ti = (tar_idx.get(name) if tar_idx is not None else None) \
         or t.getmember(name)
    fobj = t.extractfile(ti)
    return fobj.read() if fobj else b""


def _write_member(z, t, name: str, dest: Path,
                  tar_idx: "dict | None" = None) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    with open(dest, "wb") as f:
        f.write(_read_member_bytes(z, t, name, tar_idx))


def _find_content_root(file_members: list) -> tuple:
    """Determine the common archive root path."""
    dir_parts = []
    for n in file_members:
        if n.endswith("/"): continue
        parts = n.split("/")
        dir_parts.append(parts[:-1])
    if not dir_parts:
        return 0, ""
    common = list(dir_parts[0])
    for parts in dir_parts[1:]:
        # Truncate to the true common prefix by stopping at the first
        # position where the two sequences diverge.  A comprehension over
        # zip() would incorrectly retain matching elements beyond the
        # divergence point (e.g. ["a","b","c"] ∩ ["a","x","c"] → ["a","c"]
        # instead of the correct ["a"]).
        new_len = 0
        for a, b in zip(common, parts):
            if a == b:
                new_len += 1
            else:
                break
        common = common[:new_len]
        if not common: break
    if not common:
        return 0, ""
    return len(common), common[-1]


def _member_rel(member_name: str, offset: int) -> Path:
    """Build a relative path from an archive member name."""
    parts = member_name.split("/")
    inner = parts[offset:]
    return Path(*inner) if inner else Path(parts[-1])


def _build_tar_index(t) -> dict:
    """Build a name-to-TarInfo lookup index."""
    return {m.name: m for m in t.getmembers()}


# ── JSON sidecar indexing ──────────────────────────────────────────────────────

JSON_SUFFIXES = [
    ".json",
    ".supplemental-metadata.json",
    "-metadata.json",
    "_metadata.json",
]
TRUNC_LENS = [46, 47, 50, 51]   # export filenames are truncated at these lengths

_NORM_RE = re.compile(r"[\s_\-]+")


def _norm(s: str) -> str:
    return _NORM_RE.sub("_", s.lower())


def _index_add(idx: dict, key: str, val) -> None:
    k = _norm(key)
    if k and k not in idx:
        idx[k] = val


def _index_entries(fname: str, val) -> list:
    """Generate index keys for a JSON sidecar filename."""
    entries = [(fname, val)]
    for suf in JSON_SUFFIXES:
        if fname.lower().endswith(suf):
            entries.append((fname[: -len(suf)], val))
            break
    return entries


def build_index_from_paths(json_files: list) -> dict:
    """Build a lookup index from JSON sidecar files on disk."""
    idx: dict = {}
    for jf in json_files:
        for k, v in _index_entries(jf.name, jf):
            _index_add(idx, k, v)
        try:
            with open(jf, "r", encoding="utf-8", errors="replace") as fh:
                data = json.load(fh)
            title = data.get("title", "").strip()
            if title:
                _index_add(idx, title, jf)
                for tl in TRUNC_LENS:
                    if tl < len(title): _index_add(idx, title[:tl], jf)
        except Exception:
            pass
    return idx


def build_index_from_memory(json_map: dict) -> dict:
    """Build a lookup index from an in-memory {member_path: bytes} map."""
    idx: dict = {}
    for name, raw in json_map.items():
        val = ("mem", raw)
        for k, v in _index_entries(Path(name).name, val):
            _index_add(idx, k, v)
        try:
            data  = json.loads(raw.decode("utf-8", errors="replace"))
            title = data.get("title", "").strip()
            if title:
                _index_add(idx, title, val)
                for tl in TRUNC_LENS:
                    if tl < len(title): _index_add(idx, title[:tl], val)
        except Exception:
            pass
    return idx


def find_json(media: Path, idx: dict):
    """Locate the matching JSON sidecar for a media file."""
    name, stem, ext = media.name, media.stem, media.suffix
    def hit(s): return idx.get(_norm(s))

    h = hit(name)
    if h: return h
    for suf in JSON_SUFFIXES:
        h = hit(name + suf) or hit(stem + suf)
        if h: return h
    for tl in TRUNC_LENS:
        for suf in JSON_SUFFIXES:
            h = hit(name[:tl] + suf) or hit(name[:tl] + ext + suf)
            if h: return h
    m = re.match(r"^(.*?)(\(\d+\))(\.\w+)$", name)
    if m:
        base, num, fext = m.groups()
        for suf in JSON_SUFFIXES:
            h = hit(base + fext + num + suf)
            if h: return h
    for sfx in ("-edited", "_edited", "-effect", "_effect", "-smile", "_smile"):
        if stem.lower().endswith(sfx):
            orig = stem[: -len(sfx)]
            for suf in JSON_SUFFIXES:
                h = hit(orig + ext + suf) or hit(orig + suf)
                if h: return h
# Handle truncated Google Takeout sidecar filenames.
    h = hit((name + ".supplemental-metadata")[:46])
    if h: return h
    return None


# ── Field selection ────────────────────────────────────────────────────────────

FIELD_DEFS = {
    "timestamp":  "Date/time metadata",
    "gps":   "GPS coordinates and altitude",
    "title": "Title",
    "desc":  "Description",
    "views": "imageViews count (stored in XMP)",
}


def ask_fields() -> tuple:
    """Prompt for metadata fields to merge."""
    all_keys = list(FIELD_DEFS.keys())

    def _show_help():
        print()
        for i, k in enumerate(all_keys, 1):
            print(f"  [{i}] {k:8s}  {FIELD_DEFS[k]}")
        print(f"  [{len(all_keys) + 1}] skip_nojson   skip files with no JSON sidecar")
        print()
        print("  Input format:")
        print("    - Numbers: 1,2,5")
        print("    - Names:   date,gps,views")
        print("    - Mixed:   1,gps,6")
    while True:
        try:
            raw = input(
                f"\nFields to merge "
                f" (? for list): "
            ).strip()
        except EOFError:
            return set(all_keys), False
        if raw == "?":
            _show_help(); continue
        break

    if not raw:
        return set(all_keys), False

    selected: set = set()
    skip           = False
    unknown: list  = []

    for part in raw.replace(" ", "").split(","):
        if not part:
            continue
        try:
            i = int(part) - 1
            if   i == len(all_keys):      skip = True
            elif 0 <= i < len(all_keys):  selected.add(all_keys[i])
            else:                         unknown.append(part)
        except ValueError:
            if   part.lower() in ("skip_nojson", "skip"): skip = True
            elif part.lower() in FIELD_DEFS:               selected.add(part.lower())
            else:                                          unknown.append(part)

    if unknown:
        print(_yellow(f"  Unrecognised token(s): {', '.join(unknown)} — ignored."))

    return selected or set(all_keys), skip


# ── Metadata reading / writing ─────────────────────────────────────────────────

def _unix_to_dt(ts) -> str:
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%Y:%m:%d %H:%M:%S")


def _load_json(jf_val) -> dict:
    if isinstance(jf_val, tuple) and jf_val[0] == "mem":
        return json.loads(jf_val[1].decode("utf-8", errors="replace"))
    with open(jf_val, "r", encoding="utf-8", errors="replace") as fh:
        return json.load(fh)


def read_meta(jf_val, fields: set) -> dict:
    """Extract selected metadata fields from a JSON sidecar."""
    data   = _load_json(jf_val)
    result = {}

    if "date" in fields:
        for key in ("photoTakenTime", "creationTime"):
            ts = data.get(key, {}).get("timestamp")
            if ts:
                try:
                    result["unix_ts"]      = int(ts)
                    result["datetime_str"] = _unix_to_dt(ts)
                    break
                except (ValueError, TypeError):
                    continue

    if "gps" in fields:
        for gk in ("geoDataExif", "geoData"):
            geo = data.get(gk, {})
            lat, lon = geo.get("latitude"), geo.get("longitude")
            if lat is not None and lon is not None and (lat != 0.0 or lon != 0.0):
                result["gps_lat"] = float(lat)
                result["gps_lon"] = float(lon)
                alt = geo.get("altitude")
                if alt is not None: result["gps_alt"] = float(alt)
                break

    if "title" in fields:
        t = data.get("title", "").strip()
        if t: result["title"] = t

    if "desc" in fields:
        d = data.get("description", "").strip()
        if d: result["desc"] = d

    if "views" in fields:
        v = data.get("imageViews")
        if v is not None:
            v = str(v).strip()
            if v: result["views"] = v

    return result


def _et_safe(v: str) -> str:
    """Sanitize values before passing them to ExifTool."""
    return v.replace("\r", " ").replace("\n", " ").replace("\x00", "")


def build_args(meta: dict, is_video: bool) -> list:
    """Build ExifTool arguments from extracted metadata."""
    args = ["-overwrite_original", "-api", "LargeFileSupport=1"]
    dt   = meta.get("datetime_str")

    if dt:
        if is_video:
            args += [
                f"-QuickTime:CreateDate={dt}",
                f"-QuickTime:TrackCreateDate={dt}",
                f"-QuickTime:MediaCreateDate={dt}",
                f"-QuickTime:ModifyDate={dt}",
                f"-QuickTime:TrackModifyDate={dt}",
                f"-QuickTime:MediaModifyDate={dt}",
            ]
        else:
            args += [
                f"-EXIF:DateTimeOriginal={dt}",
                f"-EXIF:CreateDate={dt}",
                f"-EXIF:ModifyDate={dt}",
            ]

    if "gps_lat" in meta:
        lat, lon = meta["gps_lat"], meta["gps_lon"]
        args += [
            f"-EXIF:GPSLatitude={abs(lat)}",
            f"-EXIF:GPSLatitudeRef={'N' if lat >= 0 else 'S'}",
            f"-EXIF:GPSLongitude={abs(lon)}",
            f"-EXIF:GPSLongitudeRef={'E' if lon >= 0 else 'W'}",
        ]
        if "gps_alt" in meta:
            alt = meta["gps_alt"]
            args += [
                f"-EXIF:GPSAltitude={abs(alt)}",
                f"-EXIF:GPSAltitudeRef={'0' if alt >= 0 else '1'}",
            ]

    if "title" in meta: args.append(f"-XMP:Title={_et_safe(meta['title'])}")
    if "desc"  in meta: args.append(f"-XMP:Description={_et_safe(meta['desc'])}")
    if "views" in meta: args.append(f"-XMP:ImageSupplierImageID={_et_safe(meta['views'])}")

    return args


# ── Collision-safe destination naming (RAM-only, no disk placeholders) ─────────

_claimed_paths: set = set()   # in-memory slot reservations
_rename_lock        = threading.Lock()


def _new_dest_path(p: Path) -> Path:
    """Reserve the next available duplicate filename slot."""
    with _rename_lock:
        for i in range(1, 10_000):
            candidate = p.parent / f"{p.stem}({i}){p.suffix}"
            key       = str(candidate.absolute())
            if not candidate.exists() and key not in _claimed_paths:
                _claimed_paths.add(key)
                break
        else:
            raise RuntimeError(
                f"Could not find a free destination slot for '{p.name}' "
                f"after 9 999 attempts.")
    # Directory creation happens outside the lock so that filesystem I/O
    # does not block other threads from claiming their own slots.  The
    # entry in _claimed_paths was already added above, so no other thread
    # can claim the same candidate path.
    candidate.parent.mkdir(parents=True, exist_ok=True)
    return candidate


def _release_claim(p: Path) -> None:
    """Release an in-memory path claim after a write succeeds or fails."""
    _claimed_paths.discard(str(p.absolute()))


# ── Core file processing ───────────────────────────────────────────────────────

def process_file(task: dict) -> dict:
    """Copy a file and apply metadata from its JSON sidecar."""
    dst    = Path(task["dst"])
    jf_val = task["jf"]
    fields = task["fields"]
    is_vid = dst.suffix.lower() in VIDEO_EXT
    dst.parent.mkdir(parents=True, exist_ok=True)

    claimed_path = None
    if task.get("collision_mode") == "create_copy" and dst.exists():
        dst          = _new_dest_path(dst)
        claimed_path = dst

    def _write_failed(reason: str) -> dict:
        # The in-memory slot claim is released so another worker may reuse
        # it.  No file was written, so there is nothing to remove.
        if claimed_path is not None:
            _release_claim(claimed_path)
        return {
            "status": "fail",
            "src":    Path(task.get("src", task.get("member", "?"))),
            "reason": reason,
            "jf":     "",
        }

    # ── write the file to its destination ────────────────────────────────
    if "src" in task:
        label = str(task["src"])
        try:    shutil.copy2(task["src"], dst)
        except Exception as e: return _write_failed(f"Copy: {e}")

    elif "member_bytes" in task:
        label = task.get("member", "")
        try:
            with open(dst, "wb") as fh: fh.write(task["member_bytes"])
        except Exception as e: return _write_failed(f"Write: {e}")

    else:
        label = task.get("member", "")
        try:
            z, t = _open_archive(Path(task["archive"]))
            try:    _write_member(z, t, task["member"], dst)
            finally:
                if z: z.close()
                if t: t.close()
        except Exception as e: return _write_failed(f"Extract: {e}")

    # Release reserved destination slot.
    if claimed_path is not None:
        _release_claim(claimed_path)
        claimed_path = None

    src_path = Path(label)

    if jf_val is None:
        return {"status": "no_json", "src": src_path}

    try:
        meta = read_meta(jf_val, fields)
    except Exception as e:
        return {"status": "fail", "src": src_path, "reason": f"JSON: {e}", "jf": "?"}

    dt = meta.get("datetime_str")
    if not meta or (not dt
                    and "gps_lat" not in meta
                    and "title"   not in meta
                    and "desc"    not in meta
                    and "views"   not in meta):
        return {"status": "no_date", "src": src_path, "jf": "?"}

    out = et_run(*build_args(meta, is_vid), str(dst))

    if dt:
        tag = "QuickTime:CreateDate" if is_vid else "EXIF:DateTimeOriginal"
        rb  = et_run(f"-{tag}", "-s3", str(dst))
        if rb.strip() != dt.strip():
            # Treat unsupported metadata writes as non-fatal.
            if ("0 image files updated" in out
                    or "0 files updated" in out
                    or "files weren't updated due to errors" in out):
                return {"status": "no_date", "src": src_path, "jf": "?"}
            # ExifTool reported at least one file updated but the value read
            # back does not match — genuine data-integrity issue; log it.
            return {
                "status": "fail",
                "src":    src_path,
                "reason": f"Verify mismatch (ExifTool: {out or 'no output'})",
                "jf":     "?",
            }

    if "unix_ts" in meta:
        try: os.utime(dst, (meta["unix_ts"], meta["unix_ts"]))
        except Exception: pass

    copied = []
    if "datetime_str" in meta: copied.append(f"date={meta['datetime_str']}")
    if "gps_lat" in meta:
        copied.append(
            f"gps={meta['gps_lat']},{meta['gps_lon']}"
            + (f",alt={meta['gps_alt']}" if "gps_alt" in meta else "")
        )
    if "title" in meta: copied.append(f"title={meta['title'][:60]}")
    if "desc"  in meta: copied.append(f"desc={meta['desc'][:60]}")
    if "views" in meta: copied.append(f"views={meta['views']}")

    return {
        "status":        "ok",
        "src":           src_path,
        "dst":           dst,
        "dt":            dt or "n/a",
        "jf":            jf_val,
        "copied_fields": copied,
    }


# ── Self-test ──────────────────────────────────────────────────────────────────

def self_test() -> tuple:
    """Verify ExifTool write/read functionality."""
    import base64 as _b64
    _JPEG = (
        "/9j/4AAQSkZJRgABAQEASABIAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsLDB"
        "kSEw8UHRofHh0aHBwgJC4nICIsIxwcKDcpLDAxNDQ0Hyc5PTgyPC4zNDL/wAAR"
        "CAABAAEDASIAAhEBAxEB/8QAFAABAAAAAAAAAAAAAAAAAAAACf/EABQQAQAAAAAA"
        "AAAAAAAAAAAAAP/EABQBAQAAAAAAAAAAAAAAAAAAAAD/xAAUEQEAAAAAAAAAAAAA"
        "AAAAAAAA/9oADAMBAAIRAxEAPwCwAB//2Q=="
    )
    test_dt = "2000:01:01 00:00:00"
    try:
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
            tmp.write(_b64.b64decode(_JPEG))
            tp = Path(tmp.name)
    except Exception as e:
        return False, f"Could not create temporary test file — {e}"
    try:
        out = et_run(
            "-overwrite_original",
            f"-EXIF:DateTimeOriginal={test_dt}",
            f"-EXIF:CreateDate={test_dt}",
            str(tp),
        )
        rb = et_run("-EXIF:DateTimeOriginal", "-s3", str(tp)).strip()
        if rb == test_dt:
            return True, ""
        if not rb:
            return False, (
                f"ExifTool ran but wrote nothing.  "
                f"Possible permissions issue on the temp directory.  "
                f"ExifTool output: {out or '(empty)'}"
            )
        return False, (
            f"ExifTool wrote an unexpected value: got {rb!r}, "
            f"expected {test_dt!r}.  Try reinstalling or updating ExifTool."
        )
    except Exception as e:
        return False, f"Unexpected error during self-test — {e}"
    finally:
        try: tp.unlink(missing_ok=True)
        except Exception: pass


# ── Dispatch / progress ────────────────────────────────────────────────────────

_BAR_FMT = (
    "{desc}: {percentage:3.0f}%|{bar}| "
    "{n_fmt}/{total_fmt} [{elapsed}<{remaining}, {rate_fmt}]"
)


def dispatch(tasks, n_workers: int, desc: str = ""):
    """Process tasks concurrently with progress reporting."""
    successes, failures, no_json, no_date = [], [], [], []
    with ThreadPoolExecutor(max_workers=n_workers) as pool:
        futs = {pool.submit(process_file, t): t for t in tasks}
        with tqdm(total=len(tasks), unit="file",
                  desc=desc, bar_format=_BAR_FMT) as bar:
            for fut in as_completed(futs):
                try:
                    r = fut.result()
                except Exception as exc:
                    t = futs[fut]
                    failures.append({
                        "src":    str(t.get("src", t.get("member", "?"))),
                        "reason": f"Unhandled exception: {exc}",
                        "jf":     "",
                    })
                    bar.update(1)
                    continue
                st = r["status"]
                if   st == "ok":      successes.append(r)
                elif st == "no_json": no_json.append(r["src"])
                elif st == "no_date": no_date.append((r["src"], r.get("jf", "")))
                else:                 failures.append(r)
                bar.update(1)
    return successes, failures, no_json, no_date


# ── Log writing ────────────────────────────────────────────────────────────────

def _versioned_log_folder(out_root: Path) -> Path:
    """Return the next available log directory path."""
    base = out_root / "_logs"
    if not base.exists():
        return base
    i = 1
    while (out_root / f"_logs({i})").exists():
        i += 1
    return out_root / f"_logs({i})"


def write_logs(out_root: Path, successes, failures, no_json, no_date,
               skipped_no_json, skipped_conflict) -> None:
    log_dir = _versioned_log_folder(out_root)
    log_dir.mkdir(parents=True, exist_ok=True)

    def _wlog(name: str, lines: list) -> None:
        (log_dir / name).write_text("\n".join(lines), encoding="utf-8")

    if successes:
        out = [f"SUCCESS ({len(successes)})", "=" * 50, ""]
        for r in successes:
            jf_name = Path(r["jf"]).name if isinstance(r["jf"], Path) else "(in memory)"
            out += [f"FILE : {Path(r['src']).name}",
                    f"DATE : {r['dt']}",
                    f"JSON : {jf_name}", ""]
        _wlog("_log_success.txt", out)

        out = [f"METADATA DETAIL — {len(successes)} files", "=" * 60,
               "Only fields present in the sidecar are shown.", ""]
        for r in successes:
            out.append(f"FILE : {Path(r['dst']).name}")
            out.append(f"PATH : {r['dst']}")
            for field in r.get("copied_fields", []):
                key, _, val = field.partition("=")
                out.append(f"  {key:<12} {val}")
            if not r.get("copied_fields"):
                out.append("  (no metadata fields written)")
            out.append("")
        _wlog("_log_metadata_detail.txt", out)

    if skipped_no_json:
        _wlog("_log_skipped_no_json.txt",
              [f"SKIPPED — NO JSON ({len(skipped_no_json)})", "=" * 50, ""]
              + [str(s) for s in skipped_no_json])

    if skipped_conflict:
        _wlog("_log_skipped_conflict.txt",
              [f"SKIPPED — CONFLICT ({len(skipped_conflict)})", "=" * 50, ""]
              + [str(s) for s in skipped_conflict])

    if no_json:
        _wlog("_log_no_json.txt",
              [f"NO JSON ({len(no_json)})", "=" * 50, ""]
              + [str(s) for s in no_json])

    if no_date:
        out = [f"NO DATE ({len(no_date)})", "=" * 50, ""]
        for s, _ in no_date: out += [str(s), ""]
        _wlog("_log_no_date.txt", out)

    if failures:
        out = [f"FAILURES ({len(failures)})", "=" * 50, ""]
        for r in failures:
            out += [str(r["src"]), f"  Reason: {r['reason']}", ""]
        _wlog("_log_failures.txt", out)


# ── Conflict resolution prompt ─────────────────────────────────────────────────

def conflict_prompt(proposed_root: Path, conflicts: set, dst: Path,
                    base_name: str, raw_src: str = "", raw_dst: str = ""):
    """Prompt for conflict resolution strategy."""
    if not proposed_root.exists() or not conflicts:
        return None, proposed_root

    def _show_menu():
        print(_yellow(
            f"\n{len(conflicts)} file(s) already exist "
            f"in '{proposed_root.name}'."
        ))
        print(
            f"  [n] New folder   '{base_name}(1)'\n"
             "  [s] Skip existing\n"
             "  [c] Create renamed copies\n"
             "  [r] Re-edit paths\n"
             "  [a] Abort\n"
        )

    _show_menu()
    while True:
        try:    c = input("Select option: ").strip().lower()
        except EOFError: c = "a"

        if   c == "a": print("Operation cancelled."); sys.exit(0)
        elif c == "r": raise _ReeditPaths("", raw_src, raw_dst)
        elif c == "s": return "skip", proposed_root
        elif c == "c": return "create_copy", proposed_root
        elif c == "n":
            i = 1
            while (dst / f"{base_name}({i})").exists(): i += 1
            new_root = dst / f"{base_name}({i})"
            print(f"  Output folder: {new_root}")
            return "new_folder", new_root
        else:
            print(_yellow("  Please choose from: n  s  c  r  a"))


# ── Path parsing ───────────────────────────────────────────────────────────────

def _tokenize_ampersand(raw: str) -> list:
    """Split a path list on unquoted '&' separators."""
    tokens, current, in_q = [], [], None
    at_start = True
    escape   = False

    for ch in raw:
        if escape:
            current.append(ch); escape = False; at_start = False; continue
        if ch == "\\" and in_q is not None:
            escape = True; continue
        if ch in ('"', "'") and in_q is None and at_start:
            in_q = ch
        elif ch in ('"', "'") and in_q == ch:
            in_q = None
        elif ch == "&" and in_q is None:
            tok = "".join(current).strip()
            tokens.append(tok if tok else None)
            current = []; at_start = True; continue
        else:
            current.append(ch)
        if ch not in (" ", "\t"):
            at_start = False

    tok = "".join(current).strip()
    tokens.append(tok if tok else None)
    return tokens


def parse_sources(raw: str) -> list:
    """Parse source path input into individual paths."""
    return [t for t in _tokenize_ampersand(raw) if t]


# ── Source scanning ────────────────────────────────────────────────────────────

def scan_source(src: Path, dst: Path, skip_no_json: bool) -> dict:
    """Scan a source directory or archive for media and sidecars."""
    sd: dict = {
        "src": src, "is_arc": is_archive(src), "dst": dst,
        "collision_mode": None, "out_root": None,
        "skip_no_json": skip_no_json,
    }

    # ── archive source ────────────────────────────────────────────────────
    if sd["is_arc"]:
        arc_bytes  = src.stat().st_size
        free_bytes = shutil.disk_usage(dst).free

        z, t = _open_archive(src)
        is_tar = (t is not None)
        try:
            if z:
                infos       = z.infolist()
                all_members = [
                    (i.filename, i.filename.endswith("/"))
                    for i in infos if _safe_member(i.filename)
                ]
                uncomp = sum(i.file_size for i in infos)
            else:
                all_members = list(_iter_members(z, t))
                uncomp      = arc_bytes * 4
        finally:
            if z: z.close()
            if t: t.close()

        file_members  = [n for n, d in all_members if not d]
        media_members = [n for n in file_members
                         if Path(n).suffix.lower() in MEDIA_EXT]
        json_members  = [n for n in file_members
                         if Path(n).suffix.lower() == ".json"]

        if not media_members:
            raise ValueError(f"No media files found in archive '{src.name}'.")

        offset, root_name = _find_content_root(file_members)
        base_name         = root_name or src.stem

        # Read all JSON sidecars from the archive into memory now so that the
        # archive does not need to be re-opened during the processing phase.
        # A TarInfo index is pre-built for the same O(1) lookup benefit used
        # during media extraction.
        z, t = _open_archive(src)
        try:
            tar_idx_json = _build_tar_index(t) if t else {}
            json_map: dict = {mn: _read_member_bytes(z, t, mn, tar_idx_json)
                              for mn in json_members}
        finally:
            if z: z.close()
            if t: t.close()

        idx = build_index_from_memory(json_map)
        del json_map   # raw bytes no longer needed; parsed index is sufficient

        # tar archives are not seekable in the general case; always stream
        # them.  For zip archives, use bulk dispatch when disk space permits.
        mode = "stream" if is_tar else (
               "bulk"   if free_bytes >= uncomp * 1.05 else "stream")

        proposed_root  = dst / base_name
        existing_names = (
            {f.name for f in proposed_root.rglob("*") if f.is_file()}
            if proposed_root.exists() else set()
        )
        conflicts = existing_names & {Path(mn).name for mn in media_members}

        sd.update({
            "base_name":     base_name,
            "proposed_root": proposed_root,
            "media_members": media_members,
            "offset":        offset,
            "idx":           idx,
            "mode":          mode,
            "is_tar":        is_tar,
            "uncomp":        uncomp,
            "free_bytes":    free_bytes,
            "conflicts":     conflicts,
            "n_media":       len(media_members),
        })

    # ── directory source ──────────────────────────────────────────────────
    else:
        all_files = [f for f in src.rglob("*") if f.is_file()]
        jfiles, mfiles, afiles = classify(all_files)

        # Nested archives — catalogue without extracting to disk.
        # JSON sidecars (small) are read into memory; media is catalogued
        # as (archive_path, member_name) pairs and streamed on demand.
        nested_catalog: dict = {}   # str(archive) -> (Path, [member_names])
        nested_json_map: dict = {}  # member_path -> bytes

        for af in afiles:
            z, t = _open_archive(af)
            af_media: list = []
            try:
                for mn, is_dir in _iter_members(z, t):
                    if is_dir: continue
                    e = Path(mn).suffix.lower()
                    if e in MEDIA_EXT:
                        af_media.append(mn)
                    elif e == ".json":
                        nested_json_map[mn] = _read_member_bytes(z, t, mn)
            finally:
                if z: z.close()
                if t: t.close()
            if af_media:
                nested_catalog[str(af)] = (af, af_media)

        # Merge indices: disk JSON (idx) takes precedence over nested JSON.
        nested_idx = (build_index_from_memory(nested_json_map)
                      if nested_json_map else {})
        idx        = {**nested_idx, **build_index_from_paths(jfiles)}

        nested_media_names = {
            Path(mn).name
            for _, mems in nested_catalog.values()
            for mn in mems
        }
        nested_count = sum(len(mems) for _, mems in nested_catalog.values())

        if not mfiles and not nested_count:
            raise ValueError(f"No media files found in '{src}'.")

        base_name     = src.name
        proposed_root = dst / base_name
        existing_names = (
            {f.name for f in proposed_root.rglob("*") if f.is_file()}
            if proposed_root.exists() else set()
        )
        conflicts = existing_names & (
            {s.name for s in mfiles} | nested_media_names
        )

        sd.update({
            "base_name":      base_name,
            "proposed_root":  proposed_root,
            "mfiles":         mfiles,
            "nested_catalog": nested_catalog,
            "idx":            idx,
            "conflicts":      conflicts,
            "n_media":        len(mfiles) + nested_count,
        })

    return sd


# ── Processing runner ──────────────────────────────────────────────────────────

def run_source(sd: dict, fields: set, n_workers: int, label: str = "") -> dict:
    """Process one source (archive or directory) and return result counters."""
    src            = sd["src"]
    collision_mode = sd["collision_mode"]
    out_root       = sd["out_root"]
    skip_no_json   = sd["skip_no_json"]
    idx            = sd["idx"]

    out_root.mkdir(parents=True, exist_ok=True)
    successes: list = []
    failures:  list = []
    no_json:   list = []
    no_date:   list = []
    skipped_no_json:   list = []
    skipped_conflict:  list = []

    # ── archive source ────────────────────────────────────────────────────
    if sd["is_arc"]:
        media_members = sd["media_members"]
        offset        = sd["offset"]
        mode          = sd["mode"]

        work = []
        for mn in media_members:
            rel = _member_rel(mn, offset)
            d   = out_root / rel
            jf  = find_json(Path(mn), idx)
            if jf is None and skip_no_json:
                skipped_no_json.append(mn); continue
            if collision_mode == "skip" and d.exists():
                skipped_conflict.append(mn); continue
            work.append((mn, d, jf))

        if not work:
            reason = ("Skipped (no JSON)" if skipped_no_json
                      else "Skipped (conflict)")
            print(f"  {label}: {reason}")

        elif mode == "bulk":
            tasks = [
                {"archive": str(src), "member": mn, "dst": d,
                 "jf": jf, "fields": fields, "collision_mode": collision_mode}
                for mn, d, jf in work
            ]
            s, f, nj, nd = dispatch(tasks, n_workers, desc=label)
            successes.extend(s); failures.extend(f)
            no_json.extend(nj); no_date.extend(nd)

        else:
            # Stream mode — read each member into RAM, process, write once.
            # Opening the archive once for all members avoids repeated seeks.
            z, t = _open_archive(src)
            try:
                # Pre-build a name→TarInfo dict so every member lookup is
                # O(1).  Without it, t.getmember() scans the entire member
                # list for each file, giving O(n²) behaviour on large archives.
                tar_idx = _build_tar_index(t) if t else {}

                with tqdm(total=len(work), unit="file",
                          desc=label, bar_format=_BAR_FMT) as bar:
                    for mn, d, jf in work:
                        try:
                            data = _read_member_bytes(z, t, mn, tar_idx)
                        except Exception as e:
                            failures.append({
                                "src": mn, "reason": f"Read: {e}", "jf": ""})
                            bar.update(1); continue
                        r  = process_file({
                            "member_bytes": data, "member": mn, "dst": d,
                            "jf": jf, "fields": fields,
                            "collision_mode": collision_mode,
                        })
                        st = r["status"]
                        if   st == "ok":      successes.append(r)
                        elif st == "no_json": no_json.append(r["src"])
                        elif st == "no_date": no_date.append((r["src"], r.get("jf", "")))
                        else:                 failures.append(r)
                        bar.update(1)
            finally:
                if z: z.close()
                if t: t.close()

        total = len(media_members)

    # ── directory source ──────────────────────────────────────────────────
    else:
        mfiles         = sd.get("mfiles", [])
        nested_catalog = sd.get("nested_catalog", {})

        # Regular media files — dispatched to the thread pool.
        tasks = []
        for s in mfiles:
            jf = find_json(s, idx)
            if jf is None and skip_no_json:
                skipped_no_json.append(s); continue
            try:    rel = s.relative_to(src)
            except ValueError: rel = Path(s.name)
            d = out_root / rel
            if collision_mode == "skip" and d.exists():
                skipped_conflict.append(s); continue
            tasks.append({
                "src": s, "dst": d, "jf": jf,
                "fields": fields, "collision_mode": collision_mode,
            })

        if tasks:
            s, f, nj, nd = dispatch(tasks, n_workers, desc=label)
            successes.extend(s); failures.extend(f)
            no_json.extend(nj); no_date.extend(nd)

        # Nested archive media — RAM-based, no temporary files, one archive
        # at a time so peak memory is bounded to a single media file.
        for _af_key, (af, media_members) in nested_catalog.items():
            z, t = _open_archive(af)
            try:
                tar_idx = _build_tar_index(t) if t else {}
                for mn in media_members:
                    jf = find_json(Path(mn), idx)
                    if jf is None and skip_no_json:
                        skipped_no_json.append(mn); continue
                    d = out_root / Path(mn).name
                    if collision_mode == "skip" and d.exists():
                        skipped_conflict.append(mn); continue
                    try:
                        data = _read_member_bytes(z, t, mn, tar_idx)
                    except Exception as e:
                        failures.append({
                            "src": mn, "reason": f"Read: {e}", "jf": ""})
                        continue
                    r  = process_file({
                        "member_bytes": data, "member": mn, "dst": d,
                        "jf": jf, "fields": fields,
                        "collision_mode": collision_mode,
                    })
                    st = r["status"]
                    if   st == "ok":      successes.append(r)
                    elif st == "no_json": no_json.append(r["src"])
                    elif st == "no_date": no_date.append((r["src"], r.get("jf", "")))
                    else:                 failures.append(r)
            finally:
                if z: z.close()
                if t: t.close()

        total = sd.get("n_media", len(mfiles))

    return {
        "successes":        successes,
        "failures":         failures,
        "no_json":          no_json,
        "no_date":          no_date,
        "skipped_no_json":  skipped_no_json,
        "skipped_conflict": skipped_conflict,
        "total":            total,
    }


# ── Main ───────────────────────────────────────────────────────────────────────

HELP_TEXT = """\
  Choose multiple paths:
  path1 & path2

  Use quotes:
    '/path/with&symbol'

  Entries (history items):
    ↑/↓   navigate
    !!/!1 reference
    """


def main() -> None:

    # ── locate ExifTool ───────────────────────────────────────────────────
    def _find_exiftool():
        import shutil as _sh, platform as _pl
        is_win  = _pl.system().lower() == "windows"
        root    = (Path(sys.executable).parent
                   if getattr(sys, "frozen", False) else Path(__file__).parent)
        et_name = "exiftool.exe" if is_win else "exiftool"

        # Build a deduplicated candidate list (bundled binary first, then PATH)
        # while preserving probe order.
        _which  = _sh.which("exiftool")
        raw     = [root / et_name] + ([Path(_which)] if _which else [])
        seen, cands = set(), []
        for p in raw:
            key = str(p.resolve())
            if key not in seen:
                seen.add(key); cands.append(p)

        for p in cands:
            try:
                env = os.environ.copy()
                lib = p.parent / "lib"
                if lib.is_dir(): env["PERL5LIB"] = str(lib)
                v = subprocess.check_output(
                    [str(p), "-ver"], text=True, env=env, timeout=10).strip()
                return str(p), v, env, et_name
            except Exception:
                continue
        return None, None, None, et_name

    et_path, ver, et_env, et_name = _find_exiftool()
    if not et_path:
        import platform as _pl
        hints = {
            "linux":   "  sudo apt install libimage-exiftool-perl\n"
                       "  sudo pacman -S perl-image-exiftool",
            "darwin":  "  brew install exiftool",
            "windows": "  https://exiftool.org",
        }
        print(
            "ExifTool not found.\n"
            f"Place '{et_name}' next to this script, or install it:\n"
            + hints.get(_pl.system().lower(), "  https://exiftool.org")
        )
        sys.exit(1)

    global _ET_BIN, _ET_ENV
    _ET_BIN, _ET_ENV = et_path, et_env
    get_et()

    print(_green(f"\n{__app_name__}  v{__version__}  (ExifTool {ver})\n"))

    ok, reason = self_test()
    if not ok:
        print(_red("Self-test failed."))
        print(_red(f"  Reason: {reason}"))
        print("  Ensure ExifTool is installed correctly and the temp "
              "directory is writable.")
        sys.exit(1)

    _init_readline()


    while True:   # outer loop — returns here when the user re-edits paths
        hist_mark = len(_path_history)

        # ── source entry ──────────────────────────────────────────────────
        sources = None
        raw_src = ""
        while sources is None:
            try:
                raw_src = _input_prefilled(
                    "source (? for help): ", raw_src).strip()
            except EOFError:
                sys.exit(0)
            if raw_src.lower() in ("--help", "-h", "?"):
                print(HELP_TEXT); continue
            if not raw_src:
                print(_red("  Source path required.")); continue

            toks = parse_sources(raw_src)
            if not toks:
                print(_red("  Invalid path format.")); continue

            toks, ref_err = _resolve_path_refs(toks)
            if ref_err:
                print(_yellow(f"  {ref_err}")); continue

            bad, good = [], []
            for tok in toks:
                p = Path(os.path.expanduser(tok))
                if not p.exists():
                    hint = ""
                    if '"' in str(p):
                        hint = (f"\n  Tip: the path contains \".  "
                                f"Try single quotes: '{p}'")
                    bad.append(_yellow(f"  Path not found: {p}{hint}"))
                elif not p.is_dir() and not is_archive(p):
                    bad.append(_yellow(f"  Unsupported source type: {p}"))
                else:
                    good.append(p)

            if bad:
                for msg in bad: print(msg)
                continue
            sources = good

        n_src = len(sources)
        _path_history.extend(str(s) for s in sources)

        # ── destination entry ─────────────────────────────────────────────
        dests   = None
        raw_dst = ""
        while dests is None:
            try:
                raw_dst = _input_prefilled(
                    "dest   (? for help): ", raw_dst).strip()
            except EOFError:
                sys.exit(0)
            if raw_dst.lower() in ("--help", "-h", "?"):
                print(HELP_TEXT); continue
            if not raw_dst:
                print(_red("  Destination path required.")); continue

            toks_dst = parse_sources(raw_dst)
            if not toks_dst:
                print(_red("  Invalid path format.")); continue

            toks_dst, ref_err = _resolve_path_refs(toks_dst)
            if ref_err:
                print(_yellow(f"  {ref_err}")); continue

            n_given = len(toks_dst)
            if n_src == 1:
                path_strs, n_extra, err = toks_dst, 0, ""
            elif n_given == 1:
                path_strs, n_extra, err = toks_dst * n_src, 0, ""
            elif n_given < n_src:
                path_strs, n_extra, err = [], 0, _red(
                    f"  Got {n_given} destination(s) for {n_src} source(s).  "
                    f"Provide one per source, use !n to repeat, or enter a "
                    f"single path to send all sources there.")
            else:
                n_extra   = n_given - n_src
                path_strs = toks_dst[:n_src]
                err       = ""

            if err:
                print(err); continue
            if n_extra:
                print(f"  Note: {n_extra} extra destination(s) ignored.")

            validated, valid = [], True
            for p_str in path_strs:
                p = Path(os.path.expanduser(p_str))
                if p.exists() and not p.is_dir():
                    print(_red(f"  Not a directory: {p}"))
                    valid = False; break
                try:
                    p.mkdir(parents=True, exist_ok=True)
                except Exception as ex:
                    print(_red(f"  Cannot create '{p}': {ex}"))
                    valid = False; break
                validated.append(p)
            if not valid:
                continue
            dests = validated

        _path_history.extend(str(d) for d in dests)

        if len(sources) == 1 and len(dests) > 1:
            pairs = list(zip(sources * len(dests), dests))
        else:
            pairs = list(zip(sources, dests))

        # ── phase 1: scan ─────────────────────────────────────────────────
        scans, scan_err = [], False
        for src, dst in pairs:
            try:
                scans.append(scan_source(src, dst, False))
            except ValueError as e:
                print(_red(f"  Error: {e}")); scan_err = True
            except Exception as e:
                print(_red(f"  Error scanning {src}: {e}")); scan_err = True
        if scan_err:
            print(_yellow("  Correct the paths above and try again."))
            _path_history[hist_mark:] = []
            continue

        # ── phase 2: preview ──────────────────────────────────────────────
        print()
        for sd, (src, _dst) in zip(scans, pairs):
            out_path = sd.get("proposed_root", _dst)
            n        = sd.get("n_media", "?")
            print(f"  {src}  →  {out_path}  "
                  f"({n} media file{'s' if n != 1 else ''})")

        # ── phase 3: conflict resolution and confirmation ─────────────────
        has_conflicts = any(sd["conflicts"] for sd in scans)
        try:
            if has_conflicts:
                print()
                for sd, (src, dst) in zip(scans, pairs):
                    if not sd["conflicts"]:
                        sd["collision_mode"] = None
                        sd["out_root"]       = sd["proposed_root"]
                        continue
                    cm, out_root = conflict_prompt(
                        sd["proposed_root"], sd["conflicts"],
                        dst, sd["base_name"],
                        raw_src=raw_src, raw_dst=raw_dst,
                    )
                    sd["collision_mode"] = cm
                    sd["out_root"]       = out_root
            else:
                print()
                print("Continue?  "
                      "[y/enter] Proceed   [r] Re-edit paths   [a] Abort")
                try:    ans = input("> ").strip().lower()
                except EOFError: ans = "a"
                if ans == "a":
                    print("Operation cancelled."); sys.exit(0)
                elif ans == "r":
                    raise _ReeditPaths("", raw_src, raw_dst)
                for sd in scans:
                    sd["collision_mode"] = None
                    sd["out_root"]       = sd["proposed_root"]

        except _ReeditPaths as _rp:
            _path_history[hist_mark:] = []
            # Do not prefill — the user starts with a blank prompt and
            # can recall any previous entry with ↑ / ↓ via readline.
            continue

        break   # path entry and conflict resolution complete

    # ── field selection and worker count ──────────────────────────────────
    fields, skip_no_json = ask_fields()
    for sd in scans:
        sd["skip_no_json"] = skip_no_json

    _MAX_WORKERS = 32
    _default_w   = min(8, os.cpu_count() or 4)
    try:
        w_raw = input(
            f"Worker threads (enter = {_default_w}, max {_MAX_WORKERS}): "
        ).strip()
    except EOFError:
        w_raw = ""

    n_workers = _default_w
    if w_raw:
        if not w_raw.isdigit():
            print(_yellow(f"  '{w_raw}' is not a valid integer — "
                          f"using {_default_w}."))
        else:
            w_int = int(w_raw)
            if w_int < 1:
                print(_yellow(f"  Value must be at least 1 — "
                              f"using {_default_w}."))
            elif w_int > _MAX_WORKERS:
                print(_yellow(f"  Value exceeds the maximum of {_MAX_WORKERS} — "
                              f"using {_MAX_WORKERS}."))
                n_workers = _MAX_WORKERS
            else:
                n_workers = w_int

    # ── phase 4: process ──────────────────────────────────────────────────
    print()
    all_results = []
    n_pairs     = len(pairs)
    for i, (sd, (src, _dst)) in enumerate(zip(scans, pairs), 1):
        prefix = f"[{i}/{n_pairs}] " if n_pairs > 1 else ""
        label  = f"{prefix}{src.name}  →  {sd['out_root'].name}"
        all_results.append(run_source(sd, fields, n_workers, label=label))

    # ── phase 5: logs and summary ─────────────────────────────────────────
    grand = {k: 0 for k in ("success", "fail", "no_json", "no_date", "total")}

    for sd, res in zip(scans, all_results):
        grand["success"]  += len(res["successes"])
        grand["fail"]     += len(res["failures"])
        grand["no_json"]  += len(res["no_json"])
        grand["no_date"]  += len(res["no_date"])
        grand["total"]    += res["total"]
        write_logs(
            sd["out_root"],
            res["successes"], res["failures"],
            res["no_json"],   res["no_date"],
            res["skipped_no_json"], res["skipped_conflict"],
        )

    t = grand["total"]
    print()
    if len(scans) > 1:
        print(f"GRAND TOTAL  ({len(scans)} sources)")
    print(_green(f"  Success : {grand['success']:>5} / {t}"))
    if grand["no_json"]: print(        f"  No JSON : {grand['no_json']:>5} / {t}")
    if grand["no_date"]: print(        f"  No date : {grand['no_date']:>5} / {t}")
    if grand["fail"]:    print(_red(  f"  Failed  : {grand['fail']:>5} / {t}"))
    print()
    print("  Logs written to the _logs folder inside each output directory.")


# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    # Handle lightweight flags before any setup so they work even without
    # ExifTool installed.
    if len(sys.argv) > 1:
        _flag = sys.argv[1].lower()
        if _flag in ("--version", "-v"):
            print(f"{__app_name__} v{__version__}")
            sys.exit(0)
        if _flag in ("--help", "-h"):
            print(f"{__app_name__} v{__version__}")
            print("Run without arguments to start the interactive interface.")
            sys.exit(0)

    multiprocessing.freeze_support()
    import signal

    _orig_input = input
    _in_prompt  = False

    def _tracked_input(prompt=""):
        global _in_prompt
        _in_prompt = True
        try:    return _orig_input(prompt)
        finally: _in_prompt = False

    input = _tracked_input   # shadow built-in so SIGINT handler can detect state

    def _read_single_char() -> str:
        """Read a single keypress without requiring Enter."""
        try:
            fd = sys.stdin.fileno()
            if not os.isatty(fd): return ""
            import termios
            old = termios.tcgetattr(fd)
            new = termios.tcgetattr(fd)
            # Disable line buffering and echo only; keep ISIG, OPOST, and all
            # other flags so readline's internal terminal state is undisturbed.
            new[3] = new[3] & ~(termios.ICANON | termios.ECHO)
            new[6][termios.VMIN]  = 1
            new[6][termios.VTIME] = 0
            termios.tcsetattr(fd, termios.TCSANOW, new)
            try:
                ch = os.read(fd, 1).decode("utf-8", "ignore").lower()
            finally:
                termios.tcsetattr(fd, termios.TCSANOW, old)
            return ch
        except Exception:
            pass
        try:
            import msvcrt
            return msvcrt.getch().decode("utf-8", "ignore").lower()
        except Exception:
            return ""

    def sigint_handler(sig, frame):
        sys.stdout.write("\nQuit? [y/n] ")
        sys.stdout.flush()
        ans = _read_single_char()
        if ans in ("y", "\x03", "\x04"):
            sys.stdout.write("y\n")
            sys.stdout.flush()
            # atexit handlers are NOT invoked by os._exit(), so ExifTool
            # processes are killed directly.  _close_et_procs() is skipped
            # because it acquires _et_procs_lock; if a worker holds that lock
            # when the signal arrives, waiting for it would deadlock.
            for _p in list(_et_procs):
                try: _p.kill()
                except Exception: pass
            os._exit(0)
        sys.stdout.write("n\n")
        sys.stdout.flush()
        if _in_prompt:
            # Sending SIGWINCH to ourselves (queued, delivered after this
            # handler returns) causes readline to call rl_forced_update_display,
            # which performs a full prompt + buffer redraw — far more reliable
            # than redisplay() alone after any terminal-mode operation.
            # Falls back to redisplay() on platforms without SIGWINCH (Windows).
            try:
                if hasattr(signal, "SIGWINCH"):
                    os.kill(os.getpid(), signal.SIGWINCH)
                else:
                    import readline
                    readline.redisplay()
            except Exception:
                pass

    signal.signal(signal.SIGINT, sigint_handler)

    try:
        main()
    except EOFError:
        sys.exit(0)
