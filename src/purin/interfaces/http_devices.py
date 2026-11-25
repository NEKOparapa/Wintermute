"""Resolve configured and administrator-managed HTTP device credentials."""

from collections.abc import Mapping, Sequence
from hashlib import sha256
from hmac import compare_digest
from secrets import token_urlsafe
from typing import TYPE_CHECKING

from purin.config.models import HTTPDeviceBinding, HTTPDeviceSettings
from purin.events.core import Device, EventError, Layer
from purin.events.storage.sqlite import SQLiteStore

if TYPE_CHECKING:
    from purin.events.storage.repositories.devices import HTTPCredential


def _http_layers(binding: HTTPDeviceBinding) -> tuple[Layer, ...]:
    layers: tuple[Layer, ...] = ("conversation", "perception", "record")
    return layers if binding.is_desktop else (*layers, "slice")


def _digest(token: str) -> bytes:
    return sha256(token.encode("utf-8")).digest()


class HTTPDevices:
    def __init__(
        self,
        store: SQLiteStore,
        configured: Sequence[HTTPDeviceSettings],
        environ: Mapping[str, str] | None,
    ) -> None:
        self._store = store
        self._configured = {binding.device_id: binding for binding in configured}
        saved = store.devices.http_credentials()
        if any(
            saved[id].binding is not None and saved[id].digest is not None
            for id in self._configured.keys() & saved.keys()
        ):
            raise ValueError("Device ID is managed in SQLite and also declared in HTTP settings")
        self._digests = {
            binding.device_id: _digest(binding.resolve_token(environ))
            for binding in configured
            if binding.device_id not in saved
        }
        self._register_configured()
        for device_id, binding in self._configured.items():
            item = saved.get(device_id)
            if item is not None and item.binding is None and item.digest is None:
                self._store.devices.archive_http(binding)

    def _register_configured(self) -> None:
        # Check every existing owner before applying settings-file registrations.
        registrations: list[Device] = []
        owners: dict[str, str] = {}
        for binding in self._configured.values():
            owner = owners.setdefault(binding.conversation_id, binding.user_id)
            if owner != binding.user_id:
                raise ValueError("HTTP bindings assign a conversation to different users")
            layers = _http_layers(binding)
            try:
                device = self._store.get_device(binding.device_id)
            except EventError:
                registrations.append(
                    Device(
                        device_id=binding.device_id, user_id=binding.user_id, allowed_layers=layers
                    )
                )
            else:
                if device.user_id != binding.user_id or "conversation" not in device.allowed_layers:
                    raise ValueError("HTTP device conflicts with an existing registration")
                allowed = tuple(dict.fromkeys((*device.allowed_layers, *layers)))
                if binding.is_desktop:
                    allowed = tuple(layer for layer in allowed if layer != "slice")
                if allowed != device.allowed_layers:
                    registrations.append(device.model_copy(update={"allowed_layers": allowed}))
            try:
                owner = self._store.get_conversation_user(binding.conversation_id)
            except EventError:
                continue
            if owner != binding.user_id:
                raise ValueError("HTTP conversation conflicts with an existing registration")
        for device in registrations:
            self._store.register_device(device)
        for conversation, user in owners.items():
            self._store.register_conversation(conversation, user)

    def _active_digests(self, saved: Mapping[str, "HTTPCredential"]) -> dict[str, bytes]:
        return {
            **{id: digest for id, digest in self._digests.items() if id not in saved},
            **{
                id: item.digest
                for id, item in saved.items()
                if item.digest is not None and (item.binding is not None or id in self._configured)
            },
        }

    def list(self) -> list[tuple[HTTPDeviceBinding, bool]]:
        saved = self._store.devices.http_credentials()
        bindings: dict[str, HTTPDeviceBinding] = dict(self._configured)
        bindings.update(
            {id: item.binding for id, item in saved.items() if item.binding is not None}
        )
        active = self._active_digests(saved)
        return [(bindings[id], id in active) for id in sorted(bindings)]

    def active(self) -> tuple[HTTPDeviceBinding, ...]:
        return tuple(binding for binding, active in self.list() if active)

    def display_names(self, user_id: str) -> dict[str, str]:
        names = {
            b.device_id: b.display_name or b.device_id
            for b, _ in self.list()
            if b.user_id == user_id
        }
        names.update({id: name for id, name in self._store.devices.names(user_id).items() if name})
        return names

    def notification_targets(self, user: str, conversation: str) -> frozenset[str]:
        return frozenset(
            binding.device_id
            for binding in self.active()
            if binding.user_id == user and binding.conversation_id == conversation
        )

    def authenticate(self, device_id: str, token: str) -> HTTPDeviceBinding | None:
        saved = self._store.devices.http_credentials(device_id).get(device_id)
        configured: HTTPDeviceBinding | None = self._configured.get(device_id)
        if saved is None:
            binding = configured
            expected = self._digests.get(device_id)
        else:
            binding = saved.binding or configured
            expected = saved.digest
        if expected is not None and compare_digest(_digest(token), expected):
            return binding
        return None

    def has_unique_credential(self, device_id: str) -> bool:
        active = self._active_digests(self._store.devices.http_credentials())
        digest = active.get(device_id)
        return digest is not None and list(active.values()).count(digest) == 1

    def register(
        self,
        binding: HTTPDeviceBinding,
        *,
        token: str | None = None,
        installation: str | None = None,
        platform: str | None = None,
    ) -> str:
        token = token or token_urlsafe(32)
        self._store.devices.register_http(
            binding,
            Device(
                device_id=binding.device_id,
                user_id=binding.user_id,
                allowed_layers=_http_layers(binding),
            ),
            _digest(token),
            installation,
            platform,
        )
        return token

    def archive(self, device_id: str) -> None:
        saved = self._store.devices.http_credentials(device_id).get(device_id)
        binding = saved.binding if saved and saved.binding else self._configured.get(device_id)
        if binding is None:
            raise LookupError("Unknown HTTP device")
        self._store.devices.archive_http(binding)
