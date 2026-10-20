from __future__ import annotations

import tempfile
import unittest
from io import BytesIO
from pathlib import Path

from PIL import Image

from teslawraps.web import CAR_VIEWER_SCRIPT, UploadedFile, generate_job, parse_multipart, render_result_page


def image_bytes(color: str, size: tuple[int, int] = (64, 64)) -> bytes:
    buffer = BytesIO()
    Image.new("RGB", size, color).save(buffer, format="PNG")
    return buffer.getvalue()


class WebUiTests(unittest.TestCase):
    def test_parse_multipart_reads_fields_and_files(self) -> None:
        boundary = "----teslawrapstest"
        body = (
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="fit"\r\n\r\n'
            "polaroid\r\n"
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="images"; filename="photo.png"\r\n'
            "Content-Type: image/png\r\n\r\n"
        ).encode() + b"abc123\r\n" + f"--{boundary}--\r\n".encode()

        fields, files = parse_multipart(f"multipart/form-data; boundary={boundary}", body)

        self.assertEqual(fields["fit"], "polaroid")
        self.assertEqual(files["images"][0].filename, "photo.png")
        self.assertEqual(files["images"][0].data, b"abc123")

    def test_generate_job_creates_outputs_and_zip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            result = generate_job(
                {
                    "fit": "polaroid",
                    "mode": "batch",
                    "size": "512",
                    "seed": "12",
                    "polaroid_size": "180",
                    "name": "web.png",
                    "background": "#123ABC",
                },
                {
                    "images": [
                        UploadedFile("first.png", image_bytes("red")),
                        UploadedFile("second.png", image_bytes("blue")),
                    ],
                    "template": [UploadedFile("template.png", image_bytes("white", (512, 512)))],
                },
                root,
            )

            self.assertEqual(len(result.outputs), 1)
            self.assertTrue(result.outputs[0].is_file())
            self.assertTrue(result.zip_path.is_file())
            self.assertGreater(result.zip_path.stat().st_size, 0)

    def test_generate_job_uses_all_uploaded_images(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            result = generate_job(
                {
                    "fit": "cover",
                    "mode": "batch",
                    "size": "512",
                },
                {
                    "images": [
                        UploadedFile(f"photo-{index:02d}.png", image_bytes("red"))
                        for index in range(12)
                    ],
                    "template": [UploadedFile("template.png", image_bytes("white", (512, 512)))],
                },
                root,
            )

            self.assertEqual(len(result.outputs), 12)
            self.assertEqual(
                sorted(path.name for path in result.outputs),
                [f"photo-{index:02d}.png" for index in range(12)],
            )

    def test_result_page_includes_3d_wrap_viewer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            result = generate_job(
                {
                    "fit": "cover",
                    "mode": "batch",
                    "size": "512",
                },
                {
                    "images": [UploadedFile("photo.png", image_bytes("red"))],
                    "template": [UploadedFile("template.png", image_bytes("white", (512, 512)))],
                },
                root,
            )

            markup = render_result_page(result, model_url="/model/model-y.glb")

            self.assertIn('id="wrap-3d-viewer"', markup)
            self.assertIn('id="wrap-texture-select"', markup)
            self.assertIn('data-model-url="/model/model-y.glb"', markup)
            self.assertIn("GLTFLoader.js", markup)
            self.assertNotIn("BoxGeometry", markup)
            self.assertIn(f"/jobs/{result.job_id}/files/photo.png", markup)

    def test_viewer_selects_broad_uv_meshes_when_model_names_are_generic(self) -> None:
        self.assertIn("collectPaintTargets(THREE, gltf.scene)", CAR_VIEWER_SCRIPT)
        self.assertIn("isBroadExteriorMesh(THREE, node, modelSize)", CAR_VIEWER_SCRIPT)
        self.assertIn("uniqueMeshes([...namedTargets, ...broadExteriorTargets])", CAR_VIEWER_SCRIPT)
        self.assertIn("shell|base", CAR_VIEWER_SCRIPT)
        self.assertIn("rubber|chrome|reflect", CAR_VIEWER_SCRIPT)
        self.assertNotIn("side|front|rear", CAR_VIEWER_SCRIPT)

    def test_result_page_explains_missing_model_y_glb(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            result = generate_job(
                {
                    "fit": "cover",
                    "mode": "batch",
                    "size": "512",
                },
                {
                    "images": [UploadedFile("photo.png", image_bytes("red"))],
                    "template": [UploadedFile("template.png", image_bytes("white", (512, 512)))],
                },
                root,
            )

            markup = render_result_page(result)

            self.assertNotIn("No Model Y GLB is configured", markup)
            self.assertIn('data-model-url="/model/model-y.glb"', markup)

            disabled_markup = render_result_page(result, model_url=None)
            self.assertIn("No Model Y GLB is configured", disabled_markup)
            self.assertIn('data-model-url=""', disabled_markup)


if __name__ == "__main__":
    unittest.main()
