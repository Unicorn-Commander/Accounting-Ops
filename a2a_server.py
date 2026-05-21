import uvicorn

from app.main import app


def assert_a2a_sdk_available() -> None:
    try:
        import a2a  # noqa: F401
    except ImportError as exc:
        raise RuntimeError("Install the a2aproject/A2A Python SDK package to run the A2A endpoint") from exc


if __name__ == "__main__":
    assert_a2a_sdk_available()
    uvicorn.run(app, host="0.0.0.0", port=8001)
