from ast_indexer import TreeSitterParser


def test_tree_sitter_python_parsing(tmp_path):
    test_file = tmp_path / "sample.py"
    test_file.write_text(
        """
import os

class SampleService:
    def execute(self):
        return True

def standalone_func():
    pass
""",
        encoding="utf-8",
    )

    parser = TreeSitterParser()
    result = parser.parse_file(str(test_file))

    assert result is not None
    assert len(result["classes"]) == 1
    assert result["classes"][0]["name"] == "SampleService"
    assert len(result["functions"]) >= 2
    assert any(f["name"] == "execute" for f in result["functions"])
    assert any(f["name"] == "standalone_func" for f in result["functions"])
