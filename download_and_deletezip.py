import time
import ctypes
import subprocess
import os
import tempfile
import zipfile
from pathlib import Path

# ==================================================
# CONFIGURATION
# ==================================================

DOWNLOAD_DIR = Path(r"C:\Users\ar559\Downloads")
DESTINATION_DIR = Path(r"C:\ARYAN\GAMES")
WINRAR = Path(r"C:\Program Files\WinRAR\WinRAR.exe")
TARGET_ZIP = DOWNLOAD_DIR / "COD-BO-II-SteamRIP.com.rar"

CHECK_INTERVAL = 10           # seconds between scans
STABLE_CHECKS = 6             # unchanged checks needed (~60 s)
MAX_FAILURES = 3              # give up after this many failed attempts
DELETE_AFTER_EXTRACT = False  # set True to delete the archive after extraction
TEST_BEFORE_EXTRACT = False   # set True to run a full integrity test first (slow)
SLEEP_AFTER_EXTRACT = True    # put the laptop to sleep when finished

BATCH_EXTRACT = False       # extract in batches instead of all at once
BATCH_SIZE_GB = 20            # max uncompressed size per WinRAR run
# Free Download Manager uses .fdmdownload for unfinished files
TEMP_EXTENSIONS = (
    ".fdmdownload",
    ".crdownload",
    ".part",
    ".tmp",
    ".download",
)

# Keep Windows awake (screen may still turn off / lock)
ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001


def prevent_sleep():
    ctypes.windll.kernel32.SetThreadExecutionState(
        ES_CONTINUOUS | ES_SYSTEM_REQUIRED
    )


def restore_sleep():
    ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS)


# ==================================================
# DOWNLOAD COMPLETION CHECKS
# ==================================================

def has_temporary_download(archive):
    """True if a partial-download companion file exists (e.g. game.rar.fdmdownload)."""
    return any(
        archive.with_name(archive.name + ext).exists()
        for ext in TEMP_EXTENSIONS
    )


def wait_until_stable(archive):
    previous = None
    stable_count = 0

    print("Waiting for download to finish...")

    while stable_count < STABLE_CHECKS:
        if not archive.exists():
            return False

        if has_temporary_download(archive):
            stable_count = 0
            previous = None
            time.sleep(CHECK_INTERVAL)
            continue

        stat = archive.stat()
        current = (stat.st_size, stat.st_mtime_ns)

        if current == previous:
            stable_count += 1
        else:
            stable_count = 0

        previous = current
        print(f"Stable checks: {stable_count}/{STABLE_CHECKS}")
        time.sleep(CHECK_INTERVAL)

    return True


# ==================================================
# ARCHIVE LISTING (ZIP and RAR)
# ==================================================

def find_console_rar():
    """Rar.exe / UnRAR.exe live next to WinRAR.exe and can print file listings."""
    for name in ("UnRAR.exe", "Rar.exe"):
        p = WINRAR.parent / name
        if p.exists():
            return p
    return None


def list_entries(archive):
    """Return [(relative_name, size), ...] for files in the archive,
    or None if the listing could not be read."""
    # ZIP: read the index with Python
    if zipfile.is_zipfile(archive):
        try:
            with zipfile.ZipFile(archive) as z:
                return [(i.filename, i.file_size)
                        for i in z.infolist() if not i.is_dir()]
        except Exception:
            return None

    # RAR (and others): ask UnRAR/Rar for a technical listing
    console = find_console_rar()
    if console is None:
        return None

    try:
        result = subprocess.run(
            [str(console), "vt", "-p-", "-scfc", str(archive)],
            capture_output=True,
        )
    except OSError:
        return None

    if result.returncode not in (0, 1):
        return None

    text = result.stdout.decode("utf-8", errors="replace")

    entries = []
    name, kind, size = None, None, 0

    def finish():
        if name is not None and kind == "File":
            entries.append((name, size))

    for line in text.splitlines():
        stripped = line.strip()
        if ":" not in stripped:
            continue
        key, _, value = stripped.partition(":")
        value = value[1:] if value.startswith(" ") else value
        key = key.strip()

        if key == "Name":
            finish()
            name, kind, size = value.rstrip("\r\n"), None, 0
        elif key == "Type" and name is not None:
            kind = value.strip()
        elif key == "Size" and name is not None:
            try:
                size = int(value.strip())
            except ValueError:
                size = 0

    finish()
    return entries or None


