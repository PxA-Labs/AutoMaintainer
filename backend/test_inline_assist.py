import pytest
import json
from unittest.mock import patch
from fastapi.testclient import TestClient
from main import app

client = TestClient(app)


def test_inline_assist_endpoint_streaming():
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
