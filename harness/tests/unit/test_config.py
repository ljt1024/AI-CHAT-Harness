from agentforge.config import Settings


def test_defaults_and_secret_redaction(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-private-value")
    settings = Settings(_env_file=None)
    assert settings.agentforge_db == "agentforge"
    assert settings.deepseek_api_key.get_secret_value() == "test-private-value"
    assert "test-private-value" not in repr(settings)
