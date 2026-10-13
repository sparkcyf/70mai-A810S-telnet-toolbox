from pathlib import Path
import sys
import unittest

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "settings"))
import deploy  # noqa: E402
import mai_settings  # noqa: E402


class SwaponTests(unittest.TestCase):
    """deploy.py and mai_settings.py must agree on every shared /usr/bin/swapon."""

    def test_ours_is_the_hardware_tested_file(self):
        import hashlib
        self.assertEqual(hashlib.md5(deploy.OURS).hexdigest(), "48f713d7ce0e498cdfb6bb7dddca4451")

    def test_settings_hook_added_to_ours(self):
        self.assertEqual(mai_settings.with_boot_hook(deploy.OURS, True), deploy.OURS_HOOKED)

    def test_settings_hook_removed_from_ours(self):
        self.assertEqual(mai_settings.with_boot_hook(deploy.OURS_HOOKED, False), deploy.OURS)

    def test_settings_alone(self):
        self.assertEqual(mai_settings.with_boot_hook(None, True), deploy.SETTINGS_ONLY)
        self.assertIsNone(mai_settings.with_boot_hook(deploy.SETTINGS_ONLY, False))

    def test_variants_are_distinct(self):
        self.assertEqual(len(deploy.SWAPON), 3)


if __name__ == "__main__":
    unittest.main()
