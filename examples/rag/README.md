# RAG demo: questions over whole-life-cost guidance

Ask a question about three UK government construction and cost documents. The demo finds the relevant passages, shows them, and routes the answer through the router.

```bash
pip install -e ".[rag]"
```

```bash
python examples/rag/rag.py ask "How should unknown uncertainties in an estimate be corrected?"
```

The first run takes about a minute: it downloads the PDFs and a small embedding model, then builds the index.

**Retrieval is real and free.** Embeddings run on your machine, so no document text is sent anywhere. **Answers are not free.** Without `--paid` no model is called: you get the passages, the routing decision and a simulated cost, but no answer. `--paid` makes one real, billed call using the API keys in `.env`.

## How it works

1. **Fetch:** download each PDF and refuse it if its SHA-256 does not match the pinned hash.
2. **Chunk:** split each page into pieces of about 800 characters that overlap by 150 and remember their page. Pieces under 100 characters are dropped, as they are headers and footers.
3. **Embed:** `BAAI/bge-small-en-v1.5` via `fastembed`, saved to one JSON file. About 490 chunks need no vector database.
4. **Retrieve:** rank every chunk by meaning (embeddings) and by exact words (BM25), then merge the two rankings with reciprocal rank fusion.
5. **Route and answer:** classify the **question alone**, then send the question and top 4 passages to the chosen model. The classifier judges difficulty partly by length, so classifying the padded prompt would call everything hard and send it to the most expensive model.

PDF text is fenced as untrusted data in the prompt, so instructions inside a document are not meant to be followed.

## Measured retrieval

```bash
python examples/rag/rag.py eval
```

`eval_questions.json` holds 13 questions. Each one's answer page was found by reading the PDF text, not by using the retriever. The set was frozen before the three options below were compared.

| Retrieval | Right page ranked first | Right page in top 4 |
|---|---|---|
| Embeddings only | 6/13 | 11/13 |
| Keywords only (BM25) | 9/13 | 12/13 |
| **Hybrid (used here)** | **10/13** | **12/13** |

Embeddings alone struggled with exact jargon such as "Should Cost Model", which is why keyword search was added.

Read these numbers with care:

- 13 questions is a small sample, and one question changes a result by about 8 points.
- I wrote the questions after reading the pages, so they share wording with the documents. That flatters keyword search, and real users would phrase things differently.
- This measures retrieval only. **Answer quality has not been measured.**
- A hit means the expected page came back. Another page that also answers the question counts as a miss.

## Limitations

- "What is a Should Cost Model used for?" misses the top 4 (rank 5).
- Tables and figures extract poorly, and some pages contain garbled text.
- There is no "not found" threshold: it always returns 4 passages, however weak. Only the prompt tells the model to say when the sources do not answer.
- The real `--paid` path has not been run, in line with this project's no-new-API-spend rule. It is covered by offline tests only.

## Documents and licences

The PDFs are downloaded, not stored in this repo. Contains public sector information licensed under the [Open Government Licence](https://www.nationalarchives.gov.uk/doc/open-government-licence/).

| Document | Publisher | Licence stated in the PDF |
|---|---|---|
| The Construction Playbook, v1.1 (2022) | Cabinet Office | OGL v3.0; some images are excepted |
| Cost Estimating Guidance (2021) | Infrastructure and Projects Authority | OGL |
| Common Minimum Standards for Construction (2016) | Infrastructure and Projects Authority | OGL |

**Why there is no BREEAM document:** BRE keeps copyright on its manuals, and their terms restrict other uses. An Environment Agency evidence summary was also rejected because it says all rights are reserved.
