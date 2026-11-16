"""DeepSeek ASR：上传完整音频文件，按原始顺序合并同一录音的转录。"""

import asyncio
import json
import math
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Protocol
from urllib.parse import urlsplit

if TYPE_CHECKING:
    from aiohttp import ClientSession

DEFAULT_ENDPOINT = "https://api.deepseek.com/v1/tools/asr"


class AudioTranscriptionError(ValueError):
    """音频未成功转录；异常文本不包含文件、凭据或服务端响应正文。"""


class AudioTranscriber(Protocol):
    async def transcribe(self, path: Path) -> str:
        """返回完整转录，空字符串表示整段录音未识别到语音。"""
        ...


class DeepSeekASR:
    """向独立 ASR 接口发送录音。请求失败交由切片处理器重试。"""

    def __init__(
        self,
        *,
        api_key: str | Callable[[], str],
        endpoint: str = DEFAULT_ENDPOINT,
        timeout: float = 180,
        max_audio_bytes: int = 4 * 1024 * 1024,
        max_response_bytes: int = 8 * 1024 * 1024,
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
        if not callable(api_key) and (
            not api_key
            or not api_key.isascii()
            or not api_key.isprintable()
            or any(character.isspace() for character in api_key)
        ):
            raise ValueError("ASR requires a valid Bearer API key")
        if isinstance(timeout, bool) or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("ASR timeout must be finite and positive")
        for limit in (max_audio_bytes, max_response_bytes):
            if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
                raise ValueError("ASR size limits must be positive integers")
        self.endpoint = endpoint
        self._api_key = api_key
        self.timeout = timeout
        self.max_audio_bytes = max_audio_bytes
        self.max_response_bytes = max_response_bytes

    async def _transcribe_part(self, session: "ClientSession", mime: str, audio: bytes) -> str:
        from aiohttp import FormData

        form = FormData()
        form.add_field("file", audio, filename="audio." + mime.split("/")[1], content_type=mime)
        key = self._api_key() if callable(self._api_key) else self._api_key
        async with session.post(
            self.endpoint,
            data=form,
            headers={"Authorization": f"Bearer {key}"},
            allow_redirects=False,
        ) as response:
            if response.status != 200:
                raise AudioTranscriptionError(f"DeepSeek ASR returned HTTP {response.status}")
            body = bytearray()
            async for chunk in response.content.iter_chunked(64 * 1024):
                if len(body) + len(chunk) > self.max_response_bytes:
                    raise AudioTranscriptionError("ASR response exceeds size limit")
                body.extend(chunk)
            try:
                result = json.loads(body)
            except (ValueError, RecursionError):
                raise AudioTranscriptionError("ASR response is not valid JSON") from None
            if not isinstance(result, dict) or not isinstance(text := result.get("text"), str):
                raise AudioTranscriptionError("ASR response is missing transcript text")
            return text.strip()

    async def transcribe(self, path: Path) -> str:
        from aiohttp import ClientError, ClientSession, ClientTimeout

        from wintermute.events.slices.asr_audio import prepare_audio

        try:
            parts = await asyncio.to_thread(prepare_audio, path, self.max_audio_bytes)
        except (OSError, ValueError):
            raise AudioTranscriptionError("Cannot prepare audio file for DeepSeek ASR") from None
        transcripts: list[str] = []
        try:
            async with ClientSession(
                timeout=ClientTimeout(total=self.timeout),
            ) as session:
                for mime, audio in parts:
                    text = await self._transcribe_part(session, mime, audio)
                    if text:
                        transcripts.append(text)
        except TimeoutError:
            raise AudioTranscriptionError("ASR request timed out") from None
        except ClientError:
            raise AudioTranscriptionError("ASR connection failed") from None
        return "\n".join(transcripts)