def build_batches(entries, output_folder, limit_bytes):
    """Group entries into batches of at most limit_bytes.
    Files already extracted with the correct size are skipped (resume support)."""
    pending = []
    for name, size in entries:
        target = output_folder / name
        try:
            if target.exists() and target.stat().st_size == size:
                continue
        except OSError:
            pass
        pending.append((name, size))

    batches, current, current_size = [], [], 0
    for name, size in pending:
        if current and current_size + size > limit_bytes:
            batches.append((current, current_size))
            current, current_size = [], 0
        current.append(name)
        current_size += size
    if current:
        batches.append((current, current_size))
    return batches


# ==================================================
# PROGRESS HELPERS
# ==================================================

def folder_size(folder):
    total = 0
    for root, _, files in os.walk(folder):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
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
        bar_len = 30
        filled = int(bar_len * fraction)
        bar = "#" * filled + "-" * (bar_len - filled)
        eta = (total - done) / speed if speed > 0 else 0
        line = (f"\r[{bar}] {fraction * 100:5.1f}%  "
                f"{gb_done:.1f}/{total / 1024**3:.1f} GB  "
                f"{speed / 1024**2:.0f} MB/s  ETA {format_time(eta)}   ")
    else:
        line = f"\rExtracted {gb_done:.1f} GB  {speed / 1024**2:.0f} MB/s   "

    print(line, end="", flush=True)


# ==================================================
# WINRAR HELPERS
# ==================================================

# -y     assume Yes on all queries
# -p-    don't ask for a password (fail instead)
# -ibck  run in background
# -inul  disable error message boxes (prevents freezing)
COMMON_FLAGS = ["-y", "-p-", "-ibck", "-inul"]


def test_archive(archive):
    print("Testing archive integrity... (this can take a long time)")

    result = subprocess.run(
        [str(WINRAR), "t", *COMMON_FLAGS, str(archive)],
        capture_output=True,
        text=True,
    )

    if result.returncode == 0:
        print("Archive integrity verified!")
        return True

    print(f"Archive test failed (exit code {result.returncode}).")
    return False


