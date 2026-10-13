import json
from typing import Literal

import litellm
from pydantic import BaseModel, ValidationError

from utils import KnowledgeBase, format_analyser_input, load_common, load_system_prompt


class Error(BaseModel):
    references: list[str]
    severity: Literal["critical", "minor"]
    finding: str
    expected_behavior: str
    policy_refs: list[str]
    confidence: int


class AnalysisReport(BaseModel):
    errors: list[Error]
    first_critical_error: str | None
    summary: str
    overall_status: Literal["pass", "fail"]
    confidence: int


SEARCH_TOOL = {
    "type": "function",
    "function": {
        "name": "search_kb",
        "description": "BM25 keyword search over the bank's knowledge base. Returns the top 5 documents with their IDs, titles and full content.",
        "parameters": {
            "type": "object",
            "properties": {"query": {"type": "string", "description": "Keyword search query."}},
            "required": ["query"],
        },
    },
}

KB = KnowledgeBase()
SYSTEM_PROMPT = load_system_prompt()
COMMON = load_common()


async def analyser_agent(record, model, llm_args=None, max_steps=50):
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": format_analyser_input(record, COMMON)},
    ]
    # Cache breakpoints on the static prefix and the latest message; history is only ever appended to.
    params = {
        "cache_control_injection_points": [{"location": "message", "role": "system"}, {"location": "message", "index": -1}],
        **(llm_args or {}),
    }
    usages = {}

    for step in range(max_steps):
        last_step = step == max_steps - 1
        if last_step:
            messages.append({"role": "user", "content": "Search budget exhausted. Return the final JSON report now."})
        response = await litellm.acompletion(
            model=model,
            messages=messages,
            tools=[SEARCH_TOOL],
            tool_choice="none" if last_step else "auto",
            response_format=AnalysisReport,
            **params,
        )
        u = response.usage
        usages[len(messages)] = {
            "prompt_tokens": u.prompt_tokens,
            "completion_tokens": u.completion_tokens,
            "cached_tokens": getattr(u.prompt_tokens_details, "cached_tokens", None) or 0,
            "reasoning_tokens": getattr(u.completion_tokens_details, "reasoning_tokens", None) or 0,
            "cost": response._hidden_params.get("response_cost") or 0.0,
        }

        msg = response.choices[0].message
        messages.append(msg.model_dump(exclude_none=True))

        if msg.tool_calls:
            for tc in msg.tool_calls:
                try:
                    result = KB.search(json.loads(tc.function.arguments)["query"])
                except (json.JSONDecodeError, KeyError, TypeError) as e:
                    result = f"Invalid arguments for search_kb: {e}"
                messages.append({"role": "tool", "tool_call_id": tc.id, "content": result})
            continue

        try:
            report = AnalysisReport.model_validate_json(msg.content or "")
        except ValidationError as e:
            messages.append({"role": "user", "content": f"Your report is not valid against the required schema:\n{e}\nReturn the corrected JSON report."})
            continue
        # Usage is attached only to the saved copy so the messages sent to the model stay unchanged.
        transcript = [{**m, "usage": usages[i]} if i in usages else m for i, m in enumerate(messages)]
        usage = {k: sum(x[k] for x in usages.values()) for k in next(iter(usages.values()))}
        return {"report": report.model_dump(), "steps": step + 1, "cost": usage.pop("cost"), "usage": usage, "transcript": transcript}

    raise RuntimeError(f"No valid report after {max_steps} steps")
