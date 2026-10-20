from __future__ import annotations

import argparse
import html
import mimetypes
import re
import tempfile
import uuid
import zipfile
from dataclasses import dataclass
from email import policy
from email.parser import BytesParser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Mapping, Sequence
from urllib.parse import quote, unquote, urlparse

from PIL import Image

from .cli import (
    DEFAULT_TEMPLATE_URL,
    FIT_CHOICES,
    IMAGE_EXTENSIONS,
    POLAROID_MIN_SIZE,
    TESLA_MAX_BYTES,
    TESLA_MAX_SIZE,
    collect_images,
    render_all,
    render_fit,
    resolve_template,
    validate_args,
    RenderOptions,
)


DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
DEFAULT_JOBS_DIR = Path(tempfile.gettempdir()) / "tesla-wraps-web"
DEFAULT_MODEL_PATH = Path("assets/model-y.glb")
DEFAULT_MODEL_URL = "/model/model-y.glb"
MAX_UPLOAD_BYTES = 250 * 1024 * 1024
UPLOAD_NAME_RE = re.compile(r"[^A-Za-z0-9._ -]+")


@dataclass(frozen=True)
class UploadedFile:
    filename: str
    data: bytes


@dataclass(frozen=True)
class JobResult:
    job_id: str
    output_dir: Path
    outputs: list[Path]
    zip_path: Path


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the Tesla Wrap Generator web UI.")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument(
        "--jobs-dir",
        type=Path,
        default=DEFAULT_JOBS_DIR,
        help="Directory for temporary uploads, generated wraps, and zip files.",
    )
    parser.add_argument(
        "--model",
        type=Path,
        default=DEFAULT_MODEL_PATH,
        help=(
            "Path to a UV-mapped Tesla Model Y GLB used for the 3D preview. "
            "Defaults to ./assets/model-y.glb."
        ),
    )
    args = parser.parse_args(argv)

    args.jobs_dir.mkdir(parents=True, exist_ok=True)
    server = ThreadingHTTPServer((args.host, args.port), WrapWebHandler)
    server.jobs_dir = args.jobs_dir
    server.model_path = args.model

    print(f"Tesla Wrap Generator web UI running at http://{args.host}:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping web UI.")
    finally:
        server.server_close()
    return 0


class WrapWebHandler(BaseHTTPRequestHandler):
    server_version = "TeslaWrapsWeb/0.1"

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/":
            self.send_html(render_index_page())
            return

        if path.startswith("/jobs/"):
            self.handle_job_file(path)
            return

        if path.startswith("/model/"):
            self.handle_model_file(path)
            return

        self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if path != "/generate":
            self.send_error(HTTPStatus.NOT_FOUND)
            return

        content_type = self.headers.get("Content-Type", "")
        if "multipart/form-data" not in content_type:
            self.send_html(render_index_page("Upload images with the form."), HTTPStatus.BAD_REQUEST)
            return

        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self.send_html(render_index_page("Invalid upload length."), HTTPStatus.BAD_REQUEST)
            return

        if length <= 0 or length > MAX_UPLOAD_BYTES:
            self.send_html(
                render_index_page("Upload size must be between 1 byte and 250 MB."),
                HTTPStatus.BAD_REQUEST,
            )
            return

        try:
            body = self.rfile.read(length)
            fields, files = parse_multipart(content_type, body)
            result = generate_job(fields, files, self.server.jobs_dir)
            self.send_html(render_result_page(result, self.model_url()))
        except Exception as exc:
            self.send_html(render_index_page(str(exc)), HTTPStatus.BAD_REQUEST)

    def handle_job_file(self, path: str) -> None:
        parts = path.split("/", 4)
        if len(parts) < 4 or parts[1] != "jobs" or not is_safe_job_id(parts[2]):
            self.send_error(HTTPStatus.NOT_FOUND)
            return

        job_id = parts[2]
        action = parts[3]
        job_root = self.server.jobs_dir / job_id

        if action == "download":
            self.send_file(job_root / "wraps.zip", "application/zip", "wraps.zip")
            return

        if action == "files" and len(parts) == 5:
            rel_path = unquote(parts[4])
            output_root = (job_root / "output").resolve()
            target = (output_root / rel_path).resolve()
            try:
                target.relative_to(output_root)
            except ValueError:
                self.send_error(HTTPStatus.NOT_FOUND)
                return

            self.send_file(target)
            return

        self.send_error(HTTPStatus.NOT_FOUND)

    def handle_model_file(self, path: str) -> None:
        model_path = self.server.model_path
        model_url = self.model_url()
        if model_url is None or path != model_url or not model_path.is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return

        self.send_file(model_path, "model/gltf-binary", "model-y.glb")

    def model_url(self) -> str | None:
        model_path = self.server.model_path
        if model_path.suffix.lower() != ".glb":
            return None
        return DEFAULT_MODEL_URL

    def send_html(self, markup: str, status: HTTPStatus = HTTPStatus.OK) -> None:
        payload = markup.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def send_file(
        self,
        path: Path,
        content_type: str | None = None,
        download_name: str | None = None,
    ) -> None:
        if not path.is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return

        payload = path.read_bytes()
        guessed_type = content_type or mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", guessed_type)
        self.send_header("Content-Length", str(len(payload)))
        if download_name is not None:
            self.send_header("Content-Disposition", f'attachment; filename="{download_name}"')
        self.end_headers()
        self.wfile.write(payload)


