"""Unit tests for the semantic response cache.

Most of these are about what the cache *refuses* to do. A semantic cache that
returns nothing is a missed saving; one that returns the answer to a different
question is a confident wrong answer with no error attached to it, so the
guards are the part worth pinning down.

The embedder is a fake throughout. Real embeddings would make every assertion
depend on a model's numeric output — the threshold calibration in the module
docstring was measured against the real one, but a test that re-measures it
would break when the model is updated and would not be testing this code.
"""

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from src.gateway.models import ChatCompletionRequest, ChatCompletionResponse, TokenUsage
from src.gateway.semantic_cache import (
    DEFAULT_SIMILARITY_THRESHOLD,
    MIN_PROMPT_CHARS,
    SemanticCache,
    conversation_depth,
    cosine_similarity,
    extract_literals,
    is_cacheable,
    last_user_text,
)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeEmbedder:
    """Returns a caller-supplied vector per prompt, and counts calls.

    The call count is asserted on directly: an embedding is a paid network
    round-trip, so "does this path spend one" is a behaviour of the cache and
    not an implementation detail.
    """

    def __init__(self, vectors: dict[str, list[float]] | None = None,
                 default: list[float] | None = None) -> None:
        self.vectors = vectors or {}
        self.default = default if default is not None else [1.0, 0.0, 0.0]
        self.calls: list[str] = []

    async def embed(self, text: str) -> list[float]:
        self.calls.append(text)
        return self.vectors.get(text, self.default)


class BrokenEmbedder:
    def __init__(self) -> None:
        self.calls = 0

    async def embed(self, text: str) -> list[float]:
        self.calls += 1
        raise RuntimeError("bedrock is down")


class EmptyEmbedder:
    async def embed(self, text: str) -> list[float]:
        return []


def _req(content: str, **kw) -> ChatCompletionRequest:
    return ChatCompletionRequest(
        model=kw.pop("model", "claude-sonnet"),
        messages=kw.pop("messages", [{"role": "user", "content": content}]),
        **kw,
    )


def _resp(content: str = "the answer", model: str = "claude-sonnet") -> ChatCompletionResponse:
    return ChatCompletionResponse(
        id="resp-1",
        choices=[{"message": {"role": "assistant", "content": content}}],
        usage=TokenUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
        model=model,
        provider="anthropic",
    )


# ---------------------------------------------------------------------------
# cosine_similarity
# ---------------------------------------------------------------------------


