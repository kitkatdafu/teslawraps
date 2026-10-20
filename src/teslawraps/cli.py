from __future__ import annotations

import argparse
import math
import random
import re
import sys
import tempfile
import urllib.request
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Iterable, Sequence

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageOps


DEFAULT_TEMPLATE_URL = (
    "https://raw.githubusercontent.com/teslamotors/custom-wraps/master/modely/template.png"
)
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff"}
TESLA_MAX_BYTES = 1_000_000
TESLA_MIN_SIZE = 512
TESLA_MAX_SIZE = 1024
POLAROID_MIN_SIZE = 64
SAFE_FILENAME_RE = re.compile(r"[^A-Za-z0-9 _-]+")
HEX_RGB_RE = re.compile(r"^#?([0-9A-Fa-f]{3}|[0-9A-Fa-f]{6})$")
PINK_BACKGROUND = (236, 152, 168)
POLAROID_TILT_DEGREES = 16
POLAROID_SLOT_JITTER_FRACTION = 0.18
POLAROID_PANEL_REGIONS: tuple[tuple[float, float, float, float, float], ...] = (
    (0.02, 0.02, 0.34, 0.54, -90.0),
    (0.02, 0.52, 0.34, 0.94, -90.0),
    (0.66, 0.02, 0.98, 0.54, 90.0),
    (0.66, 0.52, 0.98, 0.94, 90.0),
    (0.25, 0.02, 0.75, 0.38, 180.0),
    (0.25, 0.61, 0.75, 0.98, 0.0),
    (0.35, 0.14, 0.65, 0.62, 180.0),
)
CONCRETE_FIT_MODES = ("cover", "contain", "stretch", "tile", "polaroid")
FIT_CHOICES = (*CONCRETE_FIT_MODES, "all")


@dataclass(frozen=True)
class RenderOptions:
    template: Path | None
    template_url: str
    output_dir: Path
    mode: str
    fit: str
    name: str
    seed: int | None
    polaroid_size: int | None
    size: int | None
    background: str
    max_bytes: int
    preserve_filenames: bool
    quality_colors: int


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Apply a directory of images to Tesla custom wrap template masks.",
    )
    parser.add_argument(
        "input_dir",
        type=Path,
        help="Directory containing source images.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("Wraps"),
        help="Directory where wrapped PNG files are written. Defaults to ./Wraps.",
    )
    parser.add_argument(
        "--template",
        type=Path,
        help="Path to a Tesla wrap template PNG. Defaults to downloading the Model Y template.",
    )
    parser.add_argument(
        "--template-url",
        default=DEFAULT_TEMPLATE_URL,
        help="Template URL used when --template is omitted.",
    )
    parser.add_argument(
        "--mode",
        choices=("batch", "mosaic"),
        default="batch",
        help="batch creates one wrap per image; mosaic creates one wrap from all images.",
    )
    parser.add_argument(
        "--fit",
        choices=FIT_CHOICES,
        default="cover",
        help="How each source image is fitted before applying the template mask.",
    )
    parser.add_argument(
        "--size",
        type=int,
        help="Output square size in pixels, from 512 to 1024. Defaults to template size.",
    )
    parser.add_argument(
        "--background",
        default="black",
        help=(
            "Pixels outside the template mask: black, pink, transparent, "
            "or a hex RGB value like #1A2B3C."
        ),
    )
    parser.add_argument(
        "--recursive",
        action="store_true",
        help="Read image files from nested folders.",
    )
    parser.add_argument(
        "--name",
        default="wrap.png",
        help="Output filename for --mode mosaic.",
    )
    parser.add_argument(
        "--seed",
        type=int,
        help="Seed used to reproduce the same polaroid positions and orientations.",
    )
    parser.add_argument(
        "--polaroid-size",
        type=int,
        help=(
            "Width in pixels for each scattered polaroid card. "
            "Defaults to automatic sizing."
        ),
    )
    parser.add_argument(
        "--max-bytes",
        type=int,
        default=TESLA_MAX_BYTES,
        help="Warn when a generated PNG is larger than this many bytes.",
    )
    parser.add_argument(
        "--preserve-filenames",
        action="store_true",
        help="Do not shorten filenames to Tesla's 30-character recommendation.",
    )
    parser.add_argument(
        "--quality-colors",
        type=int,
        default=256,
        help="Palette size used if PNG optimization needs to reduce file size.",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)

    try:
        validate_args(args)
        input_paths = collect_images(args.input_dir, args.recursive)

        if not input_paths:
            raise ValueError(f"No supported image files found in {args.input_dir}")

        args.output_dir.mkdir(parents=True, exist_ok=True)
        template_path = resolve_template(args.template, args.template_url)
        template = Image.open(template_path)

        options = RenderOptions(
            template=args.template,
            template_url=args.template_url,
            output_dir=args.output_dir,
            mode=args.mode,
            fit=args.fit,
            name=args.name,
            seed=args.seed,
            polaroid_size=args.polaroid_size,
            size=args.size,
            background=args.background,
            max_bytes=args.max_bytes,
            preserve_filenames=args.preserve_filenames,
            quality_colors=args.quality_colors,
        )

        if args.fit == "all":
            outputs = render_all(input_paths, template, options)
        else:
            outputs = render_fit(input_paths, template, options)

        for output in outputs:
            print(output)
        return 0
    except Exception as exc:
        print(f"tesla-wraps: error: {exc}", file=sys.stderr)
        return 1


