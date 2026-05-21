from __future__ import annotations

import logging
import shutil
from datetime import datetime, timedelta
from pathlib import Path


LOG_FORMAT = "%(asctime)s %(levelname)s [%(name)s] %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


def configure_logging(log_root: Path | str, retention_days: int = 7) -> Path:
    log_root = Path(log_root)
    today = datetime.now().astimezone().date()
    log_dir = log_root / today.isoformat()
    log_dir.mkdir(parents=True, exist_ok=True)

    _delete_expired_logs(log_root, today=today, retention_days=retention_days)

    log_path = log_dir / "app.log"
    handler = logging.FileHandler(log_path, encoding="utf-8")
    handler.setFormatter(logging.Formatter(LOG_FORMAT, datefmt=DATE_FORMAT))

    root_logger = logging.getLogger()
    for existing in list(root_logger.handlers):
        root_logger.removeHandler(existing)
        existing.close()
    root_logger.addHandler(handler)
    root_logger.setLevel(logging.INFO)

    return log_path


def _delete_expired_logs(log_root: Path, *, today, retention_days: int) -> None:
    cutoff = today - timedelta(days=retention_days)
    if not log_root.exists():
        return

    for path in log_root.iterdir():
        if not path.is_dir():
            continue
        try:
            log_date = datetime.strptime(path.name, "%Y-%m-%d").date()
        except ValueError:
            continue
        if log_date < cutoff:
            shutil.rmtree(path)
