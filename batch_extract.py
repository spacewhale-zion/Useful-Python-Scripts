"""
batch_extract.py - extract a large archive in batches using WinRAR.

Usage:
    python batch_extract.py "C:\\path\\game.rar"
    python batch_extract.py "C:\\path\\game.rar" -d "D:\\Games\\game" -s 20
    python batch_extract.py "C:\\path\\game.rar" --no-resume
    python batch_extract.py "C:\\path\\game.rar" -s 20 --delete-after-batch
"""

import argparse
import ctypes
import json
import os
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path

# ==================================================
# DEFAULTS (can be overridden on the command line)
# ==================================================

WINRAR = Path(r"C:\Program Files\WinRAR\WinRAR.exe")
DEFAULT_DESTINATION = Path(r"C:\ARYAN\GAMES")
DEFAULT_BATCH_GB = 20
STATE_FILE_NAME = ".batch_extract_state.json"

# -y = yes to all, -p- = never ask for password, -ibck = background,
# -inul = no error dialogs (prevents freezing)
COMMON_FLAGS = ["-y", "-p-", "-ibck", "-inul"]

EXIT_CODES = {
    0: "success",
    1: "warning (non-fatal)",
    2: "fatal error",
    3: "CRC error / wrong password",
    4: "archive is locked",
    5: "write error (disk full or no permission?)",
    6: "file open error",
    7: "command line error",
    8: "not enough memory",
    9: "file create error",
    10: "no files matched",
    255: "stopped by user",
}

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001


def prevent_sleep():
    ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)


def restore_sleep():
    ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS)


# ==================================================
# LISTING THE ARCHIVE
# ==================================================

def find_console_rar():
    for name in ("UnRAR.exe", "Rar.exe"):
        p = WINRAR.parent / name
        if p.exists():
            return p
    return None

def parse_technical_listing(text):
    """Parse the 'Name: / Type: / Size:' blocks printed by Rar.exe 'vt'."""
    entries = []
    name, kind, size = None, None, 0

    def finish():
        if name is not None and kind == "File":
            entries.append((name, size))

    for line in text.splitlines():
        if ":" not in line:
            continue
        key, _, value = line.strip().partition(":")
        key = key.strip()
        value = value[1:] if value.startswith(" ") else value
        if key == "Name":
            finish()
            name, kind, size = value, None, 0
        elif key == "Type" and name is not None:
            kind = value.strip()
        elif key == "Size" and name is not None:
            try:
                size = int(value.strip())
            except ValueError:
                size = 0
    finish()
    return entries


def parse_7z_listing(text):
    """Parse '7z l -slt' output (Path = / Size = / Attributes = blocks)."""
    entries = []
    block = {}

    def finish():
        if ("Path" in block and "Size" in block
                and block.get("Folder", "-") != "+"
                and not block.get("Attributes", "").startswith("D")):
            try:
                entries.append((block["Path"], int(block["Size"] or 0)))
            except ValueError:
                pass

    in_files = False
    for line in text.splitlines():
        if line.startswith("----------"):
            in_files = True
            continue
        if not in_files:
            continue
        if not line.strip():
            finish()
            block = {}
            continue
        key, sep, value = line.partition(" = ")
        if sep:
            block[key.strip()] = value.strip()
    finish()
    return entries


def list_entries(archive):
    """Return [(name, size), ...] or None if the archive can't be listed."""
    # 1) ZIP: Python can read the index directly
    if zipfile.is_zipfile(archive):
        try:
            with zipfile.ZipFile(archive) as z:
                return [(i.filename, i.file_size)
                        for i in z.infolist() if not i.is_dir()]
        except Exception:
            pass

    # 2) Rar.exe / UnRAR.exe: 'vt' = verbose technical listing
    console = find_console_rar()
    if console:
        try:
            r = subprocess.run(
                [str(console), "vt", "-p-", "-scfc", str(archive)],
                capture_output=True,
                stdin=subprocess.DEVNULL,
            )
            if r.returncode in (0, 1):
                entries = parse_technical_listing(
                    r.stdout.decode("utf-8", errors="replace"))
                if entries:
                    return entries
        except OSError:
            pass

    # 3) 7-Zip, if installed. No password switch and stdin closed, so an
    #    encrypted archive fails instead of waiting for input.
    for seven in (r"C:\Program Files\7-Zip\7z.exe",
                  r"C:\Program Files (x86)\7-Zip\7z.exe"):
        if Path(seven).exists():
            try:
                r = subprocess.run(
                    [seven, "l", "-slt", str(archive)],
                    capture_output=True,
                    stdin=subprocess.DEVNULL,
                )
                entries = parse_7z_listing(
                    r.stdout.decode("utf-8", errors="replace"))
                if entries:
                    return entries
            except OSError:
                pass

    return None


