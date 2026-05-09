#!/usr/bin/env python3
"""One-click installer for local zchg:// protocol registration.

Supported targets:
- Windows: HKCU protocol registration (no admin required)
- Linux: user-local .desktop + xdg-mime registration
- macOS: user-local app bundle with CFBundleURLTypes + lsregister refresh
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import platform
import subprocess
import sys
import textwrap

ROOT = Path(__file__).resolve().parent
HANDLER = ROOT / "zchg_handler.py"


def _quote(s: str) -> str:
    return '"' + s.replace('"', '\\"') + '"'


def _run_best_effort(cmd: list[str]) -> tuple[int, str]:
    try:
        p = subprocess.run(cmd, check=False, capture_output=True, text=True)
        out = (p.stdout or "") + (p.stderr or "")
        return p.returncode, out.strip()
    except FileNotFoundError:
        return 127, f"missing command: {cmd[0]}"


def install_windows(python_exe: Path, dry_run: bool) -> int:
    try:
        import winreg
    except ImportError:
        print("winreg unavailable; run on Windows with CPython.")
        return 1

    command = f"{_quote(str(python_exe))} {_quote(str(HANDLER))} \"%1\""

    print("Registering zchg:// under HKCU (no admin)...")
    print(f"Command: {command}")
    if dry_run:
        return 0

    root = winreg.HKEY_CURRENT_USER
    with winreg.CreateKey(root, r"Software\\Classes\\zchg") as k:
        winreg.SetValueEx(k, "", 0, winreg.REG_SZ, "URL:ZCHG Protocol")
        winreg.SetValueEx(k, "URL Protocol", 0, winreg.REG_SZ, "")

    with winreg.CreateKey(root, r"Software\\Classes\\zchg\\DefaultIcon") as k:
        winreg.SetValueEx(k, "", 0, winreg.REG_SZ, str(python_exe))

    with winreg.CreateKey(root, r"Software\\Classes\\zchg\\shell\\open\\command") as k:
        winreg.SetValueEx(k, "", 0, winreg.REG_SZ, command)

    print("Windows registration complete.")
    return 0


def install_linux(python_exe: Path, dry_run: bool) -> int:
    apps_dir = Path.home() / ".local" / "share" / "applications"
    desktop_file = apps_dir / "zchg-handler.desktop"

    content = textwrap.dedent(
        f"""\
        [Desktop Entry]
        Name=ZCHG Protocol Handler
        Comment=Handle zchg:// links
        Type=Application
        Terminal=false
        NoDisplay=true
        MimeType=x-scheme-handler/zchg;
        Exec={_quote(str(python_exe))} {_quote(str(HANDLER))} %u
        """
    )

    print(f"Writing {desktop_file}")
    if not dry_run:
        apps_dir.mkdir(parents=True, exist_ok=True)
        desktop_file.write_text(content, encoding="utf-8")

    cmds = [
        ["xdg-mime", "default", "zchg-handler.desktop", "x-scheme-handler/zchg"],
        ["update-desktop-database", str(apps_dir)],
    ]

    for cmd in cmds:
        print("Running:", " ".join(cmd))
        if dry_run:
            continue
        code, out = _run_best_effort(cmd)
        if out:
            print(out)
        if code not in (0, 127):
            print(f"warning: command failed ({code}): {' '.join(cmd)}")

    print("Linux registration complete (best effort).")
    return 0


def install_macos(python_exe: Path, dry_run: bool) -> int:
    app_dir = Path.home() / "Applications" / "ZCHG Handler.app"
    contents = app_dir / "Contents"
    macos_dir = contents / "MacOS"
    plist_path = contents / "Info.plist"
    launcher = macos_dir / "zchg-handler"

    plist = textwrap.dedent(
        """\
        <?xml version="1.0" encoding="UTF-8"?>
        <!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
        <plist version="1.0">
        <dict>
          <key>CFBundleDevelopmentRegion</key><string>English</string>
          <key>CFBundleExecutable</key><string>zchg-handler</string>
          <key>CFBundleIdentifier</key><string>org.zchg.handler</string>
          <key>CFBundleInfoDictionaryVersion</key><string>6.0</string>
          <key>CFBundleName</key><string>ZCHG Handler</string>
          <key>CFBundlePackageType</key><string>APPL</string>
          <key>CFBundleShortVersionString</key><string>1.0</string>
          <key>CFBundleVersion</key><string>1</string>
          <key>LSMinimumSystemVersion</key><string>10.13</string>
          <key>CFBundleURLTypes</key>
          <array>
            <dict>
              <key>CFBundleURLName</key><string>ZCHG Protocol</string>
              <key>CFBundleURLSchemes</key>
              <array>
                <string>zchg</string>
              </array>
            </dict>
          </array>
        </dict>
        </plist>
        """
    )

    launcher_script = textwrap.dedent(
        f"""\
        #!/bin/sh
        exec {_quote(str(python_exe))} {_quote(str(HANDLER))} "$@"
        """
    )

    print(f"Creating app bundle at {app_dir}")
    if not dry_run:
        macos_dir.mkdir(parents=True, exist_ok=True)
        plist_path.write_text(plist, encoding="utf-8")
        launcher.write_text(launcher_script, encoding="utf-8")
        os.chmod(launcher, 0o755)

    lsregister = (
        "/System/Library/Frameworks/CoreServices.framework/Frameworks/"
        "LaunchServices.framework/Support/lsregister"
    )

    cmds = [
        [lsregister, "-f", str(app_dir)],
        ["open", str(app_dir)],
    ]

    for cmd in cmds:
        print("Running:", " ".join(cmd))
        if dry_run:
            continue
        code, out = _run_best_effort(cmd)
        if out:
            print(out)
        if code not in (0, 127):
            print(f"warning: command failed ({code}): {' '.join(cmd)}")

    print("macOS registration complete (best effort).")
    return 0


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Install local zchg:// protocol handler")
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="Print actions without modifying the system",
    )
    ap.add_argument(
        "--python",
        default=sys.executable,
        help="Python executable to use for protocol handler command",
    )
    return ap.parse_args()


def main() -> int:
    args = parse_args()
    python_exe = Path(args.python).resolve()

    if not HANDLER.exists():
        print(f"missing handler script: {HANDLER}")
        return 1

    system = platform.system().lower()
    print(f"Detected OS: {system}")
    print(f"Using Python: {python_exe}")
    print(f"Handler script: {HANDLER}")

    if system == "windows":
        return install_windows(python_exe, args.dry_run)
    if system == "linux":
        return install_linux(python_exe, args.dry_run)
    if system == "darwin":
        return install_macos(python_exe, args.dry_run)

    print(f"Unsupported OS: {system}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
