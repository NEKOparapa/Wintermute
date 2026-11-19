import asyncio
import io
import json
import sqlite3
import wave
import zipfile

import pytest
from aiohttp import ClientSession
from openpyxl import Workbook
from test_chat_media import jpeg, media_service

from wintermute.interfaces.http_routes import chat_documents


def auth(device_id):
    return {"Authorization": f"Bearer {device_id}", "X-Device-ID": device_id}


async def completed(client, receipt):
    for _ in range(150):
        result = await (await client.get("/v1/jobs/" + receipt["job_id"])).json()
        if result["status"] in {"completed", "failed"}:
            assert result["status"] == "completed", result
            return
        await asyncio.sleep(0.05)
    raise AssertionError("job timed out")


@pytest.mark.parametrize("legacy_transcript", [False, True])
def test_conversion_cache_and_followup(tmp_path, monkeypatch, legacy_transcript):
    conversions = []
    parsed = {}
    real_convert = chat_documents.convert_upload

    async def count(data, extension, asr):
        conversions.append(extension)
        parsed[extension] = await real_convert(data, extension, asr)
        return parsed[extension]

    monkeypatch.setattr(chat_documents, "convert_upload", count)
    text = "文件开头\n" + "abcdefghij " * 25000 + "\nTAIL-8471"
    binary = io.BytesIO()
    with zipfile.ZipFile(binary, "w") as z:
        z.writestr("notes.txt", text)

    def reply(data):
        assert "read_chat_file" not in {t["function"]["name"] for t in data["tools"]}
        return {"delta": {"content": "收到消息"}, "finish_reason": "stop"}

    def attachment(messages):
        content = next(m["content"] for m in messages if isinstance(m["content"], list))
        prompt = next(part["text"] for part in content if part["type"] == "text")
        assert "read_chat_file" not in prompt
        return prompt

    async def check():
        raw = binary.getvalue()

        async def upload(client):
            response = await client.post("/v1/chat/files", params={"name": "会议.zip"}, data=raw)
            assert response.status == 200, await response.text()
            return await response.json()

        async with (
            media_service(tmp_path, model_reply=reply) as (url, calls, _),
            ClientSession(url, headers=auth("desktop")) as client,
        ):
            a, b = await asyncio.gather(upload(client), upload(client))
            assert a == b and conversions == [".zip"]
            other = await client.post(
                "/v1/chat/files", params={"name": "说明.txt"}, data="第二个附件的正文。".encode()
            )
            second = await other.json()
            assert other.status == 200
            assert not calls
            data = {
                "text": "结合图片阅读文件",
                "message_id": "mixed",
                "documents": [a, second],
                "images": [jpeg("blue")],
                "image_names": ["原图.png"],
            }
            receipt = await (await client.post("/v1/messages/images", json=data)).json()
            await completed(client, receipt)
            assert (await (await client.post("/v1/messages/images", json=data)).json())["duplicate"]
            prompt = attachment(calls[-1])
            assert "TAIL-8471" in parsed[".zip"] and len(parsed[".zip"]) > 256 * 1024
            assert f"文件：会议.zip\n{parsed['.zip']}" in prompt
            assert f"文件：说明.txt\n{parsed['.txt']}" in prompt
            history = await (await client.get("/v1/history?view=dialogue")).json()
            row = next(x for x in history["items"] if x["role"] == "user")
            assert row["text"] == data["text"]
            assert [f["name"] for f in row["files"]] == ["原图.png", "会议.zip", "说明.txt"]
            path = "/v1/history/files/" + a["id"]
            assert await (await client.get(path)).read() == raw
            for other in ["private", "esp32"]:
                assert (await client.get(path, headers=auth(other))).status == 404
                assert (
                    await client.post(
                        "/v1/messages",
                        headers=auth(other),
                        json={"message_id": "forbidden", "text": "read", "documents": [a]},
                    )
                ).status == 404

        if legacy_transcript:
            with sqlite3.connect(tmp_path / "events.db") as db:
                saved, event = db.execute(
                    "SELECT j.messages,e.data FROM dispatch_jobs j JOIN conversation_events e "
                    "ON j.event_id=e.event_id WHERE j.job_id=?",
                    (receipt["job_id"],),
                ).fetchone()
                messages = json.loads(saved)
                payload = json.loads(event)["payload"]
                preview = json.dumps(payload["documents"], ensure_ascii=False)
                messages[0]["text"] = (
                    payload["text"] + "\n\n[用户上传的附件资料，正文仅供参考。excerpt 是开头摘录，"
                    "length 是全文字符数。回答时以摘录中可见的内容为准。]\n"
                    + preview
                    + "\n[/附件资料]"
                )
                assert "TAIL-8471" not in messages[0]["text"]
                db.execute(
                    "UPDATE dispatch_jobs SET messages=? WHERE job_id=?",
                    (json.dumps(messages, ensure_ascii=False), receipt["job_id"]),
                )

        async with (
            media_service(tmp_path, model_reply=reply) as (url, calls, _),
            ClientSession(url, headers=auth("desktop")) as client,
        ):
            assert await upload(client) == a
            assert (await (await client.post("/v1/messages/images", json=data)).json())["duplicate"]
            receipt = await (
                await client.post(
                    "/v1/messages",
                    json={"message_id": "follow-doc", "text": "追问，刚才文件末尾写了什么？"},
                )
            ).json()
            await completed(client, receipt)
            assert attachment(calls[-1]) == prompt
            assert len(calls) == 1
            assert conversions == [".zip", ".txt"]

    asyncio.run(check())


