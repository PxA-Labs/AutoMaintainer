import os
import json
from pathlib import Path
from tree_sitter import Language, Parser

import tree_sitter_python
import tree_sitter_javascript
import tree_sitter_typescript

# Initialize Languages
PY_LANGUAGE = Language(tree_sitter_python.language())
JS_LANGUAGE = Language(tree_sitter_javascript.language())
TS_LANGUAGE = Language(tree_sitter_typescript.language_typescript())


class TreeSitterParser:
    def __init__(self):
        self.parser = Parser()
        self.language_map = {
            ".py": PY_LANGUAGE,
            ".js": JS_LANGUAGE,
            ".jsx": JS_LANGUAGE,
            ".ts": TS_LANGUAGE,
            ".tsx": TS_LANGUAGE,
        }

    def parse_file(self, filepath: str):
        ext = Path(filepath).suffix
        if ext not in self.language_map:
            return None

        self.parser.language = self.language_map[ext]

        try:
            with open(filepath, "r", encoding="utf-8") as f:
                code = f.read()
        except Exception:
            return None

        tree = self.parser.parse(bytes(code, "utf8"))

        classes = []
        functions = []
        imports = []

        # We will use simple queries or traversal to find them.
        # For cross-language simplicity in this prototype, let's do a recursive AST walk.
        def walk(node):
            node_type = node.type
            # Python
            if node_type == "class_definition":
                name_node = node.child_by_field_name("name")
                if name_node:
                    classes.append(
                        {
                            "name": code[name_node.start_byte : name_node.end_byte],
                            "start_line": node.start_point[0] + 1,
                            "end_line": node.end_point[0] + 1,
                        }
                    )
            elif node_type == "function_definition":
                name_node = node.child_by_field_name("name")
                if name_node:
                    functions.append(
                        {
                            "name": code[name_node.start_byte : name_node.end_byte],
                            "start_line": node.start_point[0] + 1,
                            "end_line": node.end_point[0] + 1,
                        }
                    )
            elif node_type in ["import_statement", "import_from_statement"]:
                imports.append(
                    {
                        "statement": code[node.start_byte : node.end_byte],
                        "line": node.start_point[0] + 1,
                    }
                )

            # JS / TS
            elif node_type in ["class_declaration"]:
                name_node = node.child_by_field_name("name")
                if name_node:
                    classes.append(
                        {
                            "name": code[name_node.start_byte : name_node.end_byte],
                            "start_line": node.start_point[0] + 1,
                            "end_line": node.end_point[0] + 1,
                        }
                    )
            elif node_type in [
                "function_declaration",
                "method_definition",
                "arrow_function",
            ]:
                # Arrow functions might not have a direct name field, so we just log line numbers if needed
                name_node = node.child_by_field_name("name")
                name = (
                    code[name_node.start_byte : name_node.end_byte]
                    if name_node
                    else "<anonymous>"
                )
                functions.append(
                    {
                        "name": name,
                        "start_line": node.start_point[0] + 1,
                        "end_line": node.end_point[0] + 1,
                    }
                )
            elif node_type in ["import_statement"]:
                imports.append(
                    {
                        "statement": code[node.start_byte : node.end_byte],
                        "line": node.start_point[0] + 1,
                    }
                )

            for child in node.children:
                walk(child)

        walk(tree.root_node)

        return {"classes": classes, "functions": functions, "imports": imports}


class CodebaseMapper:
    def __init__(self, repo_path: str):
        self.repo_path = Path(repo_path)
        self.parser = TreeSitterParser()
        self.ignored_dirs = {
            ".git",
            "node_modules",
            "__pycache__",
            "venv",
            "env",
            "build",
            "dist",
        }

    def generate_architecture_map(self):
        architecture_map = {}

        for root, dirs, files in os.walk(self.repo_path):
            # Prune ignored directories
            dirs[:] = [d for d in dirs if d not in self.ignored_dirs]

            for file in files:
                filepath = Path(root) / file
                ext = filepath.suffix

                # Only parse supported files to save time
                if ext in self.parser.language_map:
                    rel_path = filepath.relative_to(self.repo_path).as_posix()
                    parsed_data = self.parser.parse_file(str(filepath))
                    if parsed_data and (
                        parsed_data["classes"] or parsed_data["functions"]
                    ):
                        architecture_map[rel_path] = parsed_data

        return architecture_map

    def export_map(self, output_file="architecture_map.json"):
        arch_map = self.generate_architecture_map()
        with open(output_file, "w", encoding="utf-8") as f:
            json.dump(arch_map, f, indent=2)
        return arch_map


if __name__ == "__main__":
    # Simple test when run locally
    mapper = CodebaseMapper(".")
    arch = mapper.generate_architecture_map()
    print(json.dumps(arch, indent=2))
