"""Optional local SigLIP2 image/text embeddings (no Ollama or cloud inference)."""
from __future__ import annotations

import threading
from pathlib import Path
from typing import Sequence

import numpy as np
from PIL import Image, ImageOps

from .config import MODEL_ID, model_path


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

    model_id = MODEL_ID

    def __init__(self):
        self._lock = threading.Lock()
        self._processor = None
        self._model = None
        self._device = "cpu"

    @property
    def installed(self) -> bool:
        directory = model_path()
        return (directory / "config.json").is_file() and any(
            (directory / name).is_file() for name in ("model.safetensors", "pytorch_model.bin")
        )

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
            local_dir=str(target),
            allow_patterns=["*.json", "*.model", "*.txt", "*.safetensors"],
        )
        # Validate full model+processor load after download.
        self._ensure_loaded()

    def _ensure_loaded(self) -> None:
        if self._model is not None:
            return
        if not self.installed:
            raise RuntimeError("AI model not installed. Choose Install AI in the app first.")
        try:
            import torch
            from transformers import AutoProcessor, Siglip2Model
        except ImportError as exc:
            raise RuntimeError("AI dependencies missing. Run: pip install -r engine/requirements-ai.txt") from exc
        with self._lock:
            if self._model is not None:
                return
            path = str(model_path())
            processor = AutoProcessor.from_pretrained(path, local_files_only=True)
            model = Siglip2Model.from_pretrained(path, local_files_only=True)
            self._device = "cuda" if torch.cuda.is_available() else "cpu"
            model = model.to(self._device).eval()
            self._processor = processor
            self._model = model

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
