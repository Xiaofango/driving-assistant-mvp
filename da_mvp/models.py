from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Request:
    session_id: str
    user_id: str
    utterance: str
    context: dict[str, Any] = field(default_factory=dict)
    timestamp: int = 0

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Request":
        if not isinstance(data, dict):
            raise ValueError("请求必须是 JSON 对象")
        for key in ("session_id", "user_id", "utterance"):
            if not isinstance(data.get(key), str) or not data[key].strip():
                raise ValueError(f"{key} 必须是非空字符串")
        context = data.get("context", {})
        if not isinstance(context, dict):
            raise ValueError("context 必须是对象")
        timestamp = data.get("timestamp", 0)
        if not isinstance(timestamp, int) or isinstance(timestamp, bool):
            raise ValueError("timestamp 必须是 Unix 秒整数")
        return cls(data["session_id"], data["user_id"], data["utterance"], context, timestamp)


@dataclass(frozen=True)
class Result:
    session_id: str
    reply_text: str
    action_result: str
    action_detail: dict[str, Any]
    need_follow_up: bool
    timestamp: int

    def to_dict(self) -> dict[str, Any]:
        return vars(self)

