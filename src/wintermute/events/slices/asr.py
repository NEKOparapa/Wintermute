"""火山录音文件极速版 ASR；小段音频直接提交，无需公开文件下载地址。"""

import asyncio
import base64
import json
import math
from pathlib import Path
from typing import Protocol
from urllib.parse import urlsplit
from uuid import uuid4

DEFAULT_ENDPOINT = "https://openspeech.bytedance.com/api/v3/auc/bigmodel/recognize/flash"


class AudioTranscriptionError(ValueError):
    """音频未成功转录；异常文本不包含文件、凭据或服务端响应正文。"""


class AudioTranscriber(Protocol):
    async def transcribe(self, path: Path) -> str:
        """返回完整转录，空字符串仅表示服务端明确识别为静音。"""
        ...


def _read_audio(path: Path, limit: int) -> str:
    try:
        with path.open("rb") as audio:
            data = audio.read(limit + 1)
    except OSError:
        raise AudioTranscriptionError("Cannot read ASR audio file") from None
    if not data:
        raise AudioTranscriptionError("ASR audio file is empty")
    if len(data) > limit:
        raise AudioTranscriptionError("ASR audio file exceeds configured size limit")
    return base64.b64encode(data).decode("ascii")


class VolcengineASR:
    """每次调用持有自己的 HTTP 会话，成功、失败及取消均关闭连接。

    新版使用 api_key，旧版使用 app_key + access_key，二者不可混用。
    单次请求不自动重试，由持久化切片处理器管理重试和转录缓存。
    """

    def __init__(
        self,
        *,
        endpoint: str = DEFAULT_ENDPOINT,
        api_key: str | None = None,
        app_key: str | None = None,
        access_key: str | None = None,
        resource_id: str = "volc.bigasr.auc_turbo",
        timeout: float = 180,
        max_audio_bytes: int = 4 * 1024 * 1024,
        max_response_bytes: int = 8 * 1024 * 1024,
        uid: str = "wintermute",
    ) -> None:
        try:
            parsed = urlsplit(endpoint)
            valid_url = (
                parsed.scheme in {"http", "https"}
                and parsed.hostname is not None
                and parsed.username is None
                and parsed.password is None
                and not parsed.query
                and not parsed.fragment
                and not any(character.isspace() for character in endpoint)
            )
            _ = parsed.port
        except ValueError:
            valid_url = False
        if not valid_url:
            raise ValueError("ASR endpoint must be an HTTP(S) URL without credentials or query")
        if api_key is not None:
            if app_key is not None or access_key is not None:
                raise ValueError("ASR authentication schemes must not be mixed")
        elif app_key is None or access_key is None:
            raise ValueError("ASR requires api_key or app_key and access_key")
        for value in (api_key, app_key, access_key, resource_id):
            if value is not None and (
                not value.strip()
                or any(ord(character) < 32 or ord(character) == 127 for character in value)
            ):
                raise ValueError("ASR credentials and resource ID must be nonblank header values")
        if not uid.strip():
            raise ValueError("ASR uid must not be blank")
        if isinstance(timeout, bool) or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("ASR timeout must be finite and positive")
        for limit in (max_audio_bytes, max_response_bytes):
            if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
                raise ValueError("ASR size limits must be positive integers")
        self.endpoint = endpoint
        self._api_key = api_key
        self._app_key = app_key
        self._access_key = access_key
        self.resource_id = resource_id
        self.timeout = timeout
        self.max_audio_bytes = max_audio_bytes
        self.max_response_bytes = max_response_bytes
        self.uid = uid

    async def transcribe(self, path: Path) -> str:
        # aiohttp 是可选接口依赖；仅实际启用音频处理时需要安装。
        from aiohttp import ClientError, ClientSession, ClientTimeout

        headers = {
            "X-Api-Resource-Id": self.resource_id,
            "X-Api-Request-Id": str(uuid4()),
            "X-Api-Sequence": "-1",
        }
        if self._api_key is not None:
            headers["X-Api-Key"] = self._api_key
        else:
            assert self._app_key is not None and self._access_key is not None
            headers["X-Api-App-Key"] = self._app_key
            headers["X-Api-Access-Key"] = self._access_key
        try:
            async with asyncio.timeout(self.timeout):
                encoded = await asyncio.to_thread(_read_audio, path, self.max_audio_bytes)
                body = {
                    "user": {"uid": self.uid},
                    "audio": {"data": encoded},
                    "request": {"model_name": "bigmodel"},
                }
                async with ClientSession(timeout=ClientTimeout(total=self.timeout)) as session:
                    async with session.post(
                        self.endpoint, headers=headers, json=body, allow_redirects=False
                    ) as response:
                        if response.status != 200:
                            raise AudioTranscriptionError("ASR HTTP request failed")
                        code = response.headers.get("X-Api-Status-Code")
                        if code == "20000003":
                            return ""
                        if code != "20000000":
                            raise AudioTranscriptionError("ASR service did not report success")
                        data = bytearray()
                        async for chunk in response.content.iter_chunked(64 * 1024):
                            if len(data) + len(chunk) > self.max_response_bytes:
                                raise AudioTranscriptionError("ASR response exceeds size limit")
                            data.extend(chunk)
                        try:
                            result = json.loads(data)
                        except (ValueError, UnicodeError, RecursionError):
                            raise AudioTranscriptionError(
                                "ASR response is not valid JSON"
                            ) from None
                        if not isinstance(result, dict) or not isinstance(
                            result.get("result"), dict
                        ):
                            raise AudioTranscriptionError("ASR response is missing a result")
                        text = result["result"].get("text")
                        if not isinstance(text, str) or not text.strip():
                            raise AudioTranscriptionError("ASR response is missing transcript text")
                        return text.strip()
        except TimeoutError:
            raise AudioTranscriptionError("ASR request timed out") from None
        except ClientError:
            raise AudioTranscriptionError("ASR connection failed") from None
