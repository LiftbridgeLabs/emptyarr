import threading
import unittest

from src.maintenance import lease


class MaintenanceLeaseTests(unittest.TestCase):
    def test_concurrent_empty_trash_runs_wait_then_acquire(self):
        attempting = threading.Event()
        acquired_second = threading.Event()
        result = []

        def second_run():
            attempting.set()
            with lease(
                "Plex Two",
                operation="empty_trash",
                queue_empty_trash=True,
                wait_timeout=1,
            ) as outcome:
                result.append(outcome)
                if outcome[0]:
                    acquired_second.set()

        with lease(
            "Plex One",
            operation="empty_trash",
            queue_empty_trash=True,
        ) as first:
            self.assertTrue(first[0])
            thread = threading.Thread(target=second_run)
            thread.start()
            self.assertTrue(attempting.wait(1))
            self.assertFalse(acquired_second.wait(0.05))

        thread.join(1)
        self.assertFalse(thread.is_alive())
        self.assertEqual(result, [(True, "")])

    def test_empty_trash_does_not_wait_for_timestamp_repair(self):
        with lease("Plex One", operation="timestamp_repair") as repair:
            self.assertTrue(repair[0])
            with lease(
                "Plex Two",
                operation="empty_trash",
                queue_empty_trash=True,
            ) as empty_trash:
                self.assertFalse(empty_trash[0])
                self.assertIn("maintenance operation", empty_trash[1])

    def test_timestamp_repair_does_not_wait_for_empty_trash(self):
        with lease(
            "Plex One",
            operation="empty_trash",
            queue_empty_trash=True,
        ) as empty_trash:
            self.assertTrue(empty_trash[0])
            with lease("Plex Two", operation="timestamp_repair") as repair:
                self.assertFalse(repair[0])
                self.assertIn("maintenance operation", repair[1])

    def test_empty_trash_wait_is_bounded(self):
        with lease(
            "Plex One",
            operation="empty_trash",
            queue_empty_trash=True,
        ) as first:
            self.assertTrue(first[0])
            with lease(
                "Plex Two",
                operation="empty_trash",
                queue_empty_trash=True,
                wait_timeout=0,
            ) as second:
                self.assertFalse(second[0])
                self.assertIn("timed out", second[1])


if __name__ == "__main__":
    unittest.main()
