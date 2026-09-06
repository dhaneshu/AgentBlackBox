"""The knowledge base an agent is allowed to draw on.

Markdown files are split into passages at blank lines, and each passage keeps
the line range it came from. That line range is what makes a citation checkable:
"the answer came from plan.md" is a gesture, "the answer came from plan.md:12-14
and here are those lines" is evidence.

Retrieval is lexical coverage, not embeddings. A groundedness checker that
depends on a model needs its own evaluation, which defeats the purpose.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from blackbox.textutil import coverage

_HEADING_PREFIX = "#"


@dataclass(frozen=True)
class Passage:
    """A retrievable block of the knowledge base."""

    source: str
    lines: tuple[int, int]
    text: str
    heading: str = ""

    @property
    def searchable(self) -> str:
        """Heading plus body, so a passage under 'Support' answers 'support hours'."""
        return f"{self.heading} {self.text}".strip()

    @property
    def citation(self) -> str:
        lo, hi = self.lines
        return f"{self.source}:{lo}" if lo == hi else f"{self.source}:{lo}-{hi}"

    def __str__(self) -> str:
        return self.citation


@dataclass(frozen=True)
class Hit:
    passage: Passage
    score: float


class Corpus:
    """Every passage the agent may ground an answer in."""

    def __init__(self, passages: list[Passage]):
        self.passages = passages

    @classmethod
    def load(cls, *paths: str | Path) -> "Corpus":
        passages: list[Passage] = []
        for path in _collect(paths):
            passages.extend(_split(path))
        return cls(passages)

    def search(self, query: str, top_k: int = 3) -> list[Hit]:
        """Highest-coverage passages first. Zero-coverage passages are dropped."""
        hits = [
            Hit(passage, coverage(query, passage.searchable))
            for passage in self.passages
        ]
        hits = [hit for hit in hits if hit.score > 0]
        hits.sort(key=lambda hit: (-hit.score, hit.passage.citation))
        return hits[:top_k]

    def best(self, query: str) -> Hit | None:
        found = self.search(query, top_k=1)
        return found[0] if found else None

    def __len__(self) -> int:
        return len(self.passages)


def _collect(paths) -> list[Path]:
    found: list[Path] = []
    for raw in paths:
        path = Path(raw)
        if path.is_dir():
            found.extend(sorted(p for p in path.rglob("*.md") if p.is_file()))
        elif path.is_file():
            found.append(path)
    return found


def _split(path: Path) -> list[Passage]:
    """Blank-line separated blocks, each carrying its 1-based line range."""
    source = str(path).replace("\\", "/")
    lines = path.read_text(encoding="utf-8").splitlines()

    passages: list[Passage] = []
    heading = ""
    block: list[str] = []
    start = 0

    def flush(end: int) -> None:
        nonlocal block, start
        if block:
            passages.append(
                Passage(source, (start, end), " ".join(block).strip(), heading)
            )
            block = []

    for index, raw in enumerate(lines, start=1):
        text = raw.strip()
        if not text:
            flush(index - 1)
            continue
        if text.startswith(_HEADING_PREFIX):
            flush(index - 1)
            heading = text.lstrip("#").strip()
            continue
        if not block:
            start = index
        block.append(text.lstrip("-*+ ").strip())

    flush(len(lines))
    return passages
