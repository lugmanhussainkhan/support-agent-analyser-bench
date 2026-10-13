import argparse
import asyncio
import json
import re
import time
from datetime import datetime
from pathlib import Path

from agent import analyser_agent
from evaluator import evaluate, summarize
from utils import DATA_DIR, load_jsonl


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--analyser-llm", required=True)
    parser.add_argument("--analyser-llm-args", type=json.loads, default={})
    parser.add_argument("--evaluator-llm", help="Skip evaluation if not set")
    parser.add_argument("--evaluator-llm-args", type=json.loads, default={})
    parser.add_argument("--max-concurrency", type=int, default=2)
    parser.add_argument("--output-path", help="Pass an existing run file to resume it")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--ids", type=lambda v: v.split(","), help="Comma-separated trajectory IDs to run")
    args = parser.parse_args()

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_path = Path(args.output_path or f"results/{re.sub(r'[^a-z0-9]+', '-', args.analyser_llm.lower()).strip('-')}_{timestamp}.json")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    run = json.loads(output_path.read_text()) if output_path.exists() else {
        "analyser_llm": args.analyser_llm,
        "analyser_llm_args": args.analyser_llm_args,
        "started_at": timestamp,
        "results": [],
    }
    run.update(evaluator_llm=args.evaluator_llm, evaluator_llm_args=args.evaluator_llm_args)
    results = {r["id"]: r for r in run["results"] if "report" in r}
    records = [r for r in load_jsonl(DATA_DIR / "judge_inputs.jsonl") if not args.ids or r["id"] in args.ids][: args.limit]
    golds = {g["id"]: g for g in load_jsonl(DATA_DIR / "gold_judgements.jsonl")}
    print(f"{len(records)} trajectories ({len(results)} already analysed) -> {output_path}")

    semaphore = asyncio.Semaphore(args.max_concurrency)

    def save():
        run["results"] = list(results.values())
        run["analyser_cost"] = sum(r.get("cost", 0.0) for r in run["results"])
        if args.evaluator_llm:
            run["metrics"] = summarize([r["evaluation"] for r in run["results"] if "evaluation" in r])
        output_path.write_text(json.dumps(run, indent=2, ensure_ascii=False))

    async def process(record):
        async with semaphore:
            rid = record["id"]
            if rid not in results:
                start = time.time()
                try:
                    results[rid] = {"id": rid, **await analyser_agent(record, args.analyser_llm, args.analyser_llm_args)}
                    print(f"{rid}: {results[rid]['report']['overall_status']} ({results[rid]['steps']} steps, ${results[rid]['cost']:.4f}, {time.time() - start:.0f}s)")
                except Exception as e:
                    results[rid] = {"id": rid, "error": f"{type(e).__name__}: {e}"}
                    print(f"{rid}: ERROR {results[rid]['error']}")
            result = results[rid]
            if args.evaluator_llm and "report" in result and "evaluation" not in result:
                try:
                    result.pop("evaluation_error", None)
                    result["evaluation"] = await evaluate(record, result["report"], golds[rid], args.evaluator_llm, args.evaluator_llm_args)
                    ev = result["evaluation"]
                    matched = sum(m["matched_finding"] is not None for m in ev["error_matches"])
                    print(f"{rid}: eval status {ev['model_status']}/{ev['gold_status']}, errors {matched}/{len(ev['error_matches'])}, first error {ev['first_error']['closeness']}")
                except Exception as e:
                    result["evaluation_error"] = f"{type(e).__name__}: {e}"
                    print(f"{rid}: EVAL ERROR {result['evaluation_error']}")
            save()

    await asyncio.gather(*(process(r) for r in records))
    if args.evaluator_llm:
        print(json.dumps(run["metrics"], indent=2))


if __name__ == "__main__":
    asyncio.run(main())
