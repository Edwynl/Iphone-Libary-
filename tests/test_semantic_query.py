"""A long search query is cut to what the text model accepts rather than failing."""
from __future__ import annotations

import sys
from contextlib import nullcontext
from types import SimpleNamespace

import numpy as np
from ai_photos.semantic import Siglip2Encoder


def test_text_is_padded_and_truncated_to_the_model_length(monkeypatch):
    calls = {}

    def processor(**kwargs):
        calls.update(kwargs)
        return {}

    features = SimpleNamespace(detach=lambda: SimpleNamespace(cpu=lambda: SimpleNamespace(numpy=lambda: np.ones(4))))
    monkeypatch.setitem(sys.modules, 'torch', SimpleNamespace(inference_mode=nullcontext))
    encoder = Siglip2Encoder()
    monkeypatch.setattr(encoder, '_ensure_loaded', lambda: None)
    encoder._processor = processor
    encoder._model = SimpleNamespace(get_text_features=lambda **_: [features])
    encoder.embed_text('一张非常详细的照片描述 ' * 80)
    assert calls['truncation'] is True and calls['max_length'] == 64 and calls['padding'] == 'max_length'
