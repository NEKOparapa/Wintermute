import asyncio
import re
from pathlib import Path
from urllib.parse import urlsplit

import segno
from aiohttp.test_utils import TestClient, TestServer

from purin.config.models import HTTPSettings
from purin.events.ingress import EventGateway
from purin.events.storage.sqlite import SQLiteStore
from purin.interfaces.http import HTTPInterface


def test_pairing_from_admin_invitation(tmp_path: Path) -> None:
    asyncio.run(check_pairing(tmp_path))


async def check_pairing(tmp_path: Path) -> None:
    password = "fixture-password-for-admin"
    settings = HTTPSettings.model_validate({"enabled": True, "admin": {"enabled": True}})
    proxy = {"Host": "agent.example:8443", "X-Forwarded-Proto": "https"}
    with SQLiteStore(tmp_path / "events.sqlite") as store:
        interface = HTTPInterface(
            settings,
            EventGateway(store),
            environ={"PURIN_ADMIN_PASSWORD": password},
            base_dir=tmp_path,
        )
        async with TestClient(TestServer(interface.create_app())) as client:
            admin = {**proxy, "Origin": "https://agent.example:8443"}
            login = await client.post(
                "/admin/login", headers=admin, data={"password": password}, allow_redirects=False
            )
            assert login.status == 303
            admin["Cookie"] = login.cookies.output(header="", attrs=[]).strip()
            devices = await (await client.get("/admin/devices", headers=admin)).text()
            assert 'name="operation" value="pair"' in devices
            assert "选择配对方式" not in devices
            csrf = re.search(r'name="csrf" value="([^"]+)"', devices)[1]

            async def invite() -> str:
                response = await client.post(
                    "/admin/devices",
                    headers={**admin, "HX-Request": "true"},
                    data={"operation": "pair", "csrf": csrf},
                )
                assert response.status == 200
                dialog = await response.text()
                assert "短码" not in dialog and "手动登记" not in dialog
                link = re.search(r'id="pair-link" value="([^"]+)"', dialog)[1]
                assert link.startswith("https://agent.example:8443/#purin-pair=")
                qr = re.search(r'class="pair-qr" src="([^"]+)"', dialog)[1]
                assert qr == segno.make_qr(link, error="M").png_data_uri(scale=6)
                return urlsplit(link).fragment.removeprefix("purin-pair=")

            url = "/v1/devices/pair"
            body = {
                "device_type": "mobile",
                "device_name": "测试手机",
                "installation": "instance-12345",
                "token": "m" * 43,
                "code": await invite(),
            }
            device_id = "mobile-" + "1" * 24
            headers = {**proxy, "X-Device-ID": device_id, "X-Device-Platform": "android"}
            for _ in range(2):
                result = await client.post(url, headers=headers, json=body)
                assert result.status == 200 and await result.json() == {"device_id": device_id}
            roster = await (await client.get("/admin/devices", headers=admin)).text()
            assert device_id in roster and "Android" in roster and "测试手机" in roster
            other = {**body, "token": "t" * 43, "installation": "instance-54321"}
            assert (await client.post(url, headers=headers, json=other)).status == 403
            other["code"] = await invite()
            assert (await client.post(url, headers=headers, json=other)).status == 409
            assert interface.devices.authenticate(device_id, body["token"]) is not None
            other_id = "mobile-" + "2" * 24
            headers["X-Device-ID"] = other_id
            assert (
                await client.post(
                    url, headers={**headers, "X-Device-Platform": "esp32"}, json=other
                )
            ).status == 400
            assert (await client.post(url, headers=headers, json=other)).status == 200
            legacy = {**body, "code": await invite(), "device_type": "esp32", "token": "s" * 43}
            result = await client.post(url, headers=proxy, json=legacy)
            assert result.status == 200
            assert re.fullmatch(r"esp32-[0-9a-f]{24}", (await result.json())["device_id"])
