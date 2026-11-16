import contextlib
import importlib.util
import io
from pathlib import Path
import shutil
import tempfile
import unittest


class BundleTests(unittest.TestCase):
    def setUp(self):
        source = Path(__file__).resolve().parent
        spec = importlib.util.spec_from_file_location("cif_builder", source / "build.py")
        self.builder = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.builder)
        temporary = tempfile.TemporaryDirectory(prefix="cif-bundle-")
        self.addCleanup(temporary.cleanup)
        self.builder.ROOT = Path(temporary.name) / "aspire"
        shutil.copytree(source, self.builder.ROOT, ignore=shutil.ignore_patterns("__pycache__"))

    def run_builder(self, check=False):
        with contextlib.redirect_stdout(io.StringIO()):
            self.builder.build(check)

    def set_common(self, value):
        (self.builder.ROOT / "common.md").write_text(value, encoding="utf-8")

    def test_committed_bundle_is_current(self):
        self.run_builder(check=True)

    def test_build_replaces_stale_output(self):
        destination = self.builder.ROOT / "prompts" / "scout.md"
        expected = destination.read_bytes()
        destination.write_bytes(b"stale")
        with self.assertRaisesRegex(ValueError, "stale"):
            self.run_builder(check=True)
        self.run_builder()
        self.assertEqual(expected, destination.read_bytes())

    def test_exact_character_limit_is_allowed(self):
        self.set_common("")
        for lane in self.builder.LANES:
            (self.builder.ROOT / f"{lane}.md").write_text("x" * 11998, encoding="utf-8")
        self.run_builder()
        self.run_builder(check=True)

    def test_character_overflow_preserves_outputs(self):
        destination = self.builder.ROOT / "prompts" / "scout.md"
        before = destination.read_bytes()
        self.set_common("x" * 12001)
        with self.assertRaisesRegex(ValueError, "ceiling exceeded"):
            self.run_builder()
        self.assertEqual(before, destination.read_bytes())

    def test_multibyte_inline_overflow_is_rejected(self):
        self.set_common("\U0001f600" * 8200)
        for lane in self.builder.LANES:
            (self.builder.ROOT / f"{lane}.md").write_text("", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "ceiling exceeded"):
            self.run_builder()


if __name__ == "__main__":
    unittest.main()
