"""A small local vector store over the ``knowledge/`` markdown files.

Why local and TF-IDF?
    It runs anywhere with zero extra dependencies or API keys, is deterministic (good for
    tests and for defending the demo), and the index is persisted to disk so it is not
    rebuilt on every run. In Foundry mode the *same files* are also uploaded to a Foundry
    vector store and attached to the Anomaly & Coverage agent through the ``file_search``
    tool (see ``backends/foundry_backend.py``). Swapping this class for embeddings
    (Azure OpenAI ``text-embedding-3-small``) or Azure AI Search only needs a new
    implementation of :meth:`search`.

Chunking: one chunk per ``##`` section, because each rule / pattern is a self-contained
section. Section IDs (``UW-001``, ``FI-03``) are kept as metadata for citations.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path

_TOKEN = re.compile(r"[a-z0-9]+(?:[-_][a-z0-9]+)*")
_STOP = set(
    "a an the and or of to in on for is are be by with as at from that this it its if any should "
    "must within than more not no into their they there which when was were has have been".split()
)
_ID = re.compile(r"^##\s+([A-Z]{2}-\d{2,3})\b")


def tokenize(text: str) -> list[str]:
    out = []
    for tok in _TOKEN.findall(text.lower()):
        if tok in _STOP:
            continue
        out.append(tok)
        if "_" in tok or "-" in tok:  # water_damage -> water, damage as well
            out.extend(t for t in re.split(r"[-_]", tok) if t and t not in _STOP)
    return out


@dataclass
class Chunk:
    chunk_id: str
    source: str
    title: str
    text: str


class LocalVectorStore:
    def __init__(self, knowledge_dir: Path, cache_path: Path | None = None) -> None:
        self.knowledge_dir = Path(knowledge_dir)
        self.cache_path = cache_path
        self.chunks: list[Chunk] = []
        self._idf: dict[str, float] = {}
        self._vectors: list[dict[str, float]] = []
        self._build_or_load()

    # ---------------------------------------------------------------- build
    def _fingerprint(self) -> str:
        h = hashlib.sha256()
        for p in sorted(self.knowledge_dir.glob("*.md")):
            h.update(p.name.encode())
            h.update(p.read_bytes())
        return h.hexdigest()

    def _build_or_load(self) -> None:
        fp = self._fingerprint()
        if self.cache_path and self.cache_path.exists():
            try:
                cached = json.loads(self.cache_path.read_text(encoding="utf-8"))
                if cached.get("fingerprint") == fp:
                    self.chunks = [Chunk(**c) for c in cached["chunks"]]
                    self._idf = cached["idf"]
                    self._vectors = cached["vectors"]
                    return
            except Exception:
                pass  # corrupt cache -> rebuild
        self._build()
        if self.cache_path:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            self.cache_path.write_text(
                json.dumps(
                    {
                        "fingerprint": fp,
                        "chunks": [asdict(c) for c in self.chunks],
                        "idf": self._idf,
                        "vectors": self._vectors,
                    }
                ),
                encoding="utf-8",
            )

    def _build(self) -> None:
        self.chunks = []
        for path in sorted(self.knowledge_dir.glob("*.md")):
            self.chunks.extend(self._chunk_file(path))
        docs = [Counter(tokenize(c.title + " " + c.text)) for c in self.chunks]
        n = len(docs)
        df: Counter[str] = Counter()
        for d in docs:
            df.update(d.keys())
        self._idf = {t: math.log((1 + n) / (1 + f)) + 1.0 for t, f in df.items()}
        self._vectors = [self._weigh(d) for d in docs]

    @staticmethod
    def _chunk_file(path: Path) -> list[Chunk]:
        chunks: list[Chunk] = []
        current_title, current_lines, current_id = None, [], None
        counter = 0

        def flush() -> None:
            nonlocal counter
            if current_title and any(line.strip() for line in current_lines):
                counter += 1
                cid = current_id or f"{path.stem}#{counter}"
                chunks.append(Chunk(cid, path.name, current_title, "\n".join(current_lines).strip()))

        for line in path.read_text(encoding="utf-8").splitlines():
            if line.startswith("## "):
                flush()
                current_title = line[3:].strip()
                m = _ID.match(line)
                current_id = m.group(1) if m else None
                current_lines = []
            elif current_title is not None:
                current_lines.append(line)
        flush()
        return chunks

    def _weigh(self, counts: Counter[str]) -> dict[str, float]:
        vec = {t: (1 + math.log(c)) * self._idf.get(t, 0.0) for t, c in counts.items()}
        norm = math.sqrt(sum(v * v for v in vec.values())) or 1.0
        return {t: v / norm for t, v in vec.items()}

    # --------------------------------------------------------------- query
    def search(self, query: str, top_k: int = 3, source: str | None = None) -> list[dict]:
        q = self._weigh(Counter(tokenize(query)))
        scored = []
        for chunk, vec in zip(self.chunks, self._vectors):
            if source and chunk.source != source:
                continue
            score = sum(w * vec.get(t, 0.0) for t, w in q.items())
            if score > 0:
                scored.append((score, chunk))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [
            {"chunk_id": c.chunk_id, "source": c.source, "title": c.title, "score": round(s, 3), "text": c.text}
            for s, c in scored[:top_k]
        ]

    def get(self, chunk_id: str) -> Chunk | None:
        return next((c for c in self.chunks if c.chunk_id == chunk_id), None)

    def by_source(self, source: str) -> list[Chunk]:
        return [c for c in self.chunks if c.source == source]
