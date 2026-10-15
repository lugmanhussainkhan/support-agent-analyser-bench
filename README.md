# support-agent-analyser-bench

A benchmark for how well LLMs analyse customer-support agent trajectories.

The analyser model gets one recorded conversation between a bank's support agent and a customer, plus the agent's system prompt and tool definitions. It audits the conversation against the bank's policy and returns a JSON report: the errors the agent made, the first critical error, and an overall `pass`/`fail` verdict. The detailed policies are in a knowledge base, which the analyser has to search for itself. The report is then scored against expert gold judgements.

The trajectories come from the tau-knowledge `banking_knowledge` domain.

## Data

| Path                                  | Contents                                                                                                                               |
| ------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------- |
| `data/judge_inputs.jsonl`             | 100 trajectories (`id`, `messages`). Every item has an ID such as `user_msg_3`, `assistant_msg_5`, `tool_call_12` or `tool_result_12`. |
| `data/gold_judgements.jsonl`          | Gold verdict, errors (critical and minor) and first critical error for each trajectory                                                 |
| `data/common.json`                    | The support agent's system prompt and tool definitions, shared by every trajectory                                                     |
| `data/documents/`                     | The knowledge base: 698 policy documents                                                                                               |
| `prompts/trace_analyser_system.md`    | Analyser system prompt and output schema                                                                                               |
| `prompts/error_match_system.md`       | Evaluator prompt for matching one gold error to the analyser's findings                                                                |
| `prompts/first_error_match_system.md` | Evaluator prompt for judging the analyser's first critical error                                                                       |

## How it works

**Analyser** (`src/agent.py`): a tool-calling loop through litellm with a single tool, `search_kb`. This is a BM25 search (`rank_bm25`) over the knowledge base that returns the top 5 documents. The loop runs until the model returns a report, up to 50 steps. The report is checked against the `AnalysisReport` schema, which is sent as `response_format`, and schema errors are sent back to the model. Messages are only ever appended, and cache breakpoints sit on the system prompt and the latest message, so prompt caching works across steps.

**Evaluator** (`src/evaluator.py`), for each trajectory:

1. **Status:** the analyser's `overall_status` is compared with the gold verdict.
2. **Error matching:** for each gold critical error, an LLM decides which of the analyser's findings, if any, describes the same problem.
3. **First error:** if the analyser names a gold item, it's `exact`. Otherwise an LLM labels it `same_error`, `too_early`, `too_late` or `unrelated`. If the analyser names no first error but gold has one, it's `missing`; if the analyser names one but gold has none, it's `no_gold_error`.

The evaluator calls for a trajectory run in parallel.

**Metrics:**

| Metric                           | Meaning                                                              |
| -------------------------------- | -------------------------------------------------------------------- |
| `status_accuracy`                | Analyser verdict equals the gold verdict                             |
| `fail_precision` / `fail_recall` | Detection of failing trajectories, with `fail` as the positive class |
| `gold_critical_error_recall`     | Share of gold critical errors matched by an analyser finding         |
| `first_error_accuracy`           | (`exact` + `same_error`) ÷ gold-fail trajectories                    |

## Setup

```sh
uv sync
```

Put API keys in `.env`:

```
OPENAI_BASE_URL=...
OPENAI_API_KEY=...
ANTHROPIC_API_KEY=...
```

## Running the benchmark

The analyser and evaluator models are any litellm model strings. `--*-llm-args` takes a JSON object that is passed straight to the litellm request (for example reasoning effort, temperature or top_p).

The commands used for the benchmark runs are below. All of them use `gpt-6-luna` at `xhigh` as the evaluator.

```sh
uv run --env-file .env src/main.py \
  --analyser-llm openai/responses/gpt-6.1-sol --analyser-llm-args '{"reasoning_effort": "xhigh"}' \
  --evaluator-llm openai/responses/gpt-6-luna --evaluator-llm-args '{"reasoning_effort": "xhigh"}' \
  --max-concurrency 2
```

| Flag                            | Description                                                                                                                                                                 |
| ------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `--analyser-llm`                | Analyser model (required)                                                                                                                                                   |
| `--analyser-llm-args`           | JSON object of extra litellm arguments for the analyser                                                                                                                     |
| `--no-analyser-response-format` | Don't send the report schema. The model returns the JSON as text, which is still checked against the schema. For providers that can't combine tools with structured output. |
| `--evaluator-llm`               | Evaluator model. If omitted, evaluation is skipped.                                                                                                                         |
| `--evaluator-llm-args`          | JSON object of extra litellm arguments for the evaluator                                                                                                                    |
| `--max-concurrency`             | Trajectories processed at once (default 2)                                                                                                                                  |
| `--output-path`                 | Results file. If the file exists, the run resumes: trajectories that already have a report are skipped, and unevaluated ones are evaluated.                                 |
| `--limit`                       | Only run the first N trajectories                                                                                                                                           |
| `--ids`                         | Comma-separated trajectory IDs to run                                                                                                                                       |

Rate-limit, server, connection and timeout errors are retried with backoff.

## Output

By default each run writes `results/<analyser-model>_<timestamp>.json`. The file is saved after every trajectory and contains the run config, the metrics, and one entry per trajectory with:

- `report`: the analyser's JSON report
- `evaluation`: the status match, error matches and first-error label, with the evaluator's reasoning and cost
- `steps`, `cost`, `usage`: the totals for the analyser
- `transcript`: the analyser's full conversation, with token usage and cost on each assistant message (cost is 0 when the provider doesn't report one)

`results/` is git-ignored.

## Results

100 trajectories (63 gold fail, 37 gold pass). All four runs use `gpt-6-luna` at `xhigh` as the evaluator.

| Analyser            | Effort | Status acc. | Fail precision | Fail recall | Critical-error recall | First-error acc. | Analyser cost |
| ------------------- | ------ | ----------- | -------------- | ----------- | --------------------- | ---------------- | ------------- |
| claude-haiku-5.5    | xhigh  | 0.68        | 0.74           | 0.76        | 0.69                  | 0.54             | $13.56        |
| gpt-6.1-sol         | xhigh  | 0.68        | 0.71           | 0.83        | 0.62                  | 0.57             | $31.63        |
| DeepSeek-V4.1-Flash | high   | 0.62        | 0.79           | 0.54        | 0.45                  | 0.37             | ~$13.70       |
| gpt-6-luna          | xhigh  | 0.62        | 0.70           | 0.70        | 0.42                  | 0.43             | $1.62         |
