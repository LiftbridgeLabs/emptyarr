import logging
import logging.handlers
import ipaddress
import os
import secrets
import threading
import urllib.parse
import yaml
from functools import wraps
from flask import (Flask, jsonify, render_template, request, redirect, url_for,
                   session, send_from_directory)

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from src.config import (load_config, parse_config, AppConfig,
                        PlexInstanceConfig, LibraryConfig)
from src.plex_client import PlexClient
from src.auth import (require_auth, auth_enabled, check_credentials,
                      is_authenticated, hash_password, is_locked_out,
                      has_valid_api_token, generate_api_token, hash_api_token)
from src import runner
from src.runner import get_scheduling_enabled, set_scheduling_enabled
from src.providers import get_account_status, get_api_key
from src.providers import _ENV_KEYS as _PROVIDER_ENV_KEYS
from src.storage import atomic_write_yaml
from src import plex_auth
from src.version import __version__

LOG_DIR  = os.environ.get("LOG_DIR", "data/logs")
os.makedirs(LOG_DIR, exist_ok=True)

_log_formatter = logging.Formatter(
    "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

# Console handler
_console = logging.StreamHandler()
_console.setFormatter(_log_formatter)

# Rotating file handler — 1MB per file, keep 5 files
_file_handler = logging.handlers.RotatingFileHandler(
    os.path.join(LOG_DIR, "emptyarr.log"),
    maxBytes=1 * 1024 * 1024,  # 1MB
    backupCount=5,
    encoding="utf-8",
)
_file_handler.setFormatter(_log_formatter)

logging.basicConfig(
    level=logging.INFO,
    handlers=[_console, _file_handler],
)
logger = logging.getLogger("emptyarr")

# ── Bootstrap ─────────────────────────────────────────────────────────────────
CONFIG_PATH = os.environ.get("CONFIG_PATH", "data/config.yml")
CONFIG_LOAD_ERROR = ""
try:
    config: AppConfig = load_config(CONFIG_PATH)
except Exception as exc:
    CONFIG_LOAD_ERROR = str(exc)
    logger.exception("Configuration could not be loaded; starting in recovery mode")
    config = AppConfig(instances=[], config_missing=True)
logging.getLogger().setLevel(config.log_level.upper())

plex_clients: dict[str, PlexClient] = {
    inst.name: PlexClient(inst.url, inst.token)
    for inst in config.instances
}

app = Flask(__name__)


def _load_session_key() -> str:
    """Resolve a session key from an override or the persistent data directory."""
    configured = os.environ.get("EMPTYARR_SECRET_KEY", "").strip()
    if configured:
        return configured

    key_path = os.environ.get(
        "EMPTYARR_SECRET_KEY_FILE",
        os.path.join(os.path.dirname(os.path.abspath(CONFIG_PATH)), ".session-key"),
    )
    try:
        if os.path.exists(key_path):
            with open(key_path, "r", encoding="utf-8") as handle:
                persisted = handle.read().strip()
            if persisted:
                return persisted

        generated = secrets.token_hex(32)
        os.makedirs(os.path.dirname(os.path.abspath(key_path)), exist_ok=True)
        try:
            descriptor = os.open(
                key_path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(generated)
            logger.info("Created persistent session key at %s", key_path)
            return generated
        except FileExistsError:
            with open(key_path, "r", encoding="utf-8") as handle:
                return handle.read().strip() or generated
    except OSError as exc:
        logger.warning(
            "Could not persist the session key at %s (%s); sessions will reset on restart",
            key_path,
            exc,
        )
        return secrets.token_hex(32)


app.secret_key = _load_session_key()
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"]   = os.environ.get("SESSION_COOKIE_SECURE", "false").lower() == "true"
scheduler      = BackgroundScheduler()
_next_runs: dict = {}
_runtime_lock = threading.RLock()
_config_file_lock = threading.Lock()


# ── Scheduler ─────────────────────────────────────────────────────────────────

def _job_key(instance_name: str, library_name: str) -> str:
    return f"{instance_name}::{library_name}"


def make_job(inst: PlexInstanceConfig, lib: LibraryConfig):
    def job():
        with _runtime_lock:
            live_config = config
            live_inst = next((i for i in live_config.instances
                              if i.name == inst.name), None)
            live_lib = next((l for l in live_inst.libraries
                             if l.name == lib.name), None) if live_inst else None
            plex = plex_clients.get(inst.name)
        if not live_inst or not live_lib or plex is None:
            return
        plex_checks = runner.run_instance_checks(live_inst, plex)
        runner.run_library(live_inst, live_lib, live_config, plex,
                           plex_checks=plex_checks)
        _update_next(live_inst.name, live_lib.name)
    return job


def _update_next(instance_name: str, library_name: str):
    key = _job_key(instance_name, library_name)
    job = scheduler.get_job(key)
    if job:
        # APScheduler 3.x uses next_fire_time
        nft = getattr(job, 'next_fire_time', None) or getattr(job, 'next_run_time', None)
        if nft:
            _next_runs[key] = nft.isoformat()


def _setup_scheduler(new_config: AppConfig = None):
    target = new_config or config
    triggers = {}
    for inst in target.instances:
        for lib in inst.libraries:
            key = _job_key(inst.name, lib.name)
            triggers[key] = CronTrigger.from_crontab(lib.cron)

    scheduler.remove_all_jobs()
    _next_runs.clear()
    for inst in target.instances:
        for lib in inst.libraries:
            key = _job_key(inst.name, lib.name)
            scheduler.add_job(
                make_job(inst, lib),
                triggers[key],
                id=key,
                name=f"{inst.name} / {lib.name}",
                replace_existing=True,
            )
    for inst in target.instances:
        for lib in inst.libraries:
            _update_next(inst.name, lib.name)


try:
    _setup_scheduler()
except Exception as exc:
    CONFIG_LOAD_ERROR = CONFIG_LOAD_ERROR or str(exc)
    logger.exception("Schedules could not be loaded; starting without jobs")
    scheduler.remove_all_jobs()
scheduler.start()


def _validate_provider_checks(checks, context: str) -> None:
    if not isinstance(checks, list):
        raise ValueError(f"{context}: provider_checks must be a list")
    for provider in checks:
        if not isinstance(provider, dict):
            raise ValueError(f"{context}: provider check must be an object")
        provider_type = str(provider.get("type", ""))
        if provider_type not in _PROVIDER_ENV_MAP:
            raise ValueError(f"{context}: unknown provider: {provider_type}")


def _validate_path(path, library_type: str, context: str) -> None:
    if not isinstance(path, dict):
        raise ValueError(f"{context}: every path must be an object")
    if not str(path.get("path", "")).strip():
        raise ValueError(f"{context}: path cannot be blank")
    path_type = str(path.get("type", library_type))
    if path_type not in {"physical", "debrid", "usenet"}:
        raise ValueError(f"{context}: invalid path type: {path_type}")
    threshold = float(path.get("min_threshold", 90))
    if not 0 < threshold <= 100:
        raise ValueError(f"{context}: threshold must be between 1 and 100")
    _validate_provider_checks(path.get("provider_checks", []), context)


def _validate_library(library, instance_name: str, names: set) -> None:
    if not isinstance(library, dict):
        raise ValueError(f"{instance_name}: every library must be an object")
    name = str(library.get("name", "")).strip()
    if not name:
        raise ValueError(f"{instance_name}: every library needs a name")
    if name in names:
        raise ValueError(f"{instance_name}: duplicate library: {name}")
    names.add(name)
    context = f"{instance_name} / {name}"
    library_type = str(library.get("type", "physical"))
    if library_type not in {"physical", "debrid", "usenet", "mixed"}:
        raise ValueError(f"{context}: invalid library type")
    CronTrigger.from_crontab(str(library.get("cron", "0 * * * *")))
    paths = library.get("paths", [])
    if not isinstance(paths, list):
        raise ValueError(f"{context}: paths must be a list")
    if not paths:
        raise ValueError(f"{context}: configure at least one filesystem path")
    for path in paths:
        _validate_path(path, library_type, context)


def _validate_instance(instance, names: set, machine_ids: set) -> None:
    if not isinstance(instance, dict):
        raise ValueError("Every Plex instance must be an object")
    name = str(instance.get("name", "")).strip()
    if not name:
        raise ValueError("Every Plex instance needs a name")
    if name in names:
        raise ValueError(f"Duplicate Plex instance name: {name}")
    names.add(name)
    machine_id = str(instance.get("machine_id", "")).strip()
    if machine_id and machine_id in machine_ids:
        raise ValueError(f"Duplicate Plex server identifier: {machine_id}")
    if machine_id:
        machine_ids.add(machine_id)
    ok, reason = _is_valid_plex_url(str(instance.get("url", "")).strip())
    if not ok:
        raise ValueError(f"{name}: {reason}")
    libraries = instance.get("libraries", [])
    if not isinstance(libraries, list):
        raise ValueError(f"{name}: libraries must be a list")
    library_names = set()
    for library in libraries:
        _validate_library(library, name, library_names)


def _validate_safety_limits(raw: dict) -> None:
    if int(raw.get("max_trash_items", 1000)) < 0:
        raise ValueError("max_trash_items cannot be negative")
    percent = float(raw.get("max_trash_percent", 25))
    if not 0 <= percent <= 100:
        raise ValueError("max_trash_percent must be between 0 and 100")


def _validate_raw_config(raw: dict) -> AppConfig:
    if not isinstance(raw, dict):
        raise ValueError("Configuration must be an object")
    instances = raw.get("plex_instances", [])
    if not isinstance(instances, list):
        raise ValueError("plex_instances must be a list")
    _validate_safety_limits(raw)
    instance_names = set()
    machine_ids = set()
    for instance in instances:
        _validate_instance(instance, instance_names, machine_ids)
    return parse_config(raw)


def _apply_runtime_config(new_config: AppConfig) -> None:
    global config, plex_clients, CONFIG_LOAD_ERROR
    new_clients = {
        instance.name: PlexClient(instance.url, instance.token)
        for instance in new_config.instances
    }
    with _runtime_lock:
        old_config = config
        old_clients = plex_clients
        try:
            config = new_config
            plex_clients = new_clients
            _setup_scheduler(new_config)
        except Exception:
            config = old_config
            plex_clients = old_clients
            _setup_scheduler(old_config)
            raise
    valid = {
        (instance.name, library.name)
        for instance in new_config.instances
        for library in instance.libraries
    }
    runner.prune_runtime_state(valid)
    logging.getLogger().setLevel(new_config.log_level.upper())
    CONFIG_LOAD_ERROR = ""


def _save_and_apply(raw: dict, runtime_tokens: dict = None) -> AppConfig:
    parsed = _validate_raw_config(raw)
    for instance in parsed.instances:
        if not instance.token and runtime_tokens:
            instance.token = runtime_tokens.get(instance.name, "")
    atomic_write_yaml(CONFIG_PATH, raw)
    _apply_runtime_config(parsed)
    return parsed


def _serialized_config_write(function):
    @wraps(function)
    def decorated(*args, **kwargs):
        with _config_file_lock:
            return function(*args, **kwargs)
    return decorated


def _csrf_token() -> str:
    token = session.get("_csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        session["_csrf_token"] = token
    return token


@app.before_request
def protect_state_changes():
    if request.method not in {"POST", "PUT", "PATCH", "DELETE"}:
        return None
    if request.endpoint == "login":
        return None
    # Non-browser automations with a verified API token do not rely on cookies
    # and therefore are not susceptible to cookie-based CSRF.
    if has_valid_api_token(config):
        return None
    expected = session.get("_csrf_token", "")
    supplied = request.headers.get("X-CSRF-Token", "")
    if not expected or not supplied or not secrets.compare_digest(expected, supplied):
        return jsonify({"error": "Invalid or missing CSRF token"}), 403


@app.after_request
def add_security_headers(response):
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; img-src 'self' data:; "
        "style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; "
        "connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; "
        "form-action 'self'",
    )
    return response


# ── Template context ──────────────────────────────────────────────────────────

def _build_ui_instances():
    with _runtime_lock:
        current_instances = list(config.instances)
    inst_status = runner.get_instance_status()
    result = []
    for inst in current_instances:
        libs = []
        for lib in inst.libraries:
            key = _job_key(inst.name, lib.name)
            libs.append({
                "name":     lib.name,
                "type":     lib.type,
                "paths":    [{"path": p.path, "type": p.type} for p in lib.paths],
                "cron":     lib.cron,
                "next_run": _next_runs.get(key, "—"),
                "status":   inst_status.get(inst.name, {}).get(lib.name, {}),
            })
        result.append({
            "name":      inst.name,
            "url":       inst.url,
            "libraries": libs,
        })
    return result


def _active_config_overrides() -> list[str]:
    fixed = {
        "DISCORD_WEBHOOK",
        "LOG_LEVEL",
        "EMPTYARR_USERNAME",
        "EMPTYARR_PASSWORD",
        "RD_API_KEY",
        "AD_API_KEY",
        "TB_API_KEY",
        "DL_API_KEY",
        "PLEX_URL",
        "PLEX_TOKEN",
    }
    return sorted(
        name for name, value in os.environ.items()
        if value and (
            name in fixed
            or name.startswith("PLEX_URL_")
            or name.startswith("PLEX_TOKEN_")
        )
    )


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/favicon.ico", methods=["GET"])
def favicon():
    return send_from_directory(app.static_folder, "favicon.png", mimetype="image/png")


@app.route("/login", methods=["GET", "POST"])
def login():
    if not auth_enabled(config):
        return redirect(url_for("index"))
    if is_authenticated():
        return redirect(url_for("index"))
    error = None
    if request.method == "POST":
        username = request.form.get("username", "")
        password = request.form.get("password", "")
        ip       = request.remote_addr or ""
        if is_locked_out(ip):
            error = "Too many failed attempts — try again in 10 minutes"
        elif check_credentials(username, password, config, ip=ip):
            session["authenticated"] = True
            return redirect(url_for("index"))
        else:
            error = "Invalid username or password"
    return render_template("login.html", error=error, app_version=__version__)


@app.route("/logout", methods=["GET"])
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/", methods=["GET"])
@require_auth
def index():
    ui_instances = _build_ui_instances()
    return render_template("index.html",
        instances=ui_instances,
        instance_count=len(ui_instances),
        library_count=sum(len(instance["libraries"]) for instance in ui_instances),
        config_missing=config.config_missing,
        auth_enabled=auth_enabled(config),
        config=config,
        csrf_token=_csrf_token(),
        config_error=CONFIG_LOAD_ERROR,
        app_version=__version__,
        config_path=CONFIG_PATH,
        config_overrides=_active_config_overrides(),
    )


@app.route("/api/status", methods=["GET"])
@require_auth
def api_status():
    return jsonify({
        "instances":          _build_ui_instances(),
        "next_runs":          _next_runs,
        "global_checks":      runner.get_last_global_checks(),
        "history_count":      len(runner.get_history()),
        "scheduling_enabled": get_scheduling_enabled(),
        "config_missing":     config.config_missing,
        "auth_enabled":       auth_enabled(config),
        "version":            __version__,
    })


@app.route("/api/history", methods=["GET"])
@require_auth
def api_history():
    return jsonify(runner.get_history())


@app.route("/api/checks", methods=["GET"])
@require_auth
def api_checks():
    results = {}
    with _runtime_lock:
        runtime = [(inst, plex_clients.get(inst.name))
                   for inst in config.instances]
    for inst, plex in runtime:
        if plex is None:
            continue
        results[inst.name] = runner.run_instance_checks(inst, plex)
    return jsonify(results)


@app.route("/api/scheduling", methods=["POST"])
@require_auth
def api_scheduling():
    data    = request.get_json(silent=True) or {}
    enabled = bool(data.get("enabled", True))
    set_scheduling_enabled(enabled)
    return jsonify({"scheduling_enabled": enabled})


def _trigger(instance_name: str, library_name: str, dry_run: bool = False):
    with _runtime_lock:
        live_config = config
        inst = next((i for i in live_config.instances if i.name == instance_name), None)
        lib = next((l for l in inst.libraries
                    if l.name == library_name), None) if inst else None
        plex = plex_clients.get(inst.name) if inst else None
    if not inst or not lib:
        return False
    if plex is None:
        return False
    def _run():
        plex_checks = runner.run_instance_checks(inst, plex)
        runner.run_library(inst, lib, live_config, plex,
                           plex_checks=plex_checks, dry_run=dry_run, manual=True)
    threading.Thread(target=_run, daemon=True).start()
    return True


@app.route("/api/run/<instance_name>/<library_name>", methods=["POST"])
@require_auth
def api_run_library(instance_name: str, library_name: str):
    if _trigger(instance_name, library_name):
        return jsonify({"status": "triggered"})
    return jsonify({"error": "not found"}), 404


@app.route("/api/dryrun/<instance_name>/<library_name>", methods=["POST"])
@require_auth
def api_dryrun_library(instance_name: str, library_name: str):
    if _trigger(instance_name, library_name, dry_run=True):
        return jsonify({"status": "dry_run_triggered"})
    return jsonify({"error": "not found"}), 404


@app.route("/api/run/all", methods=["POST"])
@require_auth
def api_run_all():
    def _run():
        with _runtime_lock:
            live_config = config
            runtime = [(inst, plex_clients.get(inst.name))
                       for inst in live_config.instances]
        for inst, plex in runtime:
            if plex is None:
                continue
            plex_checks = runner.run_instance_checks(inst, plex)
            for lib in inst.libraries:
                runner.run_library(inst, lib, live_config, plex,
                                   plex_checks=plex_checks, manual=True)
    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"status": "triggered"})