class TestCosineSimilarity:
    def test_identical_vectors_score_one(self):
        v = [0.3, 0.4, 0.5]
        assert cosine_similarity(v, v) == pytest.approx(1.0)

    def test_orthogonal_vectors_score_zero(self):
        assert cosine_similarity([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)

    def test_magnitude_does_not_affect_the_score(self):
        """Cosine is direction only — a scaled copy is the same point."""
        assert cosine_similarity([1.0, 2.0], [10.0, 20.0]) == pytest.approx(1.0)

    def test_mismatched_lengths_score_zero_instead_of_raising(self):
        """A model change mid-process must degrade to misses, not to errors.

        Comparing a 1024-dim vector against a 1536-dim one is meaningless; the
        only safe answer is "not similar", which sends the request to the
        provider as if the cache were empty.
        """
        assert cosine_similarity([1.0, 2.0], [1.0, 2.0, 3.0]) == 0.0

    def test_zero_vector_scores_zero_rather_than_dividing_by_zero(self):
        assert cosine_similarity([0.0, 0.0], [1.0, 1.0]) == 0.0


# ---------------------------------------------------------------------------
# extract_literals
# ---------------------------------------------------------------------------


class TestExtractLiterals:
    def test_numbers_are_extracted(self):
        assert extract_literals("what is 17 * 23") == frozenset({"17", "23"})

    def test_two_arithmetic_questions_differ_on_their_numbers(self):
        """The case the whole guard exists for.

        17*23 and 17*24 are one character apart and score 0.7191 against each
        other on the real embedder — far enough below 0.90 to miss here, but the
        literal sets differ regardless of what any threshold admits.
        """
        assert extract_literals("what is 17 * 23") != extract_literals("what is 17 * 24")

    def test_enable_and_disable_are_distinguished(self):
        a = extract_literals("how do I enable the cache")
        b = extract_literals("how do I disable the cache")
        assert a != b

    def test_negation_is_distinguished(self):
        assert extract_literals("is it supported") != extract_literals("is it not supported")

    def test_this_week_and_next_week_are_distinguished(self):
        """The false hit that lowering the threshold to 0.90 exposed.

        "Who is the on-call engineer this week?" / "...next week?" scores 0.9385
        on the real embedder — above 0.90, no numbers, no negation — so before
        this/next joined _POLAR_WORDS the cache served last week's engineer as
        the answer to who is on call now. Nothing in the previous guard caught
        it, and at the old 0.95 threshold nothing reached the guard to reveal it.
        """
        assert (extract_literals("who is the on-call engineer this week")
                != extract_literals("who is the on-call engineer next week"))

    def test_selector_words_that_flip_the_answer_are_distinguished(self):
        """Same failure mode as this/next: one word changes which set of facts
        answers the question, while the embedding barely moves."""
        for a, b in (
            ("list the required fields", "list the optional fields"),
            ("what is the latest release", "what is the previous release"),
            ("what is the minimum retry delay", "what is the maximum retry delay"),
            ("what is the current quota", "what is the upcoming quota"),
        ):
            assert extract_literals(a) != extract_literals(b), f"{a!r} vs {b!r}"

    def test_paraphrases_still_agree_on_literals(self):
        """The guard is only useful if it lets real paraphrases through. Widening
        _POLAR_WORDS trades hit rate for safety, so this bounds the trade: these
        are the pairs the 0.90 threshold exists to serve, and a future addition
        to the word list that blocks one of them has gone too far."""
        for a, b in (
            ("what does our SLA guarantee for uptime", "what uptime does the SLA promise"),
            ("explain how photosynthesis works", "how does photosynthesis work"),
            ("summarize the key benefits of remote work",
             "what are the main advantages of working remotely"),
            ("who is responsible for approving expense reports",
             "which team approves expense reports"),
        ):
            assert extract_literals(a) == extract_literals(b), f"{a!r} vs {b!r}"

    def test_quoted_strings_are_extracted(self):
        lits = extract_literals('what does the "retry_after" header mean')
        assert "retry_after" in lits

    def test_identifiers_are_extracted_but_ordinary_words_are_not(self):
        """Not every word — that would make the guard an exact-match check and
        the embedding pointless."""
        lits = extract_literals("explain cache_manager.put to me please")
        assert "cache_manager.put" in lits or "cache_manager" in lits
        assert "explain" not in lits
        assert "please" not in lits

    def test_a_pure_paraphrase_has_the_same_literals(self):
        """The guard must not block the case the cache is for."""
        a = extract_literals("What is our refund policy?")
        b = extract_literals("what's the refund policy")
        assert a == b

    def test_extraction_is_case_insensitive(self):
        assert extract_literals("ENABLE the flag") == extract_literals("enable the flag")


# ---------------------------------------------------------------------------
# is_cacheable / helpers
# ---------------------------------------------------------------------------


class TestIsCacheable:
    def test_a_plain_request_is_cacheable(self):
        ok, reason = is_cacheable(_req("what is our refund policy this year"))
        assert ok and reason == ""

    def test_streaming_is_rejected(self):
        ok, reason = is_cacheable(_req("what is our refund policy", stream=True))
        assert not ok and reason == "streaming"

    def test_tool_requests_are_rejected(self):
        ok, reason = is_cacheable(
            _req("what is our refund policy", tools=[{"type": "function"}])
        )
        assert not ok and reason == "tools"

    def test_nonzero_temperature_is_rejected(self):
        """A caller asking for sampling asked for variety."""
        ok, reason = is_cacheable(_req("what is our refund policy", temperature=0.7))
        assert not ok and reason == "temperature"

    def test_temperature_zero_is_accepted(self):
        ok, _ = is_cacheable(_req("what is our refund policy", temperature=0.0))
        assert ok

    def test_short_prompts_are_rejected(self):
        ok, reason = is_cacheable(_req("hi"))
        assert not ok and reason == "prompt_too_short"
        assert len("hi") < MIN_PROMPT_CHARS

    def test_a_request_with_no_user_message_is_rejected(self):
        ok, reason = is_cacheable(_req("", messages=[{"role": "system", "content": "be nice"}]))
        assert not ok and reason == "no_user_text"

    def test_multiple_completions_are_rejected_via_getattr(self):
        """ChatCompletionRequest has no ``n`` today; the guard is for when it does.

        Simulated with a subclass rather than by setting an attribute on the
        dataclass, so this test states the contract for a future field instead
        of asserting today's absence.
        """
        class WithN(ChatCompletionRequest):
            n = 3

        req = WithN(model="m", messages=[{"role": "user", "content": "a long enough prompt"}])
        ok, reason = is_cacheable(req)
        assert not ok and reason == "multiple_completions"


class TestLastUserText:
    def test_returns_the_final_user_turn(self):
        req = _req("", messages=[
            {"role": "user", "content": "first"},
            {"role": "assistant", "content": "reply"},
            {"role": "user", "content": "second"},
        ])
        assert last_user_text(req) == "second"

    def test_concatenates_text_parts_of_a_multimodal_message(self):
        req = _req("", messages=[{"role": "user", "content": [
            {"type": "text", "text": "hello"},
            {"type": "text", "text": "world"},
        ]}])
        assert last_user_text(req) == "hello world"

    def test_a_message_containing_an_image_returns_none(self):
        """Two requests with identical text and different images are different
        questions, and the embedding of the text alone cannot see that."""
        req = _req("", messages=[{"role": "user", "content": [
            {"type": "text", "text": "what is in this picture"},
            {"type": "image", "source": {"data": "..."}},
        ]}])
        assert last_user_text(req) is None

    def test_no_user_message_returns_none(self):
        assert last_user_text(_req("", messages=[{"role": "system", "content": "x"}])) is None


class TestConversationDepth:
    def test_counts_user_turns_only(self):
        req = _req("", messages=[
            {"role": "system", "content": "s"},
            {"role": "user", "content": "a"},
            {"role": "assistant", "content": "b"},
            {"role": "user", "content": "c"},
        ])
        assert conversation_depth(req) == 2


# ---------------------------------------------------------------------------
# SemanticCache — get / put
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestSemanticCacheBasics:
    async def test_without_an_embedder_the_cache_is_disabled_and_never_hits(self):
        cache = SemanticCache()
        assert not cache.enabled
        await cache.put(_req("a prompt long enough to cache"), "p1", _resp(), 300)
        assert await cache.get(_req("a prompt long enough to cache"), "p1") is None
        assert cache.entry_count() == 0

    async def test_an_identical_prompt_hits(self):
        emb = FakeEmbedder()
        cache = SemanticCache(emb)
        req = _req("what is our refund policy for annual plans")
        await cache.put(req, "p1", _resp("30 days"), 300)

        hit = await cache.get(req, "p1")
        assert hit is not None
        assert hit.choices[0]["message"]["content"] == "30 days"
        assert cache.stats.hits == 1

    async def test_a_cold_bucket_misses_without_spending_an_embedding_call(self):
        """The common case on a fresh process. Embedding to discover the cache is
        empty would put a paid round-trip in front of every first request."""
        emb = FakeEmbedder()
        cache = SemanticCache(emb)
        assert await cache.get(_req("a prompt long enough to cache"), "p1") is None
        assert emb.calls == []
        assert cache.stats.misses == 1
        assert cache.stats.skipped == 0

    async def test_buckets_are_per_project(self):
        """A shared cache across projects would leak one tenant's answers into
        another's responses."""
        emb = FakeEmbedder()
        cache = SemanticCache(emb)
        req = _req("what is our refund policy for annual plans")
        await cache.put(req, "proj-a", _resp("a's answer"), 300)
        assert await cache.get(req, "proj-b") is None

    async def test_a_dissimilar_prompt_misses(self):
        emb = FakeEmbedder(vectors={
            "what is our refund policy for annual plans": [1.0, 0.0],
            "how do I reset my password today": [0.0, 1.0],
        })
        cache = SemanticCache(emb)
        await cache.put(_req("what is our refund policy for annual plans"), "p1", _resp(), 300)
        assert await cache.get(_req("how do I reset my password today"), "p1") is None
        assert cache.stats.misses == 1

    async def test_a_close_paraphrase_above_the_threshold_hits(self):
        emb = FakeEmbedder(vectors={
            "what is the refund policy": [1.0, 0.0],
            # cos ~= 0.9988, above 0.90, and the literals agree
            "what's the refund policy": [1.0, 0.05],
        })
        cache = SemanticCache(emb)
        await cache.put(_req("what is the refund policy"), "p1", _resp("30 days"), 300)
        hit = await cache.get(_req("what's the refund policy"), "p1")
        assert hit is not None

    async def test_a_score_just_below_the_threshold_misses(self):
        emb = FakeEmbedder(vectors={
            "what is the refund policy": [1.0, 0.0],
            "what is the shipping policy": [1.0, 0.55],  # cos ~= 0.876
        })
        cache = SemanticCache(emb)
        await cache.put(_req("what is the refund policy"), "p1", _resp(), 300)
        assert await cache.get(_req("what is the shipping policy"), "p1") is None


@pytest.mark.asyncio
class TestGuards:
    async def test_differing_numbers_never_hit_however_similar_the_embedding(self):
        """The headline guard. Both prompts are given the *same* vector, so
        similarity is exactly 1.0 and only the literal check can stop it."""
        emb = FakeEmbedder(default=[1.0, 0.0, 0.0])
        cache = SemanticCache(emb)
        await cache.put(_req("what is 17 times 23"), "p1", _resp("391"), 300)

        assert await cache.get(_req("what is 17 times 24"), "p1") is None
        assert cache.stats.rejected_by_literals == 1
        assert cache.stats.hits == 0

    async def test_enable_and_disable_never_hit_each_other(self):
        emb = FakeEmbedder(default=[1.0, 0.0, 0.0])
        cache = SemanticCache(emb)
        await cache.put(_req("how do I enable the response cache"), "p1", _resp("set the flag"), 300)
        assert await cache.get(_req("how do I disable the response cache"), "p1") is None
        assert cache.stats.rejected_by_literals == 1

    async def test_a_response_from_another_model_is_not_served(self):
        """Models differ in format and verbosity; a project routing to one
        deliberately should not be handed another's output."""
        emb = FakeEmbedder(default=[1.0, 0.0, 0.0])
        cache = SemanticCache(emb)
        await cache.put(
            _req("what is the refund policy", model="claude-sonnet"), "p1",
            _resp("30 days", model="claude-sonnet"), 300,
        )
        assert await cache.get(_req("what is the refund policy", model="gpt-4o"), "p1") is None

    async def test_the_model_guard_compares_requested_models_not_provider_ids(self):
        """Regression: the guard first compared ``request.model`` against
        ``response.model``, which are different namespaces — the caller names a
        gateway alias (``claude-sonnet``) and the response carries the
        provider-side id (``us.anthropic.claude-sonnet-4-6``). They are never
        equal, so every candidate was rejected and the cache could not hit at
        all. A silent 0% hit rate, not an error.
        """
        emb = FakeEmbedder()
        cache = SemanticCache(emb)
        req = _req("what is the refund policy", model="claude-sonnet")
        await cache.put(req, "p1", _resp("30 days", model="us.anthropic.claude-sonnet-4-6"), 300)
        assert await cache.get(req, "p1") is not None

    async def test_the_same_question_to_two_models_keeps_both_answers(self):
        """A prompt-only key would make the second put overwrite the first, so a
        project routing between models would keep losing one of them."""
        emb = FakeEmbedder()
        cache = SemanticCache(emb)
        prompt = "what is the refund policy for annual plans"
        await cache.put(_req(prompt, model="claude-sonnet"), "p1", _resp("sonnet says"), 300)
        await cache.put(_req(prompt, model="gpt-4o"), "p1", _resp("gpt says"), 300)

        assert cache.entry_count("p1") == 2
        a = await cache.get(_req(prompt, model="claude-sonnet"), "p1")
        b = await cache.get(_req(prompt, model="gpt-4o"), "p1")
        assert a.choices[0]["message"]["content"] == "sonnet says"
        assert b.choices[0]["message"]["content"] == "gpt says"

    async def test_multi_turn_requests_are_skipped_not_matched(self):
        """Only the last turn is embedded, so it cannot see that earlier turns
        changed what the question refers to."""
        emb = FakeEmbedder()
        cache = SemanticCache(emb)
        multi = _req("", messages=[
            {"role": "user", "content": "tell me about the refund policy"},
            {"role": "assistant", "content": "..."},
            {"role": "user", "content": "and the second one in that list"},
        ])
        await cache.put(multi, "p1", _resp(), 300)
        assert cache.entry_count() == 0
        assert await cache.get(multi, "p1") is None
        assert cache.stats.skipped == 1

    async def test_streaming_requests_are_skipped_on_both_read_and_write(self):
        emb = FakeEmbedder()
        cache = SemanticCache(emb)
        req = _req("what is our refund policy for annual plans", stream=True)
        await cache.put(req, "p1", _resp(), 300)
        assert cache.entry_count() == 0
        assert await cache.get(req, "p1") is None
        assert cache.stats.skipped == 1
        assert emb.calls == []

    async def test_skips_are_excluded_from_the_hit_rate_denominator(self):
        """A project sending only streaming requests should read as 0 attempted
        lookups, not as a cache failing every one."""
        emb = FakeEmbedder()
        cache = SemanticCache(emb)
        for _ in range(5):
            await cache.get(_req("hi", stream=True), "p1")
        assert cache.stats.skipped == 5
        assert cache.stats.hit_rate == 0.0
        assert cache.stats.lookups == 0


@pytest.mark.asyncio
class TestThresholdOverride:
    async def test_a_per_call_threshold_can_admit_a_lower_score(self):
        emb = FakeEmbedder(vectors={
            "what is the refund policy": [1.0, 0.0],
            "what is the refund policy again": [1.0, 0.55],  # cos ~= 0.876
        })
        cache = SemanticCache(emb)
        await cache.put(_req("what is the refund policy"), "p1", _resp(), 300)

        # Below the default, above the override — the gap is the point. A score
        # clearing both would assert nothing about which threshold was used.
        assert await cache.get(_req("what is the refund policy again"), "p1") is None
        assert await cache.get(
            _req("what is the refund policy again"), "p1", threshold=0.85
        ) is not None

    async def test_a_per_call_threshold_can_tighten_as_well_as_loosen(self):
        emb = FakeEmbedder(vectors={
            "what is the refund policy": [1.0, 0.0],
            "what's the refund policy": [1.0, 0.05],  # cos ~= 0.9988
        })
        cache = SemanticCache(emb)
        await cache.put(_req("what is the refund policy"), "p1", _resp(), 300)
        assert await cache.get(_req("what's the refund policy"), "p1", threshold=0.999) is None

    async def test_none_means_the_instance_default_not_zero(self):
        """The distinction that matters: 0.0 would match everything."""
        emb = FakeEmbedder(vectors={
            "what is the refund policy": [1.0, 0.0],
            "how do I reset my password": [0.0, 1.0],  # cos == 0.0
        })
        cache = SemanticCache(emb)
        await cache.put(_req("what is the refund policy"), "p1", _resp(), 300)
        assert await cache.get(_req("how do I reset my password"), "p1", threshold=None) is None

    async def test_the_default_threshold_is_the_conservative_calibrated_one(self):
        """Pinned to the measured value, not just "some float". The threshold is
        the single number deciding whether a wrong answer gets served, and it was
        calibrated against real Titan embeddings — a casual edit here should
        break a test and send the reader to the comment on the constant."""
        assert SemanticCache().threshold == DEFAULT_SIMILARITY_THRESHOLD
        assert DEFAULT_SIMILARITY_THRESHOLD == 0.90


@pytest.mark.asyncio
class TestExpiryAndEviction:
    async def test_an_expired_entry_is_not_served(self):
        emb = FakeEmbedder()
        cache = SemanticCache(emb)
        req = _req("what is our refund policy for annual plans")
        await cache.put(req, "p1", _resp(), ttl_seconds=300)
        # Reach in and backdate rather than sleep: the behaviour under test is
        # the expiry comparison, and a real wait would make the suite slower for
        # no extra coverage.
        entry = next(iter(cache._entries["p1"].values()))
        entry.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)

        assert await cache.get(req, "p1") is None
        assert cache.entry_count("p1") == 0

    async def test_the_oldest_entry_is_evicted_past_the_cap(self):
        emb = FakeEmbedder()
        cache = SemanticCache(emb, max_entries_per_project=2)
        for i in range(3):
            await cache.put(_req(f"question number {i} about the policy"), "p1", _resp(), 300)
        assert cache.entry_count("p1") == 2
        assert cache.stats.evictions == 1
        assert ("claude-sonnet", "question number 0 about the policy") not in cache._entries["p1"]

    async def test_the_same_prompt_twice_updates_one_entry(self):
        """Otherwise duplicates accumulate and compete in the linear scan."""
        emb = FakeEmbedder()
        cache = SemanticCache(emb)
        req = _req("what is our refund policy for annual plans")
        await cache.put(req, "p1", _resp("old"), 300)
        await cache.put(req, "p1", _resp("new"), 300)
        assert cache.entry_count("p1") == 1
        hit = await cache.get(req, "p1")
        assert hit.choices[0]["message"]["content"] == "new"


