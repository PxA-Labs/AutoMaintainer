import pytest
import json
from unittest.mock import patch, MagicMock
from fastapi.testclient import TestClient
from main import app, get_current_user

client = TestClient(app)


def test_inline_assist_unauthorized():
    with patch("agents.supabase", MagicMock()):
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
        assert response.status_code == 401
        assert "Authorization header" in response.json()["detail"]


def test_inline_assist_endpoint_streaming():
    async def mock_stream_generator(
        prompt, selected_code, prefix_code, suffix_code, file_path
    ):
        d1 = json.dumps({"content": "def hello():\n"})
        d2 = json.dumps({"content": "    return 'world'\n"})
        yield f"data: {d1}\n\n"
        yield f"data: {d2}\n\n"
        yield "data: [DONE]\n\n"

    app.dependency_overrides[get_current_user] = lambda: {
        "user_id": "test_user",
        "org_id": "test_org",
        "email": "test@localhost",
    }

    try:
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
    finally:
        app.dependency_overrides.pop(get_current_user, None)


@pytest.mark.asyncio
async def test_stream_inline_assist_truncates_long_inputs():
    from agents import stream_inline_assist

    mock_llm = MagicMock()

    class MockChunk:
        def __init__(self, content):
            self.content = content

    async def mock_astream(messages):
        human_msg = messages[1].content
        # prompt should be truncated to 3000 'P's
        assert "P" * 3000 in human_msg
        assert "P" * 3001 not in human_msg
        # selected_code should be truncated to 3000 'S's
        assert "S" * 3000 in human_msg
        assert "S" * 3001 not in human_msg
        # prefix_code should take the LAST 3000 chars (i.e. 'A's, excluding 'PREFIX_START_')
        assert "A" * 3000 in human_msg
        assert "PREFIX_START_" not in human_msg
        # suffix_code should take the FIRST 3000 chars (i.e. 'B's, excluding '_SUFFIX_END')
        assert "B" * 3000 in human_msg
        assert "_SUFFIX_END" not in human_msg

        yield MockChunk("result")

    mock_llm.astream = mock_astream

    class DummyKeyState:
        key = "test_key"

    class DummyKeyContext:
        async def __aenter__(self):
            return DummyKeyState()

        async def __aexit__(self, exc_type, exc, tb):
            pass

    mock_manager = MagicMock()
    mock_manager.key_context.return_value = DummyKeyContext()

    with patch("agents.get_rate_limit_manager", return_value=mock_manager), patch(
        "agents.ChatGroq", return_value=mock_llm
    ):
        long_prompt = "P" * 3500
        long_selected = "S" * 3500
        long_prefix = "PREFIX_START_" + ("A" * 3500)
        long_suffix = ("B" * 3500) + "_SUFFIX_END"

        results = []
        async for chunk in stream_inline_assist(
            prompt=long_prompt,
            selected_code=long_selected,
            prefix_code=long_prefix,
            suffix_code=long_suffix,
            file_path="test.py",
        ):
            results.append(chunk)

        assert len(results) == 2
        assert '{"content": "result"}' in results[0]
        assert "data: [DONE]" in results[1]


@pytest.mark.asyncio
async def test_stream_inline_assist_handles_exception():
    from agents import stream_inline_assist

    mock_manager = MagicMock()
    mock_manager.key_context.side_effect = Exception("Simulated Groq Failure")

    with patch("agents.get_rate_limit_manager", return_value=mock_manager):
        results = []
        async for chunk in stream_inline_assist(
            prompt="Refactor",
            selected_code="code",
            prefix_code="",
            suffix_code="",
            file_path="test.py",
        ):
            results.append(chunk)

        assert len(results) == 1
        assert "data: " in results[0]
        data = json.loads(results[0].replace("data: ", "").strip())
        assert data == {"error": "Simulated Groq Failure"}
