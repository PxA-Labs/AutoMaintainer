from fastapi import (
    FastAPI,
    WebSocket,
    WebSocketDisconnect,
    BackgroundTasks,
    HTTPException,
)
from pydantic import BaseModel
from github import Github
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from typing import Optional
from agents import run_agent_loop
import asyncio
import json
import os
import sys
import platform
import subprocess
import re
import logging
from contextlib import asynccontextmanager

logger = logging.getLogger(__name__)

gitnexus_process = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global gitnexus_process
    # Start the GitNexus MCP server in the background
    try:
        gitnexus_process = subprocess.Popen(
            ["gitnexus", "serve"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        print("GitNexus server started on port 4747")
    except Exception as e:
        print(f"Failed to start GitNexus: {e}")

    yield

    if gitnexus_process:
        gitnexus_process.terminate()
        print("GitNexus server stopped")


app = FastAPI(title="AutoMaintainer Backend", lifespan=lifespan)

# Allow the Next.js frontend to connect to this API
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class ConnectionManager:
    def __init__(self):
        self.active_connections: list[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)

    def disconnect(self, websocket: WebSocket):
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)

    async def broadcast(self, message: dict):
        for connection in self.active_connections:
            try:
                await connection.send_text(json.dumps(message))
            except Exception:
                pass


manager = ConnectionManager()


class StartRequest(BaseModel):
    repo_name: str
    target_issue: Optional[int] = None


class FileUpdateRequest(BaseModel):
    file_path: str
    content: str
    commit_message: Optional[str] = None


active_task: Optional[asyncio.Task] = None


@app.post("/start")
async def start_agents(req: StartRequest):
    global active_task
    if active_task and not active_task.done():
        return {"status": "already_running"}
    active_task = asyncio.create_task(
        run_agent_loop(req.repo_name, manager, req.target_issue)
    )
    return {"status": "started"}


@app.post("/repo/{repo_name:path}/file")
async def update_repo_file(repo_name: str, payload: FileUpdateRequest):
    token = os.getenv("GITHUB_TOKEN")
    if not token:
        raise HTTPException(status_code=401, detail="GitHub token not configured")
    gh = Github(token)
    try:
        repo = gh.get_repo(repo_name)
    except Exception as e:
        raise HTTPException(status_code=404, detail=f"Repository not found: {e}")
    try:
        file = repo.get_contents(payload.file_path)
    except Exception as e:
        raise HTTPException(status_code=404, detail=f"File not found in repo: {e}")
    message = (
        payload.commit_message or f"Update {payload.file_path} via AutoMaintainer IDE"
    )
    try:
        repo.update_file(file.path, message, payload.content, file.sha)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to update file: {e}")
    return {"status": "updated", "message": message}


@app.post("/stop")
async def stop_agents():
    global active_task
    if active_task and not active_task.done():
        active_task.cancel()
        active_task = None
        await manager.broadcast(
            {
                "agent": "System",
                "msg": "Agent loop cancelled by user.",
                "color": "text-red-500",
            }
        )
        return {"status": "stopped"}
    return {"status": "not_running"}


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await manager.connect(websocket)
    try:
        while True:
            # For now, just listen and keep the connection open
            data = await websocket.receive_text()
            await manager.broadcast({"type": "system", "msg": f"Received: {data}"})
    except WebSocketDisconnect:
        manager.disconnect(websocket)


@app.websocket("/api/terminal/ws")
async def terminal_ws(websocket: WebSocket, repo_url: str = ""):
    origin = websocket.headers.get("origin")
    if origin and not (
        origin.startswith("http://localhost")
        or "huggingface.co" in origin
        or origin.startswith("http://127.0.0.1")
    ):
        await websocket.close(code=1008)
        return

    await websocket.accept()

    cwd = None
    if repo_url:
        from urllib.parse import urlparse

        parsed = urlparse(repo_url)
        repo_name = parsed.path.strip("/")
        if repo_name.endswith(".git"):
            repo_name = repo_name[:-4]

        parts = [p for p in repo_name.split("/") if p]
        if len(parts) >= 2:
            repo_name = f"{parts[-2]}/{parts[-1]}"

        clean_name = repo_name.replace("/", "_").replace("\\", "_")
        base_tmp = os.path.abspath("/tmp")
        repo_dir = os.path.abspath(os.path.join(base_tmp, clean_name))

        if repo_dir.startswith(base_tmp + os.sep) and os.path.exists(repo_dir):
            cwd = repo_dir

    if sys.platform == "win32":
        import pywinpty

        cols, rows = 80, 24
        pty = pywinpty.PTY(cols, rows)
        pty.spawn(pywinpty.winpty.get_default_cmd(), cwd=cwd)

        async def read_from_pty():
            while True:
                try:
                    data = await asyncio.to_thread(pty.read)
                    if data:
                        await websocket.send_text(data)
                    else:
                        await asyncio.sleep(0.01)
                except Exception:
                    break

        read_task = asyncio.create_task(read_from_pty())

        try:
            while True:
                message = await websocket.receive_text()
                if message.startswith('{"type":"resize"'):
                    msg_data = json.loads(message)
                    pty.set_size(msg_data["cols"], msg_data["rows"])
                else:
                    await asyncio.to_thread(pty.write, message)
        except Exception:
            pass
        finally:
            read_task.cancel()
            try:
                del pty
            except Exception:
                pass
    else:
        import pty
        import fcntl
        import termios
        import struct
        import signal

        pid, fd = pty.fork()
        if pid == 0:
            if cwd:
                os.chdir(cwd)
            os.environ["TERM"] = "xterm-256color"
            os.execv("/bin/bash", ["/bin/bash"])
        else:

            async def read_from_pty():
                while True:
                    try:
                        data = await asyncio.to_thread(os.read, fd, 1024)
                        if data:
                            await websocket.send_text(
                                data.decode("utf-8", errors="replace")
                            )
                        else:
                            break
                    except Exception:
                        break

            read_task = asyncio.create_task(read_from_pty())

            try:
                while True:
                    message = await websocket.receive_text()
                    if message.startswith('{"type":"resize"'):
                        msg_data = json.loads(message)
                        winsize = struct.pack(
                            "HHHH", msg_data["rows"], msg_data["cols"], 0, 0
                        )
                        fcntl.ioctl(fd, termios.TIOCSWINSZ, winsize)
                    else:
                        await asyncio.to_thread(os.write, fd, message.encode("utf-8"))
            except Exception:
                pass
            finally:
                read_task.cancel()
                try:
                    os.close(fd)
                except Exception:
                    pass
                try:
                    os.kill(pid, signal.SIGKILL)
                    os.waitpid(pid, 0)
                except Exception:
                    pass


@app.get("/repo/{repo_name:path}/tree")
def get_repo_tree(repo_name: str):
    base_tmp = os.path.abspath("/tmp")
    clean_name = repo_name.replace("/", "_").replace("\\", "_")
    repo_dir = os.path.abspath(os.path.join(base_tmp, clean_name))

    if not repo_dir.startswith(base_tmp + os.sep):
        raise HTTPException(status_code=400, detail="Invalid repository name")

    if not os.path.exists(repo_dir):
        raise HTTPException(
            status_code=404,
            detail="Repository not cloned yet. Start an agent loop first.",
        )

    ignored_dirs = {
        ".git",
        "node_modules",
        "__pycache__",
        "venv",
        "env",
        "build",
        "dist",
    }

    def build_tree(path):
        tree = []
        try:
            for item in os.listdir(path):
                if item in ignored_dirs:
                    continue
                item_path = os.path.join(path, item)

                # Prevent symlink loops
                if os.path.islink(item_path):
                    continue

                is_dir = os.path.isdir(item_path)
                node = {
                    "name": item,
                    "path": os.path.relpath(item_path, repo_dir).replace("\\", "/"),
                    "type": "directory" if is_dir else "file",
                }
                if is_dir:
                    node["children"] = build_tree(item_path)
                tree.append(node)
        except (OSError, PermissionError) as e:
            logger.warning(f"Error accessing path {path}: {e}")
            raise HTTPException(status_code=500, detail=f"Error accessing file system")

        # Sort directories first, then files
        tree.sort(key=lambda x: (x["type"] != "directory", x["name"].lower()))
        return tree

    return {"name": repo_name, "type": "directory", "children": build_tree(repo_dir)}


@app.get("/repo/{repo_name:path}/file")
def get_repo_file(repo_name: str, file_path: str):
    from pathlib import Path

    base_tmp = Path("/tmp").resolve()
    clean_name = repo_name.replace("/", "_").replace("\\", "_")
    repo_dir = (base_tmp / clean_name).resolve()

    if not repo_dir.is_relative_to(base_tmp):
        raise HTTPException(status_code=400, detail="Invalid repository name")

    try:
        target_path = (repo_dir / file_path).resolve()
    except (OSError, RuntimeError) as e:
        logger.warning(f"Error resolving path {file_path}: {e}")
        raise HTTPException(status_code=400, detail="Invalid or looping file path")

    # Strict path traversal security check for CodeQL
    if not target_path.is_relative_to(repo_dir):
        raise HTTPException(status_code=403, detail="Invalid file path")

    import os, stat
    try:
        fd = os.open(target_path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError:
        raise HTTPException(status_code=404, detail="File not found")

    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise HTTPException(status_code=404, detail="File not found")

        with os.fdopen(fd, "r", encoding="utf-8") as f:
            content = f.read()
        return {"content": content}
    except UnicodeDecodeError:
        raise HTTPException(status_code=415, detail="Cannot read binary file")
    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"Failed to read file {target_path}: {e}")
        raise HTTPException(
            status_code=500, detail="An internal error occurred while reading the file"
        )
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


# Serve the static Next.js frontend if the out directory exists
if os.path.exists("../dashboard/out"):
    app.mount(
        "/", StaticFiles(directory="../dashboard/out", html=True), name="dashboard"
    )
elif os.path.exists("dashboard/out"):  # In docker container
    app.mount("/", StaticFiles(directory="dashboard/out", html=True), name="dashboard")