@app.route("/api/dryrun/all", methods=["POST"])
@require_auth
def api_dryrun_all():
    def _run():
        with _runtime_lock:
            live_config = config
            runtime = [(inst, plex_clients.get(inst.name))
                       for inst in live_config.instances]
        for inst, plex in runtime:
            if plex is None:
                continue
            plex_checks = runner.run_instance_checks(inst, plex)
            for lib in inst.libraries:
                runner.run_library(inst, lib, live_config, plex,
                                   plex_checks=plex_checks, dry_run=True, manual=True)
    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"status": "dry_run_triggered"})


# ── Wizard / Config endpoints ─────────────────────────────────────────────────

def _is_valid_plex_url(url: str) -> tuple[bool, str]:
    """
    Return (ok, reason). Accepts http(s) URLs pointing at a Plex server.
    Rejects non-http schemes and known cloud metadata endpoints.
    Port is intentionally unrestricted — Plex supports custom ports.
    """
    try:
        parsed = urllib.parse.urlparse(url)
    except Exception:
        return False, "Invalid URL"
    if parsed.scheme not in ("http", "https"):
        return False, "URL must use http or https"
    if not parsed.hostname:
        return False, "URL must include a hostname"
    if parsed.username or parsed.password:
        return False, "Credentials must not be embedded in the URL"
    # Block cloud metadata endpoints (AWS/GCP/Azure instance identity)
    host = parsed.hostname or ""
    _metadata_hosts = {"169.254.169.254", "metadata.google.internal", "fd00:ec2::254"}
    if host in _metadata_hosts:
        return False, "URL targets a cloud metadata address"
    try:
        address = ipaddress.ip_address(host)
        if (address.is_link_local or address.is_multicast or
                address.is_unspecified or address.is_reserved):
            return False, "URL targets a non-routable or reserved address"
    except ValueError:
        pass
    return True, ""


