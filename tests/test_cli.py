from __future__ import annotations

import random
import tempfile
import unittest
from pathlib import Path

from PIL import Image

from teslawraps.cli import (
    POLAROID_PANEL_REGIONS,
    main,
    polaroid_card_width,
    polaroid_layout_slots,
    polaroid_panel_angle,
    polaroid_rotation_angle,
    safe_png_name,
)


class WrapGeneratorTests(unittest.TestCase):
    def test_safe_png_name_matches_tesla_constraints(self) -> None:
        name = safe_png_name("too/long!*name with invalid chars and extra words.jpg")

        self.assertLessEqual(len(name), 30)
        self.assertEqual(Path(name).suffix, ".png")
        self.assertNotIn("!", name)
        self.assertNotIn("*", name)

    def test_batch_applies_image_only_to_template_mask(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            input_dir = root / "input"
            output_dir = root / "Wraps"
            input_dir.mkdir()

            template = Image.new("RGB", (512, 512), "black")
            for x in range(128, 384):
                for y in range(128, 384):
                    template.putpixel((x, y), (255, 255, 255))
            template_path = root / "template.png"
            template.save(template_path)

            source = Image.new("RGB", (64, 64), "red")
            source.save(input_dir / "sample.jpg")

            exit_code = main(
                [
                    str(input_dir),
                    "--template",
                    str(template_path),
                    "--output-dir",
                    str(output_dir),
                    "--size",
                    "512",
                ]
            )

            self.assertEqual(exit_code, 0)
            output_path = output_dir / "sample.png"
            self.assertTrue(output_path.is_file())

            with Image.open(output_path) as output:
                self.assertEqual(output.size, (512, 512))
                rgba = output.convert("RGBA")
                self.assertEqual(rgba.getpixel((8, 8)), (0, 0, 0, 0))
                self.assertEqual(rgba.getpixel((256, 256)), (254, 0, 0, 255))

    def test_batch_uses_all_images_in_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            input_dir = root / "input"
            output_dir = root / "Wraps"
            input_dir.mkdir()

            template_path = root / "template.png"
            Image.new("RGB", (512, 512), "white").save(template_path)

            for index in range(12):
                Image.new("RGB", (64, 64), (index * 20, index * 10, 120)).save(
                    input_dir / f"photo-{index:02d}.png"
                )

            exit_code = main(
                [
                    str(input_dir),
                    "--template",
                    str(template_path),
                    "--output-dir",
                    str(output_dir),
                    "--size",
                    "512",
                ]
            )

            self.assertEqual(exit_code, 0)
            self.assertEqual(
                sorted(path.name for path in output_dir.iterdir()),
                [f"photo-{index:02d}.png" for index in range(12)],
            )

    def test_hex_background_only_applies_inside_template_region(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            input_dir = root / "input"
            output_dir = root / "Wraps"
            input_dir.mkdir()

            template = Image.new("RGB", (512, 512), "black")
            for x in range(128, 384):
                for y in range(128, 384):
                    template.putpixel((x, y), (255, 255, 255))
            template_path = root / "template.png"
            template.save(template_path)

            Image.new("RGBA", (64, 64), (0, 0, 0, 0)).save(input_dir / "sample.png")

            exit_code = main(
                [
                    str(input_dir),
                    "--template",
                    str(template_path),
                    "--output-dir",
                    str(output_dir),
                    "--size",
                    "512",
                    "--background",
                    "#123ABC",
                ]
            )

            self.assertEqual(exit_code, 0)
            with Image.open(output_dir / "sample.png") as output:
                rgba = output.convert("RGBA")
                self.assertEqual(rgba.getpixel((8, 8)), (0, 0, 0, 0))
                self.assertEqual(rgba.getpixel((256, 256)), (18, 58, 188, 255))

    def test_pink_background_only_applies_inside_template_region(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            input_dir = root / "input"
            output_dir = root / "Wraps"
            input_dir.mkdir()

            template = Image.new("RGB", (512, 512), "black")
            for x in range(128, 384):
                for y in range(128, 384):
                    template.putpixel((x, y), (255, 255, 255))
            template_path = root / "template.png"
            template.save(template_path)

            Image.new("RGBA", (64, 64), (0, 0, 0, 0)).save(input_dir / "sample.png")

            exit_code = main(
                [
                    str(input_dir),
                    "--template",
                    str(template_path),
                    "--output-dir",
                    str(output_dir),
                    "--size",
                    "512",
                    "--background",
                    "pink",
                ]
            )

            self.assertEqual(exit_code, 0)
            with Image.open(output_dir / "sample.png") as output:
                rgba = output.convert("RGBA")
                self.assertEqual(rgba.getpixel((8, 8)), (0, 0, 0, 0))
                self.assertEqual(rgba.getpixel((256, 256)), (236, 152, 168, 255))

    def test_polaroid_fit_scatters_directory_into_single_wrap(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            input_dir = root / "input"
            output_dir = root / "Wraps"
            input_dir.mkdir()

            template_path = root / "template.png"
            Image.new("RGB", (512, 512), "white").save(template_path)

            for index, color in enumerate(("red", "green", "blue")):
                Image.new("RGB", (96, 64), color).save(input_dir / f"photo-{index}.png")

            exit_code = main(
                [
                    str(input_dir),
                    "--template",
                    str(template_path),
                    "--output-dir",
                    str(output_dir),
                    "--size",
                    "512",
                    "--fit",
                    "polaroid",
                    "--name",
                    "polaroids.png",
                ]
            )

            self.assertEqual(exit_code, 0)
            output_paths = list(output_dir.iterdir())
            self.assertEqual(output_paths, [output_dir / "polaroids.png"])

            with Image.open(output_paths[0]) as output:
                self.assertEqual(output.size, (512, 512))
                rgb_bytes = output.convert("RGB").tobytes()
                pixels = list(
                    zip(rgb_bytes[0::3], rgb_bytes[1::3], rgb_bytes[2::3])
                )

            whiteish_pixels = sum(
                1 for red, green, blue in pixels if red > 230 and green > 225 and blue > 215
            )
            colored_pixels = sum(
                1 for red, green, blue in pixels if max(red, green, blue) - min(red, green, blue) > 80
            )
            self.assertGreater(whiteish_pixels, 5_000)
            self.assertGreater(colored_pixels, 10_000)

    def test_polaroid_size_controls_card_width(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            input_dir = root / "input"
            input_dir.mkdir()

            template_path = root / "template.png"
            Image.new("RGB", (512, 512), "white").save(template_path)

            for index, color in enumerate(("red", "blue")):
                Image.new("RGB", (96, 64), color).save(input_dir / f"photo-{index}.png")

            def non_black_pixels(polaroid_size: int, output_dir: Path) -> int:
                exit_code = main(
                    [
                        str(input_dir),
                        "--template",
                        str(template_path),
                        "--output-dir",
                        str(output_dir),
                        "--size",
                        "512",
                        "--fit",
                        "polaroid",
                        "--name",
                        "polaroids.png",
                        "--seed",
                        "42",
                        "--polaroid-size",
                        str(polaroid_size),
                    ]
                )

                self.assertEqual(exit_code, 0)
                with Image.open(output_dir / "polaroids.png") as output:
                    rgb_bytes = output.convert("RGB").tobytes()
                return sum(
                    1
                    for red, green, blue in zip(
                        rgb_bytes[0::3],
                        rgb_bytes[1::3],
                        rgb_bytes[2::3],
                    )
                    if (red, green, blue) != (0, 0, 0)
                )

            small = non_black_pixels(120, root / "small")
            large = non_black_pixels(260, root / "large")

            self.assertGreater(large, small)

    def test_polaroid_seed_controls_scatter_layout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            input_dir = root / "input"
            input_dir.mkdir()

            template_path = root / "template.png"
            Image.new("RGB", (512, 512), "white").save(template_path)

            for index, color in enumerate(("red", "green", "blue", "yellow")):
                Image.new("RGB", (96, 64), color).save(input_dir / f"photo-{index}.png")

            def render(seed: int, output_dir: Path) -> bytes:
                exit_code = main(
                    [
                        str(input_dir),
                        "--template",
                        str(template_path),
                        "--output-dir",
                        str(output_dir),
                        "--size",
                        "512",
                        "--fit",
                        "polaroid",
                        "--name",
                        "polaroids.png",
                        "--seed",
                        str(seed),
                    ]
                )

                self.assertEqual(exit_code, 0)
                return (output_dir / "polaroids.png").read_bytes()

            first = render(42, root / "seed-42-a")
            second = render(42, root / "seed-42-b")
            different = render(7, root / "seed-7")

            self.assertEqual(first, second)
            self.assertNotEqual(first, different)

    def test_polaroid_auto_size_shrinks_for_large_directories(self) -> None:
        width = polaroid_card_width(88, (1024, 1024), None)

        self.assertGreaterEqual(width, 64)
        self.assertLess(width, 120)
        self.assertEqual(polaroid_card_width(88, (1024, 1024), 220), 220)

    def test_polaroid_slots_spread_across_panel_regions(self) -> None:
        slots = polaroid_layout_slots(14, (1024, 1024), random.Random(42))

        self.assertEqual(len(slots), 14)
        self.assertEqual({region for region, _x, _y in slots}, set(POLAROID_PANEL_REGIONS))

    def test_polaroid_orientation_faces_template_panel(self) -> None:
        self.assertEqual(polaroid_panel_angle(117, 414, 1024, 1024), -90.0)
        self.assertEqual(polaroid_panel_angle(338, 385, 1024, 1024), -90.0)
        self.assertEqual(polaroid_panel_angle(909, 413, 1024, 1024), 90.0)
        self.assertEqual(polaroid_panel_angle(684, 385, 1024, 1024), 90.0)
        self.assertEqual(polaroid_panel_angle(511, 78, 1024, 1024), 180.0)
        self.assertEqual(polaroid_panel_angle(511, 500, 1024, 1024), 180.0)
        self.assertEqual(polaroid_panel_angle(510, 953, 1024, 1024), 0.0)

    def test_polaroid_orientation_keeps_panel_facing_with_random_tilt(self) -> None:
        rng = random.Random(42)

        left = polaroid_rotation_angle(rng, 117, 414, 1024, 1024)
        front_or_top = polaroid_rotation_angle(rng, 511, 78, 1024, 1024)
        right = polaroid_rotation_angle(rng, 909, 413, 1024, 1024)

        self.assertTrue(-106 <= left <= -74)
        self.assertTrue(164 <= front_or_top <= 196)
        self.assertTrue(74 <= right <= 106)

    def test_all_fit_writes_each_fit_mode_to_subdirectory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            input_dir = root / "input"
            output_dir = root / "Wraps"
            input_dir.mkdir()

            template_path = root / "template.png"
            Image.new("RGB", (512, 512), "white").save(template_path)

            for index, color in enumerate(("red", "blue")):
                Image.new("RGB", (80, 120), color).save(input_dir / f"photo-{index}.png")

            exit_code = main(
                [
                    str(input_dir),
                    "--template",
                    str(template_path),
                    "--output-dir",
                    str(output_dir),
                    "--size",
                    "512",
                    "--fit",
                    "all",
                    "--name",
                    "all.png",
                ]
            )

            self.assertEqual(exit_code, 0)

            for fit in ("cover", "contain", "stretch", "tile"):
                fit_dir = output_dir / fit
                self.assertTrue(fit_dir.is_dir())
                self.assertEqual(
                    sorted(path.name for path in fit_dir.iterdir()),
                    ["photo-0.png", "photo-1.png"],
                )

            self.assertEqual(
                sorted(path.name for path in (output_dir / "polaroid").iterdir()),
                ["all.png"],
            )


if __name__ == "__main__":
    unittest.main()
