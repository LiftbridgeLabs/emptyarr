import os
import subprocess
from typing import Dict, List, Optional


def _mountpoint_fallback(path: str) -> Dict:
    try:
        if os.path.exists(path) and os.listdir(path):
            return {"pass": True, "detail": f"Path accessible: {path}"}
        if os.path.exists(path):
            return {"pass": False, "detail": f"Path exists but is empty: {path}"}
        return {"pass": False, "detail": f"Path does not exist: {path}"}
    except Exception as e:
        return {"pass": False, "detail": f"Path check error: {e}"}


def check_mountpoint(path: str) -> Dict:
    """
    Verify path (or one of its parents) is an actual mount point.
    Walks up the directory tree since media paths are often subdirectories
    of the actual mount point rather than mount points themselves.
    """
    if not os.path.exists(path):
        return {"pass": False, "detail": f"Path does not exist: {path}"}

    check = path
    while True:
        try:
            result = subprocess.run(
                ["mountpoint", "-q", check],
                capture_output=True, timeout=5
            )
            if result.returncode == 0:
                detail = f"Mounted: {path}" if check == path else f"Path accessible via mount at {check}"
                return {"pass": True, "detail": detail}
        except FileNotFoundError:
            # mountpoint binary unavailable — just check path exists and is non-empty
            return _mountpoint_fallback(path)
        except Exception as e:
            return {"pass": False, "detail": f"Mount check error: {e}"}

        parent = os.path.dirname(check)
        if parent == check:
            # Reached filesystem root — path is accessible, just not a named mount
            return {"pass": True, "detail": f"Path accessible: {path}"}
        check = parent


def check_debrid_mount(path: str, sample_size: int = 10) -> Dict:
    """
    Check FUSE mount health for debrid/usenet paths by reading symlink targets
    via os.readlink() rather than resolving them. Finds the underlying mount
    point from target paths and verifies it is accessible and non-empty.

    This handles trash scenarios where symlinks point to files that no longer
    exist at their current location — the mount just needs to be alive.
    """
    if not os.path.exists(path):
        return {"pass": False, "detail": f"Path does not exist: {path}"}

    targets: List[str] = []
    try:
        for root, dirs, files in os.walk(path, followlinks=False):
            for name in files + dirs:
                full = os.path.join(root, name)
                if os.path.islink(full):
                    try:
                        target = os.readlink(full)
                        if not os.path.isabs(target):
                            target = os.path.normpath(
                                os.path.join(os.path.dirname(full), target)
                            )
                        targets.append(target)
                    except OSError:
                        pass
                if len(targets) >= sample_size:
                    break
            if len(targets) >= sample_size:
                break
    except PermissionError as e:
        return {"pass": False, "detail": f"Permission error: {e}"}

    if not targets:
        return {"pass": True, "detail": f"No symlinks found in {path} — skipped"}

    # For each target, walk up to find the nearest mount point.
    # The mount just needs to be accessible and non-empty — targets don't need
    # to resolve (they may point into trash).
    mount_points: set = set()
    deepest_existing_dirs: set = set()
    for target in targets:
        check = os.path.dirname(target)
        deepest_existing = None
        while True:
            if os.path.isdir(check):
                if deepest_existing is None:
                    deepest_existing = check
                try:
                    result = subprocess.run(
                        ["mountpoint", "-q", check],
                        capture_output=True, timeout=5
                    )
                    if result.returncode == 0:
                        mount_points.add(check)
                        if deepest_existing:
                            deepest_existing_dirs.add(deepest_existing)
                        break
                    # Not a mount point — keep walking up
                except FileNotFoundError:
                    # mountpoint binary unavailable — use first accessible dir
                    mount_points.add(check)
                    deepest_existing_dirs.add(check)
                    break
                except Exception:
                    break
            parent = os.path.dirname(check)
            if parent == check:
                if os.path.isdir(check):
                    mount_points.add(check)
                    if deepest_existing:
                        deepest_existing_dirs.add(deepest_existing)
                break
            check = parent

    if not mount_points:
        return {"pass": False, "detail": "Could not determine mount point from symlink targets"}

    failed: List[str] = []
    for directory in sorted(deepest_existing_dirs - mount_points):
        try:
            if not os.listdir(directory):
                failed.append(
                    f"{directory} (nearest target directory is empty — "
                    "underlying mount may be missing)"
                )
        except Exception as e:
            failed.append(f"{directory} ({e})")
    for mp in sorted(mount_points):
        try:
            if not os.listdir(mp):
                failed.append(f"{mp} (empty — mount may be dead)")
        except Exception as e:
            failed.append(f"{mp} ({e})")

    if failed:
        return {"pass": False, "detail": f"Debrid mount unhealthy: {'; '.join(failed)}"}

    mounts_str = ", ".join(sorted(mount_points))
    return {"pass": True, "detail": f"Debrid mount OK ({mounts_str})"}


def count_files(path: str) -> int:
    """
    Count symlinks and files under path without following symlinks.
    For debrid/symlink libraries the symlinks themselves are the media items
    so we count them directly rather than following into their targets.
    """
    total = 0
    if not os.path.exists(path):
        return 0
    for root, dirs, files in os.walk(path, followlinks=False):
        # Count all files (includes symlinks reported as files)
        total += len(files)
        # Count directory symlinks (movie folders that are themselves symlinks)
        total += sum(1 for d in dirs
                     if os.path.islink(os.path.join(root, d)))
    return total


def check_file_threshold(path: str, min_threshold: float,
                         plex_count: Optional[int]) -> Dict:
    """
    Validate file count on disk using ratio check only.
    disk_count / plex_count must be >= min_threshold.
    If plex_count is 0 or unavailable, just verify path is non-empty.
    """
    disk_count = count_files(path)

    if plex_count is None:
        return {
            "pass":       False,
            "disk_count": disk_count,
            "plex_count": None,
            "detail":     "Plex item count unavailable — refusing to empty trash",
        }

    if plex_count > 0:
        ratio = disk_count / plex_count
        if ratio < min_threshold:
            return {
                "pass":       False,
                "disk_count": disk_count,
                "plex_count": plex_count,
                "detail":     (f"Ratio {ratio*100:.1f}% below threshold "
                               f"{min_threshold*100:.0f}% "
                               f"({disk_count} on disk / {plex_count} in Plex)")
            }
        return {
            "pass":       True,
            "disk_count": disk_count,
            "plex_count": plex_count,
            "detail":     (f"OK: {ratio*100:.1f}% "
                           f"({disk_count} on disk / {plex_count} in Plex)")
        }

    # Plex count unavailable — just verify path has at least 1 file
    if disk_count == 0:
        return {
            "pass":       False,
            "disk_count": 0,
            "plex_count": 0,
            "detail":     "No files found on disk (path may be empty or unmounted)"
        }
    return {
        "pass":       True,
        "disk_count": disk_count,
        "plex_count": 0,
        "detail":     f"{disk_count} files on disk (Plex count unavailable)"
    }