@app.route("/api/wizard/test-plex", methods=["POST"])
@require_auth
def api_test_plex():
    """Test a Plex connection and return available libraries."""
    data  = request.get_json(silent=True) or {}
    url   = data.get("url", "").rstrip("/")
    token = data.get("token", "")
    if not url or not token:
        return jsonify({"ok": False, "error": "URL and token are required"}), 400
    ok, reason = _is_valid_plex_url(url)
    if not ok:
        return jsonify({"ok": False, "error": reason}), 400
    try:
        plex = PlexClient(url, token)
        reachable = plex.check_reachable()
        if not reachable["pass"]:
            return jsonify({"ok": False, "error": reachable["detail"]})
        sections = plex.get_sections()
        return jsonify({"ok": True, "libraries": sections, "detail": reachable["detail"]})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


@app.route("/api/plex/auth/start", methods=["POST"])
@require_auth
def api_plex_auth_start():
    """Create a Plex PIN and return the official browser authorization URL."""
    try:
        return jsonify({"ok": True, **plex_auth.start_auth()})
    except Exception as e:
        logger.warning("Could not start Plex authorization: %s", e)
        return jsonify({"ok": False, "error": str(e)}), 502


@app.route("/api/plex/auth/status/<state>", methods=["GET"])
@require_auth
def api_plex_auth_status(state: str):
    """Poll a Plex PIN and discover reachable servers once it is claimed."""
    try:
        return jsonify(plex_auth.poll_auth(state))
    except Exception as e:
        logger.warning("Could not complete Plex authorization: %s", e)
        return jsonify({"ok": False, "error": str(e)}), 502


