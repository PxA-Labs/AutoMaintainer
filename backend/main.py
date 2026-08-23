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

from typing import Optional, Dict
from agents import run_agent_loop
from workspace import get_safe_repo_dir, get_safe_target_path, get_base_workspace_dir
import asyncio
import json
import uuid
import httpx
import os
import sys
import platform
import subprocess
import re
import logging
from pathlib import Path
from contextlib import asynccontextmanager

logger = logging.getLogger(__name__)

gitnexus_process = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global gitnexus_process
    # Start the GitNexus MCP server in the background
    try:
        cmd = (
            ["gitnexus.cmd", "serve"]
            if platform.system() == "Windows"
            else ["gitnexus", "serve"]
        )
        gitnexus_process = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        print("GitNexus server started on port 4747")
    except Exception as e:
        print(f"Failed to start GitNexus: {e}")

    # Check Supabase connection on startup
    from agents import supabase

    if supabase:
        try:
            await asyncio.wait_for(
                asyncio.to_thread(
                    lambda: supabase.table("runs").select("id").limit(1).execute()
                ),
                timeout=3.0,
            )
            print("Supabase connection established successfully.")
        except Exception as e:
            logger.critical(
                f"CRITICAL WARNING: Supabase connection failed on startup. "
                f"The database might be paused, unreachable, or undergoing maintenance. "
                f"Error: {e}"
            )
    else:
        logger.warning("Supabase is not configured. Persistence will be disabled.")

    yield

    if gitnexus_process:
        gitnexus_process.terminate()
        print("GitNexus server stopped")


app = FastAPI(title="AutoMaintainer Backend", lifespan=lifespan)

# Allow the Next.js frontend to connect to this API
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://127.0.0.1:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/healthz/supabase")
async def healthz_supabase():
    from agents import supabase

    if not supabase:
        raise HTTPException(
            status_code=503,
            detail="Supabase is not configured. Please check environment variables.",
        )
    try:
        await asyncio.to_thread(
            lambda: supabase.table("runs").select("id").limit(1).execute()
        )
        return {"status": "healthy", "database": "connected"}
    except Exception as e:
        logger.error(f"Supabase connection check failed: {e}", exc_info=True)
        raise HTTPException(
            status_code=503,
            detail="Supabase connection failed (database may be paused or unreachable)",
        )


class StartRequest(BaseModel):
    repo_name: str
    target_issue: Optional[int] = None


class StopRequest(BaseModel):
    run_id: Optional[str] = None


class FileUpdateRequest(BaseModel):
    file_path: str
    content: str
    commit_message: Optional[str] = None


class FileCreateRequest(BaseModel):
    file_path: str
    content: str = ""
    is_dir: bool = False
    commit_message: Optional[str] = None


active_tasks: Dict[str, asyncio.Task] = {}
tasks_lock = asyncio.Lock()


@app.post("/start")
async def start_agents(req: StartRequest):
    run_id = str(uuid.uuid4())
    task = asyncio.create_task(run_agent_loop(req.repo_name, req.target_issue, run_id))

    async with tasks_lock:
        active_tasks[run_id] = task

    def on_task_done(t):
        async def remove_task():
            async with tasks_lock:
                active_tasks.pop(run_id, None)

        try:
            loop = asyncio.get_running_loop()
            loop.create_task(remove_task())
        except RuntimeError:
            pass

    task.add_done_callback(on_task_done)
    return {"status": "started", "run_id": run_id}


@app.post("/stop")
async def stop_agents(req: Optional[StopRequest] = None):
    async with tasks_lock:
        if req and req.run_id:
            task = active_tasks.get(req.run_id)
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                active_tasks.pop(req.run_id, None)
                return {"status": "stopped", "run_id": req.run_id}
            return {"status": "not_running", "run_id": req.run_id}
        else:
            stopped_any = False
            for r_id, task in list(active_tasks.items()):
                if not task.done():
                    task.cancel()
                    stopped_any = True
            active_tasks.clear()
            return {"status": "stopped" if stopped_any else "not_running"}


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

        # Write to local clone so the IDE doesn't show stale reads
        repo_dir = get_safe_repo_dir(repo_name)
        if repo_dir.exists():
            target_path = get_safe_target_path(repo_dir, payload.file_path)
            target_path.parent.mkdir(parents=True, exist_ok=True)
            target_path.write_text(payload.content, encoding="utf-8")

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to update file: {e}")
    return {"status": "updated", "message": message}


@app.post("/repo/{repo_name:path}/file/create")
async def create_repo_file(repo_name: str, payload: FileCreateRequest):
    token = os.getenv("GITHUB_TOKEN")
    if not token:
        raise HTTPException(status_code=401, detail="GitHub token not configured")
    gh = Github(token)
    try:
        repo = gh.get_repo(repo_name)
    except Exception as e:
        raise HTTPException(status_code=404, detail=f"Repository not found: {e}")

    message = (
        payload.commit_message or f"Create {payload.file_path} via AutoMaintainer IDE"
    )

    actual_path = payload.file_path
    actual_content = payload.content
    if payload.is_dir:
        actual_path = f"{payload.file_path.rstrip('/')}/.gitkeep"
        actual_content = ""

    try:
        repo.create_file(actual_path, message, actual_content)

        # Write to local clone
        repo_dir = get_safe_repo_dir(repo_name)
        if repo_dir.exists():
            target_path = get_safe_target_path(repo_dir, actual_path)
            target_path.parent.mkdir(parents=True, exist_ok=True)
            target_path.write_text(actual_content, encoding="utf-8")

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to create file: {e}")
    return {"status": "created", "message": message}


