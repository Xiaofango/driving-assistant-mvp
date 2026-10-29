"""Run with: python -m da_mvp.demo"""

from json import dumps
from datetime import datetime, timezone
from .gateway import InMemoryGateway
from .service import DrivingAssistant


def main() -> None:
    gateway = InMemoryGateway()
    assistant = DrivingAssistant(gateway)
    for i, utterance in enumerate(("换一条更快的路", "跟车距离调远一点", "调一下那个"), 1):
        request = {"session_id": f"demo_{i}", "user_id": "demo_user", "utterance": utterance,
                   "context": {"turn_count": i}, "timestamp": int(datetime.now(timezone.utc).timestamp())}
        print(dumps({"input": utterance, "output": assistant.handle(request)}, ensure_ascii=False))
    gateway.push_event("takeover_required", "critical", "请接管")
    print(dumps({"event": "takeover_required", "output": assistant.handle(request)}, ensure_ascii=False))


if __name__ == "__main__":
    main()