@app.route("/api/wizard/browse", methods=["POST"])
@require_auth
def api_browse():
    """Browse filesystem directories for path selection.

    Restricted to BROWSE_ROOTS (comma-separated env var, default /mnt,/media,/data,/home).
    Requests for paths outside these roots are rejected.
    """
    roots_raw = os.environ.get("BROWSE_ROOTS", "/mnt,/media,/data,/home")
    browse_roots = [
        os.path.realpath(os.path.normpath(r.strip()))
        for r in roots_raw.split(",") if r.strip()
    ]
    data = request.get_json(silent=True) or {}
    raw_path = data.get("path")
    if raw_path is None or str(raw_path).strip() == "":
        return jsonify(_browse_root_response(browse_roots))

    try:
        path = os.path.realpath(os.path.normpath(raw_path))
        if not _path_within_roots(path, browse_roots):
            return jsonify({"ok": False, "error": "Path is outside allowed browse roots"}), 403
        if not os.path.exists(path):
            return jsonify({"ok": False, "error": f"Path does not exist: {path}"}), 400
        return jsonify({
            "ok": True,
            "path": path,
            "parent": _browse_parent(path, browse_roots),
            "entries": _browse_entries(path),
            "selectable": True,
        })
    except PermissionError:
        return jsonify({"ok": False, "error": f"Permission denied: {path}"}), 403
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