@app.delete("/repo/{repo_name:path}/file")
async def delete_repo_file(repo_name: str, file_path: str):
    token = os.getenv("GITHUB_TOKEN")
    if not token:
        raise HTTPException(status_code=401, detail="GitHub token not configured")
    gh = Github(token)
    try:
        repo = gh.get_repo(repo_name)
    except Exception as e:
        raise HTTPException(status_code=404, detail=f"Repository not found: {e}")

    try:
        file = repo.get_contents(file_path)
    except Exception as e:
        raise HTTPException(status_code=404, detail=f"File not found in repo: {e}")

    if isinstance(file, list):
        raise HTTPException(
            status_code=400,
            detail="Directories cannot be deleted directly via this endpoint.",
        )

    message = f"Delete {file_path} via AutoMaintainer IDE"
    try:
        repo.delete_file(file.path, message, file.sha)

        # Local delete
        import shutil

        repo_dir = get_safe_repo_dir(repo_name)
        if repo_dir.exists():
            target_path = get_safe_target_path(repo_dir, file_path)
            if target_path.exists():
                if target_path.is_file():
                    target_path.unlink()
                elif target_path.is_dir():
                    shutil.rmtree(target_path)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to delete file: {e}")
    return {"status": "deleted", "message": message}


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
        match = re.search(
            r"github\.com/([a-zA-Z0-9_.-]+)/([a-zA-Z0-9_.-]+?)(?:\.git)?$",
            repo_url,
        )
        if match:
            owner, repo = match.group(1), match.group(2)
            repo_name = f"{owner}/{repo}"
            try:
                repo_dir = get_safe_repo_dir(repo_name)
                base_dir = get_base_workspace_dir()
                real_dir = os.path.realpath(str(repo_dir))
                real_base = os.path.realpath(str(base_dir))
                if (
                    real_dir.startswith(real_base + os.path.sep)
                    and os.path.exists(real_dir)
                    and os.path.isdir(real_dir)
                ):
                    cwd = real_dir
            except HTTPException:
                pass

    if sys.platform == "win32":
        import pywinpty

        cols, rows = 80, 24
        pty = pywinpty.PTY(cols, rows)
        pid = pty.spawn(pywinpty.winpty.get_default_cmd(), cwd=cwd)

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
                pty.close()
            except Exception:
                pass
            if pid:
                try:
                    import signal

                    os.kill(pid, signal.SIGTERM)
                except OSError:
                    pass
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
    repo_dir = get_safe_repo_dir(repo_name)

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
                    "path": Path(item_path).relative_to(repo_dir).as_posix(),
                    "type": "directory" if is_dir else "file",
                }
                if is_dir:
                    node["children"] = build_tree(item_path)
                tree.append(node)
        except (OSError, PermissionError) as e:
            logger.warning(f"Error accessing path {path}: {e}")
            raise HTTPException(status_code=500, detail="Error accessing file system")

        # Sort directories first, then files
        tree.sort(key=lambda x: (x["type"] != "directory", x["name"].lower()))
        return tree

    return {"name": repo_name, "type": "directory", "children": build_tree(repo_dir)}


@app.get("/repo/{repo_name:path}/search")
def search_repo(repo_name: str, q: str):
    repo_dir = get_safe_repo_dir(repo_name)

    if not repo_dir.exists():
        raise HTTPException(status_code=404, detail="Repo not found locally")

    ignored_dirs = {
        ".git",
        "node_modules",
        "__pycache__",
        "venv",
        "env",
        "build",
        "dist",
        ".next",
    }
    results = []

    try:
        for root, dirs, files in os.walk(repo_dir):
            dirs[:] = [
                d
                for d in dirs
                if d not in ignored_dirs and not os.path.islink(os.path.join(root, d))
            ]
            for file in files:
                file_path = os.path.join(root, file)
                if os.path.islink(file_path):
                    continue
                try:
                    with open(file_path, "r", encoding="utf-8") as f:
                        for i, line in enumerate(f):
                            if q.lower() in line.lower():
                                rel_path = (
                                    Path(file_path).relative_to(repo_dir).as_posix()
                                )
                                results.append(
                                    {
                                        "file": rel_path,
                                        "line_number": i + 1,
                                        "snippet": line.strip()[:200],
                                    }
                                )
                except UnicodeDecodeError:
                    pass
                except Exception:
                    pass
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    return {"query": q, "results": results[:100]}


@app.get("/repo/{repo_name:path}/file")
def get_repo_file(repo_name: str, file_path: str):
    repo_dir = get_safe_repo_dir(repo_name)
    target_path = get_safe_target_path(repo_dir, file_path)

    import os, stat

    try:
        fd = os.open(target_path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError:
        raise HTTPException(status_code=404, detail="File not found")

    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise HTTPException(status_code=404, detail="File not found")

        # Pass closefd=False so the finally block retains exclusive ownership of closing fd
        with os.fdopen(fd, "r", encoding="utf-8", closefd=False) as f:
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