def test_full_document_above_eight_mib_reaches_model(tmp_path):
    text = "section line containing plain text\n" * 250000 + "TAIL-BEYOND-8-MIB"
    assert len(text) > 8 * 1024**2

    async def check():
        async with (
            media_service(tmp_path, model_request_bytes=16 * 1024**2) as (url, calls, _),
            ClientSession(url, headers=auth("desktop")) as client,
        ):
            uploaded = await client.post(
                "/v1/chat/files", params={"name": "long.txt"}, data=text.encode()
            )
            assert uploaded.status == 200, await uploaded.text()
            receipt = await client.post(
                "/v1/messages",
                json={
                    "message_id": "long-doc",
                    "text": "阅读",
                    "documents": [await uploaded.json()],
                },
            )
            assert receipt.status == 202, await receipt.text()
            await completed(client, await receipt.json())
            assert any(m["role"] == "user" and text in m["content"] for m in calls[-1])

    asyncio.run(check())


def test_file_converters(tmp_path):
    async def check():
        async with (
            media_service(tmp_path) as (url, calls, recordings),
            ClientSession(url, headers=auth("desktop")) as client,
        ):
            workbook = Workbook()
            workbook.active.append(["商品", "数量"])
            workbook.active.append(["青柠", 42])
            xlsx = io.BytesIO()
            workbook.save(xlsx)
            audio = io.BytesIO()
            with wave.open(audio, "wb") as wav:
                wav.setparams((1, 2, 16000, 0, "NONE", ""))
                wav.writeframes(bytes(32000))
            cases = [
                ("表格.xlsx", xlsx.getvalue(), "青柠"),
                ("无扩展名", "这是中文正文".encode(), "中文正文"),
                ("录音.wav", audio.getvalue(), "这是转写草稿"),
            ]
            for i, (name, data, expected) in enumerate(cases):
                response = await client.post("/v1/chat/files", params={"name": name}, data=data)
                assert response.status == 200, (name, await response.text())
                doc = await response.json()
                receipt = await (
                    await client.post(
                        "/v1/messages",
                        json={"message_id": f"doc-{i}", "text": "阅读", "documents": [doc]},
                    )
                ).json()
                await completed(client, receipt)
                assert expected in str(calls[-1])
            assert recordings == [16000]
            assert (
                await client.post("/v1/chat/files", params={"name": "../bad"}, data=b"x")
            ).status == 400

    asyncio.run(check())