def _path_within_roots(path: str, roots: list[str]) -> bool:
    for root in roots:
        try:
            if os.path.commonpath((path, root)) == root:
                return True
        except ValueError:
            continue
    return False


def _browse_root_response(roots: list[str]) -> dict:
    entries = [
        {"name": root, "path": root, "is_link": os.path.islink(root)}
        for root in roots if os.path.isdir(root)
    ]
    return {
        "ok": True,
        "path": "",
        "parent": None,
        "entries": entries,
        "selectable": False,
        "empty_message": (
            "No allowed browse roots are available. Check the container "
            "volume mappings or BROWSE_ROOTS."
        ),
    }


def _browse_entries(path: str) -> list[dict]:
    return [
        {"name": entry.name, "path": entry.path, "is_link": entry.is_symlink()}
        for entry in sorted(os.scandir(path), key=lambda candidate: candidate.name)
        if entry.is_dir(follow_symlinks=False)
    ]


def _browse_parent(path: str, roots: list[str]):
    if path in roots:
        return ""
    parent = os.path.dirname(path)
    if parent != path and _path_within_roots(parent, roots):
        return parent
    return None


_PROVIDER_ENV_MAP = {
    "realdebrid": "RD_API_KEY",
    "alldebrid":  "AD_API_KEY",
    "torbox":     "TB_API_KEY",
    "debridlink": "DL_API_KEY",
}


