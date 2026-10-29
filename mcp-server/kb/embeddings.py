"""
Voyage AI embeddings over plain HTTP.

Deliberately not using the `voyageai` SDK: it pulls in a large dependency tree
(huggingface_hub, tokenizers, ...) that would bloat the Vercel bundle, and all
we need is one POST endpoint. httpx is already installed as a dependency of
the MCP SDK.

input_type matters: Voyage embeds documents and queries slightly differently
(asymmetric retrieval). Use "document" when ingesting, "query" at search time.
"""

import time

import httpx

from kb.config import voyage_api_key

VOYAGE_URL = "https://api.voyageai.com/v1/embeddings"

# Changing the model means every stored vector is incompatible with new query
# vectors — the ingest script records this on each chunk and re-embeds
# everything when it changes.
EMBEDDING_MODEL = "voyage-3.5"
EMBEDDING_DIMENSIONS = 1024

# Batching limits. Voyage allows up to 1000 inputs and (for voyage-3.5)
# 320K tokens per request; we stay well under both.
BATCH_SIZE = 128
MAX_BATCH_TOKENS = 100_000

# Throttled mode, for accounts without a payment method, which Voyage limits
# to roughly 3 requests/min and 10K tokens/min. Small batches, paced so both
# limits hold.
THROTTLED_BATCH_TOKENS = 8_000
THROTTLED_TOKENS_PER_MIN = 9_000
THROTTLED_MIN_INTERVAL_S = 21

MAX_RETRIES = 8


def estimate_tokens(text: str) -> int:
    """Rough chars/4 estimate — good enough for batching and pacing."""
    return max(1, len(text) // 4)


def _batches(texts: list[str], max_items: int, max_tokens: int):
    """Yield (start, end) index ranges respecting both item and token budgets."""
    start, tokens = 0, 0
    for i, t in enumerate(texts):
        n = estimate_tokens(t)
        if i > start and (i - start >= max_items or tokens + n > max_tokens):
            yield start, i
            start, tokens = i, 0
        tokens += n
    if start < len(texts):
        yield start, len(texts)


def _post(client: httpx.Client, texts: list[str], input_type: str, max_retries: int = MAX_RETRIES) -> list[list[float]]:
    payload = {"input": texts, "model": EMBEDDING_MODEL, "input_type": input_type}
    delay = 2.0
    for attempt in range(1, max_retries + 1):
        resp = client.post(VOYAGE_URL, json=payload)
        if resp.status_code == 200:
            data = sorted(resp.json()["data"], key=lambda d: d["index"])
            return [d["embedding"] for d in data]
        # 429 = rate limited; 5xx = transient. Back off and retry.
        if resp.status_code == 429 or resp.status_code >= 500:
            wait = float(resp.headers.get("retry-after", delay))
            if attempt == max_retries:
                break
            print(f"  Voyage {resp.status_code}, retrying in {wait:.0f}s (attempt {attempt}/{max_retries})")
            time.sleep(wait)
            delay = min(delay * 2, 60)
            continue
        raise RuntimeError(f"Voyage error {resp.status_code}: {resp.text[:300]}")
    raise RuntimeError(
        "Voyage: gave up after repeated rate-limit errors. Without a payment method on "
        "the Voyage account, run ingest with --throttle (or add a payment method)."
    )


def embed_batches(texts: list[str], input_type: str = "document", throttle: bool = False):
    """
    Generator: yields (start_index, vectors) per batch, so callers can persist
    progress as it goes. throttle=True paces requests for free-tier limits.
    """
    if input_type not in ("document", "query"):
        raise ValueError("input_type must be 'document' or 'query'")
    max_tokens = THROTTLED_BATCH_TOKENS if throttle else MAX_BATCH_TOKENS
    headers = {"Authorization": f"Bearer {voyage_api_key()}"}
    ranges = list(_batches(texts, BATCH_SIZE, max_tokens))
    with httpx.Client(headers=headers, timeout=60) as client:
        for n, (a, b) in enumerate(ranges, 1):
            batch = texts[a:b]
            t0 = time.monotonic()
            vectors = _post(client, batch, input_type)
            yield a, vectors
            if throttle and n < len(ranges):
                tokens = sum(estimate_tokens(t) for t in batch)
                pace = max(THROTTLED_MIN_INTERVAL_S, tokens / THROTTLED_TOKENS_PER_MIN * 60)
                wait = pace - (time.monotonic() - t0)
                if wait > 0:
                    print(f"  batch {n}/{len(ranges)} done, pausing {wait:.0f}s for rate limit")
                    time.sleep(wait)


def embed(texts: list[str], input_type: str = "document", throttle: bool = False) -> list[list[float]]:
    """Embed any number of texts. Order is preserved."""
    out: list[list[float]] = []
    for _, vectors in embed_batches(texts, input_type, throttle):
        out.extend(vectors)
    return out


# Query-time settings. A live tool call must not sit in a minutes-long retry
# loop like ingestion can: one quick retry, short timeout, then raise so the
# server falls back to its on-disk keyword search.
QUERY_TIMEOUT_S = 8
QUERY_MAX_RETRIES = 2


def embed_query(text: str) -> list[float]:
    headers = {"Authorization": f"Bearer {voyage_api_key()}"}
    with httpx.Client(headers=headers, timeout=QUERY_TIMEOUT_S) as client:
        return _post(client, [text], "query", max_retries=QUERY_MAX_RETRIES)[0]
