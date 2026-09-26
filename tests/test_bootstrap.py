from config.bootstrap import configure_settings


def test_entrypoint_reads_settings_module_from_env_file_before_default(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("DJANGO_SETTINGS_MODULE=config.settings.dev\n", encoding="utf-8")
    monkeypatch.delenv("DJANGO_SETTINGS_MODULE", raising=False)

    selected = configure_settings(default="config.settings.prod", env_file=env_file)

    assert selected == "config.settings.dev"


def test_entrypoint_defaults_to_production_when_no_setting_is_configured(tmp_path, monkeypatch):
    monkeypatch.delenv("DJANGO_SETTINGS_MODULE", raising=False)

    selected = configure_settings(
        default="config.settings.prod",
        env_file=tmp_path / "missing.env",
    )

    assert selected == "config.settings.prod"
