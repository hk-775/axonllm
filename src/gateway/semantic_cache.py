"""Semantic response cache — serve a cached answer to a *reworded* question.

:mod:`cache_manager` keys on a SHA-256 of the request, so "What is our refund
policy?" and "what's the refund policy?" are different keys and both hit the
provider. This module adds a second lookup, tried only after that exact key
misses: embed the prompt, compare against the embeddings of recent cached
prompts, and reuse the response when one is close enough.

The risk this carries and the exact cache does not
-------------------------------------------------
An exact cache can only ever return the answer to the question that was asked.
A semantic cache can return the answer to a *different* question, and the
failure is silent — the caller gets a confident, well-formed, wrong answer with
no indication it was substituted. "What is 17 * 23?" and "What is 17 * 24?" are
one character apart and near-identical to any embedding model; so are "revenue
in Q1" and "revenue in Q2", or "how do I enable X" and "how do I disable X".

So the design is deliberately reluctant:

* **Off unless asked for.** Per-project, defaulting to disabled.
* **A high threshold.** 0.90 cosine. Chosen for its distance from the
  highest-scoring *different*-question pair (0.7476 on the calibration set), not
  for a target hit rate — the cost of a false hit (a wrong answer) is much worse
  than a false miss (a normal API call). See DEFAULT_SIMILARITY_THRESHOLD for
  the measurements.
* **Literal tokens must agree.** Numbers, dates, quoted strings, code
  identifiers and negations are compared exactly, whatever the embedding says.
  This is what stops 17*23 vs 17*24 — the semantic distance is tiny, but the
  numbers differ, so it is never a hit.
* **Whole classes of request are skipped.** Non-zero temperature (the caller
  asked for variety), tools (the answer is a side effect, not text), and
  streaming (a replayed stream is a different shape).

A cache that occasionally lies is worse than no cache, and the number of
requests it saves is not a defence.
"""

from __future__ import annotations

import logging
import math
import re
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from src.gateway.models import ChatCompletionRequest, ChatCompletionResponse

logger = logging.getLogger(__name__)


# Cosine similarity above which two prompts are treated as the same question.
#
# Calibrated against amazon.titan-embed-text-v2:0 on 16 hand-built pairs (8
# paraphrases of one question, 8 pairs of genuinely different questions), not
# taken from the ~0.85 usually quoted for "similar text". The measurement is
# worth recording because it contradicts the intuition:
#
#     paraphrases            0.47 - 0.98
#     different questions    0.09 - 0.60
#
# **The two ranges overlap.** "What are the office hours?" / "when is the office
# open?" scores 0.4734 — the same question, and lower than "Write a haiku about
# the ocean" / "...about the desert" at 0.6022, which are different questions
# with different answers. So no threshold separates them, and any choice trades
# hit rate against wrong answers rather than finding a clean line.
#
# Hits on the paraphrase set at each threshold, with the literal guard applied,
# and false hits on a 14-pair different-question set:
#
#     0.80  ->  4/8 hits, 0 false      0.95  ->  0/8 hits, 0 false
#     0.90  ->  2/8 hits, 0 false      0.98  ->  0/8 hits, 0 false
#
# 0.90 is chosen. It buys the two clearest paraphrases in the set —
#
#     0.9258  "What does our SLA guarantee for uptime?" / "What uptime does the
#             SLA promise?"
#     0.9185  "Explain how photosynthesis works" / "How does photosynthesis
#             work?"
#
# — which 0.95 rejected, and it clears the highest different-question pair the
# literal guard admits (0.7476) by 0.15. That margin matters more than the
# threshold: a low hit rate is a missed saving, a false hit is a confident wrong
# answer nobody is watching for.
#
# 0.80 doubles the hits with no false hits *on this sample*, and is still not
# taken: its extra two pairs score 0.8119 and 0.8468, close enough to the 0.7476
# ceiling that a single unseen phrasing could land between them. 14 pairs is not
# enough evidence to spend a 0.15 margin. Override per project if the workload
# is genuinely tolerant.
#
# One caveat this measurement earned the hard way: at 0.95 the guard's coverage
# was untested, because nothing scored high enough to reach it. Lowering to 0.90
# exposed "Who is the on-call engineer this week?" / "...next week?" at 0.9385 —
# a wrong answer that passed the guard, since this/next are not numbers and were
# not in _POLAR_WORDS. Lowering a threshold makes the literal guard load-bearing
# where it previously was not; see the list below.
DEFAULT_SIMILARITY_THRESHOLD = 0.90

