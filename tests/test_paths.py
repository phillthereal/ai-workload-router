"""AWR_HOME path resolution (offline, stdlib unittest)."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from router import db, paths  # noqa: E402
from router.adapters import cache  # noqa: E402
from router.benchmark import byo, tasks  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent


class ResolveHomeTests(unittest.TestCase):
    def test_unset_is_repo_root(self):
        self.assertEqual(paths.resolve_home({}), REPO_ROOT)

    def test_blank_counts_as_unset(self):
        for blank in ("", "   ", "\t"):
            self.assertEqual(paths.resolve_home({"AWR_HOME": blank}), REPO_ROOT)

    def test_set_is_used(self):
        self.assertEqual(paths.resolve_home({"AWR_HOME": "/var/lib/awr"}), Path("/var/lib/awr"))

    def test_tilde_expands(self):
        home = paths.resolve_home({"AWR_HOME": "~/awr-home"})
        self.assertEqual(home, Path("~/awr-home").expanduser())
        self.assertNotIn("~", str(home))


class ConstantsTests(unittest.TestCase):
    """With AWR_HOME unset (the test default) every path is where it always was."""

    def test_module_constants_follow_home(self):
        self.assertEqual(paths.DATA_DIR, paths.HOME / "data")
        self.assertEqual(paths.CACHE_DIR, paths.HOME / ".cache")

    def test_consumers_use_the_constants(self):
        self.assertEqual(db.DB_PATH, paths.DATA_DIR / "runs.db")
        self.assertEqual(cache.CACHE_DIR, paths.CACHE_DIR)
        self.assertEqual(tasks.DEFAULT_TASKS_PATH, paths.DATA_DIR / "tasks.json")
        self.assertEqual(byo.DATA_DIR, paths.DATA_DIR)
        self.assertEqual(byo.REPO_ROOT, paths.HOME)

    def test_default_home_matches_old_source_relative_location(self):
        self.assertEqual(paths.resolve_home({}), Path(db.__file__).resolve().parents[2])


if __name__ == "__main__":
    unittest.main()
