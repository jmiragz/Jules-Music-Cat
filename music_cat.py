# music cat
# a little windows app that cleans up music with musicbrainz picard
# and syncs it to ipod.
#
# the flow:
#   new folder   -> picard clusters, looks up, saves matches
#                -> scan the leftovers and save those matches too
#                -> clean folder
#
# then when the ipod is plugged in:
#   clean folder -> ipod library folder
#                -> refresh my ipod mirror (a local backup) from the real ipod
#                -> empty the clean folder, but only if both copies worked
#
# no third party python packages needed to run this.
# name and folder locations get set on first launch (and in settings).

from __future__ import annotations

# built in python stuff, nothing extra to install
import ctypes
import json
import os
import shutil
import stat
import string
import subprocess
import sys
import threading
import time
# tkinter draws the window, buttons and popups
import tkinter as tk
# the thread pool lets me list lots of folders at the same time
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

# app info
APP_VERSION = "1.0.0"
BASE_NAME = "Music Cat"
APP_NAME = BASE_NAME  # turns into "<your name> music cat" once a name is set
APP_ID = "MusicCat.App"  # windows uses this to group the taskbar icon

# drop this little file on the ipod to tell it apart from other drives
MARKER_NAME = "MUSICCAT_IPOD.marker"
# older versions of the app named the marker differently, so still look for these
LEGACY_MARKER_NAMES = ("JULES_IPOD.marker",)
# every marker name, to keep them out of the local mirror
ALL_MARKER_NAMES = (MARKER_NAME, *LEGACY_MARKER_NAMES)
_APP_MUTEX = None

# default home for my folders, inside the windows music folder
MUSIC_ROOT = Path.home() / "Music" / "Music Cat"

# starting settings. anything saved in config.json overrides these
DEFAULTS = {
    "user_name": "",
    "setup_done": False,  # false means show the welcome screen
    "picard_exe": "",  # try to find this on first run
    "new_dir": str(MUSIC_ROOT / "New"),
    "clean_dir": str(MUSIC_ROOT / "Clean"),
    "ipod_backup_dir": str(MUSIC_ROOT / "iPod Mirror"),
    "ipod_library_folder": "Library",
    "confirm_before_sync": True,
}


def set_app_name(user_name: str) -> None:
    # builds the app name from the name typed in setup, like "sam music cat"
    global APP_NAME
    user_name = (user_name or "").strip()
    APP_NAME = f"{user_name} {BASE_NAME}" if user_name else BASE_NAME

# file types counted as music when scanning a folder
AUDIO_EXTENSIONS = {
    ".mp3", ".flac", ".m4a", ".aac", ".ogg", ".opus",
    ".wav", ".aiff", ".aif", ".wma", ".alac", ".ape"
}


def app_data_dir() -> Path:
    # where my settings live, in the user's local appdata folder
    base = os.environ.get("LOCALAPPDATA")
    if base:
        p = Path(base) / "MusicCat"
        # older versions used a different folder name, so copy that over once
        legacy = Path(base) / "JulesMusicCat"
        if not p.exists() and legacy.is_dir():
            try:
                shutil.copytree(legacy, p)
            except OSError:
                pass
    else:
        p = Path.home() / ".music_cat"
    p.mkdir(parents=True, exist_ok=True)
    return p


# picard's commands start the work but don't wait for it to finish (a lookup
# returns right away and picard keeps matching online in the background)
# so use timers to decide when picard is done. change these if picard is
# slow or fast on your connection
# how often to tell picard to save whatever has matched so far
SAVE_EVERY_SECONDS = 15
# lookup is done when the new folder hasn't gotten any smaller for this long
LOOKUP_QUIET_SECONDS = 90
# scanning (fingerprinting) is slower, so wait longer before giving up on it
SCAN_QUIET_SECONDS = 150

# how many files robocopy checks at once when it updates the local mirror
# a network drive (especially over a vpn) is slow because every file is a round trip
# checking lots at the same time makes that a lot faster
MIRROR_THREADS = 16

# files kept in my appdata folder:
#   config.json = my settings
#   scan_counts.json = how many files each folder had last time (for the progress estimate)
#   picard_commands.txt + picard_progress = scratch files used to talk to picard
CONFIG_PATH = app_data_dir() / "config.json"
SCAN_CACHE_PATH = app_data_dir() / "scan_counts.json"
PICARD_COMMANDS_PATH = app_data_dir() / "picard_commands.txt"
PICARD_PROGRESS_DIR = app_data_dir() / "picard_progress"
PICARD_PROGRESS_DIR.mkdir(parents=True, exist_ok=True)


# finds a file that ships with the app (like the icon)
# works when running the .py and when running the built exe
def bundled_path(name: str) -> Path:
    bundle_dir = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return bundle_dir / name


# makes sure only one copy of the app is open at a time, so two windows can't fight over a sync
# gives back false if another copy is already running
def claim_single_instance() -> bool:
    global _APP_MUTEX
    if os.name != "nt":
        return True
    _APP_MUTEX = ctypes.windll.kernel32.CreateMutexW(
        None, True, "Local\\MusicCat.SingleInstance"
    )
    return ctypes.windll.kernel32.GetLastError() != 183  # error_already_exists


# starts picard (or sends a command to it if it's already open) without stealing focus
def launch_picard(args: list[str]) -> subprocess.Popen:
    startupinfo = None
    if os.name == "nt":
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        # sw_showminnoactive: minimized, without stealing focus from the kitty.
        startupinfo.wShowWindow = 7
    return subprocess.Popen(args, startupinfo=startupinfo)


# picard ignores the start minimized request, so find its window and minimize it
def minimize_picard_windows() -> bool:
    if os.name != "nt":
        return False

    user32 = ctypes.windll.user32
    found = False
    enum_proc_type = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

    # windows calls this once for every open window
    def visit(hwnd, _lparam):
        nonlocal found
        if not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if length:
            title = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, title, length + 1)
            # only touch windows with picard in the title
            if "musicbrainz picard" in title.value.lower():
                # already minimized? then leave it alone
                if not user32.IsIconic(hwnd):
                    user32.ShowWindow(hwnd, 6)  # sw_minimize
                    found = True
        return True

    user32.EnumWindows(enum_proc_type(visit), 0)
    return found


# brings picard's window back up. use this when files need fixing by hand
def restore_picard_windows() -> None:
    if os.name != "nt":
        return

    user32 = ctypes.windll.user32
    enum_proc_type = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

    def visit(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if length:
            title = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, title, length + 1)
            if "musicbrainz picard" in title.value.lower():
                user32.ShowWindow(hwnd, 9)  # sw_restore
                user32.SetForegroundWindow(hwnd)
        return True

    user32.EnumWindows(enum_proc_type(visit), 0)


# reads how many files each folder had last time (for the % estimate)
def load_scan_counts() -> dict:
    try:
        data = json.loads(SCAN_CACHE_PATH.read_text(encoding="utf-8"))
        return {k: int(v) for k, v in data.items()}
    except Exception:
        return {}


# remembers the counts so the next launch can estimate progress
def save_scan_counts(counts: dict) -> None:
    try:
        SCAN_CACHE_PATH.write_text(json.dumps(counts), encoding="utf-8")
    except Exception:
        pass


def detect_picard() -> str:
    # look in the usual install spots for picard and give back the path
    # if it can't be found, return an empty string
    for env in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
        base = os.environ.get(env)
        if not base:
            continue
        for rel in (
            Path("MusicBrainz Picard") / "picard.exe",
            Path("Programs") / "MusicBrainz Picard" / "picard.exe",
        ):
            candidate = Path(base) / rel
            if candidate.is_file():
                return str(candidate)
    # last try: maybe picard is on the path
    return shutil.which("picard") or ""


# reads picard's own settings file to see if it moves finished files, and where to
# gives back (move_files, destination). both are none if the file can't be read
def read_picard_move_settings() -> tuple[bool | None, str | None]:
    appdata = os.environ.get("APPDATA")
    if not appdata:
        return None, None
    ini = Path(appdata) / "MusicBrainz" / "Picard.ini"
    try:
        text = ini.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None, None

    move_files = None
    destination = None
    in_settings = False
    for raw in text.splitlines():
        line = raw.strip()
        # section headers look like [setting]
        if line.startswith("["):
            in_settings = line.lower() == "[setting]"
            continue
        if not in_settings or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if key == "move_files":
            move_files = value.lower() == "true"
        elif key == "move_files_to":
            # the settings file doubles every backslash, so undo that
            destination = value.replace("\\\\", "\\").strip('"')
    return move_files, destination


# true if two paths point at the same folder (ignores slashes, case and a trailing slash)
def same_folder(a: str, b: str) -> bool:
    def clean(path: str) -> str:
        return os.path.normpath(path.replace("/", os.sep)).rstrip("\\/").lower()

    return clean(a) == clean(b)