def parse_multipart(
    content_type: str,
    body: bytes,
) -> tuple[dict[str, str], dict[str, list[UploadedFile]]]:
    header = f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode()
    message = BytesParser(policy=policy.default).parsebytes(header + body)
    if not message.is_multipart():
        raise ValueError("Expected multipart form data.")

    fields: dict[str, str] = {}
    files: dict[str, list[UploadedFile]] = {}
    for part in message.iter_parts():
        name = part.get_param("name", header="content-disposition")
        if not name:
            continue

        payload = part.get_payload(decode=True) or b""
        filename = part.get_filename()
        if filename:
            files.setdefault(name, []).append(UploadedFile(filename, payload))
            continue

        charset = part.get_content_charset() or "utf-8"
        fields[name] = payload.decode(charset, errors="replace")

    return fields, files


def generate_job(
    fields: Mapping[str, str],
    files: Mapping[str, list[UploadedFile]],
    jobs_root: Path,
) -> JobResult:
    job_id = uuid.uuid4().hex[:12]
    job_root = jobs_root / job_id
    input_dir = job_root / "input"
    output_dir = job_root / "output"
    input_dir.mkdir(parents=True)
    output_dir.mkdir(parents=True)

    image_files = files.get("images", []) + files.get("image_files", [])
    image_paths = write_uploaded_images(image_files, input_dir)
    if not image_paths:
        raise ValueError("Upload at least one supported image file.")

    template_path = write_uploaded_template(files.get("template", []), job_root)
    template_url = field_value(fields, "template_url", DEFAULT_TEMPLATE_URL)
    if template_path is None:
        template_path = resolve_template(None, template_url)

    mode = field_value(fields, "mode", "batch")
    fit = field_value(fields, "fit", "cover")
    if mode not in {"batch", "mosaic"}:
        raise ValueError("Mode must be batch or mosaic.")
    if fit not in FIT_CHOICES:
        raise ValueError(f"Fit must be one of: {', '.join(FIT_CHOICES)}.")

    args = argparse.Namespace(
        input_dir=input_dir,
        size=parse_optional_int(fields, "size"),
        max_bytes=parse_int(fields, "max_bytes", TESLA_MAX_BYTES),
        polaroid_size=parse_optional_int(fields, "polaroid_size"),
        quality_colors=parse_int(fields, "quality_colors", 256),
        background=field_value(fields, "background", "black"),
    )
    validate_args(args)

    input_paths = collect_images(input_dir, recursive=False)

    options = RenderOptions(
        template=template_path,
        template_url=template_url,
        output_dir=output_dir,
        mode=mode,
        fit=fit,
        name=field_value(fields, "name", "wrap.png") or "wrap.png",
        seed=parse_optional_int(fields, "seed"),
        polaroid_size=args.polaroid_size,
        size=args.size,
        background=args.background,
        max_bytes=args.max_bytes,
        preserve_filenames=is_checked(fields, "preserve_filenames"),
        quality_colors=args.quality_colors,
    )

    with Image.open(template_path) as template:
        outputs = render_all(input_paths, template, options) if fit == "all" else render_fit(input_paths, template, options)

    zip_path = write_zip(job_root / "wraps.zip", output_dir, outputs)
    return JobResult(job_id=job_id, output_dir=output_dir, outputs=outputs, zip_path=zip_path)


