"""Face detection/recognition runs on the GPU when onnxruntime-gpu offers CUDA
(the GPU server), else on the CPU exactly as before (plain onnxruntime)."""

import onnxruntime
import pytest

from app import config, face_pipeline


@pytest.fixture
def cuda_available(monkeypatch):
    monkeypatch.setattr(onnxruntime, "get_available_providers",
                        lambda: ["TensorrtExecutionProvider", "CUDAExecutionProvider", "CPUExecutionProvider"])


@pytest.fixture
def cpu_only(monkeypatch):
    monkeypatch.setattr(onnxruntime, "get_available_providers", lambda: ["CPUExecutionProvider"])


def test_gpu_is_used_when_onnxruntime_gpu_offers_cuda(cuda_available):
    assert face_pipeline.onnx_providers("auto") == (["CUDAExecutionProvider", "CPUExecutionProvider"], 0)


def test_cpu_only_install_keeps_the_previous_behaviour(cpu_only):
    assert face_pipeline.onnx_providers("auto") == (["CPUExecutionProvider"], -1)


def test_cpu_can_be_forced_on_a_gpu_box(cuda_available):
    assert face_pipeline.onnx_providers("cpu") == (["CPUExecutionProvider"], -1)


def test_default_setting_is_auto():
    assert config.FACE_ONNX_DEVICE == "auto"


@pytest.mark.parametrize("gpu", [True, False])
def test_the_face_model_is_loaded_with_the_chosen_device(monkeypatch, gpu):
    from app import models

    seen = {}

    class FakeFaceAnalysis:
        def __init__(self, **kwargs):
            seen["init"] = kwargs

        def prepare(self, **kwargs):
            seen["prepare"] = kwargs

    providers = ["CUDAExecutionProvider", "CPUExecutionProvider"] if gpu else ["CPUExecutionProvider"]
    monkeypatch.setattr(onnxruntime, "get_available_providers", lambda: providers)
    monkeypatch.setattr(models, "require", lambda key: None)
    monkeypatch.setattr(face_pipeline.insightface.app, "FaceAnalysis", FakeFaceAnalysis)
    monkeypatch.setattr(face_pipeline.CameraFacePipeline, "_arcface", None)
    face_pipeline.CameraFacePipeline._ensure_arcface_loaded()
    assert seen["init"]["providers"] == providers
    assert seen["init"]["allowed_modules"] == ["detection", "recognition"]       # unchanged
    assert seen["prepare"] == {"ctx_id": 0 if gpu else -1, "det_size": (640, 640)}
