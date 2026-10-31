"""Optional local SigLIP2 image/text embeddings (no Ollama or cloud inference)."""
from __future__ import annotations

import threading
import hashlib
import json
from typing import Sequence

import numpy as np
from PIL import Image, ImageOps

from .config import MODEL_ID, MODEL_REVISION, model_path
from .file_safety import SafeDirectory


def _normalise(vector: np.ndarray) -> np.ndarray:
    vector = np.asarray(vector, dtype=np.float32).reshape(-1)
    norm = float(np.linalg.norm(vector))
    if norm == 0 or not np.isfinite(norm):
        raise ValueError("Invalid zero-length or non-finite model embedding")
    return np.ascontiguousarray(vector / norm, dtype=np.float32)


class Siglip2Encoder:
    """Lazy load to permit photo browsing without torch/transformers installed.

    Production follow-up: export and verify ONNX+tokenizer parity, then replace
    this research bridge with a Windows ML backend for standalone distribution.
    """

    model_id = MODEL_ID + '@' + MODEL_REVISION

    def __init__(self):
        self._lock = threading.Lock()
        self._processor = None
        self._model = None
        self._device = "cpu"
        self.last_error = ''
        self._verified_signature = None

    def _artifacts_verified(self):
        directory = model_path()
        try:
            lock_path = directory / 'model-lock.json'
            manifest = json.loads(lock_path.read_text(encoding='utf-8'))
            if manifest['repo'] != MODEL_ID or manifest['revision'] != MODEL_REVISION:
                return False
            files = manifest['files']
            if 'config.json' not in files or 'model.safetensors' not in files:
                return False
            if any(not name or '/' in name or '\\' in name or ':' in name or name in {'.', '..'} for name in files):
                return False
            signature = (lock_path.stat().st_mtime_ns, tuple(
                (name, (directory / name).stat().st_size, (directory / name).stat().st_mtime_ns) for name in sorted(files)))
            if signature != self._verified_signature:
                for name, expected in files.items():
                    hashed = hashlib.sha256()
                    with (directory / name).open('rb') as stream:
                        for block in iter(lambda: stream.read(1024 * 1024), b''):
                            hashed.update(block)
                    if hashed.hexdigest() != expected:
                        return False
                self._verified_signature = signature
            return True
        except (OSError, ValueError, KeyError, TypeError):
            return False

    @property
    def installed(self) -> bool:
        return not self.last_error and self._artifacts_verified()

    def install(self) -> None:
        try:
            import torch  # noqa: F401 - prerequisite check before large download
            from transformers import AutoProcessor, Siglip2Model  # noqa: F401
            from huggingface_hub import snapshot_download
        except ImportError as exc:
            raise RuntimeError("AI dependencies missing. Run: pip install -r engine/requirements-ai.txt") from exc
        target = model_path()
        target.mkdir(parents=True, exist_ok=True)
        # Explicit action only; never download a model during ordinary scans.
        snapshot_download(
            repo_id=MODEL_ID,
            revision=MODEL_REVISION,
            local_dir=str(target),
            allow_patterns=["*.json", "*.model", "*.txt", "*.safetensors"],
        )
        manifest = {'repo': MODEL_ID, 'revision': MODEL_REVISION, 'files': {}}
        for path in target.iterdir():
            if path.suffix in {'.json', '.model', '.txt', '.safetensors'} and path.name != 'model-lock.json':
                hashed = hashlib.sha256()
                with path.open('rb') as stream:
                    for block in iter(lambda: stream.read(1024 * 1024), b''):
                        hashed.update(block)
                manifest['files'][path.name] = hashed.hexdigest()
        with SafeDirectory(target) as directory:
            directory.write('model-lock.json', json.dumps(manifest, sort_keys=True).encode())
        self._model = None
        self._processor = None
        self.last_error = ''
        self._verified_signature = None
        # Validate full model+processor load after download.
        self._ensure_loaded()

    def _ensure_loaded(self) -> None:
        if self._model is not None:
            return
        if not self._artifacts_verified():
            raise RuntimeError("AI model missing or unverified. Choose Install AI to download/repair the pinned revision.")
        try:
            import torch
            from transformers import AutoProcessor, Siglip2Model
        except ImportError as exc:
            self.last_error = str(exc)
            raise RuntimeError("AI dependencies missing. Run: pip install -r engine/requirements-ai.txt") from exc
        with self._lock:
            if self._model is not None:
                return
            path = str(model_path())
            try:
                processor = AutoProcessor.from_pretrained(path, local_files_only=True)
                model = Siglip2Model.from_pretrained(path, local_files_only=True)
                self._device = "cuda" if torch.cuda.is_available() else "cpu"
                model = model.to(self._device).eval()
                self._processor = processor
                self._model = model
                self.last_error = ''
            except Exception as exc:
                self.last_error = str(exc)
                raise

    def embed_text(self, text: str) -> np.ndarray:
        self._ensure_loaded()
        import torch

        with self._lock, torch.inference_mode():
            # SigLIP2 text encoder must be padded to max length as trained.
            values = self._processor(text=[text], padding="max_length", return_tensors="pt")
            values = {k: v.to(self._device) for k, v in values.items()}
            output = self._model.get_text_features(**values)
            features = output.pooler_output if hasattr(output, "pooler_output") else output
            return _normalise(features[0].detach().cpu().numpy())

    def embed_images(self, paths: Sequence[str]) -> list[np.ndarray]:
        if not paths:
            return []
        self._ensure_loaded()
        import torch

        images = []
        try:
            for path in paths:
                with Image.open(path) as raw:
                    images.append(ImageOps.exif_transpose(raw).convert("RGB"))
            with self._lock, torch.inference_mode():
                values = self._processor(images=images, return_tensors="pt")
                values = {k: v.to(self._device) for k, v in values.items()}
                output = self._model.get_image_features(**values)
                features = output.pooler_output if hasattr(output, "pooler_output") else output
                return [_normalise(v) for v in features.detach().cpu().numpy()]
        finally:
            for image in images:
                image.close()