def write_uploaded_images(uploaded_files: Sequence[UploadedFile], input_dir: Path) -> list[Path]:
    paths: list[Path] = []
    for uploaded in uploaded_files:
        if not uploaded.filename or not uploaded.data:
            continue

        filename = safe_upload_name(uploaded.filename)
        if Path(filename).suffix.lower() not in IMAGE_EXTENSIONS:
            continue

        path = next_available_path(input_dir / filename)
        path.write_bytes(uploaded.data)
        paths.append(path)

    return paths


def write_uploaded_template(uploaded_files: Sequence[UploadedFile] | None, job_root: Path) -> Path | None:
    if not uploaded_files:
        return None

    for uploaded in uploaded_files:
        if not uploaded.filename or not uploaded.data:
            continue

        path = job_root / f"template{Path(safe_upload_name(uploaded.filename)).suffix or '.png'}"
        path.write_bytes(uploaded.data)
        return path

    return None


def write_zip(zip_path: Path, output_dir: Path, outputs: Sequence[Path]) -> Path:
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for output in outputs:
            archive.write(output, output.relative_to(output_dir))
    return zip_path


def parse_int(fields: Mapping[str, str], key: str, default: int) -> int:
    value = field_value(fields, key, "")
    if not value:
        return default
    try:
        return int(value)
    except ValueError as exc:
        raise ValueError(f"{key.replace('_', '-')} must be a whole number.") from exc


def parse_optional_int(fields: Mapping[str, str], key: str) -> int | None:
    value = field_value(fields, key, "")
    if not value:
        return None
    try:
        return int(value)
    except ValueError as exc:
        raise ValueError(f"{key.replace('_', '-')} must be a whole number.") from exc


def field_value(fields: Mapping[str, str], key: str, default: str) -> str:
    return str(fields.get(key, default)).strip()


def is_checked(fields: Mapping[str, str], key: str) -> bool:
    return field_value(fields, key, "").lower() in {"1", "true", "on", "yes"}


def safe_upload_name(filename: str) -> str:
    name = filename.replace("\\", "/").split("/")[-1].strip()
    name = UPLOAD_NAME_RE.sub("_", name).strip(" ._")
    return name or "upload.png"


def next_available_path(path: Path) -> Path:
    if not path.exists():
        return path

    for index in range(2, 10_000):
        candidate = path.with_name(f"{path.stem}-{index}{path.suffix}")
        if not candidate.exists():
            return candidate

    raise ValueError(f"Could not find an available filename for {path.name}.")


def is_safe_job_id(value: str) -> bool:
    return bool(re.fullmatch(r"[0-9a-f]{12}", value))


def render_index_page(error: str | None = None) -> str:
    error_markup = ""
    if error:
        error_markup = f'<div class="notice error">{html.escape(error)}</div>'

    fit_options = "\n".join(
        option("fit", value, label=value, selected=value == "cover")
        for value in FIT_CHOICES
    )

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Tesla Wrap Generator</title>
  <style>{STYLES}</style>