def run_winrar_extract(archive, output_folder, names=None, progress=None):
    """Extract the whole archive (names=None) or only the listed files.
    Returns WinRAR's exit code."""
    list_path = None
    try:
        args = [str(WINRAR), "x", *COMMON_FLAGS, "-o+"]

        if names is not None:
            with tempfile.NamedTemporaryFile(
                "w", suffix=".lst", delete=False, encoding="utf-8"
            ) as f:
                f.write("\n".join(names))
                list_path = f.name
            args += ["-scfl"]            # list file is UTF-8
            args += [str(archive), "@" + list_path]
        else:
            args += [str(archive)]

        args.append(str(output_folder) + "\\")

        process = subprocess.Popen(
            args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
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


def extract_archive(archive):
    """Returns True on success, False on failure."""
    archive = archive.resolve()

    print("\n" + "=" * 50)
    print("Detected:", archive.name)
    print("=" * 50)

    if not wait_until_stable(archive):
        print("Download is not ready.")
        return False

    if TEST_BEFORE_EXTRACT and not test_archive(archive):
        return False

    output_folder = DESTINATION_DIR / archive.stem
    output_folder.mkdir(parents=True, exist_ok=True)

    entries = list_entries(archive)
    total_all = sum(size for _, size in entries) if entries else 0

    # Decide: batches, or one single run
    batches = None
    if BATCH_EXTRACT and entries:
        batches = build_batches(entries, output_folder, BATCH_SIZE_GB * 1024**3)
        if not batches:
            print("Nothing left to extract (all files already present).")
            return True
        print(f"{sum(len(b[0]) for b in batches)} files in {len(batches)} "
              f"batch(es) of up to {BATCH_SIZE_GB} GB.")
    elif BATCH_EXTRACT:
        print("Could not read the file list; extracting everything in one run.")

    overall_start = time.time()

    if batches:
        total_size = sum(size for _, size in batches)
        done_before = 0

        for number, (names, batch_size) in enumerate(batches, start=1):
            print(f"\n--- Batch {number}/{len(batches)}: "
                  f"{len(names)} files, {batch_size / 1024**3:.1f} GB ---")

            baseline = folder_size(output_folder)

            def show():
                batch_done = max(folder_size(output_folder) - baseline, 0)
                print_progress(done_before + batch_done, total_size, overall_start)

            code = run_winrar_extract(archive, output_folder, names, progress=show)
            show()
            print()

            if code not in (0, 1):
                print(f"BATCH {number} FAILED (exit code {code}).")
                return False

            done_before += batch_size
    else:
        baseline = folder_size(output_folder)
        print("Extracting game...")

        def show():
            done = max(folder_size(output_folder) - baseline, 0)
            print_progress(done, total_all, overall_start)

        code = run_winrar_extract(archive, output_folder, None, progress=show)
        show()
        print()

        if code not in (0, 1):
            print(f"\nEXTRACTION FAILED (exit code {code})!")
            return False

    # Sanity check: make sure something was actually extracted
    if not any(output_folder.iterdir()):
        print("\nExtraction produced no files. Keeping archive.")
        return False

    print("\nEXTRACTION SUCCESSFUL!")
    print("Game extracted to:", output_folder)

    if DELETE_AFTER_EXTRACT:
        try:
            archive.unlink()
            print("Deleted:", archive.name)
        except OSError as e:
            print("Could not delete archive:", e)

    return True


# ==================================================
# MAIN LOOP
# ==================================================

def monitor():
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    DESTINATION_DIR.mkdir(parents=True, exist_ok=True)

    if not WINRAR.exists():
        print("ERROR: WinRAR executable not found at:", WINRAR)
        return

    print("=" * 50)
    print("       AUTOMATIC GAME EXTRACTOR")
    print("=" * 50)
    print("Watching file    :", TARGET_ZIP)
    print("Extraction folder:", DESTINATION_DIR)
    print("\nPreventing Windows sleep...")
    prevent_sleep()

    failures = 0

    try:
        while True:
            # FDM only creates the final file once the download completes,
            # so until then this file simply doesn't exist yet.
            if TARGET_ZIP.exists():
                try:
                    if extract_archive(TARGET_ZIP):
                        if SLEEP_AFTER_EXTRACT:
                            print("All done. Putting the laptop to sleep in 30 seconds...")
                            print("Press Ctrl+C now to cancel.")
                            time.sleep(30)
                            restore_sleep()  # allow sleep before triggering it
                            os.system("rundll32.exe powrprof.dll,SetSuspendState 0,1,0")
                        else:
                            print("All done.")
                        return

                    # Extraction failed: count it unless the download is still running
                    if TARGET_ZIP.exists() and not has_temporary_download(TARGET_ZIP):
                        failures += 1
                        print(f"Attempt {failures}/{MAX_FAILURES} failed.")
                        if failures >= MAX_FAILURES:
                            print("Giving up. The archive may be corrupt or incomplete.")
                            return

                except (OSError, subprocess.SubprocessError) as e:
                    print("Error:", e)
            else:
                print("Waiting for", TARGET_ZIP.name, "to appear...")

            time.sleep(CHECK_INTERVAL)

    except KeyboardInterrupt:
        print("\nStopping monitoring...")

    finally:
        print("Restoring normal sleep settings...")
        restore_sleep()
        print("Monitor stopped.")


if __name__ == "__main__":
    monitor()