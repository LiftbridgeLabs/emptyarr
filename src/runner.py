import json
import logging
import os
import threading
import time
from datetime import datetime
from typing import Dict, List, Optional

from src.config import AppConfig, LibraryConfig, PathConfig, PlexInstanceConfig
from src.plex_client import PlexClient
from src.checks import check_mountpoint, check_debrid_mount, check_file_threshold, count_files
from src.providers import check_provider
from src import notifications

logger = logging.getLogger("emptyarr")

MAX_HISTORY      = 100
_history: List[Dict]          = []
_instance_status: Dict        = {}   # instance_name -> {library_name -> status}
_last_global_checks: Dict     = {}   # instance_name -> {check_name -> result}
_scheduling_enabled: bool     = True
_lock = threading.Lock()

_STATE_FILE = os.environ.get("STATE_FILE", "data/state.json")


def _load_state():
    global _scheduling_enabled
    try:
        if os.path.exists(_STATE_FILE):
            with open(_STATE_FILE) as f:
                state = json.load(f)
                _scheduling_enabled = state.get("scheduling_enabled", True)
    except Exception:
        pass


def _save_state():
    try:
        os.makedirs(os.path.dirname(_STATE_FILE), exist_ok=True)
        with open(_STATE_FILE, "w") as f:
            json.dump({"scheduling_enabled": _scheduling_enabled}, f)
    except Exception:
        pass


_load_state()


# ── State accessors ───────────────────────────────────────────────────────────

def get_history() -> List[Dict]:
    with _lock:
        return list(_history)

def get_instance_status() -> Dict:
    with _lock:
        return dict(_instance_status)

def get_last_global_checks() -> Dict:
    with _lock:
        return dict(_last_global_checks)

def get_scheduling_enabled() -> bool:
    with _lock:
        return _scheduling_enabled

def set_scheduling_enabled(enabled: bool):
    global _scheduling_enabled
    with _lock:
        _scheduling_enabled = enabled
    _save_state()
    logger.info(f"Scheduling {'enabled' if enabled else 'paused'}")


# ── History recording ─────────────────────────────────────────────────────────

def _record(instance_name: str, library_name: str, status: str,
            checks: Dict, message: str, removed_items: List[Dict] = None,
            removed_count: int = None):
    items = removed_items or []
    count = removed_count if removed_count is not None else len(items)
    record = {
        "timestamp":     datetime.now().isoformat(),
        "instance":      instance_name,
        "library":       library_name,
        "status":        status,
        "checks":        checks,
        "message":       message,
        "removed_items": items,
        "removed_count": count,
    }
    with _lock:
        _history.insert(0, record)
        if len(_history) > MAX_HISTORY:
            _history.pop()
        if instance_name not in _instance_status:
            _instance_status[instance_name] = {}
        _instance_status[instance_name][library_name] = {
            "last_run":      record["timestamp"],
            "last_status":   status,
            "last_message":  message,
            "removed_count": count,
        }
    return record


# ── Per-path checks ───────────────────────────────────────────────────────────

def _run_path_checks(path_cfg: PathConfig, plex_count: int,
                     skip_threshold: bool = False) -> Dict:
    """
    Run all checks appropriate for a single path based on its type.
    skip_threshold=True skips the individual file count check (used for mixed
    libraries where combined count is checked separately).
    """
    results = {}
    label = path_cfg.path.split("/")[-1] or path_cfg.path

    # 1. Mountpoint — always
    results[f"Mount ({label})"] = check_mountpoint(path_cfg.path)

    # 2. Debrid mount health — debrid and usenet only
    if path_cfg.type in ("debrid", "usenet"):
        results[f"Debrid mount ({label})"] = check_debrid_mount(path_cfg.path)

    # 3. File threshold — skipped for mixed (handled at library level)
    if not skip_threshold:
        results[f"Files ({label})"] = check_file_threshold(
            path_cfg.path, path_cfg.min_threshold, plex_count
        )

    # 4. Provider API checks — optional
    for pc in path_cfg.provider_checks:
        check_name = f"{pc.type.capitalize()} API ({label})"
        results[check_name] = check_provider(pc.type, pc.api_key)

    return results