# Per project. Small on purpose: every entry is compared on every lookup (a
# linear scan — see _best_match), so this bounds lookup work as well as memory.
DEFAULT_MAX_ENTRIES_PER_PROJECT = 500

# Prompts shorter than this are not cached semantically. Embeddings of very
# short strings are dominated by the few tokens present, so "yes" and "no" sit
# closer together than their meanings do.
MIN_PROMPT_CHARS = 16


# Tokens that must match exactly between two prompts, whatever their embeddings
# say. Each is a case where a tiny textual difference flips the correct answer.
_NUMBER_RE = re.compile(r"\d+(?:[.,]\d+)*")
_QUOTED_RE = re.compile(r"'([^']*)'|\"([^\"]*)\"|`([^`]*)`")
# Identifier-ish: has an underscore, a dot between letters, or internal caps.
# Deliberately not "any word" — that would make the whole check an exact match.
_IDENT_RE = re.compile(r"\b(?:\w+_\w+|\w+\.\w+|[a-z]+[A-Z]\w*)\b")
# Negations and polar opposites. "how to enable X" / "how to disable X" embed
# almost identically, and the answers are opposites.
#
# The temporal and selector words in the third group were added when the
# threshold moved from 0.95 to 0.90. "Who is the on-call engineer this week?" /
# "...next week?" scores 0.9385 — above 0.90, no numbers to compare, no negation
# — so it was served as a hit, returning the wrong engineer. These words behave
# exactly like enable/disable: a one-word change that flips the answer while
# barely moving the embedding.
_POLAR_WORDS = frozenset(
    {
        "not", "no", "never", "none", "without", "cannot", "cant", "dont",
        "doesnt", "isnt", "arent", "wasnt", "werent", "shouldnt", "wouldnt",
        "enable", "disable", "enabled", "disabled", "allow", "deny", "denied",
        "add", "remove", "delete", "create", "destroy", "start", "stop",
        "increase", "decrease", "before", "after", "include", "exclude",
        "on", "off", "true", "false", "yes",
        # Which one / when — distinguishes questions the embedding treats as one.
        "this", "next", "last", "previous", "current", "upcoming", "prior",
        "today", "tomorrow", "yesterday", "now", "latest", "oldest", "newest",
        "first", "final", "min", "max", "minimum", "maximum",
        "required", "optional", "highest", "lowest", "more", "less",
    }
)


# (request model, prompt). See the comment on the key in put().
_EntryKey = tuple[str, str]


@dataclass
class SemanticCacheEntry:
    """One cached response plus what is needed to match against it."""

    prompt: str
    embedding: list[float]
    response: ChatCompletionResponse
    expires_at: datetime
    # The model the *caller asked for*, not response.model. The two differ:
    # response.model is the provider-side id (``us.anthropic.claude-sonnet-4-6``
    # — the key CostTracker bills from), while a request names a gateway alias
    # (``claude-sonnet``). Matching on response.model would compare an alias
    # against a provider id, never find them equal, and reject every candidate —
    # a cache that silently never hits. This is also the field the exact-match
    # cache keys on, so both caches agree on what "same model" means.
    request_model: str = ""
    # Extracted once at insert. Recomputing per comparison would make every
    # lookup O(entries x prompt length) on top of the vector maths.
    literals: frozenset[str] = field(default_factory=frozenset)


@dataclass
class SemanticCacheStats:
    """Counters for the admin surface.

    Kept here rather than derived from logs because the interesting number is
    the ratio, and ``skipped`` is what explains a hit rate of zero on a project
    that has caching switched on.
    """

    lookups: int = 0
    hits: int = 0
    misses: int = 0
    # Requests never eligible: streaming, tools, temperature, too short.
    skipped: int = 0
    # A candidate cleared the similarity threshold but disagreed on a literal.
    # Worth its own counter: a high value means the threshold is admitting
    # near-misses and the literal guard is the only thing catching them.
    rejected_by_literals: int = 0
    embed_failures: int = 0
    evictions: int = 0

    @property
    def hit_rate(self) -> float:
        """Hits as a fraction of lookups that were actually attempted.

        Skipped requests are excluded from the denominator: they never consulted
        the cache, so counting them would report a cache that is working as one
        that is failing.
        """
        attempted = self.hits + self.misses
        return self.hits / attempted if attempted else 0.0

    def as_dict(self) -> dict:
        return {
            "lookups": self.lookups,
            "hits": self.hits,
            "misses": self.misses,
            "skipped": self.skipped,
            "rejected_by_literals": self.rejected_by_literals,
            "embed_failures": self.embed_failures,
            "evictions": self.evictions,
            "hit_rate": round(self.hit_rate, 4),
        }