</head>
<body>
  <main class="shell">
    <section class="panel form-panel">
      <div class="title-row">
        <div>
          <p class="eyebrow">Tesla Wrap Generator</p>
          <h1>Build custom wrap PNGs</h1>
        </div>
        <span class="badge">Model Y template default</span>
      </div>
      {error_markup}
      <form action="/generate" method="post" enctype="multipart/form-data">
        <div class="field upload-field">
          <label for="images">Images</label>
          <input id="images" name="images" type="file" accept="image/*" multiple required>
        </div>
        <div class="field upload-field">
          <label for="image-folder">Folder</label>
          <input id="image-folder" name="images" type="file" accept="image/*" multiple webkitdirectory>
        </div>
        <div class="field upload-field">
          <label for="template">Template override</label>
          <input id="template" name="template" type="file" accept="image/*">
        </div>

        <div class="grid">
          <label class="field">Fit
            <select name="fit">{fit_options}</select>
          </label>
          <label class="field">Mode
            <select name="mode">
              <option value="batch" selected>batch</option>
              <option value="mosaic">mosaic</option>
            </select>
          </label>
          <label class="field">Output size
            <input name="size" type="number" min="512" max="1024" placeholder="1024">
          </label>
          <label class="field">Background
            <input name="background" type="text" value="black" placeholder="pink or #1A2B3C">
          </label>
          <label class="field">Output name
            <input name="name" type="text" value="wrap.png">
          </label>
          <label class="field">Seed
            <input name="seed" type="number" placeholder="42">
          </label>
          <label class="field">Polaroid size
            <input name="polaroid_size" type="number" min="{POLAROID_MIN_SIZE}" max="{TESLA_MAX_SIZE}" placeholder="auto">
          </label>
          <label class="field">Max bytes
            <input name="max_bytes" type="number" min="1" value="{TESLA_MAX_BYTES}">
          </label>
          <label class="field">Palette colors
            <input name="quality_colors" type="number" min="2" max="256" value="256">
          </label>
        </div>

        <label class="check">
          <input name="preserve_filenames" type="checkbox">
          <span>Preserve filenames</span>
        </label>

        <div class="actions">
          <button type="submit">Generate wraps</button>
        </div>
      </form>
    </section>
  </main>
</body>
</html>"""


def render_result_page(result: JobResult, model_url: str | None = DEFAULT_MODEL_URL) -> str:
    cards = []
    for output in result.outputs:
        rel_path = output.relative_to(result.output_dir).as_posix()
        preview_url = f"/jobs/{result.job_id}/files/{quote(rel_path, safe='/')}"
        cards.append(
            f"""<article class="card">
  <a href="{preview_url}" target="_blank" rel="noreferrer">
    <img src="{preview_url}" alt="{html.escape(rel_path)}">
  </a>
  <div class="card-footer">
    <span>{html.escape(rel_path)}</span>
    <a href="{preview_url}" download>Download</a>
  </div>
</article>"""
        )

    gallery = "\n".join(cards)
    viewer = render_car_viewer(result, model_url)
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Generated Wraps</title>
  <style>{STYLES}</style>
</head>
<body>
  <main class="shell">
    <section class="panel">
      <div class="title-row">
        <div>
          <p class="eyebrow">Generated Wraps</p>
          <h1>{len(result.outputs)} PNG file{"s" if len(result.outputs) != 1 else ""}</h1>
        </div>
        <a class="button-link" href="/jobs/{result.job_id}/download">Download zip</a>
      </div>
      <div class="actions secondary-actions">
        <a href="/">Generate another set</a>
      </div>
      {viewer}
      <div class="gallery">{gallery}</div>
    </section>
  </main>
</body>
</html>"""


