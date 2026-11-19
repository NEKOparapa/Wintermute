import asyncio
import io
import json
import wave
import zipfile

from aiohttp import ClientSession
from openpyxl import Workbook
from test_chat_media import jpeg, media_service

from wintermute.interfaces.http_routes import chat_documents


def auth(terminal):
    return {"Authorization": f"Bearer {terminal}", "X-Terminal-ID": terminal}


async def completed(client, receipt):
    for _ in range(150):
        result = await (await client.get("/v1/jobs/" + receipt["job_id"])).json()
        if result["status"] in {"completed", "failed"}:
            assert result["status"] == "completed", result
            return
        await asyncio.sleep(0.05)
    raise AssertionError("job timed out")


def test_conversion_cache_and_followup(tmp_path, monkeypatch):
    conversions = []
    real_convert = chat_documents.convert_upload

    async def count(data, extension, asr):
        conversions.append(extension)
        return await real_convert(data, extension, asr)

    monkeypatch.setattr(chat_documents, "convert_upload", count)
    text = "文件开头 " + "abcdefghij " * 240 + "TAIL-8471"
    binary = io.BytesIO()
    with zipfile.ZipFile(binary, "w") as z:
        z.writestr("notes.txt", text)
    doc = {}

    def reply(data):
        messages = data["messages"]
        if messages[-1]["role"] == "user" and "追问" in str(messages[-1]["content"]):
            return {
                "delta": {
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "read-tail",
                            "type": "function",
                            "function": {
                                "name": "read_chat_file",
                                "arguments": json.dumps({"file_id": doc["id"], "offset": 2000}),
                            },
                        }
                    ]
                },
                "finish_reason": "tool_calls",
            }
        return {"delta": {"content": "收到消息"}, "finish_reason": "stop"}

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
            doc.update(a)
            assert not calls
            data = {
                "text": "结合图片阅读文件",
                "message_id": "mixed",
                "documents": [a],
                "images": [jpeg("blue")],
                "image_names": ["原图.png"],
            }
            receipt = await (await client.post("/v1/messages/images", json=data)).json()
            await completed(client, receipt)
            assert (await (await client.post("/v1/messages/images", json=data)).json())["duplicate"]
            assert "notes.txt" in str(calls[-1]) and "TAIL-8471" not in str(calls[-1])
            history = await (await client.get("/v1/history?view=dialogue")).json()
            row = next(x for x in history["items"] if x["role"] == "user")
            assert row["text"] == data["text"]
            assert [f["name"] for f in row["files"]] == ["原图.png", "会议.zip"]
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

        async with (
            media_service(tmp_path, model_reply=reply) as (url, calls, _),
            ClientSession(url, headers=auth("desktop")) as client,
        ):
            assert await upload(client) == doc
            receipt = await (
                await client.post(
                    "/v1/messages",
                    json={"message_id": "follow-doc", "text": "追问，文件末尾是什么？"},
                )
            ).json()
            await completed(client, receipt)
            assert any(m["role"] == "tool" and "TAIL-8471" in m["content"] for m in calls[-1])
            assert conversions == [".zip"]

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
