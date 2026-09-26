from app.settings import Settings


def test_defaults():
    # The ignore: _env_file is a pydantic-settings init kwarg that the model signature pyright sees omits.
    config = Settings(_env_file=None)  # pyright: ignore[reportCallIssue]

    assert config.HOST == "127.0.0.1"
    assert config.LOG_LEVEL == "INFO"
    assert config.CORS_ORIGINS == ""
    assert config.FORWARDED_ALLOW_IPS == "127.0.0.1"


def test_new_settings_load_from_env_file(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("LOG_LEVEL=DEBUG\nCORS_ORIGINS=https://a.example\nFORWARDED_ALLOW_IPS=*\n")

    # The ignore: _env_file is a pydantic-settings init kwarg that the model signature pyright sees omits.
    config = Settings(_env_file=env_file)  # pyright: ignore[reportCallIssue]

    assert config.LOG_LEVEL == "DEBUG"
    assert config.CORS_ORIGINS == "https://a.example"
    assert config.FORWARDED_ALLOW_IPS == "*"
