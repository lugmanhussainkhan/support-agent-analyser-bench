import asyncio
from collections import Counter
from typing import Literal

from pydantic import BaseModel

from utils import acompletion, item_ids, load_prompt


class ErrorMatch(BaseModel):
    reasoning: str
    matched_finding: str | None


class FirstErrorMatch(BaseModel):
    reasoning: str
    closeness: Literal["same_error", "too_early", "too_late", "unrelated"]


ERROR_MATCH_PROMPT = load_prompt("error_match_system")
FIRST_ERROR_MATCH_PROMPT = load_prompt("first_error_match_system")


async def llm_json(model, system, user, schema, llm_args):
    response = await acompletion(
        model=model,
        messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
        response_format=schema,
        **llm_args,
    )
    u = response.usage
    usage = {
        "prompt_tokens": u.prompt_tokens,
        "completion_tokens": u.completion_tokens,
        "cost": response._hidden_params.get("response_cost") or 0.0,
    }
    return schema.model_validate_json(response.choices[0].message.content), usage


def format_findings(findings):
    return "\n\n".join(
        f"F{i} ({f['severity']}) at {', '.join(f['references'])}\nWhat went wrong: {f['finding']}\nWhat should have happened: {f['expected_behavior']}"
        for i, f in enumerate(findings, 1)
    ) or "None"


def format_gold_errors(errors):
    return "\n\n".join(
        f"G{i} ({e['severity']}) at {', '.join(e['references'])}\n{e['finding']}" for i, e in enumerate(errors, 1)
    ) or "None"


async def evaluate(record, report, gold, model, llm_args=None):
    llm_args = llm_args or {}
    findings = report["errors"]
    finding_ids = {f"F{i}" for i in range(1, len(findings) + 1)}
    gold_critical = [e for e in gold["errors"] if e["severity"] == "critical"]

    async def match_error(error):
        result = {"gold_references": error["references"], "matched_finding": None}
        if not findings:
            return {**result, "reasoning": "The analyst reported no findings."}
        user = (
            f"# Gold error\n\nLocation: {', '.join(error['references'])}\nCategory: {error['category']}\n{error['finding']}\n\n"
            f"# Model findings\n\n{format_findings(findings)}"
        )
        out, usage = await llm_json(model, ERROR_MATCH_PROMPT, user, ErrorMatch, llm_args)
        matched = out.matched_finding if out.matched_finding in finding_ids else None
        return {**result, "matched_finding": matched, "reasoning": out.reasoning, "usage": usage}

    async def match_first_error():
        gold_first, model_first = gold["first_critical_error"], report["first_critical_error"]
        result = {"gold": gold_first and gold_first["references"], "model": model_first}
        if gold_first is None or model_first is None:
            closeness = None if gold_first is None and model_first is None else "missing" if model_first is None else "no_gold_error"
            return {**result, "closeness": closeness}
        if model_first in gold_first["references"]:
            return {**result, "closeness": "exact"}
        order = item_ids(record)
        if model_first in order:
            offset = order.index(model_first) - order.index(gold_first["earliest_reference"])
            position = f"The analyst's item is {abs(offset)} items {'after' if offset > 0 else 'before'} the gold item."
        else:
            position = "The analyst's item does not appear in the conversation."
        referencing = [f for f in findings if model_first in f["references"]]
        user = (
            f"# Gold first critical error\n\nItems: {', '.join(gold_first['references'])}\nCategory: {gold_first['category']}\n{gold_first['description']}\n\n"
            f"# Relative position\n\n{position}\n\n"
            f"# Model first critical error\n\nItem: {model_first}\nFindings referencing it:\n\n{format_findings(referencing)}\n\n"
            f"# Context\n\n## Gold errors\n\n{format_gold_errors(gold['errors'])}\n\n## Model findings\n\n{format_findings(findings)}"
        )
        out, usage = await llm_json(model, FIRST_ERROR_MATCH_PROMPT, user, FirstErrorMatch, llm_args)
        return {**result, "closeness": out.closeness, "reasoning": out.reasoning, "usage": usage}

    *error_matches, first_error = await asyncio.gather(*(match_error(e) for e in gold_critical), match_first_error())
    cost = sum(x.get("usage", {}).get("cost", 0.0) for x in [*error_matches, first_error])
    return {
        "gold_status": gold["verdict"],
        "model_status": report["overall_status"],
        "status_match": gold["verdict"] == report["overall_status"],
        "error_matches": error_matches,
        "first_error": first_error,
        "cost": cost,
    }


def summarize(evaluations):
    n = len(evaluations)
    confusion = Counter(f"gold_{e['gold_status']}__model_{e['model_status']}" for e in evaluations)
    tp, fp, fn = confusion["gold_fail__model_fail"], confusion["gold_pass__model_fail"], confusion["gold_fail__model_pass"]
    matches = [m for e in evaluations for m in e["error_matches"]]
    gold_fails = [e for e in evaluations if e["gold_status"] == "fail"]
    closeness = Counter(e["first_error"]["closeness"] for e in gold_fails)
    return {
        "n": n,
        "status_accuracy": sum(e["status_match"] for e in evaluations) / n if n else None,
        "fail_precision": tp / (tp + fp) if tp + fp else None,
        "fail_recall": tp / (tp + fn) if tp + fn else None,
        "confusion": dict(confusion),
        "gold_critical_errors": len(matches),
        "gold_critical_error_recall": sum(m["matched_finding"] is not None for m in matches) / len(matches) if matches else None,
        "first_error_accuracy": (closeness["exact"] + closeness["same_error"]) / len(gold_fails) if gold_fails else None,
        "first_error_closeness": dict(closeness),
        "evaluator_cost": sum(e["cost"] for e in evaluations),
    }
