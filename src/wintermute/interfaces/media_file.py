"""Immutable shared media files, named by content digest."""

import hashlib
import os
import tempfile
from pathlib import Path


def save_media(directory: Path, data: bytes, suffix: str) -> str:
    digest = hashlib.sha256(data).hexdigest()
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / (digest + suffix)

    def intact() -> bool:
        try:
            with target.open("rb") as source:
                return hashlib.file_digest(source, "sha256").hexdigest() == digest
        except FileNotFoundError:
            return False

    # On Windows an ASR reader may hold the destination open. Identical retries
    # need no replacement and must still return the original receipt.
    if intact():
        return digest
    # All writers use content-addressed, immutable names; atomic replacement also
    # repairs a damaged local copy when the device retries the original segment.
    descriptor, temporary = tempfile.mkstemp(prefix=".upload-", dir=directory)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        try:
            os.replace(temporary, target)
        except PermissionError:
            if not intact():
                raise
    finally:
        Path(temporary).unlink(missing_ok=True)
    return digest