def render_car_viewer(result: JobResult, model_url: str | None = DEFAULT_MODEL_URL) -> str:
    if not result.outputs:
        return ""

    options = []
    first_url = ""
    for index, output in enumerate(result.outputs):
        rel_path = output.relative_to(result.output_dir).as_posix()
        preview_url = f"/jobs/{result.job_id}/files/{quote(rel_path, safe='/')}"
        if not first_url:
            first_url = preview_url
        selected_attr = " selected" if index == 0 else ""
        options.append(
            f'<option value="{html.escape(preview_url, quote=True)}"{selected_attr}>'
            f"{html.escape(rel_path)}</option>"
        )

    texture_options = "\n".join(options)
    first_url_attr = html.escape(first_url, quote=True)
    model_url_attr = html.escape(model_url or "", quote=True)
    model_note = ""
    if not model_url:
        model_note = (
            '<p class="viewer-note">No Model Y GLB is configured. '
            'Place a UV-mapped Model Y model at assets/model-y.glb or start '
            'the web UI with --model /path/to/model-y.glb.</p>'
        )
    return f"""<section class="viewer-section">
  <div class="viewer-header">
    <div>
      <p class="eyebrow">3D Preview</p>
      <h2>Model Y wrap preview</h2>
    </div>
    <label class="viewer-select">Wrap file
      <select id="wrap-texture-select">{texture_options}</select>
    </label>
  </div>
  {model_note}
  <div id="wrap-3d-viewer" class="car-viewer" data-wrap-url="{first_url_attr}" data-model-url="{model_url_attr}"></div>
</section>
<script type="module">{CAR_VIEWER_SCRIPT}</script>"""


def option(name: str, value: str, label: str, selected: bool = False) -> str:
    selected_attr = " selected" if selected else ""
    return f'<option name="{html.escape(name)}" value="{html.escape(value)}"{selected_attr}>{html.escape(label)}</option>'