def validate_args(args: argparse.Namespace) -> None:
    if not args.input_dir.is_dir():
        raise ValueError(f"Input directory does not exist: {args.input_dir}")
    if args.size is not None and not TESLA_MIN_SIZE <= args.size <= TESLA_MAX_SIZE:
        raise ValueError(f"--size must be between {TESLA_MIN_SIZE} and {TESLA_MAX_SIZE}")
    if args.max_bytes <= 0:
        raise ValueError("--max-bytes must be greater than zero")
    if args.polaroid_size is not None and not POLAROID_MIN_SIZE <= args.polaroid_size <= TESLA_MAX_SIZE:
        raise ValueError(
            f"--polaroid-size must be between {POLAROID_MIN_SIZE} and {TESLA_MAX_SIZE}"
        )
    if not 2 <= args.quality_colors <= 256:
        raise ValueError("--quality-colors must be between 2 and 256")
    parse_background(args.background)


def collect_images(input_dir: Path, recursive: bool) -> list[Path]:
    paths = input_dir.rglob("*") if recursive else input_dir.iterdir()
    return sorted(
        path
        for path in paths
        if path.is_file() and path.suffix.lower() in IMAGE_EXTENSIONS
    )


def resolve_template(template: Path | None, template_url: str) -> Path:
    if template is not None:
        if not template.is_file():
            raise ValueError(f"Template file does not exist: {template}")
        return template

    cache_path = Path(tempfile.gettempdir()) / "tesla-modely-template.png"
    if cache_path.is_file():
        return cache_path

    with urllib.request.urlopen(template_url, timeout=30) as response:
        cache_path.write_bytes(response.read())
    return cache_path


def render_fit(
    input_paths: Sequence[Path],
    template: Image.Image,
    options: RenderOptions,
) -> list[Path]:
    if options.fit == "polaroid":
        output_path = next_available_path(
            options.output_dir / safe_png_name(options.name, options.preserve_filenames)
        )
        return [render_polaroid(input_paths, template, output_path, options)]

    if options.mode == "batch":
        return render_batch(input_paths, template, options)

    output_path = next_available_path(
        options.output_dir / safe_png_name(options.name, options.preserve_filenames)
    )
    return [render_mosaic(input_paths, template, output_path, options)]


def render_all(
    input_paths: Sequence[Path],
    template: Image.Image,
    options: RenderOptions,
) -> list[Path]:
    outputs: list[Path] = []
    for fit in CONCRETE_FIT_MODES:
        fit_options = replace(
            options,
            fit=fit,
            output_dir=options.output_dir / fit,
        )
        fit_options.output_dir.mkdir(parents=True, exist_ok=True)
        outputs.extend(render_fit(input_paths, template, fit_options))
    return outputs


def render_batch(
    input_paths: Iterable[Path],
    template: Image.Image,
    options: RenderOptions,
) -> list[Path]:
    outputs: list[Path] = []
    for source_path in input_paths:
        output_path = options.output_dir / safe_png_name(
            source_path.stem,
            options.preserve_filenames,
        )
        output_path = next_available_path(output_path)
        outputs.append(render_one(source_path, template, output_path, options))
    return outputs


def render_one(
    source_path: Path,
    template: Image.Image,
    output_path: Path,
    options: RenderOptions,
) -> Path:
    with Image.open(source_path) as source:
        fill = prepare_fill(source, target_size(template, options.size), options.fit)
        wrapped = apply_template(fill, template, options)
    save_png(wrapped, output_path, options.max_bytes, options.quality_colors)
    return output_path


