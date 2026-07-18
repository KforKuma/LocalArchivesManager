from __future__ import annotations

from lam.config import Settings
from lam.models import OcrAvailability, WorkflowStatus
from lam.workflows.doctor import DoctorWorkflow
from lam.services.ocr_service import OcrService


class FakeOcrDoctor:
    def __init__(self, availability):
        self.availability = availability
        self.deep = None
        self.initialize_models = None

    def check_availability(self, *, deep=False, initialize_models=False):
        self.deep = deep
        self.initialize_models = initialize_models
        return self.availability


def test_doctor_reports_available_runtime(library_factory):
    root = library_factory([])
    settings = Settings.from_root(root)
    settings.ensure_runtime_directories()
    service = FakeOcrDoctor(
        OcrAvailability(
            available=True,
            pdf2image_available=True,
            poppler_available=True,
            easyocr_available=True,
            model_available=True,
            torch_available=True,
            cuda_available=False,
            temporary_directory_writable=True,
            status="available",
        )
    )
    result = DoctorWorkflow(settings, service).run()
    assert result.status == WorkflowStatus.SUCCESS
    assert service.deep is True
    assert service.initialize_models is False
    assert result.details["ocr"]["model_available"] is True
    assert result.details["uses_network"] is False
    assert result.details["may_download_models"] is False


def test_doctor_reports_missing_model_as_review(library_factory):
    root = library_factory([])
    settings = Settings.from_root(root)
    settings.ensure_runtime_directories()
    service = FakeOcrDoctor(
        OcrAvailability(
            available=False,
            pdf2image_available=True,
            poppler_available=True,
            easyocr_available=True,
            model_available=False,
            temporary_directory_writable=True,
            status="ocr_unavailable_model_missing",
        )
    )
    result = DoctorWorkflow(settings, service).run()
    assert result.status == WorkflowStatus.NEEDS_REVIEW
    assert result.needs_review[0]["issue"] == "ocr_unavailable_model_missing"


def test_doctor_explicit_model_initialization_reports_side_effects(library_factory):
    root = library_factory([])
    service = FakeOcrDoctor(
        OcrAvailability(
            available=True,
            pdf2image_available=True,
            poppler_available=True,
            easyocr_available=True,
            model_available=True,
            temporary_directory_writable=True,
            status="available",
        )
    )
    result = DoctorWorkflow(Settings.from_root(root), service).run(
        initialize_ocr_models=True
    )
    assert service.initialize_models is True
    assert result.details["uses_network"] is True
    assert result.details["may_download_models"] is True


def test_real_ocr_service_default_doctor_does_not_construct_reader(
    library_factory, monkeypatch
):
    root = library_factory([])
    service = OcrService(Settings.from_root(root))
    monkeypatch.setattr(
        "lam.services.ocr_service.importlib.util.find_spec",
        lambda name: object() if name in {"pdf2image", "easyocr"} else None,
    )
    monkeypatch.setattr(service, "_poppler_executable", lambda: root / "pdftoppm.exe")
    monkeypatch.setattr(service, "_probe_easyocr_import", lambda: True)
    monkeypatch.setattr(service, "_temporary_directory_writable", lambda: True)

    def forbidden_reader(*args, **kwargs):
        raise AssertionError("default doctor must not initialize EasyOCR Reader")

    monkeypatch.setattr(service, "_get_reader", forbidden_reader)
    availability = service.check_availability(deep=True, initialize_models=False)
    assert availability.available is True
    assert availability.model_available is None
    assert availability.details["uses_network"] is False
    assert availability.details["may_download_models"] is False


def test_real_ocr_service_explicit_initialization_enables_download_flag(
    library_factory, monkeypatch
):
    root = library_factory([])
    service = OcrService(Settings.from_root(root))
    monkeypatch.setattr(
        "lam.services.ocr_service.importlib.util.find_spec",
        lambda name: object() if name in {"pdf2image", "easyocr"} else None,
    )
    monkeypatch.setattr(service, "_poppler_executable", lambda: root / "pdftoppm.exe")
    monkeypatch.setattr(service, "_probe_easyocr_import", lambda: True)
    monkeypatch.setattr(service, "_temporary_directory_writable", lambda: True)
    observed = {}

    def reader(config):
        observed["download_enabled"] = config.download_enabled
        return object(), "cpu"

    monkeypatch.setattr(service, "_get_reader", reader)
    availability = service.check_availability(deep=True, initialize_models=True)
    assert availability.available is True
    assert observed["download_enabled"] is True
    assert availability.details["uses_network"] is True
    assert availability.details["may_download_models"] is True
