#!/usr/bin/env python3
"""Prove assets/check_icons.py fails when it should (docs/decisions.md §173).

A guard that has only ever run against correct assets proves nothing. Each test
copies the committed icon set into a temporary tree, breaks exactly one thing,
and requires main() to return 1 and name the problem. The real tree is never
touched.

Standard library only, like the checker: run it with
  python3 -m unittest assets/test_check_icons.py   (pnpm test:icons)
"""
import contextlib
import importlib.util
import io
import json
import os
import shutil
import struct
import tempfile
import unittest
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


check_icons = _load("check_icons", os.path.join(HERE, "check_icons.py"))

# Directories the checker reads besides the masters themselves.
COPIED_DIRS = [
    "frontend/static",
    "mobile/assets/brand",
    "mobile/android/app/src/main",
    "mobile/ios/Runner/Assets.xcassets/AppIcon.appiconset",
    "mobile/ios/Runner/Assets.xcassets/LaunchImage.imageset",
    "backend/app/assets/brand",
    "mobile/lib",
]


def _chunk(kind, payload):
    body = kind + payload
    return struct.pack(">I", len(payload)) + body + struct.pack(">I", zlib.crc32(body))


def write_png(path, width, height, colour_type, trns=False):
    """A header-only PNG: exactly the chunks the checker parses, nothing else."""
    ihdr = struct.pack(">IIBBBBB", width, height, 8, colour_type, 0, 0, 0)
    data = b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", ihdr)
    if trns:
        data += _chunk(b"tRNS", b"\x00")
    data += _chunk(b"IEND", b"")
    with open(path, "wb") as fh:
        fh.write(data)


def write_ico(path, sides):
    entries = b"".join(struct.pack("<BBBBHHII", s % 256, s % 256, 0, 0, 1, 32, 0, 0) for s in sides)
    with open(path, "wb") as fh:
        fh.write(struct.pack("<HHH", 0, 1, len(sides)) + entries)


class CheckIconsFailsWhenItShould(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="check-icons-")
        # Directories first: two of the masters are copies living inside
        # frontend/static, so copying them first would pre-create that directory.
        for rel in COPIED_DIRS:
            shutil.copytree(os.path.join(REPO, rel), self.path(rel), dirs_exist_ok=True)
        for rel in check_icons.load_generator().masters():
            self._copy_file(rel)
        self._real_repo = check_icons.REPO
        check_icons.REPO = self.tmp

    def tearDown(self):
        check_icons.REPO = self._real_repo
        shutil.rmtree(self.tmp)

    def _copy_file(self, rel):
        os.makedirs(os.path.dirname(self.path(rel)), exist_ok=True)
        shutil.copy2(os.path.join(REPO, rel), self.path(rel))

    def path(self, rel):
        return os.path.join(self.tmp, rel)

    def run_check(self):
        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            code = check_icons.main()
        return code, err.getvalue()

    def assertFailsNaming(self, *fragments):
        code, err = self.run_check()
        self.assertEqual(code, 1, "the check passed a broken icon set")
        for fragment in fragments:
            self.assertIn(fragment, err)

    def test_the_untouched_copy_passes(self):
        # Without this the failures below could come from a broken fixture.
        code, err = self.run_check()
        self.assertEqual(code, 0, err)

    def test_a_hand_edited_master_fails(self):
        with open(self.path("assets/icon.svg"), "a", encoding="utf-8") as fh:
            fh.write("<!-- nudged by hand -->\n")
        self.assertFailsNaming("assets/icon.svg", "differs from logo-render/gen_svg.py")

    def test_a_missing_copy_fails(self):
        os.remove(self.path("frontend/static/favicon.svg"))
        self.assertFailsNaming("frontend/static/favicon.svg: missing")

    def test_a_raster_at_the_wrong_size_fails(self):
        write_png(self.path("frontend/static/icon-192.png"), 190, 190, 6)
        self.assertFailsNaming("icon-192.png: 190x190, expected 192x192")

    def test_a_social_card_at_the_wrong_size_fails(self):
        # The card is the one non-square target: a square render must not pass.
        write_png(self.path("frontend/static/og-image.png"), 1200, 1200, 2)
        self.assertFailsNaming("og-image.png: 1200x1200, expected 1200x630")

    def test_an_ios_icon_with_an_alpha_channel_fails(self):
        target = f"{check_icons.IOS_SET}/Icon-App-1024x1024@1x.png"
        write_png(self.path(target), 1024, 1024, 6)
        self.assertFailsNaming("Icon-App-1024x1024@1x.png: has an alpha channel")

    def test_an_indexed_ios_icon_carrying_trns_fails(self):
        # The ImageMagick palette trap: colour type 3 looks opaque, tRNS is alpha.
        target = f"{check_icons.IOS_SET}/Icon-App-20x20@2x.png"
        write_png(self.path(target), 40, 40, 3, trns=True)
        self.assertFailsNaming("Icon-App-20x20@2x.png: has an alpha channel")

    def test_an_opaque_notification_icon_fails(self):
        target = f"{check_icons.ANDROID_RES}/drawable-mdpi/ic_notification.png"
        write_png(self.path(target), 24, 24, 2)
        self.assertFailsNaming("ic_notification.png: no alpha channel")

    def test_a_dangling_android_resource_reference_fails(self):
        with open(self.path("mobile/lib/dangling_ref.dart"), "w", encoding="utf-8") as fh:
            fh.write("const icon = '@drawable/ic_not_rendered';\n")
        self.assertFailsNaming("@drawable/ic_not_rendered")

    def test_a_commented_out_reference_is_not_a_reference(self):
        # Flutter's launch_background.xml ships `@mipmap/launch_image` inside a
        # comment; counting it would fail every untouched Flutter project.
        with open(self.path("mobile/lib/commented_ref.dart"), "w", encoding="utf-8") as fh:
            fh.write("// const a = '@drawable/ic_line_comment';\n/* '@mipmap/ic_block_comment' */\n")
        xml = self.path(f"{check_icons.ANDROID_RES}/values/commented_ref.xml")
        with open(xml, "w", encoding="utf-8") as fh:
            fh.write('<resources><!-- <item android:src="@mipmap/ic_xml_comment" /> --></resources>\n')
        code, err = self.run_check()
        self.assertEqual(code, 0, err)

    def test_a_favicon_missing_an_entry_fails(self):
        write_ico(self.path("frontend/static/favicon.ico"), [16, 32])
        self.assertFailsNaming("favicon.ico: no entry for [(48, 48)]")

    def test_a_missing_adaptive_icon_fails(self):
        os.remove(self.path(f"{check_icons.ANDROID_RES}/mipmap-anydpi-v26/ic_launcher.xml"))
        self.assertFailsNaming("ic_launcher.xml: missing")

    def test_a_manifest_declaring_the_wrong_size_fails(self):
        manifest_path = self.path("frontend/static/manifest.webmanifest")
        with open(manifest_path, encoding="utf-8") as fh:
            manifest = json.load(fh)
        manifest["icons"][0]["sizes"] = "180x180"
        with open(manifest_path, "w", encoding="utf-8") as fh:
            json.dump(manifest, fh)
        self.assertFailsNaming("declares 180x180")


if __name__ == "__main__":
    unittest.main()