def _run_mixed_threshold(library: LibraryConfig, plex_count: int) -> Dict:
    """
    For mixed libraries: sum files across ALL paths and compare combined
    total to Plex count. Uses the lowest min_threshold across all paths.
    """
    total_disk = sum(count_files(p.path) for p in library.paths)
    threshold  = min((p.min_threshold for p in library.paths), default=0.90)

    if plex_count > 0:
        ratio = total_disk / plex_count
        if ratio < threshold:
            return {
                "pass":   False,
                "detail": (f"Combined ratio {ratio*100:.1f}% below threshold "
                           f"{threshold*100:.0f}% "
                           f"({total_disk} total on disk / {plex_count} in Plex)")
            }
        return {
            "pass":   True,
            "detail": (f"Combined OK: {ratio*100:.1f}% "
                       f"({total_disk} total on disk / {plex_count} in Plex)")
        }

    if total_disk == 0:
        return {"pass": False, "detail": "No files found across any path"}
    return {"pass": True, "detail": f"{total_disk} total files on disk"}


# ── Plex instance global checks ───────────────────────────────────────────────

def run_instance_checks(instance: PlexInstanceConfig,
                        plex: PlexClient) -> Dict:
    """Run Plex reachability check for an instance. Store result."""
    checks = {
        f"Plex ({instance.name})": plex.check_reachable(),
    }
    with _lock:
        _last_global_checks[instance.name] = checks
    return checks


# ── Library runner ────────────────────────────────────────────────────────────

def _breakdown(items: list) -> str:
    counts: dict = {}
    for item in items:
        t = item.get("type", "item")
        counts[t] = counts.get(t, 0) + 1
    order = ["episode", "season", "show", "movie"]
    parts = [f"{counts[k]} {k}{'s' if counts[k] != 1 else ''}" for k in order if k in counts]
    parts += [f"{v} {k}{'s' if v != 1 else ''}" for k, v in counts.items() if k not in order]
    return ", ".join(parts) if parts else f"{len(items)} item(s)"


def _trash_item_key(item: Dict) -> tuple:
    return (
        item.get("media_type_id", ""),
        item.get("type", ""),
        item.get("title", ""),
        item.get("year", ""),
        item.get("index", ""),
        item.get("parent_title", ""),
        item.get("parent_index", ""),
        item.get("grandparent_title", ""),
    )


def _items_removed(before: List[Dict], after: List[Dict]) -> List[Dict]:
    after_keys = {_trash_item_key(item) for item in after}
    return [item for item in before if _trash_item_key(item) not in after_keys]


def _headline_count(items: List[Dict]) -> int:
    episode_count = sum(1 for i in items if i.get("type") == "episode")
    return episode_count if episode_count > 0 else len(items)


def _handle_checks_failed(config, instance, library, all_checks, failed):
    failed_names = ", ".join(failed.keys())
    msg = f"Checks failed ({failed_names}) — trash empty skipped"
    logger.warning(f"[{instance.name} / {library.name}] {msg}")
    _record(instance.name, library.name, "skipped", all_checks, msg)
    if config.notify.on_health_fail and config.discord_webhook:
        notifications.notify_health_fail(config.discord_webhook,
                                         instance.name, library.name,
                                         failed, all_checks)


def _handle_dry_run(instance, library, trash_items, all_checks, headline_count):
    trash_count = len(trash_items)
    if trash_count > 0:
        msg = f"[DRY RUN] Would remove {_breakdown(trash_items)} from trash — no action taken"
    else:
        msg = "[DRY RUN] Trash is already empty"
    logger.info(f"[{instance.name} / {library.name}] {msg}")
    _record(instance.name, library.name, "dry_run", all_checks, msg,
            trash_items, removed_count=headline_count)


def _handle_empty_failed(config, instance, library, result, all_checks, trash_items):
    msg = f"emptyTrash failed: {result.get('error', result.get('http'))}"
    logger.error(f"[{instance.name} / {library.name}] {msg}")
    _record(instance.name, library.name, "error", all_checks, msg, trash_items)
    if config.notify.on_error and config.discord_webhook:
        notifications.notify_error(config.discord_webhook,
                                   instance.name, library.name,
                                   str(result.get('error', result.get('http'))),
                                   all_checks)


