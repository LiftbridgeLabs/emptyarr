import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

_TEST_ROOT = Path(__file__).resolve().parent
os.environ.setdefault("LOG_DIR", str(_TEST_ROOT / ".runtime-logs"))
os.environ.setdefault("CONFIG_PATH", str(_TEST_ROOT / ".runtime-bootstrap.yml"))
os.environ.setdefault("STATE_FILE", str(_TEST_ROOT / ".runtime-state.json"))
os.environ.setdefault("PLEX_CLIENT_ID_FILE", str(_TEST_ROOT / ".runtime-client.json"))

import app
from src.checks import check_file_threshold
from src.config import (AppConfig, LibraryConfig, PathConfig,
                        PlexInstanceConfig, ProviderCheck)
from src.plex_client import PlexClient
from src import runner
from src import plex_auth


class WebSecurityTests(unittest.TestCase):
    def test_ui_renders_with_security_headers(self):
        response = app.app.test_client().get("/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["X-Frame-Options"], "DENY")
        self.assertIn("default-src 'self'", response.headers["Content-Security-Policy"])

    def test_state_change_requires_csrf_for_browser_session(self):
        client = app.app.test_client()
        self.assertEqual(client.post("/api/scheduling", json={"enabled": True}).status_code, 403)
        with client.session_transaction() as browser_session:
            browser_session["_csrf_token"] = "known-token"
        response = client.post(
            "/api/scheduling",
            json={"enabled": True},
            headers={"X-CSRF-Token": "known-token"},
        )
        self.assertEqual(response.status_code, 200)

    def test_metadata_address_is_rejected(self):
        ok, _ = app._is_valid_plex_url("http://169.254.10.10:32400")
        self.assertFalse(ok)


class PlexAuthTests(unittest.TestCase):
    def test_connections_prefer_local_non_relay_and_parse_string_booleans(self):
        connections = plex_auth._connections({
            "connections": [
                {"uri": "https://relay", "local": "0", "relay": "1", "protocol": "https"},
                {"uri": "http://local", "local": "1", "relay": "0", "protocol": "http"},
            ],
        })
        self.assertEqual(connections[0]["uri"], "http://local")
        self.assertTrue(connections[0]["local"])
        self.assertFalse(connections[0]["relay"])


class PlexClientTests(unittest.TestCase):
    def test_tv_count_uses_episode_type(self):
        client = PlexClient("http://plex:32400", "token")
        response = Mock()
        response.json.return_value = {"MediaContainer": {"totalSize": 123}}
        response.raise_for_status.return_value = None
        with patch.object(client, "get_section_type", return_value="show"), \
             patch.object(client, "_get", return_value=response) as get:
            self.assertEqual(client.get_library_item_count("7"), 123)
        self.assertEqual(get.call_args.kwargs["params"]["type"], 4)

    def test_count_failure_is_not_reported_as_zero(self):
        client = PlexClient("http://plex:32400", "token")
        with patch.object(client, "get_section_type", return_value="show"), \
             patch.object(client, "_get", side_effect=RuntimeError("boom")):
            self.assertIsNone(client.get_library_item_count("7"))

    def test_trash_inventory_failure_is_explicit(self):
        client = PlexClient("http://plex:32400", "token")
        with patch.object(client, "get_section_type", return_value="movie"), \
             patch.object(client, "_fetch_deleted_xml", return_value=None):
            self.assertIsNone(client.get_trash_items("1"))


class SafetyTests(unittest.TestCase):
    def test_missing_plex_count_fails_closed(self):
        with patch("src.checks.count_files", return_value=1):
            directory = "/media"
            result = check_file_threshold(directory, 0.9, None)
        self.assertFalse(result["pass"])
        self.assertIn("refusing", result["detail"])

    def test_provider_checks_receive_live_config(self):
        config = AppConfig(
            instances=[],
            providers={"realdebrid": {"api_key": "saved-key"}},
        )
        path = PathConfig(
            path="/media",
            type="debrid",
            provider_checks=[ProviderCheck(type="realdebrid")],
        )
        with patch("src.runner.check_mountpoint",
                   return_value={"pass": True, "detail": "ok"}), \
             patch("src.runner.check_debrid_mount",
                   return_value={"pass": True, "detail": "ok"}), \
             patch("src.runner.check_file_threshold",
                   return_value={"pass": True, "detail": "ok"}), \
             patch("src.runner.check_provider",
                   return_value={"pass": True, "detail": "ok"}) as provider:
            runner._run_path_checks(path, 1, config)
        self.assertIs(provider.call_args.kwargs["config"], config)

    def test_overlapping_library_run_is_skipped(self):
        instance = PlexInstanceConfig("Plex", "http://plex", "token", [])
        library = LibraryConfig("Movies", "physical", [])
        config = AppConfig(instances=[instance])
        plex = Mock()
        started = threading.Event()
        release = threading.Event()

        def slow_run(*args, **kwargs):
            started.set()
            release.wait(2)

        with patch("src.runner._run_library", side_effect=slow_run), \
             patch("src.runner._record") as record:
            thread = threading.Thread(
                target=runner.run_library,
                args=(instance, library, config, plex),
            )
            thread.start()
            self.assertTrue(started.wait(1))
            runner.run_library(instance, library, config, plex)
            release.set()
            thread.join(2)
        record.assert_called_once()
        self.assertIn("already in progress", record.call_args.args[4])


class LiveConfigTests(unittest.TestCase):
    def test_invalid_cron_is_rejected_before_apply(self):
        raw = {
            "plex_instances": [{
                "name": "Plex",
                "url": "http://plex:32400",
                "token": "x",
                "libraries": [{
                    "name": "Movies",
                    "cron": "not a cron",
                    "paths": [{"path": "/media", "type": "physical"}],
                }],
            }],
        }
        with self.assertRaises(ValueError):
            app._validate_raw_config(raw)

    def test_library_without_safety_path_is_rejected(self):
        raw = {
            "plex_instances": [{
                "name": "Plex",
                "url": "http://plex:32400",
                "libraries": [{
                    "name": "Movies",
                    "cron": "0 * * * *",
                    "paths": [],
                }],
            }],
        }
        with self.assertRaisesRegex(ValueError, "at least one"):
            app._validate_raw_config(raw)

    def test_live_apply_reconciles_jobs_and_removed_libraries(self):
        directory = str(Path("tests").resolve())
        old_path = app.CONFIG_PATH
        app.CONFIG_PATH = str(Path("tests/.runtime-config.yml").resolve())
        try:
            raw = {
                "plex_instances": [{
                    "name": "Plex",
                    "url": "http://plex:32400",
                    "token": "x",
                    "libraries": [{
                        "name": "Movies",
                        "type": "physical",
                        "cron": "0 * * * *",
                        "paths": [{
                            "path": directory,
                            "type": "physical",
                            "min_threshold": 90,
                        }],
                    }],
                }],
            }
            app._save_and_apply(raw)
            self.assertIsNotNone(app.scheduler.get_job("Plex::Movies"))
            raw["plex_instances"][0]["libraries"] = []
            app._save_and_apply(raw)
            self.assertIsNone(app.scheduler.get_job("Plex::Movies"))
            self.assertEqual(app.config.instances[0].libraries, [])
        finally:
            app.CONFIG_PATH = old_path
            Path("tests/.runtime-config.yml").unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