def extract_literals(text: str) -> frozenset[str]:
    """Tokens that must agree exactly for two prompts to be the same question.

    Numbers, quoted strings, identifiers, and polar words (negations and
    opposites). Everything else is left to the embedding — the point is to catch
    the specific cases where embeddings are blind, not to reimplement equality.
    """
    lowered = text.lower()
    found: set[str] = set()

    found.update(_NUMBER_RE.findall(lowered))
    for groups in _QUOTED_RE.findall(text):
        # findall over alternated groups yields a tuple with empties for the
        # branches that did not match.
        for g in groups:
            if g:
                found.add(g.lower())
    found.update(m.lower() for m in _IDENT_RE.findall(text))
    found.update(w for w in re.findall(r"[a-z']+", lowered) if w in _POLAR_WORDS)

    return frozenset(found)


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """Cosine similarity, or 0.0 when either vector is degenerate.

    Hand-rolled rather than numpy: numpy is not a dependency, and the Dockerfile
    installs a hardcoded package list, so adding one to pyproject.toml would not
    reach the image. Over a 1024-dim vector this is fast enough next to the
    network call it avoids.
    """
    if len(a) != len(b):
        # Different embedding models, or a model change mid-process. Not
        # comparable; report no similarity rather than raising, so a config
        # change degrades to cache misses instead of failing requests.
        return 0.0
    dot = 0.0
    norm_a = 0.0
    norm_b = 0.0
    for x, y in zip(a, b):
        dot += x * y
        norm_a += x * x
        norm_b += y * y
    if norm_a <= 0.0 or norm_b <= 0.0:
        return 0.0
    return dot / (math.sqrt(norm_a) * math.sqrt(norm_b))


def is_cacheable(request: ChatCompletionRequest) -> tuple[bool, str]:
    """Whether a request may be served from, or stored in, the semantic cache.

    Returns ``(ok, reason)`` — the reason is for the skip counter and logs, so a
    project seeing no hits can find out why without a debugger.

    These are correctness limits, not optimisations. Each one is a case where
    reusing a response would return something the caller did not ask for.
    """
    if request.stream:
        # A cached response is a complete object; replaying it as a stream is
        # possible but changes timing and chunk boundaries that callers key off.
        return False, "streaming"
    if request.tools:
        # The value of a tool call is the side effect. Serving a cached one
        # would return a stale tool_use block the caller then acts on.
        return False, "tools"
    if request.temperature is not None and request.temperature > 0:
        # A caller asking for sampling asked for variety. Returning a fixed
        # answer silently overrides that.
        return False, "temperature"
    # getattr, not request.n: ChatCompletionRequest has no ``n`` field today.
    # Guarded anyway so that adding one later cannot silently start serving one
    # cached completion to a caller who asked for several.
    n = getattr(request, "n", None)
    if n is not None and n > 1:
        return False, "multiple_completions"

    prompt = last_user_text(request)
    if prompt is None:
        return False, "no_user_text"
    if len(prompt.strip()) < MIN_PROMPT_CHARS:
        return False, "prompt_too_short"
    return True, ""