def _handle_empty_success(config, instance, library, trash_items, all_checks,
                          removed_items):
    trash_count = len(trash_items)
    removed_count = len(removed_items)

    if removed_count > 0:
        msg = f"Emptied {_breakdown(removed_items)} from trash"
    elif trash_count > 0:
        msg = (f"emptyTrash completed, but Plex still reports "
               f"{_breakdown(trash_items)} in trash")
    else:
        msg = "Trash was already empty"
    logger.info(f"[{instance.name} / {library.name}] {msg}")
    _record(instance.name, library.name, "success", all_checks, msg,
            removed_items if removed_items else [],
            removed_count=_headline_count(removed_items))
    if removed_count > 0 and config.notify.on_emptied and config.discord_webhook:
        notifications.notify_emptied(config.discord_webhook,
                                     instance.name, library.name,
                                     removed_items, all_checks,
                                     breakdown=_breakdown(removed_items))
    elif trash_count == 0 and config.notify.on_clean and config.discord_webhook:
        notifications.notify_clean(config.discord_webhook,
                                   instance.name, library.name, all_checks)


def _scheduling_blocked(dry_run: bool, manual: bool) -> bool:
    return not dry_run and not manual and not get_scheduling_enabled()


def _handle_section_not_found(config, instance, library):
    msg = f"Could not find Plex section for '{library.name}'"
    logger.warning(f"[{instance.name} / {library.name}] {msg}")
    _record(instance.name, library.name, "error", {}, msg)
    if config.notify.on_skip and config.discord_webhook:
        notifications.notify_skip(config.discord_webhook, instance.name, library.name, msg)


def run_library(instance: PlexInstanceConfig, library: LibraryConfig,
                config: AppConfig, plex: PlexClient,
                plex_checks: Optional[Dict] = None,
                dry_run: bool = False,
                manual: bool = False):
    """
    Full run for one library.
    manual=True bypasses the scheduling gate (used for UI button triggers).
    """
    mode = "DRY RUN" if dry_run else "run"
    logger.info(f"[{instance.name} / {library.name}] Starting {mode}{'  (manual)' if manual else ''}")

    # Scheduling gate — only applies to cron-triggered runs, not manual or dry run
    if _scheduling_blocked(dry_run, manual):
        logger.info(f"[{instance.name} / {library.name}] Scheduling paused — skipping")
        _record(instance.name, library.name, "skipped", {}, "Scheduling is paused")
        return

    # Resolve section ID
    section_id = library.section_id or plex.find_section_id(library.name)
    if not section_id:
        _handle_section_not_found(config, instance, library)
        return

    all_checks = dict(plex_checks or run_instance_checks(instance, plex))
    plex_count  = plex.get_library_item_count(section_id)
    is_mixed    = library.type == "mixed"

    for path_cfg in library.paths:
        # For mixed libraries skip individual threshold — use combined check below
        all_checks.update(_run_path_checks(path_cfg, plex_count, skip_threshold=is_mixed))

    if is_mixed and library.paths:
        all_checks["Files (combined)"] = _run_mixed_threshold(library, plex_count)

    failed = {n: c for n, c in all_checks.items() if not c["pass"]}
    if failed:
        _handle_checks_failed(config, instance, library, all_checks, failed)
        return

    trash_items    = plex.get_trash_items(section_id)
    trash_count    = len(trash_items)

    if dry_run:
        _handle_dry_run(instance, library, trash_items, all_checks,
                        _headline_count(trash_items))
        return

    logger.info(f"[{instance.name} / {library.name}] "
                f"{_breakdown(trash_items)} in trash snapshot, emptying…")

    # Clean bundles first — moves unavailable/replaced items into actual trash
    # so emptyTrash can pick them up. Harmless if nothing to clean.
    plex.clean_bundles()
    result = plex.empty_trash(section_id)

    if not result["ok"]:
        _handle_empty_failed(config, instance, library, result, all_checks, trash_items)
        return

    time.sleep(2)
    remaining_items = plex.get_trash_items(section_id)
    removed_items = _items_removed(trash_items, remaining_items)

    _handle_empty_success(config, instance, library, trash_items, all_checks,
                          removed_items)
