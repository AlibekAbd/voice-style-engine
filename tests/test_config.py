import pytest

from tgservice import config


def test_load_env_strips_spaces_and_skips_comments(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "# комментарий\n"
        "TG_API_ID= 12345\n"
        "TG_API_HASH=abc\n"
        "\n"
        "#TG_API_ID=999\n"
        "GEMINI_API_KEY = key-1\n",
        encoding="utf-8",
    )

    values = config.load_env(env_file)

    assert values["TG_API_ID"] == "12345"
    assert values["TG_API_HASH"] == "abc"
    assert values["GEMINI_API_KEY"] == "key-1"
    assert set(values) == {"TG_API_ID", "TG_API_HASH", "GEMINI_API_KEY"}


def test_load_env_reads_file_with_bom(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("TG_API_ID=12345\n", encoding="utf-8-sig")

    values = config.load_env(env_file)

    assert values["TG_API_ID"] == "12345"


def test_load_env_strips_quotes_around_value(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        'TG_PHONE="+77010000000"\n' "TG_API_HASH='abc'\n",
        encoding="utf-8",
    )

    values = config.load_env(env_file)

    assert values["TG_PHONE"] == "+77010000000"
    assert values["TG_API_HASH"] == "abc"


def test_load_env_splits_on_first_equals(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("GEMINI_API_KEY=key=with=equals\n", encoding="utf-8")

    values = config.load_env(env_file)

    assert values["GEMINI_API_KEY"] == "key=with=equals"


def test_require_reports_missing_keys(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("TG_API_ID=1\n", encoding="utf-8")
    values = config.load_env(env_file)

    try:
        config.require(values, ["TG_API_ID", "TG_API_HASH"])
    except KeyError as error:
        assert "TG_API_HASH" in str(error)
    else:
        raise AssertionError("ожидалась ошибка о недостающем ключе")


def test_require_returns_values_in_key_order():
    assert config.require({"A": "1", "B": "2"}, ["B", "A"]) == ["2", "1"]


def test_only_the_strong_model_is_configured():
    """Слабая модель ушла из прохода А целиком: обе постановки задачи идут сильной."""
    assert config.MODEL_PRO
    assert not hasattr(config, "MODEL_FLASH")


def test_roster_lies_next_to_the_dump_index():
    """Постоянный рабочий список — такой же файл прогона, как индекс выгрузки."""
    assert config.ROSTER.parent == config.OUT
    assert config.ROSTER.name.endswith(".json")


def test_env_path_stops_at_repository_root(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text("TG_API_ID=чужой\n", encoding="utf-8")
    repository = tmp_path / "repo"
    repository.mkdir()
    (repository / ".git").write_text("", encoding="utf-8")
    monkeypatch.setattr(config, "CODE_DIR", repository)

    with pytest.raises(FileNotFoundError) as error:
        config.env_path()

    assert str(repository) in str(error.value)


def test_env_path_finds_env_next_to_package(tmp_path, monkeypatch):
    (tmp_path / ".git").write_text("", encoding="utf-8")
    env_file = tmp_path / ".env"
    env_file.write_text("TG_API_ID=1\n", encoding="utf-8")
    monkeypatch.setattr(config, "CODE_DIR", tmp_path)

    assert config.env_path() == env_file


def test_env_path_climbs_to_repository_root(tmp_path, monkeypatch):
    (tmp_path / ".git").write_text("", encoding="utf-8")
    env_file = tmp_path / ".env"
    env_file.write_text("TG_API_ID=1\n", encoding="utf-8")
    code_dir = tmp_path / "Код"
    code_dir.mkdir()
    monkeypatch.setattr(config, "CODE_DIR", code_dir)

    assert config.env_path() == env_file