def load_config() -> dict:
    # start from the defaults, then layer my saved settings on top
    cfg = dict(DEFAULTS)
    saved: dict = {}
    if CONFIG_PATH.exists():
        try:
            saved = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except Exception:
            saved = {}
        cfg.update(saved)
        # settings saved before the welcome screen existed count as already set up
        if saved and "setup_done" not in saved:
            cfg["setup_done"] = True
    # no picard path saved yet, so go look for it
    if not cfg.get("picard_exe"):
        cfg["picard_exe"] = detect_picard()
    return cfg


# writes my settings to config.json
def save_config(cfg: dict) -> None:
    CONFIG_PATH.write_text(
        json.dumps(cfg, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


# turns a byte count into something readable, like 1.5 gb
def human_size(num: int) -> str:
    value = float(num)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{num} B"


# looks inside one folder only
# gives back (music file count, total bytes, list of subfolders)
# file sizes come with the folder listing, so there's no extra trip per file
# (that matters a lot on a network drive)
def _scan_one_dir(path: str) -> tuple[int, int, list[str]]:
    count = 0
    total = 0
    subdirs: list[str] = []
    try:
        it = os.scandir(path)
    except OSError:
        return 0, 0, subdirs
    # go through every entry in the folder, one at a time
    with it:
        while True:
            try:
                entry = next(it)
            except StopIteration:
                break
            except OSError:
                break
            try:
                # subfolders go on the to do list, music files get counted
                if entry.is_dir(follow_symlinks=False):
                    subdirs.append(entry.path)
                elif os.path.splitext(entry.name)[1].lower() in AUDIO_EXTENSIONS:
                    count += 1
                    try:
                        total += entry.stat().st_size
                    except OSError:
                        pass
            except OSError:
                continue
    return count, total, subdirs


# counts the music files and total size in a folder, including everything inside it
# a small pool of threads lists folders at the same time, because on a network drive
# most of the time is just waiting
# progress (optional) gets called a few times a second with the count so far
def folder_stats(folder: Path, progress=None, workers: int = 8) -> tuple[int, int]:
    folder = Path(folder)
    if not folder.exists():
        return 0, 0
    count = 0
    total = 0
    last_report = 0.0
    # start with the top folder, then add each subfolder to the pool as each one is found
    with ThreadPoolExecutor(max_workers=workers) as pool:
        pending = {pool.submit(_scan_one_dir, str(folder))}
        while pending:
            # wait for any one folder to finish, then handle it
            done, pending = wait(pending, return_when=FIRST_COMPLETED)
            for fut in done:
                try:
                    c, t, subs = fut.result()
                except Exception:
                    continue
                count += c
                total += t
                for sub in subs:
                    pending.add(pool.submit(_scan_one_dir, sub))
            # tell the window how many have been found so far (only about 4 times a second)
            if progress is not None:
                now = time.monotonic()
                if now - last_report >= 0.25:
                    last_report = now
                    progress(count)
    return count, total


# empties the clean folder after a good sync
# tries a few times in case a file is read only or briefly locked
# gives back (music files removed, anything still left over)
def empty_staging_folder(folder: Path) -> tuple[int, list[Path]]:
    starting_audio, _ = folder_stats(folder)

    # if windows says a file is read only, make it writable and try again
    def remove_readonly(func, path, _exc_info):
        os.chmod(path, stat.S_IWRITE)
        func(path)

    # up to 3 tries, with a short pause in between
    for attempt in range(3):
        # delete everything directly inside the folder (files and subfolders)
        for child in list(folder.iterdir()):
            try:
                if child.is_dir():
                    shutil.rmtree(child, onerror=remove_readonly)
                else:
                    try:
                        child.unlink()
                    except PermissionError:
                        child.chmod(stat.S_IWRITE)
                        child.unlink()
            except OSError:
                # continue with every other item; report anything still present later.
                pass
        remaining = list(folder.iterdir())
        if not remaining:
            return starting_audio, []
        if attempt < 2:
            time.sleep(0.6)

    return starting_audio - folder_stats(folder)[0], list(folder.iterdir())


# lists every drive letter that exists right now, like c:\ d:\ y:\
def windows_drive_roots() -> list[Path]:
    if os.name != "nt":
        return []
    # windows gives me one number where each bit is a drive letter (bit 0 = a, bit 1 = b, ...)
    bitmask = ctypes.windll.kernel32.GetLogicalDrives()
    roots = []
    for i, letter in enumerate(string.ascii_uppercase):
        if bitmask & (1 << i):
            roots.append(Path(f"{letter}:\\"))
    return roots


# looks through the drives for the one with my marker file on it. that's the ipod
# network drives and cd drives get skipped so a sleepy network drive can't freeze the app
def find_ipod_by_marker() -> Path | None:
    for root in windows_drive_roots():
        try:
            # never probe network shares / optical drives: an asleep or
            # unreachable share can block here for minutes.
            # 4 = drive_remote, 5 = drive_cdrom
            if ctypes.windll.kernel32.GetDriveTypeW(str(root)) in (4, 5):
                continue
        except Exception:
            pass
        try:
            if any((root / n).is_file() for n in (MARKER_NAME, *LEGACY_MARKER_NAMES)):
                return root
        except OSError:
            continue
    return None


# copies a folder using robocopy, which is built into windows
# mirror=true makes the destination match the source exactly (it deletes extras)
# dry_run=true only pretends, to preview what would happen
# exit codes 0-7 mean ok, 8 or higher means something failed
def robocopy(
    source: Path,
    dest: Path,
    *,
    mirror: bool = False,
    dry_run: bool = False,
    exclude_dirs: list[str] | None = None,
    exclude_files: list[str] | None = None,
    threads: int = 0,
    progress=None,
) -> tuple[int, str]:
    source = Path(source)
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)

    # robocopy flags:
    #   /e or /mir      copy subfolders (mirror also removes extras)
    #   /copy:dat       keep data, attributes and timestamps
    #   /dcopy:t        keep folder timestamps
    #   /r:2 /w:2       retry twice, wait 2 seconds between tries
    #   /fft            ok with the ipod's looser timestamps
    #   /xj             skip junction links
    #   /np /njh /ndl   keep the output short (no percent, no header, no folder lines)
    #   /v              also list files it skips because they're already up to date,
    #                   to count them and show progress
    args = [
        "robocopy",
        str(source),
        str(dest),
        "/MIR" if mirror else "/E",
        "/COPY:DAT",
        "/DCOPY:T",
        "/R:2",
        "/W:2",
        "/FFT",
        "/XJ",
        "/NP",
        "/NJH",
        "/NDL",
        "/V",
    ]

    # check or copy several files at the same time
    if threads:
        args.append(f"/MT:{threads}")

    # /l means list only, nothing gets copied
    if dry_run:
        args.append("/L")

    # folders to skip (windows system folders on the ipod)
    if exclude_dirs:
        args.append("/XD")
        args.extend(exclude_dirs)

    # files to skip (like my ipod marker file)
    if exclude_files:
        args.append("/XF")
        args.extend(exclude_files)

    # keeps a black console window from flashing up
    creationflags = 0
    if os.name == "nt":
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)

    # run it and read what it prints one line at a time, to show progress
    proc = subprocess.Popen(
        args,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        errors="replace",
        creationflags=creationflags,
    )

    # running totals: files copied (or that would be), files already up to date, extras
    counts = {"copied": 0, "same": 0, "extra": 0}
    # the lines to keep for the log. leave out the "same" lines, there can be thousands
    kept = []
    last_report = 0.0
    for raw in proc.stdout:
        line = raw.strip()
        if not line:
            continue
        low = line.lower()
        # the file name is the last part of the line
        current = line.split("\t")[-1].strip()

        if low.startswith("same"):
            counts["same"] += 1
        else:
            kept.append(line)
            if low.startswith(("new file", "newer", "older", "changed", "modified")):
                counts["copied"] += 1
            elif low.startswith("*extra file"):
                counts["extra"] += 1

        # tell the window how it's going, about 4 times a second
        if progress is not None:
            now = time.monotonic()
            if now - last_report >= 0.25:
                last_report = now
                progress(counts, current)

    proc.wait()
    # one last update so the final counts show up
    if progress is not None:
        progress(counts, "")
    return proc.returncode, "\n".join(kept).strip()


# robocopy's exit code is a few flags added together. this turns it into plain english
def robocopy_result_text(code: int, *, mirror: bool, preview: bool = False) -> str:
    if code >= 8:
        return "Could not complete the comparison" if preview else "Copy failed"
    if code == 0:
        return "Already up to date — no changes needed"

    details = []
    # 1 = files copied, 2 = extra files in the destination, 4 = mismatches
    if code & 1:
        details.append("files would be copied" if preview else "files copied")
    if code & 2:
        if mirror:
            details.append("extra mirror items would be cleaned up" if preview else "extra mirror items cleaned up")
        else:
            details.append("extra destination items left safely in place")
    if code & 4:
        details.append("different versions found")
    return "Ready — " + "; ".join(details)