def _build_path_cfg(p: dict, env_vars_needed: list) -> dict:
    path_cfg = {
        "path":          p.get("path", ""),
        "type":          p.get("type", "physical"),
        "min_threshold": int(p.get("min_threshold", 90)),
    }
    pcs = p.get("provider_checks", [])
    if not pcs:
        return path_cfg
    path_cfg["provider_checks"] = [
        {"type": pc.get("type", ""), "api_key": ""}
        for pc in pcs
    ]
    for pc in pcs:
        ptype    = pc.get("type", "")
        env_name = _PROVIDER_ENV_MAP.get(ptype)
        if env_name and not any(e["name"] == env_name for e in env_vars_needed):
            env_vars_needed.append({
                "name":        env_name,
                "description": f"{ptype.capitalize()} API key (optional — for provider health checks)",
                "value":       "",
            })
    return path_cfg


def _build_library_cfg(lib: dict, env_vars_needed: list) -> dict:
    lib_cfg = {
        "name":  lib.get("name", ""),
        "type":  lib.get("type", "physical"),
        "cron":  lib.get("cron", "0 * * * *"),
        "paths": [_build_path_cfg(p, env_vars_needed) for p in lib.get("paths", [])],
    }
    if lib.get("section_id") is not None:
        lib_cfg["section_id"] = str(lib["section_id"])
    return lib_cfg


def _build_instance_cfg(inst: dict, store_tokens: bool, env_vars_needed: list) -> dict:
    inst_name = inst.get("name", "")
    token     = inst.get("token", "")
    safe_name = inst_name.upper().replace(" ", "_").replace("-", "_")

    if not store_tokens:
        env_vars_needed.append({
            "name":        f"PLEX_TOKEN_{safe_name}",
            "description": f"Plex token for '{inst_name}'",
            "value":       token,
        })

    return {
        "name":      inst_name,
        "url":       inst.get("url", ""),
        "token":     token if store_tokens else "",
        **({"machine_id": str(inst["machine_id"])}
           if inst.get("machine_id") else {}),
        "libraries": [_build_library_cfg(lib, env_vars_needed) for lib in inst.get("libraries", [])],
    }