@pytest.mark.asyncio
class TestEmbedderFailure:
    async def test_a_raising_embedder_degrades_to_a_miss(self):
        """An embedding outage must not fail requests — the gateway keeps
        serving and the cache reports the failure."""
        cache = SemanticCache(FakeEmbedder())
        req = _req("what is our refund policy for annual plans")
        await cache.put(req, "p1", _resp(), 300)

        cache._embedder = BrokenEmbedder()
        cache._last_embedding = None  # defeat the memo so the failure is reached
        assert await cache.get(req, "p1") is None
        assert cache.stats.embed_failures == 1
        assert cache.stats.misses == 1

    async def test_put_never_raises_when_the_embedder_fails(self):
        """The response is already on its way to the caller; a raise here would
        turn a successful request into an error."""
        cache = SemanticCache(BrokenEmbedder())
        await cache.put(_req("what is our refund policy for annual plans"), "p1", _resp(), 300)
        assert cache.entry_count() == 0

    async def test_an_empty_vector_counts_as_a_failure_rather_than_being_stored(self):
        """A stored empty vector would score 0.0 against everything, which looks
        like a legitimate no-match instead of a broken embedder."""
        cache = SemanticCache(EmptyEmbedder())
        await cache.put(_req("what is our refund policy for annual plans"), "p1", _resp(), 300)
        assert cache.entry_count() == 0
        assert cache.stats.embed_failures == 1