# safely ejects a usb drive like the ipod, same as the eject button in the tray
# flushes writes, locks the drive, dismounts it, then tells it to eject
def eject_drive(root: Path) -> None:
    if os.name != "nt" or not root.drive:
        raise OSError("Safe eject is only available for a Windows drive.")

    # talking to windows directly through its kernel32 library
    kernel32 = ctypes.windll.kernel32
    kernel32.CreateFileW.restype = ctypes.c_void_p
    kernel32.CreateFileW.argtypes = [
        ctypes.c_wchar_p,
        ctypes.c_ulong,
        ctypes.c_ulong,
        ctypes.c_void_p,
        ctypes.c_ulong,
        ctypes.c_ulong,
        ctypes.c_void_p,
    ]
    # open the whole drive (not a file on it) to send it commands
    volume = rf"\\.\{root.drive}"
    handle = kernel32.CreateFileW(
        volume,
        0xC0000000,  # generic_read | generic_write
        0x00000003,  # file_share_read | file_share_write
        None,
        3,  # open_existing
        0,
        None,
    )
    # windows hands back -1 when the open failed
    invalid_handle = ctypes.c_void_p(-1).value
    if handle == invalid_handle:
        raise ctypes.WinError()

    returned = ctypes.c_ulong()

    # sends one command to the drive and raises an error if windows says no
    def control(code: int) -> None:
        if not kernel32.DeviceIoControl(
            handle, code, None, 0, None, 0, ctypes.byref(returned), None
        ):
            raise ctypes.WinError()

    try:
        # order matters: flush writes, lock the drive, dismount it, then eject
        kernel32.FlushFileBuffers(handle)
        control(0x00090018)  # fsctl_lock_volume
        control(0x00090020)  # fsctl_dismount_volume
        control(0x002D4808)  # ioctl_storage_eject_media
    finally:
        kernel32.CloseHandle(handle)


# the text on the welcome screen the first time the app opens
WELCOME_TEXT = (
    "Music Cat tidies up messy music files and gets them onto your iPod.\n\n"
    "How it works\n"
    "  1. Drop new music files into your New folder.\n"
    "  2. Press \u201cClean new music.\u201d Music Cat sends the files through "
    "MusicBrainz Picard, which identifies and tags them, and saves the "
    "matches into your Clean folder.\n"
    "  3. Plug in your iPod and press \u201cSync to iPod.\u201d The Clean folder "
    "is copied to the iPod, your iPod mirror (a backup copy of the iPod) is "
    "refreshed, and the Clean folder is emptied.\n\n"
    "Before you start\n"
    "  \u2022 Install MusicBrainz Picard (free: picard.musicbrainz.org) and set "
    "its file naming / save options the way you like. Music Cat uses "
    "Picard\u2019s current settings.\n"
    "  \u2022 Pick your folders below. Missing folders are created for you.\n"
    "  \u2022 The first time you sync, you\u2019ll be asked to point at your iPod\u2019s "
    "drive once.\n"
    "  \u2022 You can change all of this later in Settings."
)


# the settings window. on first launch it doubles as the welcome screen
class SettingsDialog(tk.Toplevel):
    def __init__(self, parent: "MusicCatApp", first_run: bool = False):
        super().__init__(parent)
        self.parent = parent
        self.first_run = first_run
        self.title("Welcome to Music Cat" if first_run else "Music Cat Settings")
        self.resizable(True, False)
        self.transient(parent)
        self.grab_set()  # block the main window until this one is closed

        # one text variable per box, filled with whatever is saved right now
        self.name_var = tk.StringVar(value=str(parent.cfg.get("user_name", "")))
        self.vars = {
            key: tk.StringVar(value=str(parent.cfg.get(key, "")))
            for key in ("picard_exe", "new_dir", "clean_dir", "ipod_backup_dir")
        }
        self.confirm_var = tk.BooleanVar(
            value=bool(parent.cfg.get("confirm_before_sync", True))
        )

        frame = ttk.Frame(self, padding=18)
        frame.grid(sticky="nsew")
        self.columnconfigure(0, weight=1)
        frame.columnconfigure(1, weight=1)

        # row keeps track of the current row as things stack top to bottom
        row = 0

        # welcome text only shows up on the very first launch
        if first_run:
            ttk.Label(
                frame, text=WELCOME_TEXT, wraplength=640, justify="left"
            ).grid(row=row, column=0, columnspan=3, sticky="w", pady=(0, 14))
            row += 1

        # name box
        ttk.Label(frame, text="Your name").grid(
            row=row, column=0, sticky="w", padx=(0, 8), pady=6
        )
        ttk.Entry(frame, textvariable=self.name_var, width=58).grid(
            row=row, column=1, sticky="ew", pady=6
        )
        row += 1
        ttk.Label(
            frame,
            text="Shown as \u201cYour Name Music Cat\u201d in the title bar. Optional.",
        ).grid(row=row, column=1, sticky="w", pady=(0, 6))
        row += 1

        # label, settings key, and whether browse picks a file or a folder
        rows = [
            ("Picard executable", "picard_exe", "file"),
            ("New music folder", "new_dir", "dir"),
            ("Clean (ready) folder", "clean_dir", "dir"),
            ("iPod local mirror", "ipod_backup_dir", "dir"),
        ]
        for label, key, kind in rows:
            ttk.Label(frame, text=label).grid(
                row=row, column=0, sticky="w", padx=(0, 8), pady=6
            )
            ttk.Entry(frame, textvariable=self.vars[key], width=58).grid(
                row=row, column=1, sticky="ew", pady=6
            )
            ttk.Button(
                frame,
                text="Browse\u2026",
                command=lambda k=key, t=kind: self.browse(k, t),
            ).grid(row=row, column=2, padx=(8, 0), pady=6)
            row += 1
            # small hint under the picard box about where to get it
            if key == "picard_exe":
                hint = (
                    "Picard not found automatically \u2014 install it from "
                    "picard.musicbrainz.org, then Browse\u2026 to picard.exe."
                    if not self.vars[key].get().strip()
                    else "Picard is free: picard.musicbrainz.org"
                )
                ttk.Label(frame, text=hint).grid(
                    row=row, column=1, sticky="w", pady=(0, 6)
                )
                row += 1

        ttk.Checkbutton(
            frame,
            text="Ask for confirmation before syncing / emptying the Clean folder",
            variable=self.confirm_var,
        ).grid(row=row, column=0, columnspan=3, sticky="w", pady=(12, 4))
        row += 1

        # reminder of how the safety checks work
        note = (
            "The app only mirrors the iPod back to the local backup AFTER the "
            "new music copy succeeds. The Clean folder is emptied only after "
            "both steps succeed."
        )
        ttk.Label(frame, text=note, wraplength=620).grid(
            row=row, column=0, columnspan=3, sticky="w", pady=(4, 14)
        )
        row += 1

        # bottom buttons
        buttons = ttk.Frame(frame)
        buttons.grid(row=row, column=0, columnspan=3, sticky="e")
        ttk.Button(
            buttons,
            text="Skip for now" if first_run else "Cancel",
            command=self.destroy,
        ).pack(side="left", padx=4)
        ttk.Button(
            buttons,
            text="Save & start" if first_run else "Save",
            command=self.save,
        ).pack(side="left", padx=4)

    def browse(self, key: str, kind: str) -> None:
        # open a file picker (for picard) or a folder picker (for the rest)
        # start in whatever is already typed in the box, if it exists
        current = self.vars[key].get().strip()
        initial = current if current and Path(current).exists() else None
        if kind == "file":
            value = filedialog.askopenfilename(
                parent=self,
                title="Choose MusicBrainz Picard",
                filetypes=[("Applications", "*.exe"), ("All files", "*.*")],
            )
        else:
            value = filedialog.askdirectory(
                parent=self, title="Choose folder", initialdir=initial
            )
        if value:
            self.vars[key].set(value)

    def save(self) -> None:
        values = {key: var.get().strip() for key, var in self.vars.items()}

        # on first launch make the folders and double check picard
        if self.first_run:
            problems = []
            for key in ("new_dir", "clean_dir", "ipod_backup_dir"):
                if not values[key]:
                    problems.append(f"A folder is missing ({key}).")
                    continue
                try:
                    Path(values[key]).mkdir(parents=True, exist_ok=True)
                except OSError as exc:
                    problems.append(f"{values[key]}\n   {exc}")
            # stop here if a folder couldn't be made
            if problems:
                messagebox.showerror(
                    APP_NAME,
                    "I couldn't set up these folders:\n\n" + "\n".join(problems),
                    parent=self,
                )
                return
            # picard missing is allowed, but warn about it
            if not Path(values["picard_exe"]).is_file():
                if not messagebox.askyesno(
                    APP_NAME,
                    "I can't find Picard at that location, so \u201cClean new "
                    "music\u201d won't work until it's installed and set in "
                    "Settings.\n\nSave anyway?",
                    parent=self,
                ):
                    return

        # save everything and refresh the title and cards
        name = self.name_var.get().strip()
        self.parent.cfg.update(values)
        self.parent.cfg["user_name"] = name
        self.parent.cfg["confirm_before_sync"] = self.confirm_var.get()
        self.parent.cfg["setup_done"] = True
        set_app_name(name)
        save_config(self.parent.cfg)
        self.parent.apply_branding()
        self.parent.log("Settings saved. ฅ^•ﻌ•^ฅ")
        self.parent.refresh_status()
        self.destroy()