CAR_VIEWER_SCRIPT = """
const viewerMount = document.getElementById("wrap-3d-viewer");
const textureSelect = document.getElementById("wrap-texture-select");

if (viewerMount) {
  bootWrapViewer(viewerMount, textureSelect);
}

async function bootWrapViewer(mount, select) {
  const modelUrl = mount.dataset.modelUrl || "";
  if (!modelUrl) {
    showViewerMessage(
      mount,
      "Add a UV-mapped Model Y GLB at assets/model-y.glb or start the web UI with --model."
    );
    return;
  }

  try {
    const THREE = await import("https://esm.sh/three@0.165.0");
    const { GLTFLoader } = await import(
      "https://esm.sh/three@0.165.0/examples/jsm/loaders/GLTFLoader.js"
    );
    initWrapViewer(THREE, GLTFLoader, mount, select, modelUrl);
  } catch (error) {
    showViewerMessage(mount, "3D preview unavailable.");
    console.error(error);
  }
}

function showViewerMessage(mount, message) {
  mount.classList.add("is-unavailable");
  mount.textContent = message;
}

function initWrapViewer(THREE, GLTFLoader, mount, select, modelUrl) {
  const scene = new THREE.Scene();
  scene.background = new THREE.Color(0x101513);

  const camera = new THREE.PerspectiveCamera(34, 1, 0.1, 100);
  camera.position.set(5, 2.8, 5.5);
  camera.lookAt(0, 0.35, 0);

  const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: false });
  renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
  renderer.outputColorSpace = THREE.SRGBColorSpace;
  mount.replaceChildren(renderer.domElement);

  const car = new THREE.Group();
  scene.add(car);

  scene.add(new THREE.HemisphereLight(0xffffff, 0x303a36, 2.2));

  const key = new THREE.DirectionalLight(0xffffff, 2.8);
  key.position.set(3, 5, 4);
  scene.add(key);

  const fill = new THREE.DirectionalLight(0xbfd8ff, 1.2);
  fill.position.set(-5, 3, -4);
  scene.add(fill);

  const ground = new THREE.Mesh(
    new THREE.PlaneGeometry(8, 5.6),
    new THREE.MeshStandardMaterial({ color: 0x28312d, roughness: 0.9 })
  );
  ground.rotation.x = -Math.PI / 2;
  ground.position.y = -0.72;
  scene.add(ground);

  let paintTargets = [];
  let activeWrapUrl = mount.dataset.wrapUrl || (select ? select.value : "");

  const resize = () => {
    const width = mount.clientWidth || 640;
    const height = mount.clientHeight || 380;
    renderer.setSize(width, height, false);
    camera.aspect = width / height;
    camera.updateProjectionMatrix();
  };
  new ResizeObserver(resize).observe(mount);
  resize();

  const modelLoader = new GLTFLoader();
  modelLoader.load(
    modelUrl,
    (gltf) => {
      car.add(gltf.scene);
      normalizeModel(THREE, gltf.scene);
      paintTargets = collectPaintTargets(THREE, gltf.scene);
      if (paintTargets.length === 0) {
        showViewerMessage(mount, "The Model Y GLB has no UV-mapped paintable meshes.");
        return;
      }
      if (activeWrapUrl) {
        applyWrap(activeWrapUrl).catch((error) => console.error(error));
      }
    },
    undefined,
    (error) => {
      showViewerMessage(mount, "Could not load the Model Y GLB.");
      console.error(error);
    }
  );

  let dragStartX = 0;
  let dragStartRotation = 0;
  let dragging = false;
  car.rotation.y = -0.55;

  mount.addEventListener("pointerdown", (event) => {
    dragging = true;
    dragStartX = event.clientX;
    dragStartRotation = car.rotation.y;
    mount.setPointerCapture(event.pointerId);
  });
  mount.addEventListener("pointermove", (event) => {
    if (!dragging) return;
    car.rotation.y = dragStartRotation + (event.clientX - dragStartX) * 0.01;
  });
  mount.addEventListener("pointerup", () => {
    dragging = false;
  });
  mount.addEventListener("pointercancel", () => {
    dragging = false;
  });

  const loadTexture = (url) => new Promise((resolve, reject) => {
    const loader = new THREE.TextureLoader();
    loader.load(
      url,
      (texture) => {
        texture.colorSpace = THREE.SRGBColorSpace;
        texture.flipY = false;
        texture.wrapS = THREE.ClampToEdgeWrapping;
        texture.wrapT = THREE.ClampToEdgeWrapping;
        resolve(texture);
      },
      undefined,
      reject
    );
  });

  const applyWrap = async (url) => {
    activeWrapUrl = url;
    if (paintTargets.length === 0) return;
    const texture = await loadTexture(url);
    paintTargets.forEach((mesh) => {
      mesh.material = cloneMaterialWithTexture(mesh.material, texture);
    });
  };

  if (select) {
    select.addEventListener("change", () => {
      mount.dataset.wrapUrl = select.value;
      applyWrap(select.value).catch((error) => console.error(error));
    });
  }

  renderer.setAnimationLoop(() => {
    if (!dragging) {
      car.rotation.y += 0.0025;
    }
    renderer.render(scene, camera);
  });
}

function normalizeModel(THREE, object) {
  const box = new THREE.Box3().setFromObject(object);
  const size = new THREE.Vector3();
  box.getSize(size);
  const scale = Math.max(size.x, size.z) > 0 ? 4.9 / Math.max(size.x, size.z) : 1;
  object.scale.setScalar(scale);

  const scaledBox = new THREE.Box3().setFromObject(object);
  const center = new THREE.Vector3();
  scaledBox.getCenter(center);
  object.position.x -= center.x;
  object.position.z -= center.z;
  object.position.y -= scaledBox.min.y + 0.58;
}

function collectPaintTargets(THREE, model) {
  const namedTargets = [];
  const broadExteriorTargets = [];
  const uvTargets = [];
  const modelBox = new THREE.Box3().setFromObject(model);
  const modelSize = new THREE.Vector3();
  modelBox.getSize(modelSize);

  model.traverse((node) => {
    if (!node.isMesh || !node.geometry || !node.geometry.attributes.uv) {
      return;
    }

    const label = meshLabel(node);
    if (isExcludedPart(label)) {
      return;
    }

    uvTargets.push(node);
    if (isPaintPart(label)) {
      namedTargets.push(node);
    }
    if (isBroadExteriorMesh(THREE, node, modelSize)) {
      broadExteriorTargets.push(node);
    }
  });

  if (namedTargets.length > 0) {
    return uniqueMeshes([...namedTargets, ...broadExteriorTargets]);
  }
  if (broadExteriorTargets.length > 0) {
    return broadExteriorTargets;
  }
  return uvTargets;
}

function uniqueMeshes(meshes) {
  return [...new Set(meshes)];
}

function isBroadExteriorMesh(THREE, mesh, modelSize) {
  const meshBox = new THREE.Box3().setFromObject(mesh);
  const meshSize = new THREE.Vector3();
  meshBox.getSize(meshSize);
  const modelDimensions = sortedDimensions(modelSize);
  const meshDimensions = sortedDimensions(meshSize);

  return (
    meshDimensions[0] >= modelDimensions[0] * 0.48 &&
    meshDimensions[1] >= modelDimensions[1] * 0.36 &&
    meshDimensions[2] >= modelDimensions[2] * 0.18
  );
}

function sortedDimensions(vector) {
  return [vector.x, vector.y, vector.z].sort((a, b) => b - a);
}

function meshLabel(mesh) {
  const materialNames = Array.isArray(mesh.material)
    ? mesh.material.map((material) => material.name).join(" ")
    : mesh.material && mesh.material.name
      ? mesh.material.name
      : "";
  return `${mesh.name || ""} ${materialNames}`.toLowerCase();
}

function isPaintPart(label) {
  return /(body|paint|panel|door|hood|bonnet|bumper|fender|quarter|trunk|tailgate|roof|side|shell|base)/.test(label);
}

function isExcludedPart(label) {
  return /(glass|window|windshield|windscreen|wheel|tire|tyre|rim|light|lamp|mirror|interior|seat|brake|license|plate|camera|sensor|rubber|chrome|reflect)/.test(label);
}

function cloneMaterialWithTexture(material, texture) {
  if (Array.isArray(material)) {
    return material.map((entry) => cloneMaterialWithTexture(entry, texture));
  }

  const next = material.clone();
  const map = texture.clone();
  map.needsUpdate = true;
  next.map = map;
  if (next.color) {
    next.color.set(0xffffff);
  }
  next.needsUpdate = true;
  return next;
}
"""