@pytest.mark.asyncio
class TestEmbeddingMemo:
    async def test_a_miss_then_a_put_of_the_same_prompt_embeds_once(self):
        """The path every cache miss takes. Embedding twice would double the
        cost of the common case.

        The seeded prompt is given an orthogonal vector so the lookup genuinely
        misses — with FakeEmbedder's shared default vector it would score 1.0
        against the seed and hit, and the assertion below would pass for the
        wrong reason.
        """
        emb = FakeEmbedder(vectors={
            "an unrelated seeded question here": [1.0, 0.0],
            "what is our refund policy for annual plans": [0.0, 1.0],
        })
        cache = SemanticCache(emb)
        await cache.put(_req("an unrelated seeded question here"), "p1", _resp(), 300)
        emb.calls.clear()

        req = _req("what is our refund policy for annual plans")
        assert await cache.get(req, "p1") is None
        await cache.put(req, "p1", _resp(), 300)
        assert emb.calls == ["what is our refund policy for annual plans"]

    async def test_the_memo_is_keyed_on_the_exact_prompt(self):
        """A one-slot memo must never pair a prompt with another's vector."""
        emb = FakeEmbedder(vectors={
            "an unrelated seeded question here": [1.0, 0.0],
            "first question about the refund policy": [0.0, 1.0],
            "second question about the refund policy": [0.0, 0.0, 1.0],
        })
        cache = SemanticCache(emb)
        await cache.put(_req("an unrelated seeded question here"), "p1", _resp(), 300)
        emb.calls.clear()

        await cache.get(_req("first question about the refund policy"), "p1")
        await cache.get(_req("second question about the refund policy"), "p1")
        assert emb.calls == [
            "first question about the refund policy",
            "second question about the refund policy",
        ]