# the main window. everything the app does hangs off this class
class MusicCatApp(tk.Tk):
    # runs once when the app opens: load settings, build the window, start the first scan
    def __init__(self):
        super().__init__()

        self.cfg = load_config()
        save_config(self.cfg)
        # use the saved name for the title bar and popups
        set_app_name(self.cfg.get("user_name", ""))

        self.title(APP_NAME)
        self.geometry("760x650")
        self.minsize(690, 590)
        self.configure(bg="#f2ecff")
        self.protocol("WM_DELETE_WINDOW", self.on_close)

        # busy is true while picard or a sync is running, so the buttons can't be clicked twice
        self.busy = False
        # bookkeeping for the folder scans so never start two at once
        self._refresh_running = False
        self._refresh_again = False
        self._backup_text = None  # cached ipod mirror scan (the big one)
        self._scanned_once = False
        self._scan_counts = load_scan_counts()  # last-known file counts
        # for the progress message: the text, when it started, and the timer that updates it
        self._progress_running = False
        self._progress_base = ""
        self._progress_started = 0.0
        self._tick_job = None
        # build the window: icon, colors, then all the buttons and cards
        self._make_icon()
        self._style()
        self._build_ui()
        self.refresh_status()  # runs in the background; window opens at once
        self.after(5000, self.auto_refresh)
        # first launch ever: pop up the welcome / setup screen
        if not self.cfg.get("setup_done"):
            self.after(300, self.first_run_setup)

    def apply_branding(self) -> None:
        # updates the title bar and the big header after the name changes
        self.title(APP_NAME)
        self.title_label.config(text=f"≽^•⩊•^≼  {APP_NAME}")

    def first_run_setup(self) -> None:
        # same dialog as settings, just with the welcome text on top
        SettingsDialog(self, first_run=True)

    # sets the window icon. uses music_cat.ico if it's there,
    # otherwise draw a tiny cat face by hand
    def _make_icon(self) -> None:
        try:
            ico_path = bundled_path("music_cat.ico")
            if ico_path.is_file():
                self.iconbitmap(default=str(ico_path))
                return

            # no .ico file found, so paint a 32x32 cat icon pixel by pixel
            icon = tk.PhotoImage(width=32, height=32)
            bg = "#8d76d8"
            face = "#fff4d8"
            ear = "#f3b6c8"
            dark = "#3b314f"

            icon.put(bg, to=(0, 0, 32, 32))
            # ears
            for y in range(5, 14):
                left_start = 4 + (y - 5) // 2
                right_end = 27 - (y - 5) // 2
                icon.put(face, to=(left_start, y, 12, y + 1))
                icon.put(face, to=(20, y, right_end, y + 1))
            icon.put(ear, to=(7, 8, 11, 13))
            icon.put(ear, to=(21, 8, 25, 13))
            # face
            icon.put(face, to=(6, 11, 26, 27))
            icon.put(face, to=(9, 8, 23, 27))
            # eyes
            icon.put(dark, to=(11, 16, 13, 19))
            icon.put(dark, to=(19, 16, 21, 19))
            # nose + mouth
            icon.put("#d77b96", to=(15, 20, 17, 22))
            icon.put(dark, to=(14, 23, 18, 24))
            self._icon_ref = icon
            self.iconphoto(True, icon)
        except Exception:
            pass

    # all the colors and fonts live here
    def _style(self) -> None:
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass

        # purple background for the window, whiter background for the cards
        style.configure("Root.TFrame", background="#f2ecff")
        style.configure(
            "Title.TLabel",
            background="#f2ecff",
            foreground="#3b314f",
            font=("Segoe UI", 20, "bold"),
        )
        style.configure(
            "Subtitle.TLabel",
            background="#f2ecff",
            foreground="#675d78",
            font=("Segoe UI", 10),
        )
        style.configure(
            "Card.TFrame",
            background="#fffaff",
            relief="solid",
            borderwidth=1,
        )
        style.configure(
            "CardTitle.TLabel",
            background="#fffaff",
            foreground="#3b314f",
            font=("Segoe UI", 10, "bold"),
        )
        style.configure(
            "CardValue.TLabel",
            background="#fffaff",
            foreground="#5b4b85",
            font=("Segoe UI", 11),
        )
        style.configure(
            "Big.TButton",
            font=("Segoe UI", 12, "bold"),
            padding=(14, 12),
        )
        style.configure(
            "Small.TButton",
            font=("Segoe UI", 9),
            padding=(8, 5),
        )

    # lays out everything in the window, top to bottom
    def _build_ui(self) -> None:
        root = ttk.Frame(self, padding=20, style="Root.TFrame")
        root.pack(fill="both", expand=True)

        # header: the big title and the little tagline
        header = ttk.Frame(root, style="Root.TFrame")
        header.pack(fill="x", pady=(0, 16))

        # keeping a reference to change the name later without restarting
        self.title_label = ttk.Label(
            header,
            text=f"≽^•⩊•^≼  {APP_NAME}",
            style="Title.TLabel",
        )
        self.title_label.pack(anchor="w")
        ttk.Label(
            header,
            text="music in → Picard magic → clean staging → iPod",
            style="Subtitle.TLabel",
        ).pack(anchor="w", pady=(3, 0))

        # the four status cards: new, clean, ipod, ipod mirror
        cards = ttk.Frame(root, style="Root.TFrame")
        cards.pack(fill="x")
        cards.columnconfigure((0, 1), weight=1)

        # the text on each card. changing these updates the screen by itself
        self.new_value = tk.StringVar()
        self.clean_value = tk.StringVar()
        self.ipod_value = tk.StringVar()
        self.backup_value = tk.StringVar()

        self._card(cards, 0, 0, "📥 New", self.new_value)
        self._card(cards, 0, 1, "✨ Clean", self.clean_value)
        self._card(cards, 1, 0, "🎧 iPod", self.ipod_value)
        self._card(cards, 1, 1, "💾 iPod mirror", self.backup_value)

        # the two big buttons
        action_frame = ttk.Frame(root, style="Root.TFrame")
        action_frame.pack(fill="x", pady=(18, 8))
        action_frame.columnconfigure((0, 1), weight=1)

        self.clean_btn = ttk.Button(
            action_frame,
            text="🐾  CLEAN NEW MUSIC",
            style="Big.TButton",
            command=self.clean_new_music,
        )
        self.clean_btn.grid(row=0, column=0, sticky="ew", padx=(0, 6))

        self.sync_btn = ttk.Button(
            action_frame,
            text="🐈  SYNC TO IPOD",
            style="Big.TButton",
            command=self.sync_ipod,
        )
        self.sync_btn.grid(row=0, column=1, sticky="ew", padx=(6, 0))

        # the row of smaller buttons
        secondary = ttk.Frame(root, style="Root.TFrame")
        secondary.pack(fill="x", pady=(0, 12))

        ttk.Button(
            secondary,
            text="Preview iPod sync",
            style="Small.TButton",
            command=self.preview_sync,
        ).pack(side="left", padx=(0, 6))

        ttk.Button(
            secondary,
            text="Choose / mark iPod",
            style="Small.TButton",
            command=self.choose_and_mark_ipod,
        ).pack(side="left", padx=6)

        ttk.Button(
            secondary,
            text="Open Clean",
            style="Small.TButton",
            command=lambda: self.open_folder(Path(self.cfg["clean_dir"])),
        ).pack(side="left", padx=6)

        self.eject_btn = ttk.Button(
            secondary,
            text="⏏ Eject iPod",
            style="Small.TButton",
            command=self.eject_ipod,
        )
        self.eject_btn.pack(side="left", padx=6)

        ttk.Button(
            secondary,
            text="⚙ Settings",
            style="Small.TButton",
            command=lambda: SettingsDialog(self),
        ).pack(side="right")

        # progress text and bar (the bar only moves while something is running)
        self.progress_frame = ttk.Frame(root, style="Root.TFrame")
        self.progress_frame.pack(fill="x", pady=(0, 10))
        self.progress_text = tk.StringVar(value="Ready")
        ttk.Label(
            self.progress_frame,
            textvariable=self.progress_text,
            style="Subtitle.TLabel",
        ).pack(anchor="w", pady=(0, 3))
        self.progress_bar = ttk.Progressbar(
            self.progress_frame,
            mode="indeterminate",
            maximum=100,
        )
        self.progress_bar.pack(fill="x")

        ttk.Label(
            root,
            text=(
                "Picard uses your current Picard save + naming settings. "
                "The cleaner sends Picard the same Cluster → Lookup → Save → "
                "Scan leftovers → Save sequence."
            ),
            style="Subtitle.TLabel",
            wraplength=710,
        ).pack(fill="x", pady=(2, 10))

        # the kitty log at the bottom, where what's happening gets written down
        log_frame = ttk.Frame(root, style="Card.TFrame", padding=10)
        log_frame.pack(fill="both", expand=True)
        ttk.Label(log_frame, text="kitty log", style="CardTitle.TLabel").pack(
            anchor="w", pady=(0, 6)
        )

        self.log_box = tk.Text(
            log_frame,
            height=13,
            wrap="word",
            bg="#fffaff",
            fg="#3b314f",
            relief="flat",
            font=("Cascadia Mono", 9),
            padx=4,
            pady=4,
        )
        self.log_box.pack(fill="both", expand=True)
        self.log_box.configure(state="disabled")

        # first message in the log
        self.log("Ready. Put music in the New folder and bonk the left button. ฅ^•ﻌ•^ฅ")

    # makes one status card: a title on top and a line of text under it
    def _card(self, parent, row: int, col: int, title: str, variable: tk.StringVar) -> None:
        frame = ttk.Frame(parent, style="Card.TFrame", padding=12)
        frame.grid(
            row=row,
            column=col,
            sticky="nsew",
            padx=(0 if col == 0 else 6, 0 if col == 1 else 6),
            pady=(0 if row == 0 else 6, 6 if row == 0 else 0),
        )
        ttk.Label(frame, text=title, style="CardTitle.TLabel").pack(anchor="w")
        ttk.Label(
            frame,
            textvariable=variable,
            style="CardValue.TLabel",
            wraplength=300,
        ).pack(anchor="w", pady=(5, 0))

    # adds a line to the kitty log with the time in front
    def log(self, msg: str) -> None:
        stamp = time.strftime("%H:%M:%S")
        self.log_box.configure(state="normal")
        self.log_box.insert("end", f"[{stamp}] {msg}\n")
        self.log_box.see("end")
        self.log_box.configure(state="disabled")


    # a little popup in the corner that goes away on its own
    def toast(self, title: str, message: str, duration_ms: int = 4500) -> None:
        try:
            win = tk.Toplevel(self)
            # build the notification while hidden so an empty frame never flashes.
            win.withdraw()
            win.title(title)
            win.resizable(False, False)
            win.attributes("-topmost", True)

            outer = ttk.Frame(win, padding=14, style="Card.TFrame")
            outer.pack(fill="both", expand=True)

            ttk.Label(
                outer,
                text=f"🐈  {title}",
                style="CardTitle.TLabel",
            ).pack(anchor="w")

            ttk.Label(
                outer,
                text=message,
                style="CardValue.TLabel",
                wraplength=330,
            ).pack(anchor="w", pady=(6, 0))

            self.update_idletasks()
            win.update_idletasks()

            width = max(win.winfo_reqwidth(), 360)
            height = max(win.winfo_reqheight(), 105)
            x = self.winfo_screenwidth() - width - 28
            y = self.winfo_screenheight() - height - 78
            win.geometry(f"{width}x{height}+{x}+{y}")
            win.deiconify()
            win.lift()

            # close it by itself after a few seconds
            win.after(duration_ms, win.destroy)
        except Exception:
            pass

    # picard likes to jump to the front, so minimize it and bring my window back
    def _keep_picard_in_background(self) -> None:
        if minimize_picard_windows():
            try:
                self.lift()
                self.focus_force()
            except tk.TclError:
                pass

    # turns the big buttons on or off while something is running
    def set_busy(self, busy: bool) -> None:
        self.busy = busy
        state = "disabled" if busy else "normal"
        self.clean_btn.configure(state=state)
        self.sync_btn.configure(state=state)
        self.eject_btn.configure(state=state)

    # starts the moving bar with a message
    def start_progress(self, message: str) -> None:
        self._progress_base = message
        self._progress_started = time.monotonic()
        self._progress_running = True
        self.progress_bar.configure(mode="indeterminate")
        self.progress_bar.start(12)
        self._tick_progress()

    # shows the message with how long the whole job has been running, like (2:35)
    # it updates every second, so it's obvious the app isn't frozen
    def _tick_progress(self) -> None:
        if self._tick_job is not None:
            self.after_cancel(self._tick_job)
            self._tick_job = None
        if not self._progress_running:
            return
        elapsed = int(time.monotonic() - self._progress_started)
        minutes, seconds = divmod(elapsed, 60)
        self.progress_text.set(f"{self._progress_base}   ({minutes}:{seconds:02d})")
        self._tick_job = self.after(1000, self._tick_progress)

    # changes the message without touching the bar
    def update_progress(self, message: str) -> None:
        self._progress_base = message
        if self._progress_running:
            self._tick_progress()
        else:
            self.progress_text.set(message)

    # builds the function that turns robocopy's counts into a message like
    # "checked 3,400 of ~9,203 files". total is my best guess of how many there'll be
    # checking=false is for plain copying, like "12 of 222 files"
    def _robocopy_progress(self, label: str, *, total=None, checking: bool = False):
        def report(counts: dict, current: str) -> None:
            copied = counts["copied"]
            if checking:
                checked = counts["same"] + copied
                text = f"{label} checked {checked:,}"
                if total:
                    text += f" of ~{total:,}"
                text += " files"
                if copied:
                    text += f" \u00b7 {copied:,} new or changed"
            else:
                text = f"{label} {copied:,}"
                if total:
                    text += f" of {total:,}"
                text += " files"
            # robocopy runs on a background thread, so hand the message to the main thread
            try:
                self.after(0, lambda t=text: self.update_progress(t))
            except Exception:
                pass

        return report

    # stops the bar and shows a message. a few seconds later it goes back to ready
    def finish_progress(self, message: str = "Ready") -> None:
        self._progress_running = False
        if self._tick_job is not None:
            self.after_cancel(self._tick_job)
            self._tick_job = None
        self.progress_bar.stop()
        self.progress_bar.configure(mode="determinate", value=100 if message != "Ready" else 0)
        self.progress_text.set(message)
        if message != "Ready":
            self.after(3500, lambda: self.finish_progress("Ready"))

    # every 5 seconds refresh the cards, unless something is running
    def auto_refresh(self) -> None:
        if not self.busy and not self._refresh_running:
            # timer refreshes skip the big ipod mirror rescan.
            self.refresh_status(rescan_backup=False)
        self.after(5000, self.auto_refresh)

    # rescans the folders and updates the four cards
    # each folder gets its own thread so the window never freezes,
    # and each card updates as soon as its own scan is done
    # rescan_backup=false skips the big ipod mirror folder (the 5 second timer uses that)
    def refresh_status(self, rescan_backup: bool = True) -> None:
        if self._refresh_running:
            # a scan is in flight; run once more when it finishes.
            self._refresh_again = True
            return
        self._refresh_running = True

        new_dir = Path(self.cfg["new_dir"])
        clean_dir = Path(self.cfg["clean_dir"])
        backup = Path(self.cfg["ipod_backup_dir"])
        # only scan the ipod mirror when asked, or if it has never been scanned yet
        need_backup = rescan_backup or self._backup_text is None
        show_progress = need_backup or not self._scanned_once

        # these three turn a count and a size into the text on each card
        def fmt_new(n: int, size: int) -> str:
            return f"{n} audio file{'s' if n != 1 else ''} \u2022 {human_size(size)}"

        def fmt_clean(n: int, size: int) -> str:
            return f"{n} ready \u2022 {human_size(size)}"

        def fmt_backup(n: int, size: int) -> str:
            return f"{n} audio files \u2022 {human_size(size)}"

        # the folders to scan: name, which card to update, the folder, and how to write the text
        jobs = [
            ("new", self.new_value, new_dir, fmt_new),
            ("clean", self.clean_value, clean_dir, fmt_clean),
        ]
        if need_backup:
            jobs.append(("backup", self.backup_value, backup, fmt_backup))

        # counts down as each scan finishes to know when everything is done
        remaining = {"n": len(jobs) + 1}  # +1 for the ipod marker check

        # called every time one scan finishes
        # when the last one is done, unlock things and maybe start another scan
        def done_one() -> None:  # always runs on the ui thread
            remaining["n"] -= 1
            if remaining["n"] > 0:
                return
            save_scan_counts(self._scan_counts)
            self._refresh_running = False
            self._scanned_once = True
            if self._refresh_again:
                self._refresh_again = False
                self.refresh_status()

        # threads can't touch the window directly, so hand the update to the main thread
        def post(fn) -> None:
            try:
                self.after(0, fn)
            except Exception:
                pass  # window was closed mid-scan

        # builds the function that shows "scanning... 1,200 files so far"
        def make_progress(key: str, var):
            prev = self._scan_counts.get(key, 0)

            def report(n: int) -> None:
                text = f"scanning\u2026 {n:,} files so far"
                if prev > 0:
                    # percent of last time's count. capped at 99 because there's no way to know it's done until it is
                    pct = min(99, int(n * 100 / prev))
                    text += f"  (\u2248{pct}% of last scan's {prev:,})"
                post(lambda t=text: var.set(t))

            return report if show_progress else None

        # scans one folder (this runs in its own thread)
        def run_job(key, var, path, fmt) -> None:
            try:
                if path.exists():
                    n, size = folder_stats(path, make_progress(key, var))
                    self._scan_counts[key] = n
                    text = fmt(n, size)
                else:
                    text = f"folder not found: {path}"
            except Exception as exc:  # never let a thread die silently
                text = f"scan error: {exc}"

            # back on the main thread: show the result on the card
            def apply() -> None:
                var.set(text)
                if key == "backup":
                    self._backup_text = text
                done_one()

            post(apply)

        # checks if the ipod is plugged in by looking for the marker file
        def run_ipod() -> None:
            try:
                ipod = find_ipod_by_marker()
                if ipod:
                    text = f"connected \u2713  {ipod}"
                else:
                    text = "not found \u2014 use \u201cChoose / mark iPod\u201d once"
            except Exception as exc:
                text = f"scan error: {exc}"

            # back on the main thread: show the ipod status
            def apply() -> None:
                self.ipod_value.set(text)
                done_one()

            post(apply)

        # put "scanning..." up right away so the cards aren't blank
        if show_progress and not self._scanned_once:
            self.new_value.set("scanning\u2026")
            self.clean_value.set("scanning\u2026")
        if need_backup and self._backup_text is None:
            self.backup_value.set("scanning\u2026")

        # start every scan at the same time
        for key, var, path, fmt in jobs:
            threading.Thread(
                target=run_job, args=(key, var, path, fmt), daemon=True
            ).start()
        threading.Thread(target=run_ipod, daemon=True).start()

    # opens a folder in windows explorer
    def open_folder(self, path: Path) -> None:
        if not path.exists():
            messagebox.showerror(APP_NAME, f"Folder does not exist:\n{path}")
            return
        if os.name == "nt":
            os.startfile(str(path))
        else:
            messagebox.showinfo(APP_NAME, str(path))

    # the big left button. sends everything in the new folder through picard
    def clean_new_music(self) -> None:
        if self.busy:
            self.log("A cleaning pass is already running — no duplicate kitty jobs sent.")
            return

        # make sure picard and the new folder are there before starting
        picard = Path(self.cfg["picard_exe"])
        new_dir = Path(self.cfg["new_dir"])
        clean_dir = Path(self.cfg["clean_dir"])

        if not picard.is_file():
            messagebox.showerror(
                APP_NAME,
                f"I can't find Picard here:\n{picard}\n\nCheck Settings.",
            )
            return
        if not new_dir.is_dir():
            messagebox.showerror(APP_NAME, f"I can't find the New folder:\n{new_dir}")
            return

        # check picard will move finished files into my clean folder
        # if it won't, they'd stay in the new folder or land somewhere else
        move_files, destination = read_picard_move_settings()
        problem = None
        if move_files is False:
            problem = (
                "Picard's \"Move files when saving\" option is turned off, so cleaned "
                "files would stay in the New folder instead of moving to the Clean folder."
            )
        elif destination and not same_folder(destination, str(clean_dir)):
            problem = (
                f"Picard's destination directory is:\n{destination}\n\n"
                f"but your Clean folder is:\n{clean_dir}"
            )
        if problem and not messagebox.askyesno(
            APP_NAME,
            problem + "\n\nYou can fix this in Picard under Options > Options > "
            "File Naming.\n\nContinue anyway?",
        ):
            return

        # count the music first to tell how many went in
        count, _ = folder_stats(new_dir)
        if count == 0:
            messagebox.showinfo(APP_NAME, "The New folder has no audio files to clean. =^._.^=")
            return

        # picard writes this little log file once it has taken my cluster + lookup
        # commands. that's my signal that the lookup has been sent (not that it's finished)
        run_id = time.strftime("%Y%m%d_%H%M%S")
        stage_files = {
            "lookup": PICARD_PROGRESS_DIR / f"{run_id}_1_lookup_sent.log",
        }

        # make absolutely sure this run starts with no stale markers.
        for marker in stage_files.values():
            try:
                marker.unlink(missing_ok=True)
            except OSError:
                pass

        # picard command files use plain paths after write_logs.
        # quoting protects the localappdata path if the windows username/path has spaces.
        def q(path: Path) -> str:
            return f'"{path}"'

        # the command file handed to picard: load the folder, cluster the files, look them up
        commands = f"""\
# Music Cat - standard Picard pass

# Load and process this folder as one ordered job.  The pause gives Picard's
# asynchronous file reader time to populate the clustering pane before CLUSTER.
LOAD {q(new_dir)}
PAUSE 5

# 1) Group files, then look up the clusters
CLUSTER
LOOKUP clustered
WRITE_LOGS {q(stage_files["lookup"])}
"""
        # save the command file where picard can read it
        PICARD_COMMANDS_PATH.write_text(commands, encoding="utf-8")

        # send one command-file request.  keeping load in the same queue avoids
        # the race that occurs when an already-running picard receives separate
        # load and from_file requests over its single-instance connection.
        args = [
            str(picard),
            "-e", "FROM_FILE", str(PICARD_COMMANDS_PATH),
        ]

        try:
            self.set_busy(True)
            # start picard, then keep an eye on it until each stage is done
            launch_picard(args)
            # picard's qt window can disregard windows' minimized-launch flag.
            # catch both its first window and its post-startup redraw.
            self.after(150, self._keep_picard_in_background)
            self.after(500, self._keep_picard_in_background)
            self.after(1200, self._keep_picard_in_background)
            self.log(
                f"Sent {count} file{'s' if count != 1 else ''} from the New folder to Picard."
            )
            self.log("1/4  Clustering + lookup started…")
            self._watch_picard_progress(
                stage_files=stage_files,
                picard=picard,
                run_id=run_id,
                starting_new_count=count,
                clean_dir=clean_dir,
                new_dir=new_dir,
            )
        except Exception as exc:
            self.set_busy(False)
            messagebox.showerror(APP_NAME, f"Could not start Picard:\n{exc}")
            self.log(f"ERROR starting Picard: {exc}")

        # check the folder counts again in a moment
        self.after(1500, self.refresh_status)

    # picard can't tell me when it's done. its commands only start the work, they don't wait for it
    # so this is how it works:
    #   1. send cluster + lookup
    #   2. every few seconds tell picard to save whatever has matched so far
    #   3. when the new folder stops getting smaller for a while, the lookup is done
    #   4. scan only the files that are left (acoustid) and keep saving matches as they come in
    #   5. when it stops getting smaller again, finished
    def _watch_picard_progress(
        self,
        *,
        stage_files: dict[str, Path],
        picard: Path,
        run_id: str,
        starting_new_count: int,
        clean_dir: Path,
        new_dir: Path,
    ) -> None:
        started_at = time.monotonic()
        slow_warning_shown = False
        save_counter = 0

        # where things are in the job:
        #   starting = waiting for picard to take the lookup commands
        #   lookup   = matching online, saving as files match
        #   scan     = fingerprinting the leftovers, saving as files match
        state = {
            "phase": "starting",
            "last_progress": started_at,  # last time the new folder got smaller
            "last_check": started_at,  # last time saved + counted
            "last_remaining": starting_new_count,
        }

        # writes the next set of picard commands to a file and sends it to picard
        def queue_phase(phase: str, commands: str) -> None:
            command_path = PICARD_PROGRESS_DIR / f"{run_id}_{phase}_commands.txt"
            command_path.write_text(commands, encoding="utf-8")
            launch_picard(
                [str(picard), "-e", "FROM_FILE", str(command_path)]
            )

        # tells picard to save everything that has matched so far
        # saved files leave the new folder, so that's how progress shows
        def save_matches() -> None:
            nonlocal save_counter
            save_counter += 1
            queue_phase(
                f"save{save_counter}",
                "SAVE_MATCHED\nREMOVE_SAVED\nREMOVE_EMPTY\n",
            )

        # runs when done waiting: tell me how it went, then clean up
        def finish_pass(remaining: int, remaining_size: int, *, scanned: bool) -> None:
            clean_count, clean_size = folder_stats(clean_dir)
            total_moved = max(0, starting_new_count - remaining)

            # some files didn't match, so picard stays open for me to fix those by hand
            if remaining:
                restore_picard_windows()
                self.log(
                    f"⚠ Automatic matching finished with {remaining} unresolved audio "
                    f"file{'s' if remaining != 1 else ''} left in the New folder "
                    f"({human_size(remaining_size)})."
                )
                self.log("Picard is staying open for manual love.")
                self.log(
                    "If Picard's status bar still shows pending files, let it finish first, "
                    "then save from there."
                )
                self.toast(
                    "Picard needs manual love",
                    f"{total_moved} moved successfully. "
                    f"{remaining} file{'s' if remaining != 1 else ''} still need attention in Picard.",
                    duration_ms=6500,
                )
            else:
                method = "lookup + scan" if scanned else "lookup"
                self.log(f"✓ All {starting_new_count} files were matched by {method} and saved!")
                self.toast(
                    "All clean! ฅ^•ﻌ•^ฅ",
                    f"All {starting_new_count} files were matched, saved, and moved. "
                    f"The Clean folder now has {clean_count} audio files ({human_size(clean_size)}).",
                    duration_ms=6500,
                )
                try:
                    launch_picard([str(picard), "-e", "QUIT"])
                    self.log("Picard finished its work and is closing.")
                except OSError as exc:
                    self.log(f"Picard finished, but I could not close it: {exc}")

            self.refresh_status()
            self.set_busy(False)

            # delete the little log and command files made earlier, they're not needed anymore
            for marker in stage_files.values():
                try:
                    marker.unlink(missing_ok=True)
                except OSError:
                    pass
            for command_path in PICARD_PROGRESS_DIR.glob(f"{run_id}_*_commands.txt"):
                try:
                    command_path.unlink(missing_ok=True)
                except OSError:
                    pass

        # runs twice a second for the whole job
        def poll() -> None:
            nonlocal slow_warning_shown
            now = time.monotonic()

            # picard likes to pop up in front, so keep pushing it back down
            self._keep_picard_in_background()

            phase = state["phase"]

            if phase == "starting":
                # waiting for picard to take the cluster + lookup commands
                if stage_files["lookup"].exists():
                    self.log("✓ 1/4  Cluster + lookup sent. Picard is matching online now…")
                    self.log("2/4  Saving matches as they come in…")
                    state.update(phase="lookup", last_progress=now, last_check=now)
                    save_matches()
                elif not slow_warning_shown and now - started_at >= 120:
                    # only warn once
                    slow_warning_shown = True
                    self.log(
                        "⚠ Picard has not finished clustering after 2 minutes. "
                        "The job is still being watched; check Picard for an error or dialog."
                    )

            elif now - state["last_check"] >= SAVE_EVERY_SECONDS:
                # every so often: count what's left in the new folder, then save again
                state["last_check"] = now
                remaining, remaining_size = folder_stats(new_dir)

                # the folder got smaller, so picard is still making progress
                if remaining < state["last_remaining"]:
                    state["last_remaining"] = remaining
                    state["last_progress"] = now
                    self.log(
                        f"        {remaining} file{'s' if remaining != 1 else ''} "
                        "still in the New folder…"
                    )
                    self.refresh_status()

                # everything got matched and saved
                if remaining == 0:
                    finish_pass(0, 0, scanned=(phase == "scan"))
                    return

                # no progress for a while means picard has settled down
                quiet = SCAN_QUIET_SECONDS if phase == "scan" else LOOKUP_QUIET_SECONDS
                if now - state["last_progress"] >= quiet:
                    if phase == "lookup":
                        # lookup is done. scan only what's left
                        self.log(
                            f"✓ 2/4  Lookup is finished. {remaining} file"
                            f"{'s' if remaining != 1 else ''} didn't match."
                        )
                        self.log("3/4  Scanning only those leftovers with AcoustID…")
                        queue_phase("scan_leftovers", "SCAN\n")
                        state.update(phase="scan", last_progress=now, last_check=now)
                        self.log("4/4  Saving scan matches as they come in…")
                    else:
                        # scan is done too, so whatever is left needs me
                        finish_pass(remaining, remaining_size, scanned=True)
                        return
                else:
                    save_matches()

            # keep watching while the job is running
            self.after(500, poll)

        self.after(500, poll)

    # the choose / mark ipod button
    # pick the ipod's drive and drop a marker file on it
    # after that the app finds the ipod by itself, whatever drive letter it gets
    def choose_and_mark_ipod(self) -> Path | None:
        chosen = filedialog.askdirectory(
            parent=self,
            title="Choose the ROOT of the actual iPod",
            mustexist=True,
        )
        if not chosen:
            return None

        root = Path(chosen)

        # discourage accidentally picking a normal subfolder.
        if root.parent != root:
            answer = messagebox.askyesno(
                APP_NAME,
                f"You chose:\n{root}\n\n"
                "This should be the ROOT of the actual iPod "
                "(for example F:\\), not its Library folder.\n\n"
                "Create the iPod marker here?",
            )
            if not answer:
                return None

        try:
            # write the marker file onto the ipod
            marker = root / MARKER_NAME
            marker.write_text(
                f"This file lets {APP_NAME} safely identify the actual iPod.\n",
                encoding="utf-8",
            )
            self.log(f"Marked {root} as the iPod.")
            self.refresh_status()
            return root
        except Exception as exc:
            messagebox.showerror(
                APP_NAME,
                f"Could not create the marker on that drive:\n{exc}",
            )
            return None

    # finds the ipod. if it can't be found, ask whether to pick it now
    def get_ipod_or_ask(self) -> Path | None:
        ipod = find_ipod_by_marker()
        if ipod:
            return ipod

        if messagebox.askyesno(
            APP_NAME,
            "I can't find the iPod marker.\n\nChoose the actual iPod now?",
        ):
            return self.choose_and_mark_ipod()
        return None

    # the eject button
    def eject_ipod(self) -> None:
        if self.busy:
            return
        ipod = find_ipod_by_marker()
        if not ipod:
            messagebox.showinfo(APP_NAME, "The marked iPod is not currently connected.")
            return
        if not messagebox.askyesno(
            APP_NAME,
            f"Safely eject the iPod at {ipod}?\n\n"
            "Make sure no files or folders on the iPod are open.",
        ):
            return

        self.set_busy(True)
        self.start_progress("Safely ejecting the iPod…")

        # eject on a background thread so the window doesn't freeze
        def worker():
            try:
                eject_drive(ipod)

                # back on the main thread: update the window
                def done():
                    self.set_busy(False)
                    self.finish_progress("iPod safely ejected ✓")
                    self.log(f"✓ iPod at {ipod} safely ejected.")
                    self.refresh_status()
                    self.toast(
                        "Safe to unplug",
                        "The iPod was safely ejected and can now be unplugged.",
                    )

                self.after(0, done)
            except Exception as exc:
                def failed():
                    self.set_busy(False)
                    self.finish_progress("Eject needs attention")
                    self.log(f"Could not eject the iPod: {exc}")
                    messagebox.showerror(
                        APP_NAME,
                        "Windows could not safely eject the iPod.\n\n"
                        "Close any Explorer windows or apps using it, then try again.\n\n"
                        f"Details: {exc}",
                    )

                self.after(0, failed)

        threading.Thread(target=worker, daemon=True).start()

    # the preview ipod sync button
    # does a pretend run and shows what would change. nothing gets copied or deleted
    def preview_sync(self) -> None:
        if self.busy:
            return

        ipod = self.get_ipod_or_ask()
        if not ipod:
            return

        clean = Path(self.cfg["clean_dir"])
        backup = Path(self.cfg["ipod_backup_dir"])
        # music gets merged into the library folder on the ipod
        library = ipod / self.cfg.get("ipod_library_folder", "Library")

        if not clean.is_dir():
            messagebox.showerror(APP_NAME, f"The Clean folder does not exist:\n{clean}")
            return

        staged_count, staged_size = folder_stats(clean)

        self.set_busy(True)
        self.start_progress("Preview step 1 of 2 — checking what would copy to the iPod…")
        self.log("Preview only — nothing will be copied or deleted.")

        # same thing, the robocopy preview runs on a background thread
        def worker():
            try:
                # step 1 (pretend): what would get copied from clean to the ipod?
                code1, out1 = robocopy(
                    clean,
                    library,
                    mirror=False,
                    dry_run=True,
                    progress=self._robocopy_progress(
                        "Preview step 1 of 2 — checking what would copy to the iPod…",
                        total=staged_count,
                        checking=True,
                    ),
                )
                # step 2 (pretend): what would change in the local mirror?
                code2, out2 = robocopy(
                    ipod,
                    backup,
                    mirror=True,
                    dry_run=True,
                    exclude_dirs=["System Volume Information", "$RECYCLE.BIN"],
                    exclude_files=list(ALL_MARKER_NAMES),
                    threads=MIRROR_THREADS,
                    progress=self._robocopy_progress(
                        "Preview step 2 of 2 — checking the local mirror…",
                        total=self._scan_counts.get("backup"),
                        checking=True,
                    ),
                )

                # back on the main thread: show the preview
                def done():
                    self.set_busy(False)
                    status1 = robocopy_result_text(code1, mirror=False, preview=True)
                    status2 = robocopy_result_text(code2, mirror=True, preview=True)
                    will_copy = bool(code1 & 1)
                    if will_copy:
                        headline = (
                            f"YES — {staged_count} staged audio file"
                            f"{'s' if staged_count != 1 else ''} ({human_size(staged_size)}) "
                            "will be added to the iPod."
                        )
                        mirror_explanation = (
                            "The mirror matches the iPod's current state. During the real "
                            "sync it will be refreshed again after the new music is copied."
                        )
                    else:
                        headline = "No staged audio files need to be copied to the iPod."
                        mirror_explanation = status2 + "."
                    self.finish_progress("Preview complete ✓")
                    self.log(f"Preview result: {headline}")
                    self.log(f"Preview mirror: {mirror_explanation}")
                    preview_text = (
                        "PREVIEW RESULT\n"
                        "==============\n"
                        f"{headline}\n\n"
                        "STEP 1 — the Clean folder → iPod Library\n"
                        "----------------------------------\n"
                        f"{status1}.\n\n"
                        f"{out1[-5000:] if out1 else '(no changes listed)'}\n\n"
                        "STEP 2 — actual iPod → local mirror\n"
                        "--------------------------------------\n"
                        f"{mirror_explanation}\n\n"
                        f"{out2[-5000:] if out2 else '(no changes listed)'}"
                    )
                    self.show_preview_window(preview_text)

                self.after(0, done)
            except Exception as exc:
                self.after(0, lambda: self._worker_error("Preview failed", exc))

        threading.Thread(target=worker, daemon=True).start()

    # shows the preview results in a plain text window
    def show_preview_window(self, text: str) -> None:
        win = tk.Toplevel(self)
        win.title("iPod Sync Preview — nothing changed")
        win.geometry("820x560")
        box = tk.Text(win, wrap="none", font=("Cascadia Mono", 9))
        box.pack(fill="both", expand=True, padx=10, pady=10)
        box.insert("1.0", text)
        box.configure(state="disabled")

    # the sync to ipod button
    #   1. copy the clean folder onto the ipod
    #   2. refresh my local mirror from the real ipod
    #   3. empty the clean folder, but only if 1 and 2 both worked
    def sync_ipod(self) -> None:
        if self.busy:
            return

        ipod = self.get_ipod_or_ask()
        if not ipod:
            return

        clean = Path(self.cfg["clean_dir"])
        backup = Path(self.cfg["ipod_backup_dir"])
        # music gets merged into the library folder on the ipod
        library = ipod / self.cfg.get("ipod_library_folder", "Library")

        if not clean.is_dir():
            messagebox.showerror(APP_NAME, f"The Clean folder does not exist:\n{clean}")
            return

        count, size = folder_stats(clean)
        if count == 0:
            messagebox.showinfo(APP_NAME, "The Clean folder is already empty. Nothing to sync! 🐈")
            return

        # ask first, unless confirmation is turned off in settings
        if self.cfg.get("confirm_before_sync", True):
            ok = messagebox.askyesno(
                APP_NAME,
                f"The Clean folder currently has {count} staged audio "
                f"file{'s' if count != 1 else ''} ({human_size(size)}).\n\n"
                "Sync these staged files now?\n\n"
                f"1. Merge the Clean folder into:\n   {library}\n\n"
                f"2. Refresh the local iPod mirror from the ACTUAL iPod:\n"
                f"   {backup}\n\n"
                "3. Empty the Clean folder only if BOTH steps succeed.\n\n"
                "Continue?",
            )
            if not ok:
                return

        self.set_busy(True)
        self.start_progress(
            f"Sync step 1 of 3 — copying {count} audio file"
            f"{'s' if count != 1 else ''} to the iPod…"
        )
        self.log(f"Found iPod at {ipod}")
        self.log(f"Syncing {count} clean audio file{'s' if count != 1 else ''}…")

        # the actual sync runs on a background thread so the window stays alive
        def worker():
            try:
                # step 1: merge staged music onto actual ipod.
                code1, out1 = robocopy(
                    clean,
                    library,
                    mirror=False,
                    progress=self._robocopy_progress(
                        "Sync step 1 of 3 — copying to the iPod…", total=count
                    ),
                )

                # 8 or higher means robocopy failed, so stop here and leave the clean folder alone
                if code1 >= 8:
                    self.after(
                        0,
                        lambda: self._sync_failed(
                            "Copy to iPod failed",
                            code1,
                            out1,
                        ),
                    )
                    return

                self.after(
                    0,
                    lambda: (
                        self.log("✓ New music copied to the iPod Library."),
                        self.update_progress(
                            "Sync step 2 of 3 — updating the local iPod mirror…"
                        ),
                    ),
                )

                # step 2: make local mirror reflect the actual ipod.
                # this mirrors the ipod root to the ipod mirror folder while skipping
                # windows-only system folders that are noisy / permission-prone.
                # my marker file stays on the real ipod only, not in the mirror
                code2, out2 = robocopy(
                    ipod,
                    backup,
                    mirror=True,
                    exclude_dirs=["System Volume Information", "$RECYCLE.BIN"],
                    exclude_files=list(ALL_MARKER_NAMES),
                    threads=MIRROR_THREADS,
                    progress=self._robocopy_progress(
                        "Sync step 2 of 3 — updating the local iPod mirror…",
                        total=self._scan_counts.get("backup"),
                        checking=True,
                    ),
                )

                # mirror refresh failed, so the clean folder stays put and nothing is lost
                if code2 >= 8:
                    self.after(
                        0,
                        lambda: self._sync_failed(
                            "iPod copied successfully, but refreshing iPod mirror failed.\n"
                            "The Clean folder was NOT emptied.",
                            code2,
                            out2,
                        ),
                    )
                    return

                # older syncs copied the marker into the mirror, so clean those up
                for marker_name in ALL_MARKER_NAMES:
                    try:
                        (backup / marker_name).unlink(missing_ok=True)
                    except OSError:
                        pass

                self.after(
                    0,
                    lambda: (
                        self.log("✓ Local iPod mirror refreshed from the actual iPod."),
                        self.update_progress(
                            "Sync step 3 of 3 — tidying the clean staging folder…"
                        ),
                    ),
                )

                # step 3: only now remove staging files.
                removed_audio, cleanup_leftovers = empty_staging_folder(clean)
                clear_error = None
                if cleanup_leftovers:
                    names = ", ".join(path.name for path in cleanup_leftovers[:5])
                    if len(cleanup_leftovers) > 5:
                        names += ", …"
                    clear_error = RuntimeError(
                        f"{len(cleanup_leftovers)} item(s) are still locked or read-only: {names}"
                    )

                # back on the main thread: show how the sync went
                def finish():
                    self.set_busy(False)
                    if clear_error:
                        self.finish_progress("Sync finished — staging needs attention")
                        self.log(
                            f"⚠ Sync succeeded, but I couldn't fully empty the Clean folder: {clear_error}"
                        )
                        messagebox.showwarning(
                            APP_NAME,
                            "The iPod and local mirror were updated successfully, "
                            "but the Clean folder could not be fully emptied.\n\n"
                            f"{clear_error}",
                        )
                    else:
                        self.finish_progress("iPod sync complete ✓")
                        self.log(
                            f"✓ Removed {removed_audio} staged audio "
                            f"file{'s' if removed_audio != 1 else ''}; the Clean folder verified empty."
                        )
                        self.log("All done. tiny music cat has completed the mission. ฅ^•ﻌ•^ฅ")
                        messagebox.showinfo(
                            APP_NAME,
                            "iPod synced ✓\n"
                            "iPod mirror refreshed ✓\n"
                            "The Clean folder emptied ✓\n\n"
                            "meow complete.",
                        )
                    self.refresh_status()

                self.after(0, finish)

            # robocopy missing (shouldn't happen on windows)
            except FileNotFoundError as exc:
                self.after(
                    0,
                    lambda: self._worker_error(
                        "Windows robocopy was not found.", exc
                    ),
                )
            except Exception as exc:
                self.after(0, lambda: self._worker_error("Sync failed", exc))

        threading.Thread(target=worker, daemon=True).start()

    # shows what went wrong when a copy fails. the clean folder is left alone
    def _sync_failed(self, title: str, code: int, output: str) -> None:
        self.set_busy(False)
        self.finish_progress("Sync failed — nothing was removed from staging")
        tail = output[-3000:] if output else "(no robocopy output)"
        self.log(f"ERROR: {title} — {robocopy_result_text(code, mirror=False)}.")
        self.log("The Clean folder was NOT emptied.")
        messagebox.showerror(
            APP_NAME,
            f"{title}\n\nRobocopy exit code: {code}\n\n"
            f"Last output:\n{tail}",
        )
        self.refresh_status()

    # catch all for unexpected errors in a background job
    def _worker_error(self, title: str, exc: Exception) -> None:
        self.set_busy(False)
        self.finish_progress(f"{title} — needs attention")
        self.log(f"ERROR: {title}: {exc}")
        messagebox.showerror(APP_NAME, f"{title}\n\n{exc}")
        self.refresh_status()

    def on_close(self) -> None:
        # normal close for now. packaging as a tray app can be a cute v2.
        self.destroy()


# this is where the app starts when running it
if __name__ == "__main__":
    # windows must know the app identity before tk creates its first window.
    # setting this afterwards lets the taskbar fall back to python's icon.
    if os.name == "nt":
        try:
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)
        except Exception:
            pass

    # load the saved name first so the popup below uses it
    set_app_name(load_config().get("user_name", ""))

    if not claim_single_instance():
        if os.name == "nt":
            ctypes.windll.user32.MessageBoxW(
                None,
                f"{APP_NAME} is already open — check the existing window or taskbar.\n\n"
                "If a sync is running, let that window finish before starting another.",
                APP_NAME,
                0x40,
            )
        raise SystemExit(0)

    # open the window and keep it running until it's closed
    app = MusicCatApp()
    app.mainloop()