STYLES = """
:root {
  color-scheme: light;
  font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  background: #f4f6f4;
  color: #151a18;
}
* { box-sizing: border-box; }
body { margin: 0; min-height: 100vh; background: #f4f6f4; }
.shell { width: min(1180px, calc(100% - 32px)); margin: 32px auto; }
.panel {
  background: #ffffff;
  border: 1px solid #d9dfda;
  border-radius: 8px;
  box-shadow: 0 18px 50px rgba(24, 34, 30, 0.08);
  padding: 28px;
}
.title-row {
  display: flex;
  align-items: start;
  justify-content: space-between;
  gap: 18px;
  margin-bottom: 24px;
}
.eyebrow {
  margin: 0 0 6px;
  font-size: 0.78rem;
  text-transform: uppercase;
  letter-spacing: 0;
  color: #607067;
}
h1 { margin: 0; font-size: clamp(1.7rem, 3vw, 2.4rem); line-height: 1.1; }
.badge {
  border: 1px solid #bdd0c4;
  background: #eff6f1;
  color: #31513d;
  border-radius: 999px;
  padding: 8px 12px;
  font-size: 0.86rem;
  white-space: nowrap;
}
form { display: grid; gap: 18px; }
.grid {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 14px;
}
.field {
  display: grid;
  gap: 7px;
  font-size: 0.88rem;
  font-weight: 650;
  color: #2b3430;
}
.upload-field {
  border: 1px dashed #b9c7bd;
  border-radius: 8px;
  padding: 14px;
  background: #f9fbf9;
}
input, select {
  width: 100%;
  min-height: 42px;
  border: 1px solid #bfc9c2;
  border-radius: 6px;
  padding: 9px 10px;
  font: inherit;
  font-weight: 500;
  background: #fff;
  color: #151a18;
}
input[type="file"] { padding: 8px; background: transparent; }
.check { display: inline-flex; align-items: center; gap: 10px; font-weight: 650; }
.check input { width: 18px; min-height: 18px; }
.actions { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; }
button, .button-link {
  border: 0;
  border-radius: 6px;
  background: #226646;
  color: #fff;
  padding: 11px 16px;
  font: inherit;
  font-weight: 750;
  text-decoration: none;
  cursor: pointer;
}
.secondary-actions a { color: #226646; font-weight: 700; text-decoration: none; }
.notice {
  margin-bottom: 18px;
  border-radius: 6px;
  padding: 12px 14px;
  font-weight: 650;
}
.error { background: #fff0ed; border: 1px solid #ffc4b8; color: #8a2c1d; }
.gallery {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(220px, 1fr));
  gap: 16px;
  margin-top: 22px;
}
.card {
  border: 1px solid #d9dfda;
  border-radius: 8px;
  overflow: hidden;
  background: #fff;
}
.card img {
  display: block;
  width: 100%;
  aspect-ratio: 1;
  object-fit: contain;
  background: #0b0d0c;
}
.card-footer {
  display: flex;
  justify-content: space-between;
  gap: 10px;
  align-items: center;
  padding: 10px 12px;
  font-size: 0.84rem;
}
.card-footer span {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.card-footer a { color: #226646; font-weight: 750; text-decoration: none; }
.viewer-section {
  margin-top: 22px;
  border: 1px solid #d9dfda;
  border-radius: 8px;
  overflow: hidden;
  background: #101513;
  color: #f7faf8;
}
.viewer-header {
  display: flex;
  justify-content: space-between;
  align-items: end;
  gap: 16px;
  padding: 16px 18px;
  border-bottom: 1px solid rgba(255, 255, 255, 0.12);
}
.viewer-header h2 {
  margin: 0;
  font-size: 1.2rem;
  line-height: 1.2;
}
.viewer-select {
  display: grid;
  gap: 7px;
  min-width: min(320px, 100%);
  color: #dce7e1;
  font-size: 0.84rem;
  font-weight: 700;
}
.viewer-select select {
  border-color: #46534e;
  background: #161d1a;
  color: #f7faf8;
}
.viewer-note {
  margin: 0;
  padding: 0 18px 14px;
  color: #dce7e1;
  font-size: 0.9rem;
  line-height: 1.45;
}
.car-viewer {
  width: 100%;
  height: clamp(320px, 45vw, 520px);
  overflow: hidden;
  touch-action: none;
}
.car-viewer canvas {
  display: block;
  width: 100%;
  height: 100%;
}
.car-viewer.is-unavailable {
  display: grid;
  place-items: center;
  min-height: 320px;
  padding: 24px;
  color: #f7faf8;
  font-weight: 700;
}
@media (max-width: 820px) {
  .shell { width: min(100% - 20px, 1180px); margin: 10px auto; }
  .panel { padding: 18px; }
  .title-row { flex-direction: column; }
  .grid { grid-template-columns: 1fr; }
  .badge { white-space: normal; }
  .viewer-header { align-items: stretch; flex-direction: column; }
  .car-viewer { height: 330px; }
}
"""
