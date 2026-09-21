import pytest
import json
from unittest.mock import patch
from fastapi.testclient import TestClient
from main import app, get_current_user

client = TestClient(app)


@pytest.fixture
def authenticated_user():
    """/assist/inline requires authentication; stub the dependency."""

    async def allow_test_user():
        return {"org_id": "test-org", "user_id": "test-user"}

    app.dependency_overrides[get_current_user] = allow_test_user
    yield
    app.dependency_overrides.pop(get_current_user, None)


def test_inline_assist_endpoint_streaming(authenticated_user):
    async def mock_stream_generator(
        prompt, selected_code, prefix_code, suffix_code, file_path
    ):
        d1 = json.dumps({"content": "def hello():\n"})
        d2 = json.dumps({"content": "    return 'world'\n"})
        yield f"data: {d1}\n\n"
        yield f"data: {d2}\n\n"
        yield "data: [DONE]\n\n"

    with patch(
        "agents.stream_inline_assist", side_effect=mock_stream_generator
    ) as mock_assist:
        payload = {
            "repo_name": "owner/repo",
            "file_path": "src/hello.py",
            "prompt": "Refactor function",
            "selected_code": "def hello(): pass",
            "prefix_code": "# Header\n",
            "suffix_code": "# Footer\n",
            "selection": {
                "startLine": 2,
                "startColumn": 1,
                "endLine": 2,
                "endColumn": 18,
            },
        }

        response = client.post("/assist/inline", json=payload)
        assert response.status_code == 200
        assert "text/event-stream" in response.headers.get("content-type", "")
        text = response.text
        assert '{"content": "def hello():\\n"}' in text
        assert '{"content": "    return \'world\'\\n"}' in text
        assert "data: [DONE]" in text
        mock_assist.assert_called_once_with(
            prompt="Refactor function",
            selected_code="def hello(): pass",
            prefix_code="# Header\n",
            suffix_code="# Footer\n",
            file_path="src/hello.py",
        )


def test_inline_assist_requires_authentication():
    """Anonymous callers must not be able to consume Groq inference quota."""
    payload = {
        "repo_name": "owner/repo",
        "file_path": "src/hello.py",
        "prompt": "Refactor function",
        "selected_code": "def hello(): pass",
        "prefix_code": "",
        "suffix_code": "",
        "selection": {
            "startLine": 1,
            "startColumn": 1,
            "endLine": 1,
            "endColumn": 1,
        },
    }

    with patch("agents.stream_inline_assist") as mock_assist:
        response = client.post("/assist/inline", json=payload)

    assert response.status_code != 200
    mock_assist.assert_not_called()