# ==================================================
# RESUME STATE
# ==================================================

def archive_fingerprint(archive):
    st = archive.stat()
    return {"size": st.st_size, "mtime_ns": st.st_mtime_ns}


def load_state(output_folder, fingerprint):
    """Names of files from batches that were fully extracted and verified.
    Ignored if the archive changed since the state was written."""
    try:
        data = json.loads((output_folder / STATE_FILE_NAME).read_text(encoding="utf-8"))
        if data.get("archive") == fingerprint:
            return set(data.get("done", []))
    except (OSError, ValueError):
        pass
    return set()


def save_state(output_folder, fingerprint, done_names):
    path = output_folder / STATE_FILE_NAME
    tmp = path.with_suffix(".tmp")
    tmp.write_text(
        json.dumps({"archive": fingerprint, "done": sorted(done_names)}),
        encoding="utf-8",
    )
    os.replace(tmp, path)


# ==================================================
# BATCHES, VERIFICATION, PROGRESS
# ==================================================

def build_batches(entries, limit_bytes):
    """Group (name, size) entries into batches of at most limit_bytes.
    Returns [(list_of_entries, batch_size), ...]."""
    batches, current, current_size = [], [], 0
    for name, size in entries:
        if current and current_size + size > limit_bytes:
            batches.append((current, current_size))
            current, current_size = [], 0
        current.append((name, size))
        current_size += size
    if current:
        batches.append((current, current_size))
    return batches


def safe_target(output_folder, name):
    """Path of an archive entry inside output_folder, or None if it would escape it."""
    root = output_folder.resolve()
    target = (root / name).resolve()
    try:
        target.relative_to(root)
    except ValueError:
        return None
    return target


def clear_stale_files(output_folder, batch):
    """Remove leftover copies of this batch's files (e.g. partial files from an
    interrupted run) so progress is measured accurately. They are re-extracted anyway."""
    for name, _ in batch:
        target = safe_target(output_folder, name)
        if target is not None and target.is_file():
            try:
                target.unlink()
            except OSError:
                pass


def verify_batch(output_folder, batch):
    """Check that every file of the batch exists with the expected size."""
    problems = []
    for name, size in batch:
        target = safe_target(output_folder, name)
        if target is None:
            problems.append(f"unsafe path: {name}")
        elif not target.is_file():
            problems.append(f"missing: {name}")
        else:
            try:
                if target.stat().st_size != size:
                    problems.append(f"size mismatch: {name}")
            except OSError:
                problems.append(f"unreadable: {name}")
    return problems


def batch_bytes_written(output_folder, batch):
    """Bytes of this batch's files currently on disk (only these files are checked)."""
    total = 0
    for name, size in batch:
        target = safe_target(output_folder, name)
        if target is None:
            continue
        try:
            total += min(target.stat().st_size, size)
        except OSError:
            pass
    return total


def format_time(seconds):
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h:d}:{m:02d}:{s:02d}"


def print_progress(done, total, start_time):
    elapsed = time.time() - start_time
    speed = done / elapsed if elapsed > 0 else 0
    gb_done = done / 1024**3

    if total > 0:
        fraction = min(done / total, 1.0)
        filled = int(30 * fraction)
        bar = "#" * filled + "-" * (30 - filled)
        eta = (total - done) / speed if speed > 0 else 0
        line = (f"\r[{bar}] {fraction * 100:5.1f}%  "
                f"{gb_done:.1f}/{total / 1024**3:.1f} GB  "
                f"{speed / 1024**2:.0f} MB/s  ETA {format_time(eta)}   ")
    else:
        line = f"\rExtracted {gb_done:.1f} GB  {speed / 1024**2:.0f} MB/s   "
    print(line, end="", flush=True)


# ==================================================
# EXTRACTION
# ==================================================

