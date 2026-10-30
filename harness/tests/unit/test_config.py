from agentforge.config import Settings


def test_defaults_and_secret_redaction(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-private-value")
    settings = Settings(_env_file=None)
    assert settings.agentforge_db == "agentforge"
    assert settings.deepseek_api_key.get_secret_value() == "test-private-value"
    assert "test-private-value" not in repr(settings)


def test_sandbox_image_env_overrides_default(monkeypatch):
    # A machine that built the document image points SANDBOX_IMAGE at it once;
    # every SandboxPolicy that does not name an image then starts there.
    monkeypatch.setenv("SANDBOX_IMAGE", "agentforge/python-docs:3.12")
    settings = Settings(_env_file=None)
    assert settings.sandbox_image == "agentforge/python-docs:3.12"
    from agentforge.models import SandboxPolicy
    assert SandboxPolicy().image == "agentforge/python-docs:3.12"


def test_python_sandbox_images_admit_document_image():
    # The integrated file tools run on any image with a CPython interpreter,
    # including the document image; node:22-slim is deliberately excluded.
    from agentforge.models import PYTHON_SANDBOX_IMAGES, SandboxPolicy
    assert "python:3.12-slim" in PYTHON_SANDBOX_IMAGES
    assert "agentforge/python-docs:3.12" in PYTHON_SANDBOX_IMAGES
    assert "node:22-slim" not in PYTHON_SANDBOX_IMAGES
    assert SandboxPolicy(image="agentforge/python-docs:3.12")
