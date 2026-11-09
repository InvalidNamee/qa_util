import asyncio
import uuid

import httpx

from .storage import private_json, read_json


class RemoteError(Exception):
    def __init__(self, message, code="invalid", status=400):
        super().__init__(message)
        self.code, self.status = code, status


class RemoteClient:
    def __init__(self, config, control, log, status, *, transport=None):
        self.config, self.control, self.log, self.status = config, control, log, status
        identity = read_json(config.data_directory / "identity.json", {})
        self.client_id = identity.get("client_id") or str(uuid.uuid4())
        private_json(config.data_directory / "identity.json", {"client_id": self.client_id})
        self.session_id = ""
        self.connect_lock = asyncio.Lock()
        self.http = httpx.AsyncClient(base_url=config.server_url + "/api/v1/", timeout=10, transport=transport,
                                     headers={"Authorization": "Bearer " + config.access_token})

    async def connect(self):
        async with self.connect_lock:
            result = await self.call("POST", "session", {"client_id": self.client_id,
                                                          "username": self.config.account.username})
            self.session_id = result["session_id"]
            return result

    async def call(self, method, path, data=None):
        while True:
            self.control.check()
            try:
                response = await self.control.interruptible(self.http.request(
                    method, path, json=data, headers={"X-Session-ID": self.session_id}))
                if response.status_code >= 500:
                    raise httpx.TransportError(f"服务端暂不可用（HTTP {response.status_code}）")
            except httpx.TransportError:
                if self.control.online:
                    self.log("与服务端断开，暂停新的发送，等待重新连接")
                self.control.online = False
                self.status("等待连接服务端")
                await self.control.sleep(3)
                continue
            if not self.control.online:
                self.log("已重新连接服务端")
                self.status("")
            self.control.online = True
            body = response.json()
            if response.is_error:
                if body.get("code") == "session_expired" and path != "session":
                    await self.connect()
                    continue
                raise RemoteError(body.get("error", "服务端请求失败"), body.get("code", "invalid"), response.status_code)
            return body

    async def close(self):
        try:
            if self.session_id:
                await self.http.post("session/close", headers={"X-Session-ID": self.session_id}, timeout=2)
        except httpx.TransportError:
            pass  # 断线时会话由服务端心跳时限释放，提交记录仍保留。
        finally:
            await self.http.aclose()
