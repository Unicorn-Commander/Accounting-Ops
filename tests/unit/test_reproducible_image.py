from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
UV_VERSION = "0.9.30"


def test_dockerfile_uses_the_frozen_runtime_lock() -> None:
    dockerfile = (REPO_ROOT / "Dockerfile").read_text()

    assert f"ghcr.io/astral-sh/uv:{UV_VERSION}" in dockerfile
    assert "COPY pyproject.toml uv.lock ./" in dockerfile
    assert "uv sync --frozen --no-dev --extra tax-agents --no-install-project" in dockerfile
    assert "uv sync --frozen --no-dev --extra tax-agents --no-editable" in dockerfile
    assert dockerfile.index("COPY pyproject.toml uv.lock ./") < dockerfile.index("COPY . .")
    assert "PYTHONDONTWRITEBYTECODE=1" in dockerfile
    assert 'PATH="/app/.venv/bin:$PATH"' in dockerfile
    assert 'CMD ["uvicorn", "app.main:app"' in dockerfile


def test_ci_pins_uv_and_checks_the_lock_before_tests() -> None:
    workflow = (REPO_ROOT / ".forgejo/workflows/ci.yml").read_text()

    assert f'python -m pip install "uv=={UV_VERSION}"' in workflow
    assert workflow.index("run: uv lock --check") < workflow.index(
        "- name: Test suite (no stack)"
    )