@pytest.mark.asyncio
class TestInvalidation:
    async def test_invalidating_one_project_leaves_the_others(self):
        emb = FakeEmbedder()
        cache = SemanticCache(emb)
        await cache.put(_req("a question long enough to cache"), "p1", _resp(), 300)
        await cache.put(_req("a question long enough to cache"), "p2", _resp(), 300)

        assert cache.invalidate("p1") == 1
        assert cache.entry_count("p1") == 0
        assert cache.entry_count("p2") == 1

    async def test_invalidating_everything_returns_the_total_removed(self):
        emb = FakeEmbedder()
        cache = SemanticCache(emb)
        await cache.put(_req("question one about the policy"), "p1", _resp(), 300)
        await cache.put(_req("question two about the policy"), "p1", _resp(), 300)
        await cache.put(_req("question three about the policy"), "p2", _resp(), 300)

        assert cache.invalidate() == 3
        assert cache.entry_count() == 0

    async def test_invalidating_an_unknown_project_is_not_an_error(self):
        assert SemanticCache(FakeEmbedder()).invalidate("nope") == 0


class TestStats:
    def test_hit_rate_is_zero_with_no_attempted_lookups(self):
        """Not a ZeroDivisionError, and not 1.0."""
        assert SemanticCache().stats.hit_rate == 0.0

    def test_as_dict_carries_every_counter(self):
        d = SemanticCache().stats.as_dict()
        for key in ("lookups", "hits", "misses", "skipped", "rejected_by_literals",
                    "embed_failures", "evictions", "hit_rate"):
            assert key in d
