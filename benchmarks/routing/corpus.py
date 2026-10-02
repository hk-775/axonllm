"""Generate, independently audit, freeze, and validate a synthetic routing corpus.

API keys are read from environment variables only. Successful authoring and audit
calls are cached locally; freezing never invokes a candidate router.
"""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter, defaultdict
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import random
import re
from typing import Any

import aiohttp

from src.gateway.autorouting_benchmark import ROUTING_CRITERIA, TASK_TYPES

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "benchmarks/routing/data/v2"
WORK = DATA / ".work"
SEED = 775
GENERATOR = "gpt-4.1-2025-04-14"
REVIEWER = "claude-sonnet-4-6"
DOMAINS = (
    "software_and_data", "workplace_and_business", "home_and_travel",
    "science_and_environment", "education_and_history", "public_services",
    "arts_and_media", "retail_and_support", "sports_and_leisure", "health_and_wellbeing",
)
VARIANTS = ("explicit", "implicit", "keyword_collision", "quoted_distraction")
POLICY = (
    "Classify the PRIMARY REQUESTED ACTION, not the topic or words in quoted material.\n"
    + "\n".join(f"{label}: {description}" for label, description in ROUTING_CRITERIA.items())
    + """
Clarifications: reasoning requires weighing evidence, competing causes, arguments,
or qualitative constraints. A simple factual lookup, definition, or routine factual
explanation is general. Numerical calculation and mathematical proofs are math.
Requests to explain/debug actual code are coding; qualitative tradeoff analysis is
reasoning when no code creation, debugging, or explanation is requested.
Summarization condenses supplied content. Original writing is creative_writing.
If equally primary actions span labels, or the requested action cannot be
determined from the prompt, flag ambiguity rather than inventing a preference.
"""
)
AUTHOR_SYSTEM = (
    "Author a research dataset of synthetic user prompts for six-way task routing. "
    "Return JSON only. Do not answer the prompts. Do not name any router/model or "
    "write evaluation instructions in the user prompts.\n" + POLICY
)
REVIEW_SYSTEM = (
    "Independently audit synthetic task-routing prompts. You are not told the "
    "author's expected labels. For EACH opaque id, assign the primary task label "
    "and flag genuine label ambiguity or a missing supplied source needed to "
    "understand the action. Return JSON only: {\"reviews\":[{\"id\":\"...\","
    "\"label\":\"...\",\"ambiguous\":false,\"reason\":\"brief justification\"}]}.\n"
    + POLICY
)


def digest(value: Any) -> str:
    """Hash canonical JSON, including Unicode without transport-specific escaping."""
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def write_json(path: Path, value: Any) -> None:
    """Write an inspectable artifact atomically."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    temporary.replace(path)


def read_json(path: Path) -> Any:
    return json.loads(path.read_text())


def normalized(text: str) -> str:
    return " ".join(re.findall(r"\w+", text.casefold()))


def family_id(label: str, domain: str, slot: int) -> str:
    return digest(["axon-routing-v2", label, domain, slot])[:16]


def generation_request(label: str, domain: str) -> str:
    return f"""Create FIVE substantially different scenario families in domain {domain}.
