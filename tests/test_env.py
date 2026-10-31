import os

from src.env import load_environment


def test_shared_key_wins_over_project_placeholder_without_copying_secret(monkeypatch, tmp_path):
    shared = tmp_path / "keys.env"
    project = tmp_path / ".env"
    shared.write_text("MINIMAX_API_KEY=shared-test-key\n")
    project.write_text("MINIMAX_API_KEY=xxx\n")
    monkeypatch.setenv("HORIZON_GLOBAL_ENV_FILE", str(shared))
    monkeypatch.delenv("MINIMAX_API_KEY", raising=False)
    load_environment(project)
    assert os.environ["MINIMAX_API_KEY"] == "shared-test-key"
    assert project.read_text() == "MINIMAX_API_KEY=xxx\n"


def test_github_secret_wins_over_shared_key(monkeypatch, tmp_path):
    shared = tmp_path / "keys.env"
    shared.write_text("MINIMAX_API_KEY=shared-test-key\n")
    monkeypatch.setenv("HORIZON_GLOBAL_ENV_FILE", str(shared))
    monkeypatch.setenv("MINIMAX_API_KEY", "injected-github-secret")
    load_environment(tmp_path / "absent.env")
    assert os.environ["MINIMAX_API_KEY"] == "injected-github-secret"


def test_shared_key_replaces_already_loaded_placeholder(monkeypatch, tmp_path):
    shared = tmp_path / "keys.env"
    shared.write_text("MINIMAX_API_KEY=shared-test-key\n")
    monkeypatch.setenv("HORIZON_GLOBAL_ENV_FILE", str(shared))
    monkeypatch.setenv("MINIMAX_API_KEY", "xxx")
    load_environment(tmp_path / "absent.env")
    assert os.environ["MINIMAX_API_KEY"] == "shared-test-key"


def test_missing_shared_file_keeps_project_configuration(monkeypatch, tmp_path):
    project = tmp_path / ".env"
    project.write_text("MINIMAX_API_KEY=project-test-key\n")
    monkeypatch.setenv("HORIZON_GLOBAL_ENV_FILE", str(tmp_path / "absent.env"))
    monkeypatch.delenv("MINIMAX_API_KEY", raising=False)
    load_environment(project)
    assert os.environ["MINIMAX_API_KEY"] == "project-test-key"