@app.route("/api/wizard/save", methods=["POST"])
@require_auth
@_serialized_config_write
def api_wizard_save():
    """
    Receive wizard form data and write config.yml.
    Expects JSON matching the config structure.
    If store_tokens=True, writes tokens directly to config (less secure but simpler).
    If store_tokens=False, leaves tokens blank and returns the env var names needed.
    """
    data         = request.get_json(silent=True) or {}
    store_tokens = bool(data.get("store_tokens", False))

    # Load existing config to preserve auth, providers and other blocks
    # that aren't managed by the wizard/settings form
    try:
        with open(CONFIG_PATH, "r") as f:
            existing = yaml.safe_load(f) or {}
    except Exception:
        existing = {}

    cfg = {
        "discord_webhook": data.get("discord_webhook", ""),
        "log_level": data.get("log_level", existing.get("log_level", "INFO")),
        "notify": {
            "on_emptied":     data.get("notify_emptied",     data.get("notify_success", True)),
            "on_health_fail": data.get("notify_health_fail", data.get("notify_failure", True)),
            "on_error":       data.get("notify_error",       True),
            "on_clean":       data.get("notify_clean",       False),
            "on_skip":        data.get("notify_skip",        False),
        },
        "plex_instances": [],
        "clean_bundles_before_empty": bool(
            data.get(
                "clean_bundles_before_empty",
                existing.get("clean_bundles_before_empty", False),
            )
        ),
        "max_trash_items": int(
            data.get("max_trash_items", existing.get("max_trash_items", 1000))
        ),
        "max_trash_percent": float(
            data.get("max_trash_percent", existing.get("max_trash_percent", 25))
        ),
    }

    # Preserve existing auth block unless new credentials are being set
    wiz_user = data.get("auth_username", "").strip()
    wiz_pass = data.get("auth_password", "").strip()
    if wiz_user and wiz_pass:
        cfg["auth"] = {"username": wiz_user, "password_hash": hash_password(wiz_pass)}
        if isinstance(existing.get("auth"), dict) and existing["auth"].get("api_token_hash"):
            cfg["auth"]["api_token_hash"] = existing["auth"]["api_token_hash"]
    elif "auth" in existing:
        cfg["auth"] = existing["auth"]

    if "providers" in existing:
        cfg["providers"] = existing["providers"]

    env_vars_needed: list = []
    cfg["plex_instances"] = [
        _build_instance_cfg(inst, store_tokens, env_vars_needed)
        for inst in data.get("instances", [])
    ]

    try:
        runtime_tokens = {
            str(instance.get("name", "")): str(instance.get("token", ""))
            for instance in data.get("instances", [])
        }
        _save_and_apply(cfg, runtime_tokens=runtime_tokens)
        return jsonify({
            "ok":              True,
            "store_tokens":    store_tokens,
            "env_vars_needed": env_vars_needed,
            "message":         "Config saved and applied immediately.",
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/config/load", methods=["GET"])
@require_auth
def api_config_load():
    """Return current config.yml contents for the settings editor."""
    try:
        with open(CONFIG_PATH, "r") as f:
            raw = yaml.safe_load(f) or {}
        # Do not send password hashes back to the browser.
        if isinstance(raw.get("auth"), dict):
            raw["auth"].pop("password_hash", None)
            raw["auth"].pop("api_token_hash", None)
        return jsonify({"ok": True, "config": raw})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/providers/status", methods=["GET"])
@require_auth
def api_providers_status():
    """Return account status for all configured providers."""
    result = {}
    for provider, env_name in _PROVIDER_ENV_KEYS.items():
        key = get_api_key(provider, config=config)
        if key:
            status = get_account_status(provider, key)
            if os.environ.get(env_name, ""):
                status["source"] = "env"
                status["source_name"] = env_name
            elif config.providers.get(provider, {}).get("api_key", ""):
                status["source"] = "config"
                status["source_name"] = "config.yml"
            else:
                status["source"] = "path"
                status["source_name"] = "path provider check"
            result[provider] = status
        else:
            result[provider] = {"ok": False, "error": "no_key"}
    return jsonify(result)


@app.route("/api/providers/save", methods=["POST"])
@require_auth
@_serialized_config_write
def api_providers_save():
    """Save provider API keys to config.yml providers block."""
    global config
    data = request.get_json(silent=True) or {}
    try:
        with open(CONFIG_PATH, "r") as f:
            raw = yaml.safe_load(f) or {}
        providers = raw.get("providers", {})
        for provider, key in data.items():
            key = key.strip()
            if key:
                providers[provider] = {"api_key": key}
            else:
                providers.pop(provider, None)
        if providers:
            raw["providers"] = providers
        else:
            raw.pop("providers", None)
        new_config = _validate_raw_config(raw)
        atomic_write_yaml(CONFIG_PATH, raw)
        _apply_runtime_config(new_config)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


def _require_browser_auth():
    if not auth_enabled(config) or not is_authenticated():
        return jsonify({
            "ok": False,
            "error": "Sign in through the browser to manage API tokens",
        }), 403
    return None


def _update_api_token_hash(token_hash: str = ""):
    with open(CONFIG_PATH, "r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle) or {}
    auth = raw.get("auth")
    if not auth_enabled(config):
        raise ValueError("Set login credentials before managing an API token")
    if not isinstance(auth, dict):
        auth = {}
        raw["auth"] = auth
    if token_hash:
        auth["api_token_hash"] = token_hash
    else:
        auth.pop("api_token_hash", None)
    _save_and_apply(raw)


@app.route("/api/auth/token", methods=["GET"])
@require_auth
def api_auth_token():
    """Return API-token status without disclosing bearer credentials."""
    denied = _require_browser_auth()
    if denied:
        return denied
    configured_by_env = bool(os.environ.get("EMPTYARR_API_TOKEN", ""))
    return jsonify({
        "ok": True,
        "configured": configured_by_env or bool(config.auth_api_token_hash),
        "source": "environment" if configured_by_env else "config",
    })


@app.route("/api/auth/token", methods=["POST"])
@require_auth
@_serialized_config_write
def api_auth_token_generate():
    """Generate or rotate an API token and reveal it exactly once."""
    denied = _require_browser_auth()
    if denied:
        return denied
    if os.environ.get("EMPTYARR_API_TOKEN", ""):
        return jsonify({
            "ok": False,
            "error": "API token is managed by EMPTYARR_API_TOKEN",
        }), 409
    try:
        token = generate_api_token()
        _update_api_token_hash(hash_api_token(token))
        return jsonify({
            "ok": True,
            "token": token,
            "message": "Copy this token now; it cannot be shown again.",
        })
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.route("/api/auth/token", methods=["DELETE"])
@require_auth
@_serialized_config_write
def api_auth_token_revoke():
    """Revoke the independently stored API token."""
    denied = _require_browser_auth()
    if denied:
        return denied
    if os.environ.get("EMPTYARR_API_TOKEN", ""):
        return jsonify({
            "ok": False,
            "error": "Remove EMPTYARR_API_TOKEN to revoke this token",
        }), 409
    try:
        _update_api_token_hash()
        return jsonify({"ok": True, "message": "API token revoked"})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.route("/api/auth/save", methods=["POST"])
@require_auth
@_serialized_config_write
def api_auth_save():
    """Save or clear username/password in config.yml."""
    global config
    data     = request.get_json(silent=True) or {}
    username = data.get("username", "").strip()
    password = data.get("password", "").strip()
    clear    = data.get("clear", False)

    try:
        with open(CONFIG_PATH, "r") as f:
            raw = yaml.safe_load(f) or {}

        if clear or (not username and not password):
            raw.pop("auth", None)
        else:
            if not username:
                return jsonify({"ok": False, "error": "Username required"}), 400
            if not password:
                return jsonify({"ok": False, "error": "Password required"}), 400
            existing_auth = raw.get("auth", {})
            raw["auth"] = {
                "username":      username,
                "password_hash": hash_password(password),
            }
            if isinstance(existing_auth, dict) and existing_auth.get("api_token_hash"):
                raw["auth"]["api_token_hash"] = existing_auth["api_token_hash"]

        new_config = _validate_raw_config(raw)
        atomic_write_yaml(CONFIG_PATH, raw)
        _apply_runtime_config(new_config)

        if clear or not username:
            session.pop("authenticated", None)
        else:
            session["authenticated"] = True
        action = "cleared" if (clear or not username) else f"set for '{username}'"
        return jsonify({"ok": True, "message": f"Auth {action} — takes effect immediately."})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


if __name__ == "__main__":
    host = os.environ.get("FLASK_HOST", "127.0.0.1")
    app.run(host=host, port=8222, debug=False, use_reloader=False)