def render_mosaic(
    input_paths: Sequence[Path],
    template: Image.Image,
    output_path: Path,
    options: RenderOptions,
) -> Path:
    size = target_size(template, options.size)
    fill = build_mosaic(input_paths, size, options.fit)
    wrapped = apply_template(fill, template, options)
    save_png(wrapped, output_path, options.max_bytes, options.quality_colors)
    return output_path


def render_polaroid(
    input_paths: Sequence[Path],
    template: Image.Image,
    output_path: Path,
    options: RenderOptions,
) -> Path:
    size = target_size(template, options.size)
    fill = build_polaroid_collage(
        input_paths,
        size,
        options.seed,
        options.polaroid_size,
    )
    wrapped = apply_template(fill, template, options)
    save_png(wrapped, output_path, options.max_bytes, options.quality_colors)
    return output_path


def target_size(template: Image.Image, requested_size: int | None) -> tuple[int, int]:
    if requested_size is not None:
        return requested_size, requested_size

    width, height = template.size
    size = min(width, height, TESLA_MAX_SIZE)
    size = max(size, TESLA_MIN_SIZE)
    return size, size


def prepare_fill(source: Image.Image, size: tuple[int, int], fit: str) -> Image.Image:
    image = ImageOps.exif_transpose(source).convert("RGBA")

    if fit == "cover":
        return ImageOps.fit(image, size, method=Image.Resampling.LANCZOS)
    if fit == "contain":
        contained = ImageOps.contain(image, size, method=Image.Resampling.LANCZOS)
        canvas = Image.new("RGBA", size, (0, 0, 0, 0))
        left = (size[0] - contained.width) // 2
        top = (size[1] - contained.height) // 2
        canvas.alpha_composite(contained, (left, top))
        return canvas
    if fit == "stretch":
        return image.resize(size, Image.Resampling.LANCZOS)
    if fit == "tile":
        return tile_image(image, size)
    if fit == "polaroid":
        return make_polaroid_card(image, min(size) // 2)

    raise ValueError(f"Unsupported fit mode: {fit}")


def tile_image(image: Image.Image, size: tuple[int, int]) -> Image.Image:
    tile = ImageOps.contain(image, size, method=Image.Resampling.LANCZOS)
    canvas = Image.new("RGBA", size, (0, 0, 0, 0))
    for y in range(0, size[1], tile.height):
        for x in range(0, size[0], tile.width):
            canvas.alpha_composite(tile, (x, y))
    return canvas


def build_mosaic(input_paths: Sequence[Path], size: tuple[int, int], fit: str) -> Image.Image:
    count = len(input_paths)
    columns = math.ceil(math.sqrt(count))
    rows = math.ceil(count / columns)
    cell_width = math.ceil(size[0] / columns)
    cell_height = math.ceil(size[1] / rows)
    canvas = Image.new("RGBA", size, (0, 0, 0, 0))

    for index, path in enumerate(input_paths):
        with Image.open(path) as source:
            tile = prepare_fill(source, (cell_width, cell_height), fit)
        x = (index % columns) * cell_width
        y = (index // columns) * cell_height
        canvas.alpha_composite(tile, (x, y))

    return canvas.crop((0, 0, size[0], size[1]))


def build_polaroid_collage(
    input_paths: Sequence[Path],
    size: tuple[int, int],
    seed: int | None,
    polaroid_size: int | None,
) -> Image.Image:
    count = len(input_paths)
    rng_seed = seed if seed is not None else "\n".join(path.name for path in input_paths)
    rng = random.Random(rng_seed)
    canvas = Image.new("RGBA", size, (0, 0, 0, 0))
    card_width = polaroid_card_width(count, size, polaroid_size)
    slots = polaroid_layout_slots(count, size, rng)
    ordered_paths = list(input_paths)
    rng.shuffle(ordered_paths)

    for path, (_region, center_x, center_y) in zip(ordered_paths, slots):

        with Image.open(path) as source:
            card = make_polaroid_card(source, card_width)

        rotated = card.rotate(
            polaroid_rotation_angle(rng, center_x, center_y, size[0], size[1]),
            expand=True,
            fillcolor=(0, 0, 0, 0),
            resample=Image.Resampling.BICUBIC,
        )
        shadow = make_drop_shadow(rotated)

        left = clamp(
            center_x - rotated.width // 2,
            -rotated.width // 4,
            size[0] - rotated.width * 3 // 4,
        )
        top = clamp(
            center_y - rotated.height // 2,
            -rotated.height // 4,
            size[1] - rotated.height * 3 // 4,
        )
        shadow_offset = max(4, card_width // 36)

        canvas.paste(shadow, (left + shadow_offset, top + shadow_offset), shadow)
        canvas.paste(rotated, (left, top), rotated)

    return canvas


def polaroid_card_width(
    count: int,
    size: tuple[int, int],
    polaroid_size: int | None,
) -> int:
    if polaroid_size is not None:
        return polaroid_size

    columns = math.ceil(math.sqrt(count))
    rows = math.ceil(count / columns)
    cell_width = size[0] / columns
    cell_height = size[1] / rows
    card_width = int(min(cell_width * 0.86, cell_height * 0.72, size[0] * 0.46))
    return max(POLAROID_MIN_SIZE, card_width)


def polaroid_layout_slots(
    count: int,
    size: tuple[int, int],
    rng: random.Random,
) -> list[tuple[tuple[float, float, float, float, float], int, int]]:
    region_count = len(POLAROID_PANEL_REGIONS)
    slot_counts = [count // region_count for _region in POLAROID_PANEL_REGIONS]
    extra_regions = list(range(region_count))
    rng.shuffle(extra_regions)
    for region_index in extra_regions[: count % region_count]:
        slot_counts[region_index] += 1

    slots: list[tuple[tuple[float, float, float, float, float], int, int]] = []
    for region, slot_count in zip(POLAROID_PANEL_REGIONS, slot_counts):
        for center_x, center_y in polaroid_region_grid_centers(rng, region, slot_count, size):
            slots.append((region, center_x, center_y))

    rng.shuffle(slots)
    return slots


def polaroid_region_grid_centers(
    rng: random.Random,
    region: tuple[float, float, float, float, float],
    count: int,
    size: tuple[int, int],
) -> list[tuple[int, int]]:
    if count <= 0:
        return []

    x0, y0, x1, y1, _angle = region
    region_width = max((x1 - x0) * size[0], 1)
    region_height = max((y1 - y0) * size[1], 1)
    columns = max(1, math.ceil(math.sqrt(count * region_width / region_height)))
    rows = math.ceil(count / columns)
    centers: list[tuple[int, int]] = []

    for index in range(count):
        column = index % columns
        row = index // columns
        jitter_x = rng.uniform(-POLAROID_SLOT_JITTER_FRACTION, POLAROID_SLOT_JITTER_FRACTION)
        jitter_y = rng.uniform(-POLAROID_SLOT_JITTER_FRACTION, POLAROID_SLOT_JITTER_FRACTION)
        normalized_x = x0 + ((column + 0.5 + jitter_x) / columns) * (x1 - x0)
        normalized_y = y0 + ((row + 0.5 + jitter_y) / rows) * (y1 - y0)
        centers.append(
            (
                clamp(int(normalized_x * size[0]), 0, size[0] - 1),
                clamp(int(normalized_y * size[1]), 0, size[1] - 1),
            )
        )

    rng.shuffle(centers)
    return centers


def polaroid_panel_angle(
    center_x: int,
    center_y: int,
    canvas_width: int,
    canvas_height: int,
) -> float:
    normalized_x = center_x / canvas_width
    normalized_y = center_y / canvas_height
    for x0, y0, x1, y1, angle in POLAROID_PANEL_REGIONS:
        if x0 <= normalized_x <= x1 and y0 <= normalized_y <= y1:
            return angle
    return 0.0


def polaroid_rotation_angle(
    rng: random.Random,
    center_x: int,
    center_y: int,
    canvas_width: int,
    canvas_height: int,
) -> float:
    return polaroid_panel_angle(center_x, center_y, canvas_width, canvas_height) + rng.uniform(
        -POLAROID_TILT_DEGREES,
        POLAROID_TILT_DEGREES,
    )


def make_polaroid_card(source: Image.Image, card_width: int) -> Image.Image:
    border = max(10, card_width // 14)
    bottom_border = max(border * 2, card_width // 4)
    photo_width = card_width - border * 2
    photo_height = photo_width
    card_height = photo_height + border + bottom_border

    card = Image.new("RGBA", (card_width, card_height), (248, 245, 238, 255))
    draw = ImageDraw.Draw(card)
    draw.rectangle(
        (0, 0, card_width - 1, card_height - 1),
        outline=(222, 218, 208, 255),
        width=max(1, card_width // 100),
    )

    photo = prepare_fill(source, (photo_width, photo_height), "cover")
    card.alpha_composite(photo, (border, border))
    return card


def make_drop_shadow(image: Image.Image) -> Image.Image:
    blur_radius = max(4, min(image.size) // 28)
    shadow_alpha = image.getchannel("A").filter(ImageFilter.GaussianBlur(blur_radius))
    shadow = Image.new("RGBA", image.size, (0, 0, 0, 120))
    shadow.putalpha(shadow_alpha.point(lambda value: int(value * 0.45)))
    return shadow


def clamp(value: int, lower: int, upper: int) -> int:
    return max(lower, min(value, upper))


def parse_background(value: str) -> tuple[int, int, int] | None:
    normalized = value.strip()
    lowered = normalized.lower()
    if lowered == "transparent":
        return None
    if lowered == "black":
        return (0, 0, 0)
    if lowered == "pink":
        return PINK_BACKGROUND

    match = HEX_RGB_RE.fullmatch(normalized)
    if match is None:
        raise ValueError(
            "--background must be black, pink, transparent, or a hex RGB value like #1A2B3C"
        )

    digits = match.group(1)
    if len(digits) == 3:
        digits = "".join(character * 2 for character in digits)

    return (
        int(digits[0:2], 16),
        int(digits[2:4], 16),
        int(digits[4:6], 16),
    )


def apply_template(
    fill: Image.Image,
    template: Image.Image,
    options: RenderOptions,
) -> Image.Image:
    size = fill.size
    mask = template_mask(template, size)

    output = Image.new("RGBA", size, (0, 0, 0, 0))
    background_color = parse_background(options.background)
    if background_color is not None:
        background = Image.new("RGBA", size, (*background_color, 255))
        background.putalpha(mask)
        output.alpha_composite(background)

    masked_fill = fill.convert("RGBA")
    alpha = ImageChops.multiply(masked_fill.getchannel("A"), mask)
    masked_fill.putalpha(alpha)
    output.alpha_composite(masked_fill)
    return output


def template_mask(template: Image.Image, size: tuple[int, int]) -> Image.Image:
    mask = ImageOps.grayscale(template)
    if mask.size != size:
        mask = mask.resize(size, Image.Resampling.LANCZOS)
    return mask.point(lambda value: 0 if value < 8 else value)


def save_png(
    image: Image.Image,
    output_path: Path,
    max_bytes: int,
    quality_colors: int,
) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    image.save(output_path, format="PNG", optimize=True)

    if output_path.stat().st_size <= max_bytes:
        return

    if image.mode == "RGBA":
        palette_image = image.quantize(
            colors=quality_colors,
            method=Image.Quantize.FASTOCTREE,
        )
    else:
        palette_image = image.convert("RGB").quantize(
            colors=quality_colors,
            method=Image.Quantize.MEDIANCUT,
        )
    palette_image.save(output_path, format="PNG", optimize=True)

    if output_path.stat().st_size > max_bytes:
        size_kb = output_path.stat().st_size / 1024
        limit_kb = max_bytes / 1024
        print(
            f"warning: {output_path} is {size_kb:.0f} KB, above {limit_kb:.0f} KB",
            file=sys.stderr,
        )


def safe_png_name(name: str, preserve: bool = False) -> str:
    stem = Path(name).stem
    if not preserve:
        stem = SAFE_FILENAME_RE.sub("_", stem).strip(" ._-")
        stem = re.sub(r"\s+", " ", stem)
        if not stem:
            stem = "wrap"
        stem = stem[:26].rstrip(" ._-") or "wrap"
    return f"{stem}.png"


def next_available_path(path: Path) -> Path:
    if not path.exists():
        return path

    stem = path.stem
    suffix = path.suffix
    for index in range(2, 10_000):
        candidate_stem = f"{stem}-{index}"
        if len(candidate_stem) > 26:
            candidate_stem = f"{stem[: 26 - len(str(index)) - 1]}-{index}"
        candidate = path.with_name(f"{candidate_stem}{suffix}")
        if not candidate.exists():
            return candidate

    raise ValueError(f"Could not find an available filename for {path}")


if __name__ == "__main__":
    raise SystemExit(main())
