import json
import re
from pathlib import Path

from rank_bm25 import BM25Okapi

DATA_DIR = Path(__file__).parent.parent / "data"
PROMPTS_DIR = Path(__file__).parent.parent / "prompts"


def load_jsonl(path):
    with open(path) as f:
        return [json.loads(line) for line in f if line.strip()]


def load_system_prompt():
    return (PROMPTS_DIR / "trace_analyser_system.md").read_text()


def load_common():
    return json.loads((DATA_DIR / "common.json").read_text())


def tokenize(text):
    return re.findall(r"\w+", text.lower())


class KnowledgeBase:
    def __init__(self, docs_dir=DATA_DIR / "documents", k=5):
        self.docs = [json.loads(p.read_text()) for p in sorted(Path(docs_dir).glob("*.json"))]
        self.bm25 = BM25Okapi([tokenize(f"{d['title']}\n{d['content']}") for d in self.docs])
        self.k = k

    def search(self, query):
        scores = self.bm25.get_scores(tokenize(query))
        top = sorted(range(len(self.docs)), key=lambda i: scores[i], reverse=True)[: self.k]
        return "\n\n".join(
            f"{rank}. {self.docs[i]['title']}\n   ID: {self.docs[i]['id']}\n   Score: {scores[i]:.4f}\n   Content:\n{self.docs[i]['content']}"
            for rank, i in enumerate(top, 1)
        )


def format_analyser_input(record, common):
    lines = []
    for m in record["messages"]:
        if m["role"] == "user":
            lines.append(f"[{m['id']}] Customer:\n{m['content']}")
        elif m["role"] == "tool":
            lines.append(f"[{m['id']}] Tool result:\n{m['content']}")
        else:
            if m.get("content"):
                lines.append(f"[{m['id']}] Agent:\n{m['content']}")
            for tc in m.get("tool_calls") or []:
                lines.append(f"[{tc['id']}] Agent tool call: {tc['function']['name']}\nArguments: {tc['function']['arguments']}")
    trajectory = "\n\n".join(lines)
    tools = json.dumps(common["tools"], indent=2)
    return (
        f"# Agent system prompt\n\n<agent_system_prompt>\n{common['system']['content']}\n</agent_system_prompt>\n\n"
        f"# Agent tool definitions\n\n<agent_tools>\n{tools}\n</agent_tools>\n\n"
        f"# Trajectory\n\n<trajectory>\n{trajectory}\n</trajectory>\n\n"
        "Audit this trajectory. Search the knowledge base as needed, then return the JSON report."
    )