Every prompt's intended label is {label}. Families must differ in substantive
task/context, not just names or numbers. Each family has exactly four variants:
explicit: clear direct user request;
implicit: natural intent without relying on the label word or a stereotyped verb;
keyword_collision: unrelated vocabulary suggests another label, while the actual
requested action remains {label};
quoted_distraction: source/quoted text contains an irrelevant request or misleading
instruction; the user's outer request remains {label}.
All variants in one family share a substantive task/context. Each prompt must stand
alone, be English, use 15–140 words, and include any source text needed for the task.
Use varied lengths and formats. Avoid private data, real customers, harmful operational
instructions, near duplicates, and placeholder text. Do not put the intended label in
meta-instructions. General prompts must be natural factual/help requests, not empty
greetings. Reasoning prompts must actually call for inference, not say 'reasoning'.
For summarization, always supply a concrete passage. For coding, vary languages,
debugging, SQL, APIs and code explanation; include actual snippets where appropriate.
Do not turn all domain-specific prompts into workplace memos.
Return {{"families":[{{"concept":"short distinct scenario description",
"variants":[{{"variant":"explicit","prompt":"...","rationale":"why the requested action fits"}},
{{"variant":"implicit","prompt":"...","rationale":"..."}},
{{"variant":"keyword_collision","prompt":"...","rationale":"..."}},
{{"variant":"quoted_distraction","prompt":"...","rationale":"..."}}]}}]}}."""


class AuthoringClient:
    """Two fixed official API destinations with bounded, non-reflecting errors."""

    def __init__(self, session: aiohttp.ClientSession) -> None:
        self.session = session

    async def call(
        self, provider: str, system: str, user: str, *, cached: Path,
        response_schema: dict | None = None,
    ) -> dict:
        spec = {"provider": provider, "system": system, "user": user,
                "model": GENERATOR if provider == "openai" else REVIEWER}
        if response_schema is not None:
            spec["response_schema"] = response_schema
        request_hash = digest(spec)
        if cached.exists():
            saved = read_json(cached)
            if saved["request_sha256"] == request_hash:
                return saved
        if provider == "openai":
            endpoint = "https://api.openai.com/v1/chat/completions"
            headers = {"Authorization": "Bearer " + os.environ["OPENAI_API_KEY"]}
            payload = {
                "model": GENERATOR, "temperature": 0.7, "max_tokens": 5000,
                "response_format": {"type": "json_object"},
                "messages": [{"role": "system", "content": system},
                             {"role": "user", "content": user}],
            }
            if response_schema is not None:
                payload["response_format"] = {
                    "type": "json_schema",
                    "json_schema": {"name": "case_revisions", "strict": True, "schema": response_schema},
                }
        else:
            endpoint = "https://api.anthropic.com/v1/messages"
            headers = {"x-api-key": os.environ["ANTHROPIC_API_KEY"],
                       "anthropic-version": "2023-06-01"}
            payload = {"model": REVIEWER, "temperature": 0, "max_tokens": 6000,
                       "system": system, "messages": [{"role": "user", "content": user}]}
        for attempt in range(8):
            async with self.session.post(endpoint, headers=headers, json=payload,
                                         allow_redirects=False) as response:
                if response.status in {429, 500, 502, 503, 529} and attempt < 7:
                    body = await response.json(content_type=None)
                    if body.get("error", {}).get("code") == "insufficient_quota":
                        raise RuntimeError(f"{provider} authoring quota unavailable")
                    try:
                        delay = float(response.headers.get("retry-after", "0"))
                    except ValueError:
                        delay = 0
                    await asyncio.sleep(min(30, max(delay, 10 * (attempt + 1))))
                    continue
                if response.status != 200:
                    raise RuntimeError(f"{provider} authoring HTTP {response.status}")
                document = await response.json()
            if provider == "openai":
                choice = document["choices"][0]
                if choice["finish_reason"] != "stop":
                    raise RuntimeError("authoring output did not finish")
                text = choice["message"]["content"]
            else:
                if document["stop_reason"] != "end_turn":
                    raise RuntimeError("review output did not finish")
                text = "".join(x.get("text", "") for x in document["content"])
            text = text.strip()
            if text.startswith("```"):
                text = text.split("\n", 1)[1].rsplit("```", 1)[0]
            try:
                parsed = json.loads(text)
            except ValueError:
                raise RuntimeError(f"{provider} authoring returned invalid JSON") from None
            saved = {"request_sha256": request_hash, "model": document["model"],
                     "usage": document.get("usage", {}), "output": parsed,
                     "parameters": {"temperature": payload["temperature"],
                                    "max_tokens": payload["max_tokens"]},
                     "created_at": datetime.now(timezone.utc).isoformat()}
            write_json(cached, saved)
            return saved
        raise RuntimeError("authoring retry budget exhausted")


def flatten(label: str, domain: str, document: dict) -> list[dict]:
    """Assign opaque IDs independently of the text or later model predictions."""
    families = document.get("families", [])
    if len(families) != 5:
        raise ValueError("author must supply exactly five families")
    cases = []
    for slot, family in enumerate(families):
        fid = family_id(label, domain, slot)
        variants = family.get("variants", [])
        if Counter(v.get("variant") for v in variants) != Counter(VARIANTS):
            raise ValueError("each family needs all four distinct variants")
        for variant in variants:
            prompt = variant["prompt"]
            if not isinstance(prompt, str) or not prompt.strip():
                raise ValueError("author supplied an empty prompt")
            cases.append({
                "id": digest([fid, variant["variant"]])[:20], "family_id": fid,
                "family_slot": slot, "concept": family["concept"],
                "domain": domain, "variant": variant["variant"],
                "prompt": prompt, "expected_task": label,
                "rationale": variant["rationale"],
                "tags": ["english", domain, variant["variant"]],
            })
    return cases


async def generate(client: AuthoringClient) -> None:
    semaphore = asyncio.Semaphore(3)
    completed = 0

    async def one(label: str, domain: str) -> None:
        nonlocal completed
        async with semaphore:
            path = WORK / "generation" / f"{label}--{domain}.json"
            response = await client.call("openai", AUTHOR_SYSTEM, generation_request(label, domain),
                                         cached=path)
            flatten(label, domain, response["output"])
            completed += 1
            print(f"Authored {completed}/60 batches; label and length review follows", flush=True)

    await asyncio.gather(*(one(label, domain) for label in TASK_TYPES for domain in DOMAINS))


def collect() -> list[dict]:
    cases = []
    for label in TASK_TYPES:
        for domain in DOMAINS:
            response = read_json(WORK / "generation" / f"{label}--{domain}.json")
            cases.extend(flatten(label, domain, response["output"]))
    repairs = WORK / "repairs"
    if repairs.exists():
        for path in sorted(repairs.glob("*.json")):
            saved = read_json(path)
            replacements = {x["id"]: x for x in saved["output"]["replacements"]}
            for case in cases:
                if case["id"] in replacements:
                    case.update({k: replacements[case["id"]][k] for k in ("prompt", "rationale")})
    return cases


async def audit(client: AuthoringClient, cases: list[dict]) -> list[dict]:
    """Cache reviews per exact prompt, retaining valid entries from partial batches."""
    shuffled = sorted(cases, key=lambda case: case["id"])
    random.Random(SEED).shuffle(shuffled)
    accepted = {}
    semaphore = asyncio.Semaphore(4)

    def cache_path(case: dict) -> Path:
        return WORK / "reviewed-cases" / f"{digest([REVIEWER, REVIEW_SYSTEM, case['id'], case['prompt']])}.json"

    def valid(review: dict) -> bool:
        return (type(review.get("ambiguous")) is bool
                and (review.get("label") in TASK_TYPES or review["ambiguous"]))

    def retain(case: dict, review: dict, saved: dict) -> None:
        record = {**review, "id": case["id"], "reviewed_model": saved["model"],
                  "source_request_sha256": saved["request_sha256"]}
        write_json(cache_path(case), record)
        accepted[case["id"]] = record

    # Import the already-completed original batches. Unknown, duplicate, or
    # mistyped IDs are never assigned by position or guessed; those prompts are
    # submitted again. Only unchanged prompt batches can reuse these records.
    for i in range(0, len(shuffled), 20):
        chunk = shuffled[i:i+20]
        payload = [{"id": c["id"], "prompt": c["prompt"]} for c in chunk]
        path = WORK / "reviews" / f"{digest(payload)}.json"
        if not path.exists():
            continue
        saved = read_json(path)
        expected_hash = digest({"provider": "anthropic", "system": REVIEW_SYSTEM,
                                "user": json.dumps(payload), "model": REVIEWER})
        if saved["request_sha256"] != expected_hash:
            continue
        reviews = saved["output"].get("reviews", [])
        counts = Counter(r.get("id") for r in reviews)
        by_id = {r.get("id"): r for r in reviews if isinstance(r, dict)}
        for case in chunk:
            review = by_id.get(case["id"], {})
            if counts[case["id"]] == 1 and valid(review):
                retain(case, review, saved)
    for case in shuffled:
        path = cache_path(case)
        if path.exists():
            saved = read_json(path)
            if saved.get("id") == case["id"] and valid(saved):
                accepted[case["id"]] = saved
    print(f"Blind review: {len(accepted)}/{len(cases)} exact-prompt reviews restored", flush=True)

    async def batch(chunk: list[dict], round_number: int) -> None:
        # Short opaque transport IDs avoid copying errors in 20-character hashes.
        # They carry no gold category, family, tag, or author rationale.
        mapping = {f"item-{i:02d}": case for i, case in enumerate(chunk)}
        payload = [{"id": key, "prompt": case["prompt"]} for key, case in mapping.items()]
        path = WORK / "reviews" / f"short-{digest(payload)}-{round_number}.json"
        async with semaphore:
            saved = await client.call("anthropic", REVIEW_SYSTEM, json.dumps(payload), cached=path)
            reviews = saved["output"].get("reviews", [])
            counts = Counter(r.get("id") for r in reviews)
            for review in reviews:
                key = review.get("id")
                if key in mapping and counts[key] == 1 and valid(review):
                    retain(mapping[key], review, saved)
            print(f"Blind review: {len(accepted)}/{len(cases)} prompts complete", flush=True)

    for round_number in range(3):
        missing = [case for case in shuffled if case["id"] not in accepted]
        if not missing:
            return [accepted[case["id"]] for case in cases]
        results = await asyncio.gather(*(batch(missing[i:i+20], round_number)
                                         for i in range(0, len(missing), 20)),
                                       return_exceptions=True)
        failures = [r for r in results if isinstance(r, Exception)]
        if failures:
            raise RuntimeError("blind review batch failed: " + type(failures[0]).__name__) from None
    raise ValueError("reviewer did not supply a valid matching ID for every prompt")


def audit_failures(cases: list[dict], reviews: list[dict]) -> list[dict]:
    by_id = {r["id"]: r for r in reviews}
    failures = []
    for case in cases:
        review = by_id[case["id"]]
        if not 10 <= len(case["prompt"].split()) <= 160:
            failures.append({"case": case, "review": review, "mechanical_issue":
                             "Prompt must contain 10–160 words; expand or shorten it without changing intent."})
        elif review["ambiguous"] or review["label"] != case["expected_task"]:
            failures.append({"case": case, "review": review})
    return failures


async def repair(client: AuthoringClient, failures: list[dict], round_number: int) -> None:
    """Revise disputed cases; retain earlier revisions across interrupted runs."""
    previous = [int(p.stem.split("-")[0]) for p in (WORK / "repairs").glob("*.json")]
    round_number = max(round_number, max(previous, default=-1) + 1)
    semaphore = asyncio.Semaphore(3)

    async def batch(chunk: list[dict], index: int) -> None:
        mapping = {f"item-{i:02d}": item for i, item in enumerate(chunk)}
        target = chunk[0]["case"]["expected_task"]
        if any(item["case"]["expected_task"] != target for item in chunk):
            raise ValueError("a revision batch must share one intended task")
        transport = [
            {"id": key, "target_task": target,
             "domain": item["case"]["domain"], "concept": item["case"]["concept"],
             "variant": item["case"]["variant"],
             "prompt_to_replace": item["case"]["prompt"],
             "rejection_feedback": item["review"]["reason"],
             "mechanical_issue": item.get("mechanical_issue")}
            for key, item in mapping.items()
        ]
        request = (
            f"EVERY replacement must request the task {target}: {ROUTING_CRITERIA[target]}. "
            "Revise these rejected synthetic prompts before the dataset is frozen. "
            "Return a replacement for EVERY case, including ones you think were already clear. "
            "Use the exact SHORT id as each property name. Keep the domain, family "
            "concept and variant style, but change the requested action to the target task "
            "if necessary. The previous prompt was rejected: do not merely make its "
            "wrong task clearer. Ask for ONLY one action; remove secondary user instructions. "
            "Misleading task words may remain in quoted/source material. Include all "
            "source context and stay within 15–140 words. Invent concrete synthetic source "
            "passages/code if needed; NEVER use placeholders such as '[Insert text here]'. "
            "Do not mention labels or audits "
            "in the user prompt. Return {\"item-00\":{\"prompt\":\"...\","
            "\"rationale\":\"...\"}, \"item-01\":{...}, ...}.\n" + json.dumps(transport)
        )
        item_schema = {
            "type": "object", "properties": {"prompt": {"type": "string"}, "rationale": {"type": "string"}},
            "required": ["prompt", "rationale"], "additionalProperties": False,
        }
        schema = {"type": "object", "properties": {key: item_schema for key in mapping},
                  "required": list(mapping), "additionalProperties": False}
        async with semaphore:
            saved = await client.call(
                "openai", AUTHOR_SYSTEM, request,
                cached=WORK / "repair-requests" / f"{round_number:02d}-{index:03d}.json",
                response_schema=schema,
            )
            replacements = [{"id": key, **value} for key, value in saved["output"].items()]
            counts = Counter(x.get("id") for x in replacements)
            normalized_replacements = []
            for replacement in replacements:
                key = replacement.get("id")
                if (key in mapping and counts[key] == 1
                        and isinstance(replacement.get("prompt"), str)
                        and isinstance(replacement.get("rationale"), str)):
                    normalized_replacements.append({**replacement, "id": mapping[key]["case"]["id"]})
            write_json(WORK / "repairs" / f"{round_number:02d}-{index:03d}.json", {
                **saved, "transport_ids_normalized": True,
                "output": {"replacements": normalized_replacements},
            })
            print(f"Revision batch {index + 1}: {len(normalized_replacements)}/{len(chunk)} prompts retained", flush=True)

    write_json(WORK / f"disputes-{round_number}.json", failures)
    chunks = []
    for label in TASK_TYPES:
        selected = [item for item in failures if item["case"]["expected_task"] == label]
        chunks.extend(selected[i:i+8] for i in range(0, len(selected), 8))
    results = await asyncio.gather(*(batch(chunk, index) for index, chunk in enumerate(chunks)),
                                   return_exceptions=True)
    errors = [r for r in results if isinstance(r, Exception)]
    if errors:
        raise RuntimeError("revision batch failed: " + type(errors[0]).__name__) from None


def validate_cases(cases: list[dict], *, expected_size: int = 1200) -> dict:
    """Validate size, diversity, grouping and exact duplicates, without API calls."""
    if len(cases) != expected_size:
        raise ValueError("unexpected dataset size")
    if len({c["id"] for c in cases}) != len(cases):
        raise ValueError("duplicate case IDs")
    if len({normalized(c["prompt"]) for c in cases}) != len(cases):
        raise ValueError("duplicate normalized prompts")
    families = defaultdict(list)
    for case in cases:
        if case["expected_task"] not in TASK_TYPES or case["domain"] not in DOMAINS:
            raise ValueError("invalid class or domain")
        if not 10 <= len(case["prompt"].split()) <= 160:
            raise ValueError("prompt outside acceptance word bounds")
        families[case["family_id"]].append(case)
    for family in families.values():
        if (Counter(c["variant"] for c in family) != Counter(VARIANTS)
                or len({c["expected_task"] for c in family}) != 1
                or len({c["domain"] for c in family}) != 1):
            raise ValueError("invalid scenario family")
    counts = Counter(c["expected_task"] for c in cases)
    if set(counts.values()) != {expected_size // 6}:
        raise ValueError("unbalanced class distribution")
    return {"cases": len(cases), "families": len(families),
            "labels": dict(counts), "domains": dict(Counter(c["domain"] for c in cases))}


def split_cases(cases: list[dict]) -> tuple[list[dict], list[dict]]:
    """One of five families per class/domain is development, chosen before scoring."""
    rng = random.Random(SEED)
    dev_slots = {(label, domain): rng.randrange(5) for label in TASK_TYPES for domain in DOMAINS}
    dev, test = [], []
    for case in sorted(cases, key=lambda c: c["id"]):
        destination = dev if case["family_slot"] == dev_slots[case["expected_task"], case["domain"]] else test
        destination.append(case)
    assert not ({c["family_id"] for c in dev} & {c["family_id"] for c in test})
    return dev, test


def duplicate_audit(cases: list[dict]) -> dict:
    """Audit word-shingle overlap across families and with the earlier corpora."""
    legacy = []
    for filename in ("autorouting-generated-2026-09-16.jsonl",):
        legacy.extend(json.loads(line) for line in (ROOT / "docs/benchmarks" / filename).read_text().splitlines() if line.strip())
    packaged = ROOT / "src/gateway/resources/benchmarks/autorouting.jsonl"
    legacy.extend(json.loads(line) for line in packaged.read_text().splitlines() if line.strip() and not line.startswith("#"))

    def shingles(text: str) -> set[tuple[str, ...]]:
        words = normalized(text).split()
        return {tuple(words[i:i+4]) for i in range(len(words)-3)}

    combined = cases + [{**c, "family_id": "legacy"} for c in legacy]
    sets = [shingles(c["prompt"]) for c in combined]
    findings = []
    max_overlap = 0.0
    for i, case in enumerate(cases):
        for j in range(i+1, len(combined)):
            other = combined[j]
            if case["family_id"] == other["family_id"]:
                continue
            overlap = len(sets[i] & sets[j]) / max(1, len(sets[i] | sets[j]))
            max_overlap = max(max_overlap, overlap)
            if normalized(case["prompt"]) == normalized(other["prompt"]) or overlap >= 0.65:
                findings.append({"id": case["id"], "other_id": other["id"], "jaccard": overlap})
    return {"method": "normalized four-word-shingle Jaccard; cross-family and legacy only",
            "threshold": 0.65, "max_overlap": max_overlap, "findings": findings,
            "legacy_cases_checked": len(legacy)}


def freeze(cases: list[dict], reviews: list[dict]) -> dict:
    if (DATA / "manifest.json").exists():
        raise ValueError("dataset already frozen; create a new version rather than overwrite")
    totals = validate_cases(cases)
    if audit_failures(cases, reviews):
        raise ValueError("unresolved label disputes")
    duplication = duplicate_audit(cases)
    if duplication["findings"]:
        write_json(WORK / "duplicate-findings.json", duplication)
        raise ValueError("cross-family/legacy near duplicates require review before freeze")
    dev, test = split_cases(cases)
    for name, rows in (("development", dev), ("test", test)):
        DATA.mkdir(parents=True, exist_ok=True)
        (DATA / f"{name}.jsonl").write_text(
            "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows)
        )
    write_json(DATA / "label-review.json", sorted(reviews, key=lambda row: row["id"]))
    disputes = [item for path in sorted(WORK.glob("disputes-*.json")) for item in read_json(path)]
    write_json(DATA / "pre-freeze-revisions.json", disputes)
    write_json(DATA / "duplicate-audit.json", duplication)
    usage = []
    for directory in ("generation", "repairs", "reviews"):
        for path in sorted((WORK / directory).glob("*.json")):
            record = read_json(path)
            usage.append({k: record[k] for k in ("model", "usage", "created_at", "request_sha256")})
    write_json(DATA / "authoring-usage.json", usage)
    manifest = {
        "schema": "axonllm.routing-corpus/v2", "seed": SEED,
        "frozen_at": datetime.now(timezone.utc).isoformat(),
        "kind": "synthetic; AI-authored and cross-provider model-reviewed; not human-labeled",
        "generator": GENERATOR, "reviewer": REVIEWER,
        "authoring_policy_sha256": digest(AUTHOR_SYSTEM), "review_policy_sha256": digest(REVIEW_SYSTEM),
        "totals": totals, "development": {"cases": len(dev), "families": len(dev)//4},
        "test": {"cases": len(test), "families": len(test)//4},
        "pre_freeze_revisions": len(disputes),
        "files": {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                  for p in sorted(DATA.iterdir()) if p.is_file()},
        "locked_router_implementation": {
            name: hashlib.sha256((ROOT / "src/gateway" / name).read_bytes()).hexdigest()
            for name in ("task_classifier.py", "decision_model_routing.py")
        },
        "routing_system_prompt_sha256": digest(
            __import__("src.gateway.autorouting_benchmark", fromlist=["ROUTER_SYSTEM_PROMPT"]).ROUTER_SYSTEM_PROMPT
        ),
    }
    write_json(DATA / "manifest.json", manifest)
    return manifest


def verify_frozen() -> dict:
    manifest = read_json(DATA / "manifest.json")
    for filename, expected in manifest["files"].items():
        if hashlib.sha256((DATA / filename).read_bytes()).hexdigest() != expected:
            raise ValueError(f"frozen file changed: {filename}")
    dev = [json.loads(line) for line in (DATA / "development.jsonl").read_text().splitlines()]
    test = [json.loads(line) for line in (DATA / "test.jsonl").read_text().splitlines()]
    validate_cases(dev + test)
    if {c["family_id"] for c in dev} & {c["family_id"] for c in test}:
        raise ValueError("scenario family leaked across splits")
    if audit_failures(dev + test, read_json(DATA / "label-review.json")):
        raise ValueError("frozen labels do not agree with blind review")
    return manifest


async def build() -> None:
    if (DATA / "manifest.json").exists():
        print(json.dumps(verify_frozen(), indent=2))
        return
    timeout = aiohttp.ClientTimeout(total=240)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        client = AuthoringClient(session)
        await generate(client)
        for round_number in range(4):
            cases = collect()
            reviews = await audit(client, cases)
            failures = audit_failures(cases, reviews)
            print(f"Label audit: {len(failures)} disputed prompts, round {round_number}", flush=True)
            if not failures:
                print(json.dumps(freeze(cases, reviews), indent=2))
                return
            if round_number < 3:
                await repair(client, failures, round_number)
        raise RuntimeError("unresolved label disputes after three revision rounds")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("build", "validate"))
    args = parser.parse_args()
    if args.command == "validate":
        print(json.dumps(verify_frozen(), indent=2))
    else:
        asyncio.run(build())


if __name__ == "__main__":
    main()