def run_winrar_extract(archive, output_folder, names, progress=None):
    """Extract only the listed files. Returns WinRAR's exit code."""
    list_path = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", suffix=".lst", delete=False, encoding="utf-8"
        ) as f:
            f.write("\n".join(names))
            list_path = f.name

        args = [str(WINRAR), "x", *COMMON_FLAGS, "-o+", "-scfl",
                str(archive), "@" + list_path, str(output_folder) + "\\"]

        process = subprocess.Popen(
            args,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        while process.poll() is None:
            if progress:
                progress()
            time.sleep(2)
        return process.returncode
    finally:
        if list_path:
            try:
                os.unlink(list_path)
            except OSError:
                pass


def batch_extract(archive, output_folder, batch_gb,
                  delete_after_batch, resume, verify):
    output_folder.mkdir(parents=True, exist_ok=True)

    print("Reading archive file list...")
    entries = list_entries(archive)
    if not entries:
        print("Could not read the file list of this archive.")
        print("Make sure UnRAR.exe/Rar.exe exists next to WinRAR.exe (for RAR files),")
        print("or install 7-Zip (for 7z and other formats).")
        return False

    fingerprint = archive_fingerprint(archive)
    use_state = resume and not delete_after_batch
    done_names = load_state(output_folder, fingerprint) if use_state else set()

    pending = [e for e in entries if e[0] not in done_names]
    if done_names:
        print(f"Resuming: {len(done_names)} files already completed in earlier runs.")

    batches = build_batches(pending, int(batch_gb * 1024**3))
    if not batches:
        print("Nothing left to extract.")
        return True

    total_size = sum(size for _, size in batches)
    total_files = sum(len(b) for b, _ in batches)
    print(f"{total_files} files, {total_size / 1024**3:.1f} GB, "
          f"{len(batches)} batch(es) of up to {batch_gb} GB.")

    start = time.time()
    done_before = 0

    for number, (batch, batch_size) in enumerate(batches, start=1):
        print(f"\n--- Batch {number}/{len(batches)}: "
              f"{len(batch)} files, {batch_size / 1024**3:.1f} GB ---")

        clear_stale_files(output_folder, batch)

        def show():
            written = batch_bytes_written(output_folder, batch)
            print_progress(done_before + written, total_size, start)

        code = run_winrar_extract(
            archive, output_folder, [n for n, _ in batch], progress=show)
        show()
        print()

        if code not in (0, 1):
            reason = EXIT_CODES.get(code, "unknown error")
            print(f"BATCH {number} FAILED (WinRAR exit code {code}: {reason}).")
            return False

        # Never trust the exit code alone: check the files themselves
        if verify:
            problems = verify_batch(output_folder, batch)
            if problems:
                print(f"BATCH {number} FAILED verification "
                      f"(exit code {code}, {len(problems)} problem(s)):")
                for p in problems[:10]:
                    print("  ", p)
                if len(problems) > 10:
                    print(f"   ... and {len(problems) - 10} more")
                return False

        if code == 1:
            print("Note: WinRAR reported warnings for this batch, but all "
                  "expected files are present with the correct size.")

        done_before += batch_size

        if delete_after_batch:
            print("Deleting this batch's extracted files...")
            for name, _ in batch:
                target = safe_target(output_folder, name)
                if target is not None:
                    try:
                        target.unlink()
                    except OSError:
                        pass
        elif use_state:
            done_names.update(n for n, _ in batch)
            save_state(output_folder, fingerprint, done_names)

    print("\nDONE.")
    if not delete_after_batch:
        print("Extracted to:", output_folder)
    return True


# ==================================================
# ENTRY POINT
# ==================================================

def main():
    parser = argparse.ArgumentParser(
        description="Extract an archive in batches using WinRAR.")
    parser.add_argument("archive", nargs="?", help="path to the archive")
    parser.add_argument("-d", "--dest", help="destination folder "
                        "(default: <DEFAULT_DESTINATION>\\<archive name>)")
    parser.add_argument("-s", "--size", type=float, default=DEFAULT_BATCH_GB,
                        help=f"batch size in GB (default {DEFAULT_BATCH_GB})")
    parser.add_argument("--no-resume", action="store_true",
                        help="ignore saved progress and extract everything again")
    parser.add_argument("--no-verify", action="store_true",
                        help="skip the per-batch check of file existence and size")
    parser.add_argument("--delete-after-batch", action="store_true",
                        help="delete each batch's extracted files after it is "
                             "extracted (test mode: you will NOT keep the files)")
    args = parser.parse_args()

    archive_str = args.archive or input("Archive path: ").strip().strip('"')
    archive = Path(archive_str)

    if not archive.is_file():
        print("File not found:", archive)
        sys.exit(1)
    if not WINRAR.exists():
        print("WinRAR not found at:", WINRAR)
        sys.exit(1)
    if args.size <= 0:
        print("Batch size must be greater than 0.")
        sys.exit(1)

    output_folder = Path(args.dest) if args.dest else DEFAULT_DESTINATION / archive.stem

    print("=" * 50)
    print("          BATCH EXTRACTOR")
    print("=" * 50)
    print("Archive    :", archive)
    print("Destination:", output_folder)
    print("Batch size :", args.size, "GB")
    if args.delete_after_batch:
        print("Mode       : DELETE files after each batch (nothing is kept!)")
    print()

    prevent_sleep()
    try:
        ok = batch_extract(
            archive.resolve(), output_folder, args.size,
            delete_after_batch=args.delete_after_batch,
            resume=not args.no_resume,
            verify=not args.no_verify,
        )
    except KeyboardInterrupt:
        print("\nStopped by user. Run the same command again to resume.")
        ok = False
    finally:
        restore_sleep()

    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()