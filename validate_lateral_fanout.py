import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from deep_think_mcp.api import reasoning


class FakeMCP:
    def __init__(self):
        self.tools = {}

    def tool(self):
        def decorator(func):
            self.tools[func.__name__] = func
            return func

        return decorator


async def main():
    fake = FakeMCP()
    reasoning.register(fake)

    deep_think_fan_out = fake.tools["deep_think_fan_out"]
    get_thinking_result = fake.tools["get_thinking_result"]

    queued = await deep_think_fan_out(
        question=(
            "Review deep_think_mcp for one grounded risk in engine/orchestrator.py "
            "with exact file:line citation. If not grounded, return INSUFFICIENT_EVIDENCE."
        ),
        width=2,
        height=1,
        task_class="code_review",
        data_policy="local",
        enable_tool_use=True,
        provider_config={
            "provider": "ollama",
            "base_url": "http://100.73.200.19:11434",
            "light": "heretic-phi4-mini-reasoning:latest",
            "medium": "heretic-llama31-8b-instruct:latest",
            "heavy": "heretic-llama31-8b-instruct:latest",
        },
    )

    job_id = queued.get("job_id")
    result = {"status": "failed", "error": "missing job_id from queue response"}
    if job_id:
        for _ in range(20):
            polled = await get_thinking_result(job_id)
            if polled.get("status") in {"complete", "failed"}:
                result = polled
                break
            await asyncio.sleep(3)
        else:
            result = polled

    compact = {
        "status": result.get("status"),
        "error": result.get("error"),
        "inference_only": result.get("inference_only"),
        "grounding_warnings": result.get("grounding_warnings"),
        "tool_successes_total": result.get("tool_successes_total"),
        "perspectives_succeeded": result.get("perspectives_succeeded"),
        "perspectives": result.get("perspectives"),
    }
    print(json.dumps(compact, separators=(",", ":")))


if __name__ == "__main__":
    asyncio.run(main())
