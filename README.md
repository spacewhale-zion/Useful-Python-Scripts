# Useful Python Scripts

Small Windows utilities for keeping a computer awake and managing large archive downloads and extraction jobs.

## Requirements

- Windows
- Python 3.9 or newer
- WinRAR installed at `C:\Program Files\WinRAR\WinRAR.exe` for the archive scripts
- `UnRAR.exe` or `Rar.exe` next to `WinRAR.exe` when the scripts need to inspect a RAR archive
- 7-Zip can be used as a fallback for archive listing in `batch_extract.py`

The scripts use only Python's standard library. No `pip install` step is required.

Run commands from this folder:

```powershell
cd "C:\ARYAN\STUDY\C++\Python_Useful_scripts"
```

## Scripts

### `don'tsleep.py`

Keeps Windows awake and keeps the display on for two hours. At the end, or when interrupted with `Ctrl+C`, it restores normal sleep behavior.

Run it with:

```powershell
python ".\don'tsleep.py"
```

Change `DURATION` in the file to change the runtime. For example, one hour is:

```python
DURATION = 1 * 60 * 60
```

This script is Windows-only because it calls `ctypes.windll.kernel32`.

### `batch_extract.py`

Extracts a large archive in batches through WinRAR. It:

- reads file sizes from ZIP/RAR archives before extraction;
- groups files into batches, 20 GB by default;
- shows progress, speed, and an estimated time remaining;
- verifies that each extracted file exists and has the expected size;
- saves completed file names in `.batch_extract_state.json` so an interrupted run can resume;
- prevents Windows from sleeping during extraction.

Basic usage:

```powershell
python ".\batch_extract.py" "C:\path\to\archive.rar"
```

The default destination is:

```text
C:\ARYAN\GAMES\<archive-name-without-extension>
```

Useful options:

```powershell
# Set a custom destination and use 10 GB batches
python ".\batch_extract.py" "C:\path\to\archive.rar" -d "D:\Games\MyGame" -s 10

# Ignore saved resume state and extract everything again
python ".\batch_extract.py" "C:\path\to\archive.rar" --no-resume

# Skip file existence/size verification
python ".\batch_extract.py" "C:\path\to\archive.rar" --no-verify
```

If no archive path is supplied, the script prompts for one:

```powershell
python ".\batch_extract.py"
```

#### Test-only batch mode

`--delete-after-batch` extracts and verifies each batch, then deletes that batch's files. This is useful for testing an archive or storage workflow, but it does not leave a completed extracted copy:

```powershell
python ".\batch_extract.py" "C:\path\to\archive.rar" --delete-after-batch
```

Do not combine this option with an expectation that the destination will contain the extracted archive afterward. Resume state is also disabled in this mode.

### `download_and_deletezip.py`

Monitors a specific download location until the archive appears and remains unchanged, then extracts it with WinRAR. It can optionally test the archive first, extract in batches, delete the archive after success, and put the laptop to sleep.

Before running it, edit the configuration near the top of the file. The important values are:

```python
DOWNLOAD_DIR = Path(r"C:\Users\ar559\Downloads")
DESTINATION_DIR = Path(r"C:\ARYAN\GAMES")
TARGET_ZIP = DOWNLOAD_DIR / "COD-BO-II-SteamRIP.com.rar"
```

For example, update `TARGET_ZIP` to match the archive that your download manager creates. Optional behavior is controlled by:

```python
DELETE_AFTER_EXTRACT = False  # delete the archive after successful extraction
TEST_BEFORE_EXTRACT = False   # run WinRAR's full integrity test first
SLEEP_AFTER_EXTRACT = True    # suspend Windows after a 30-second countdown
BATCH_EXTRACT = False         # extract in batches instead of one WinRAR run
BATCH_SIZE_GB = 20
```

Start the monitor with:

```powershell
python ".\download_and_deletezip.py"
```

The monitor checks every 10 seconds by default. It waits for six unchanged checks, about one minute, and also waits until common temporary download files such as `.fdmdownload`, `.crdownload`, `.part`, `.tmp`, or `.download` disappear. If extraction fails three times after the download is complete, it stops.

Press `Ctrl+C` to stop monitoring. If `SLEEP_AFTER_EXTRACT` is enabled, press `Ctrl+C` during the 30-second countdown to cancel the automatic sleep.

## Archive and path notes

- The scripts assume WinRAR is installed in its default location. Change the `WINRAR` variable if it is installed elsewhere.
- Password-protected archives are not prompted for. The WinRAR arguments use `-p-`, so extraction fails instead of waiting for a password.
- `batch_extract.py` protects against archive entries that try to escape the destination folder.
- Keep enough free disk space for the extracted files. A failed extraction can leave partial files behind; the batch extractor removes and retries the current batch on the next run.
- Only enable archive deletion after confirming that extraction and verification work as expected.

## Troubleshooting

### WinRAR is not found

Install WinRAR or update the `WINRAR` path in the relevant script. For `batch_extract.py`, archive listing may also require `UnRAR.exe`/`Rar.exe` next to WinRAR, or 7-Zip installed in its default location.

### The script says the archive is not ready

Make sure the download manager has finished and that `TARGET_ZIP` points to the final archive file, not a temporary download file.

### Extraction stops with an error

Check the free disk space, archive integrity, destination permissions, and whether the archive requires a password. For the automatic extractor, temporarily set `TEST_BEFORE_EXTRACT = True` to run a full WinRAR integrity test first.

### Resume behavior

For `batch_extract.py`, run the same command again after an interruption. The `.batch_extract_state.json` file is accepted only when the archive size and modification time still match. Use `--no-resume` to ignore it.