def last_user_text(request: ChatCompletionRequest) -> str | None:
    """Plain text of the final user message, or None if there isn't any.

    Only the last user turn is embedded, not the whole conversation: the
    conversation is what makes two requests different even when the question is
    the same, and it is handled by keying the cache on the exact-match hash
    first. Multi-turn requests are excluded outright below.
    """
    messages = request.messages or []
    for msg in reversed(messages):
        if msg.get("role") != "user":
            continue
        content = msg.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            # Multimodal: concatenate the text parts, ignore images. A request
            # whose text matches but whose image differs must not hit, which is
            # why images make it uncacheable below rather than being skipped.
            parts = [p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text"]
            non_text = any(
                isinstance(p, dict) and p.get("type") not in ("text", None) for p in content
            )
            if non_text:
                return None
            joined = " ".join(t for t in parts if t)
            return joined or None
        return None
    return None


def conversation_depth(request: ChatCompletionRequest) -> int:
    """Number of user turns. Used to keep multi-turn requests out of the cache.

    Two conversations can end on the same question and require different
    answers, because the earlier turns changed what it refers to ("and the
    second one?"). Embedding only the final turn cannot see that, so anything
    past the first user message is left to the exact-match cache.
    """
    return sum(1 for m in (request.messages or []) if m.get("role") == "user")


class SemanticCache:
    """Per-project semantic response cache.

    Consulted only after :class:`CacheManager` misses, so a byte-identical
    repeat never pays for an embedding call.
    """

    def __init__(
        self,
        embedder=None,
        *,
        similarity_threshold: float = DEFAULT_SIMILARITY_THRESHOLD,
        max_entries_per_project: int = DEFAULT_MAX_ENTRIES_PER_PROJECT,
    ) -> None:
        self._embedder = embedder
        self._threshold = similarity_threshold
        self._max_entries = max_entries_per_project
        # project_id -> prompt-keyed entries. Keyed by prompt so the same
        # question asked twice updates one entry instead of accumulating
        # duplicates that then compete in the scan.
        self._entries: dict[str, OrderedDict[_EntryKey, SemanticCacheEntry]] = {}
        self.stats = SemanticCacheStats()
        # One-slot memo of the most recent (prompt, embedding). A miss is
        # normally followed by a put of the same prompt, and embedding it twice
        # would double the cost of every miss — the common case.
        #
        # One slot, and reused only when the prompt matches exactly, so
        # concurrent requests can evict each other but can never pair a prompt
        # with another prompt's vector. The failure mode is a wasted re-embed,
        # not a wrong comparison.
        self._last_embedding: tuple[str, list[float]] | None = None

    @property
    def enabled(self) -> bool:
        """False without an embedder, which makes every method a no-op.

        Separate from the per-project flag: this is "the gateway cannot embed"
        (no credentials, no Bedrock), where the per-project flag is "this
        project asked not to".
        """
        return self._embedder is not None

    @property
    def threshold(self) -> float:
        """The default threshold, for the admin surface to report.

        Worth exposing: a project with no override of its own is matching at
        this value, and an operator diagnosing an unexpected hit needs to know
        what it was compared against.
        """
        return self._threshold

    async def get(
        self,
        request: ChatCompletionRequest,
        project_id: str,
        threshold: float | None = None,
    ) -> ChatCompletionResponse | None:
        """Closest acceptable cached response, or None.

        ``threshold`` overrides the instance default for this lookup — that is
        how a project's own ``semantic_cache_threshold`` is honoured without a
        cache instance per project. None means "use the default"; 0.0 would mean
        "match everything", so the two cannot be collapsed.

        None covers every negative case — disabled, ineligible, no entries, no
        candidate above threshold, candidate rejected on literals, embedding
        failure. The caller then proceeds to the provider, which is correct for
        all of them.
        """
        if not self.enabled:
            return None

        ok, reason = is_cacheable(request)
        if not ok or conversation_depth(request) > 1:
            self.stats.skipped += 1
            return None

        bucket = self._entries.get(project_id)
        if not bucket:
            # No entries yet: a miss, but do not spend an embedding call to
            # discover that. Counted as a miss rather than a skip because the
            # request was eligible — the cache was simply cold.
            self.stats.lookups += 1
            self.stats.misses += 1
            return None

        prompt = last_user_text(request)
        if prompt is None:  # pragma: no cover — is_cacheable already rejected it
            self.stats.skipped += 1
            return None

        self.stats.lookups += 1
        embedding = await self._embed(prompt)
        if embedding is None:
            self.stats.misses += 1
            return None

        self._purge_expired(project_id)

        match = self._best_match(
            bucket,
            prompt,
            embedding,
            request.model,
            self._threshold if threshold is None else threshold,
        )
        if match is None:
            self.stats.misses += 1
            return None

        key, entry, score = match
        bucket.move_to_end(key)
        self.stats.hits += 1
        logger.info(
            "semantic cache hit: project=%s similarity=%.4f cached_prompt=%r",
            project_id,
            score,
            entry.prompt[:80],
        )
        return entry.response

    async def put(
        self,
        request: ChatCompletionRequest,
        project_id: str,
        response: ChatCompletionResponse,
        ttl_seconds: int,
    ) -> None:
        """Store a response for future semantic lookups. Never raises.

        A failure here must not fail the request: the response has already been
        produced and is on its way to the caller, so the only thing a raise
        would achieve is turning a successful request into an error.
        """
        if not self.enabled:
            return
        ok, _ = is_cacheable(request)
        if not ok or conversation_depth(request) > 1:
            return

        prompt = last_user_text(request)
        if prompt is None:  # pragma: no cover — is_cacheable already rejected it
            return

        embedding = await self._embed(prompt)
        if embedding is None:
            return

        bucket = self._entries.setdefault(project_id, OrderedDict())
        # Keyed on (model, prompt), not prompt alone: the same question asked of
        # two models is two answers, and a prompt-only key would make the second
        # overwrite the first — so a project routing between models would keep
        # losing whichever it asked less recently.
        key = (request.model or "", prompt)
        bucket[key] = SemanticCacheEntry(
            prompt=prompt,
            embedding=embedding,
            response=response,
            expires_at=datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds),
            literals=extract_literals(prompt),
            request_model=request.model or "",
        )
        bucket.move_to_end(key)
        while len(bucket) > self._max_entries:
            bucket.popitem(last=False)
            self.stats.evictions += 1

    def invalidate(self, project_id: str | None = None) -> int:
        """Drop entries for one project, or all of them. Returns the count.

        Exists because a semantic cache has no natural way to know its answers
        went stale — the underlying documents or prompts change with nothing
        observable in the request. An operator needs a way to clear it that
        does not involve a restart.
        """
        if project_id is None:
            removed = sum(len(b) for b in self._entries.values())
            self._entries.clear()
            return removed
        bucket = self._entries.pop(project_id, None)
        return len(bucket) if bucket else 0

    def entry_count(self, project_id: str | None = None) -> int:
        if project_id is None:
            return sum(len(b) for b in self._entries.values())
        return len(self._entries.get(project_id, ()))

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _best_match(
        self,
        bucket: OrderedDict[_EntryKey, SemanticCacheEntry],
        prompt: str,
        embedding: list[float],
        model: str | None,
        threshold: float,
    ) -> tuple[str, SemanticCacheEntry, float] | None:
        """Highest-scoring entry that clears the threshold and the guards.

        A linear scan. With max_entries_per_project at 500 and 1024-dim vectors
        that is well under the latency of the provider call being avoided, and
        it keeps the whole cache dependency-free — an index would mean numpy or
        a vector store, neither of which is available (see cosine_similarity).
        """
        literals = extract_literals(prompt)
        best: tuple[str, SemanticCacheEntry, float] | None = None

        for key, entry in bucket.items():
            # A response from a different model is a different answer: models
            # differ in format, verbosity and quality, and a project routing to
            # one deliberately should not be served another's output.
            if model and entry.request_model and entry.request_model != model:
                continue

            score = cosine_similarity(embedding, entry.embedding)
            if score < threshold:
                continue

            # The guard that does the real work. Two prompts can sit at 0.99
            # and still be different questions when a number or a negation
            # differs, and no threshold short of 1.0 separates them.
            if literals != entry.literals:
                self.stats.rejected_by_literals += 1
                logger.debug(
                    "semantic cache rejected on literals: %.4f %r vs %r (%s vs %s)",
                    score, prompt[:60], entry.prompt[:60],
                    sorted(literals), sorted(entry.literals),
                )
                continue

            if best is None or score > best[2]:
                best = (key, entry, score)

        return best

    def _purge_expired(self, project_id: str) -> None:
        bucket = self._entries.get(project_id)
        if not bucket:
            return
        now = datetime.now(timezone.utc)
        for key in [k for k, e in bucket.items() if now >= e.expires_at]:
            del bucket[key]

    async def _embed(self, text: str) -> list[float] | None:
        """Embed, or None on any failure.

        Swallowing is deliberate: an embedding outage must degrade the cache to
        misses, not take the gateway down with it. Counted so the failure is
        visible on the admin surface rather than only in logs.
        """
        memo = self._last_embedding
        if memo is not None and memo[0] == text:
            return memo[1]
        try:
            vector = await self._embedder.embed(text)
        except Exception:
            self.stats.embed_failures += 1
            logger.warning("semantic cache: embedding failed", exc_info=True)
            return None
        if not vector:
            self.stats.embed_failures += 1
            return None
        self._last_embedding = (text, vector)
        return vector
