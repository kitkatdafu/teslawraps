# Tesla Wrap Generator

Batch-generate Tesla custom wrap PNG files from a directory of source images.
The default template is Tesla's Model Y custom wrap mask, where the white areas
are filled with your image and the black areas remain hidden.

Tesla's published requirements are:

- PNG output
- 512x512 to 1024x1024 pixels
- no larger than 1 MB
- filename max 30 characters using letters, numbers, spaces, underscores, and dashes
- up to 10 images in a `Wraps` USB folder

## Install

```bash
uv sync --no-editable
```

Or run without installing into your global Python:

```bash
uv run --no-editable tesla-wraps --help
```

## Web UI

```bash
uv run --no-editable tesla-wraps-web --port 8765
```

Open `http://127.0.0.1:8765` to upload images, set wrap options, preview the
generated PNGs, and download all outputs as a zip.

The 3D preview is enabled by default and looks for a licensed UV-mapped
Model Y GLB at `assets/model-y.glb`. You can also pass an explicit path:

```bash
uv run --no-editable tesla-wraps-web --model ./assets/model-y.glb
```

The project does not ship a Model Y mesh. The GLB needs body-panel UVs that
match the Tesla Paint Shop wrap template for the generated PNG to align.

## Batch Images Onto The Model Y Template

```bash
uv run --no-editable tesla-wraps ./images --output-dir ./Wraps
```

That command downloads the Model Y template from Tesla if no local template is
provided, then produces one wrapped PNG per input image.

Use a template file you already downloaded:

```bash
uv run --no-editable tesla-wraps ./images --template ./modely/template.png --output-dir ./Wraps
```

Generate a single collage-style wrap from all images in the directory:

```bash
uv run --no-editable tesla-wraps ./images --mode mosaic --output-dir ./Wraps --name My_Wrap.png
```

Generate a single wrap with every image scattered as polaroid photos:

```bash
uv run --no-editable tesla-wraps ./images --fit polaroid --output-dir ./Wraps --name Polaroids.png
```

Control the polaroid card positions, order, and orientation with a seed:

```bash
uv run --no-editable tesla-wraps ./images --fit polaroid --seed 42 --output-dir ./Wraps
```

Set the size of each scattered polaroid card:

```bash
uv run --no-editable tesla-wraps ./images --fit polaroid --polaroid-size 220 --output-dir ./Wraps
```

Use a custom background color:

```bash
uv run --no-editable tesla-wraps ./images --background '#1A2B3C' --output-dir ./Wraps
```

Generate every fit mode at once:

```bash
uv run --no-editable tesla-wraps ./images --fit all --output-dir ./Wraps
```

`--fit all` writes each fit into its own subfolder, such as `Wraps/cover`,
`Wraps/contain`, and `Wraps/polaroid`.

## Useful Options

```text
--fit cover       Crop the image to fill the template square. This is the default.
--fit contain     Fit the whole image inside the template, leaving background around it.
--fit stretch     Stretch the image to the template square.
--fit tile        Repeat the image as a pattern across the template.
--fit polaroid    Scatter every input image as polaroid photos on one wrap.
--fit all         Generate cover, contain, stretch, tile, and polaroid outputs.
--size 1024       Resize output to a square between 512 and 1024 pixels.
--background      Use black, pink, transparent, or hex RGB inside the template region.
--seed 42         Reproduce specific polaroid positions and orientations.
--polaroid-size   Set scattered polaroid card width in pixels.
--recursive       Include images in nested input folders.
```

## USB Layout

Copy the generated PNG files into a folder named `Wraps` at the root of a
supported USB drive. In the car, load them from Toybox > Paint Shop > Wraps.
