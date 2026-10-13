import argparse
import asyncio
import json
import re
import time
from datetime import datetime
from pathlib import Path

from agent import analyser_agent
from utils import DATA_DIR, load_jsonl


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--analyser-llm", required=True)
    parser.add_argument("--analyser-llm-args", type=json.loads, default={})
    parser.add_argument("--max-concurrency", type=int, default=8)
    parser.add_argument("--output-path")
    parser.add_argument("--limit", type=int)
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
    run["results"] = [r for r in run["results"] if "report" in r]
    done = {r["id"] for r in run["results"]}
    records = [r for r in load_jsonl(DATA_DIR / "judge_inputs.jsonl") if r["id"] not in done][: args.limit]
    print(f"{len(records)} trajectories to analyse ({len(done)} already done) -> {output_path}")

    semaphore = asyncio.Semaphore(args.max_concurrency)

    async def analyse(record):
        async with semaphore:
            start = time.time()
            try:
                result = await analyser_agent(record, args.analyser_llm, args.analyser_llm_args)
                print(f"{record['id']}: {result['report']['overall_status']} ({result['steps']} steps, ${result['cost']:.4f}, {time.time() - start:.0f}s)")
            except Exception as e:
                result = {"error": f"{type(e).__name__}: {e}"}
                print(f"{record['id']}: ERROR {result['error']}")
            run["results"].append({"id": record["id"], **result})
            run["cost"] = sum(r.get("cost", 0.0) for r in run["results"])
            output_path.write_text(json.dumps(run, indent=2, ensure_ascii=False))

    await asyncio.gather(*(analyse(r) for r in records))


if __name__ == "__main__":
    asyncio.run(main())
