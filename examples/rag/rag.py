"""Router demo: answer questions over public whole-life-cost guidance PDFs.

    pip install -e ".[rag]"
    python examples/rag/rag.py ask "What is a Should Cost Model?"
    python examples/rag/rag.py eval

The first run downloads the PDFs and a small embedding model, then builds the
index. Retrieval is always real and free. The answer is simulated unless you pass
--paid, which makes one real, billed model call. See examples/rag/README.md.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import math
import os
import re
import sys
import urllib.request
from collections import Counter
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from router.api import Result, Router, RulesPolicy  # noqa: E402
from router.classifier import heuristic_classify  # noqa: E402
from router.untrusted import IGNORE_NOTE, fence  # noqa: E402

HERE = Path(__file__).resolve().parent
DOCS_DIR = HERE / "docs"
INDEX_PATH = HERE / ".index.json"
EVAL_PATH = HERE / "eval_questions.json"
EMBED_MODEL = "BAAI/bge-small-en-v1.5"
CHUNK_CHARS = 800
OVERLAP_CHARS = 150
MIN_CHUNK_CHARS = 100  # shorter text is page headers and footers, not content
TOP_K = 4
BM25_K1, BM25_B = 1.5, 0.75  # the usual defaults
RRF_K = 60  # the usual default

# All three carry the Open Government Licence. The hashes pin the exact files.
ASSETS = "https://assets.publishing.service.gov.uk"
DOCS = {
    "construction_playbook.pdf": (
        ASSETS + "/media/6312222de90e075880923330/14.116_CO_Construction_Playbook_Web.pdf",
        "e0d219f95cf1458b3ccc03e9578e60c0a16ded9090ec2b1cf092624fa233cac3"),
    "ipa_cost_estimating_guidance.pdf": (
        ASSETS + "/government/uploads/system/uploads/attachment_data/file/970022/IPA_Cost_Estimating_Guidance.pdf",
        "eaad187f9e511f50659c55415be2245fc40fddf7f02d301f9cd709359581c989"),
    "construction_common_minimum_standards.pdf": (
        ASSETS + "/media/5a81c330e5274a2e8ab55afa/2017-03-15_Construction_Common__Minimum_Standards__final___1_.pdf",
        "554c4df9c4dbd6f1b0cf4147e1d6116906f130fc68f20a76760d023996278977"),
}

Chunk = dict[str, Any]
Embed = Callable[[list[str]], list[list[float]]]


def fetch_docs() -> None:
    """Download any missing PDF and refuse one whose hash does not match."""
    DOCS_DIR.mkdir(exist_ok=True)
    for name, (url, sha256) in DOCS.items():
        path = DOCS_DIR / name
        if not path.exists():
            print(f"Downloading {name}")
            with urllib.request.urlopen(url, timeout=120) as response:
                data = response.read()
            if hashlib.sha256(data).hexdigest() != sha256:
                raise SystemExit(f"{name} does not match its pinned hash; not saving it.")
            path.write_bytes(data)


def chunk_text(text: str, size: int = CHUNK_CHARS, overlap: int = OVERLAP_CHARS) -> list[str]:
    """Split text into overlapping pieces, so a sentence on a boundary stays whole in one."""
    words = " ".join(text.split())
    chunks: list[str] = []
    start = 0
    while start < len(words):
        end = min(start + size, len(words))
        if end < len(words):
            space = words.rfind(" ", start + 1, end)
            end = space if space != -1 else end
        chunks.append(words[start:end])
        if end == len(words):
            break
        start = max(end - overlap, start + 1)
    return chunks


def read_chunks() -> list[Chunk]:
    """Turn every PDF into chunks that remember their document and page."""
    from pypdf import PdfReader

    logging.getLogger("pypdf").setLevel(logging.ERROR)
    chunks: list[Chunk] = []
    for name in DOCS:
        for page, content in enumerate(PdfReader(DOCS_DIR / name).pages, 1):
            for text in chunk_text(content.extract_text() or ""):
                if len(text) >= MIN_CHUNK_CHARS:
                    chunks.append({"doc": name, "page": page, "text": text})
    return chunks


def embed_passages(texts: list[str]) -> list[list[float]]:
    """Embed document text with a small local model: free, and nothing leaves this machine."""
    from fastembed import TextEmbedding

    return [v.tolist() for v in TextEmbedding(EMBED_MODEL).passage_embed(texts)]


def embed_queries(texts: list[str]) -> list[list[float]]:
    """Embed questions. This model encodes them differently from the passages they match."""
    from fastembed import TextEmbedding

    return [v.tolist() for v in TextEmbedding(EMBED_MODEL).query_embed(texts)]


def build_index(embed: Embed = embed_passages) -> list[Chunk]:
    """Fetch, chunk and embed the PDFs, then save the result as one JSON file."""
    fetch_docs()
    chunks = read_chunks()
    print(f"Embedding {len(chunks)} chunks")
    for chunk, vector in zip(chunks, embed([c["text"] for c in chunks])):
        chunk["vector"] = vector
    INDEX_PATH.write_text(json.dumps(chunks))
    return chunks


def load_index() -> list[Chunk]:
    """Load the saved index, building it on first use."""
    return json.loads(INDEX_PATH.read_text()) if INDEX_PATH.exists() else build_index()


def embedding_ranking(query_vector: list[float], chunks: list[Chunk]) -> list[int]:
    """Chunk positions, best first, by meaning."""
    # The model outputs unit-length vectors, so a dot product is cosine similarity.
    # ponytail: exact scan of every chunk. Fine for hundreds; use a vector index at ~1M.
    scores = [sum(a * b for a, b in zip(query_vector, c["vector"])) for c in chunks]
    return sorted(range(len(chunks)), key=lambda i: -scores[i])


def keyword_ranking(question: str, chunks: list[Chunk]) -> list[int]:
    """Chunk positions, best first, by BM25: rare words from the question count most."""
    docs = [Counter(re.findall(r"\w+", c["text"].lower())) for c in chunks]
    average_length = sum(sum(d.values()) for d in docs) / len(docs)
    scores = [0.0] * len(docs)
    for term in set(re.findall(r"\w+", question.lower())):
        matches = sum(term in d for d in docs)
        rarity = math.log(1 + (len(docs) - matches + 0.5) / (matches + 0.5))
        for i, d in enumerate(docs):
            length_norm = 1 - BM25_B + BM25_B * sum(d.values()) / average_length
            scores[i] += rarity * d[term] * (BM25_K1 + 1) / (d[term] + BM25_K1 * length_norm)
    return sorted(range(len(docs)), key=lambda i: -scores[i])


def retrieve(question: str, query_vector: list[float], chunks: list[Chunk], k: int = TOP_K) -> list[Chunk]:
    """Return the top k chunks, merging both rankings by reciprocal rank fusion."""
    fused = [0.0] * len(chunks)
    for ranking in (embedding_ranking(query_vector, chunks), keyword_ranking(question, chunks)):
        for rank, i in enumerate(ranking):
            fused[i] += 1 / (RRF_K + rank + 1)
    best = sorted(range(len(chunks)), key=lambda i: -fused[i])[:k]
    return [{**chunks[i], "score": fused[i]} for i in best]


def build_prompt(question: str, hits: list[Chunk]) -> str:
    """Ask for an answer from the sources alone, with PDF text fenced as untrusted data."""
    sources = "\n\n".join(
        fence(f"[{i}] {h['doc']}, page {h['page']}\n{h['text']}", "source")
        for i, h in enumerate(hits, 1))
    return (f"Answer the question using only the sources below, citing them like [1]. "
            f"If they do not contain the answer, say so.\n\n{IGNORE_NOTE}\n\n{sources}\n\n"
            f"{fence(question, 'question')}")


def answer(question: str, hits: list[Chunk]) -> Result:
    """Route on the question alone, then answer with the sources attached."""
    # Difficulty is judged partly by length, so classifying the padded prompt would
    # call every question "hard" and send it to the most expensive model.
    c = heuristic_classify(question)
    task = {"task_type": c.task_type, "difficulty": c.difficulty}
    return Router(RulesPolicy(), use_cache=False).complete(build_prompt(question, hits), task)


def ask(question: str, k: int) -> None:
    """Print the retrieved passages, the routing decision and the answer."""
    chunks = load_index()
    hits = retrieve(question, embed_queries([question])[0], chunks, k)
    print(f"\nRetrieved {len(hits)} of {len(chunks)} chunks:")
    for i, h in enumerate(hits, 1):
        print(f"  [{i}] {h['score']:.3f}  {h['doc']}, page {h['page']}\n      {h['text'][:160]}...")
    result = answer(question, hits)
    print(f"\nRouted to {result.model} ({result.task_type}/{result.difficulty}), "
          f"cost ${result.total_cost_usd:.6f}{' (simulated)' if result.simulated else ''}")
    if result.simulated:
        print("\nNo answer: demo mode calls no model. Use --paid for one real, billed call.")
    else:
        print(f"\nAnswer:\n{result.text}")


def evaluate(k: int) -> None:
    """Measure retrieval: does a passage from the expected page come back in the top k?"""
    chunks = load_index()
    cases = json.loads(EVAL_PATH.read_text())
    vectors = embed_queries([c["question"] for c in cases])
    top1 = topk = 0
    for case, vector in zip(cases, vectors):
        found = [(h["doc"], h["page"]) for h in retrieve(case["question"], vector, chunks, k)]
        want = [(case["doc"], p) for p in case["pages"]]
        rank = next((i for i, f in enumerate(found, 1) if f in want), None)
        top1 += rank == 1
        topk += rank is not None
        print(f"{'miss' if rank is None else f'rank {rank}':7} {case['question']}")
    n = len(cases)
    print(f"\nHit@1: {top1}/{n}. Hit@{k}: {topk}/{n}.")


def main() -> None:
    """Parse the command line and run it."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    ask_parser = commands.add_parser("ask", help="answer one question")
    ask_parser.add_argument("question")
    ask_parser.add_argument("--paid", action="store_true", help="make one real, billed model call")
    commands.add_parser("eval", help="measure retrieval on eval_questions.json")
    args = parser.parse_args()
    if args.command == "eval":
        evaluate(TOP_K)
        return
    if not args.paid:
        os.environ["AWR_FORCE_MOCK"] = "1"
    ask(args.question, TOP_K)


if __name__ == "__main__":
    main()
