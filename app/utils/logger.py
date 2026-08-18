"""日志配置 —— 迁移自 Research-Agent(backend/utils/logger.py)。

统一附带 request_id（来自 app.audit.middleware 的 contextvar），
线程池内无 contextvar 时为空，不影响原有格式。

结构化：调用方可用 extra={...} 追加 key=value 字段（自动 JSON 序列化非标量），
示例：logger.info("分析完成", extra={"ticker": "600519.SS", "seconds": 1.2})
"""
import json
import logging
import sys


class _RequestIdFormatter(logging.Formatter):
    # logging.LogRecord 自带字段（+ 本项目注入的 request_id），其余视为结构化字段
    _STD = set(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {
        "request_id", "message", "asctime", "taskName", "name", "msg",
    }

    def format(self, record: logging.LogRecord) -> str:
        rid = ""
        try:
            from app.audit.middleware import get_request_id
            rid = get_request_id()
        except Exception:
            rid = ""
        record.request_id = f"[req={rid}]" if rid else ""
        text = super().format(record)
        extra = []
        for k, v in sorted(record.__dict__.items()):
            if k in self._STD or k.startswith("_") or k in ("request_id", "asctime"):
                continue
            if isinstance(v, str):
                extra.append(f"{k}={v}")
            elif isinstance(v, (int, float, bool)) or v is None:
                extra.append(f"{k}={v}")
            else:
                try:
                    extra.append(f"{k}={json.dumps(v, ensure_ascii=False)}")
                except Exception:  # noqa: BLE001 不可序列化则 repr
                    extra.append(f"{k}={v!r}")
        return text + ("  " + " ".join(extra) if extra else "")


def setup_logger(name: str = "super_investment_agent") -> logging.Logger:
    logger = logging.getLogger(name)
    logger.setLevel(logging.INFO)

    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setLevel(logging.DEBUG)
        formatter = _RequestIdFormatter(
            "%(asctime)s | %(levelname)-8s | %(name)s | %(request_id)s%(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
        handler.setFormatter(formatter)
        logger.addHandler(handler)

    return logger


logger = setup_logger()
