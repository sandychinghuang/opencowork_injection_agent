"""
runner.py — 驅動 open-claude-cowork 的 runner
"""

import asyncio
import json
import os
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

import httpx


def build_runner(config: dict) -> "CoworkRunner":
    return CoworkRunner(config)


class CoworkRunner:
    SERVER_PORT = 3001
    CANDIDATE_ENDPOINTS = [
        "/api/chat",
        "/api/message",
        "/api/messages",
        "/chat",
        "/message",
        "/api/run",
    ]

    def __init__(self, config: dict):
        self.project_dir = Path(config["cowork_project_dir"])
        self.server_dir = self.project_dir / "server"
        self.base_url = f"http://localhost:{self.SERVER_PORT}"
        self.endpoint: str | None = config.get("cowork_api_endpoint")
        self._server_proc: asyncio.subprocess.Process | None = None
        self._session_id = str(uuid.uuid4())

    async def startup(self):
        await self._kill_existing_server()
        await self._start_server()
        await self._wait_server_ready()
        if not self.endpoint:
            self.endpoint = await self._discover_endpoint()

    async def run(self, user_prompt: str, folder: str, timeout: int = 90) -> str:
        """送出 prompt，等待 agent 跑完並回傳完整的文字回應"""
        if not self._server_proc:
            await self.startup()

        session_id = str(uuid.uuid4())
        payload = self._build_payload(user_prompt, folder, session_id)
        
        print(f"  [runner] POST {self.endpoint}  session={session_id[:8]}")

        try:
            full_response = await asyncio.wait_for(
                self._call_and_stream(payload, timeout),
                timeout=timeout + 10,
            )
            return full_response
        except asyncio.TimeoutError:
            print(f"  [runner] ⚠ Timeout after {timeout}s")
            return "[TIMEOUT ERROR]"
        except Exception as e:
            print(f"  [runner] ⚠ Run error: {e}")
            return f"[ERROR] {str(e)}"

    async def shutdown(self):
        await self._kill_existing_server()

    async def _start_server(self):
        print(f"  [runner] Starting server in {self.server_dir}")
        # 檢查資料夾是否存在，不存在的話回退到專案根目錄（有些專案 server 就在根目錄）
        cwd = str(self.server_dir) if self.server_dir.exists() else str(self.project_dir)
        
        self._server_proc = await asyncio.create_subprocess_shell(
            "npm start",
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0,
        )

    async def _wait_server_ready(self, max_wait: int = 20):
        print(f"  [runner] Waiting for server on port {self.SERVER_PORT}...", end="", flush=True)
        deadline = time.time() + max_wait
        async with httpx.AsyncClient(timeout=2) as client:
            while time.time() < deadline:
                try:
                    r = await client.get(f"{self.base_url}/")
                    if r.status_code < 500:
                        print(" ready!")
                        return
                except Exception:
                    pass
                await asyncio.sleep(0.8)
                print(".", end="", flush=True)
        print(" (timeout, proceeding anyway)")

    async def _kill_existing_server(self):
        if sys.platform == "win32":
            cmd = f'for /f "tokens=5" %a in (\'netstat -aon ^| findstr :{self.SERVER_PORT}\') do taskkill /f /pid %a'
            subprocess.run(cmd, shell=True, capture_output=True)
        else:
            subprocess.run(f"lsof -ti:{self.SERVER_PORT} | xargs kill -9 2>/dev/null || true", shell=True)
        
        if self._server_proc and self._server_proc.returncode is None:
            try:
                if sys.platform == "win32":
                    self._server_proc.send_signal(signal.CTRL_BREAK_EVENT)
                else:
                    self._server_proc.terminate()
                await asyncio.sleep(1)
            except Exception: pass
        self._server_proc = None

    async def _discover_endpoint(self) -> str:
        async with httpx.AsyncClient(timeout=5, base_url=self.base_url) as client:
            for ep in self.CANDIDATE_ENDPOINTS:
                try:
                    r = await client.post(ep, json={"message": "ping", "test": True})
                    if r.status_code in (200, 400, 422, 415):
                        return ep
                except Exception: pass
        return "/api/chat"

    def _build_payload(self, message: str, folder: str, session_id: str) -> dict:
        # UI 的做法：把資料夾路徑直接附加在 message 文字裡
        # 參見 renderer.js 第 841-847 行
        message_with_folder = f"{message}\n\n[附加資料夾路徑: {folder}]"
        return {
            "message": message_with_folder,
            "chatId": session_id,
        }

    async def _call_and_stream(self, payload: dict, timeout: int) -> str:
        """POST 後讀 SSE 串流，擷取所有文字內容並回傳"""
        url = f"{self.base_url}{self.endpoint}"
        done_markers = {"done", "complete", "finished", "end", "[done]", "[DONE]"}
        full_text = []

        async with httpx.AsyncClient(timeout=httpx.Timeout(timeout)) as client:
            async with client.stream("POST", url, json=payload) as resp:
                if resp.status_code >= 400:
                    body = await resp.aread()
                    err = f"HTTP {resp.status_code}: {body.decode(errors='replace')}"
                    print(f"  [runner] {err[:200]}")
                    return err

                is_sse = "text/event-stream" in resp.headers.get("content-type", "")

                async for line in resp.aiter_lines():
                    if not line: continue

                    if is_sse and line.startswith("data:"):
                        data_str = line[5:].strip()
                        if data_str in done_markers:
                            break
                        
                        try:
                            obj = json.loads(data_str)
                            # 嘗試抓取各種常見的文字欄位
                            content = obj.get("content") or obj.get("text") or obj.get("message") or ""
                            if content:
                                full_text.append(str(content))
                                # 即時印出一點點內容讓 user 知道 agent 有在動
                                print(f"    [agent] {str(content).replace(chr(10), ' ')[:60]}...", end="\r")
                            
                            etype = obj.get("type") or obj.get("status") or ""
                            if str(etype).lower() in done_markers:
                                break
                        except Exception: pass
                    elif not is_sse:
                        full_text.append(line)

        final_content = "".join(full_text).strip()
        print(f"\n  [runner] ✓ Received {len(final_content)} characters")
        return final_content
