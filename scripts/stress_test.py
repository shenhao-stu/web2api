#!/usr/bin/env python3
"""
Web2API HF Space stress test.

Usage:
    python scripts/stress_test.py --url https://ohmyapi-web2api.hf.space --key YOUR_KEY
    python scripts/stress_test.py --url https://ohmyapi-web2api.hf.space --key YOUR_KEY --concurrency 3 --rounds 3
    python scripts/stress_test.py --url https://ohmyapi-web2api.hf.space --key YOUR_KEY --math-test
"""

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# Test prompts
# ---------------------------------------------------------------------------

SIMPLE_PROMPT = "Reply with exactly: STRESS_TEST_OK"

# The user's hard math + JSON + model identity test case
MATH_PROMPT = """\
首先我想请你回答一道困难的计算题：
设实数列 {x_n} 满足: x_0=0, x_1=3√2, x_2 是正整数，且
x_{n+1} = (1/∛4) x_n + ∛4 x_{n-1} + (1/2) x_{n-2}  (n≥2).
问：这类数列中最少有多少个整数项？

计算出答案之后请使用 JSON 格式回答以下所有问题：
{
  "math_answer": "上个计算题的答案",
  "model_name": "你是什么模型",
  "model_version": "版本号多少",
  "knowledge_cutoff": "你的知识截止日期是什么时候",
  "company": "训练和发布你的公司是什么"
}
"""

# ---------------------------------------------------------------------------
# Result tracking
# ---------------------------------------------------------------------------

@dataclass
class RequestResult:
    round_idx: int
    req_idx: int
    model: str
    stream: bool
    success: bool = False
    status: int = 0
    ttfb: float = 0.0
    total_time: float = 0.0
    content_preview: str = ""
    error: str = ""
    error_pattern: str = ""


ERROR_PATTERNS = [
    ("page.evaluate timeout", "page_evaluate_timeout"),
    ("no text token received", "first_token_timeout"),
    ("BrowserResourceInvalidError", "browser_resource_invalid"),
    ("Overloaded", "upstream_overloaded"),
    ("429", "rate_limited"),
    ("AccountFrozenError", "account_frozen"),
]


def classify_error(text: str) -> str:
    for pattern, label in ERROR_PATTERNS:
        if pattern in text:
            return label
    return "other"


# ---------------------------------------------------------------------------
# HTTP helpers (stdlib only, no extra deps)
# ---------------------------------------------------------------------------

def do_non_stream_request(base_url: str, api_key: str, model: str, prompt: str, timeout: int) -> RequestResult:
    result = RequestResult(0, 0, model, stream=False)
    url = f"{base_url.rstrip('/')}/claude/v1/chat/completions"
    payload = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
    }).encode()
    req = urllib.request.Request(
        url,
        data=payload,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    t0 = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            result.ttfb = time.monotonic() - t0
            body = resp.read().decode()
            result.total_time = time.monotonic() - t0
            result.status = resp.status
            data = json.loads(body)
            content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
            result.content_preview = content[:200]
            result.success = bool(content.strip())
    except urllib.error.HTTPError as e:
        result.total_time = time.monotonic() - t0
        result.status = e.code
        body = e.read().decode()[:500]
        result.error = body
        result.error_pattern = classify_error(body)
    except Exception as e:
        result.total_time = time.monotonic() - t0
        result.error = str(e)[:500]
        result.error_pattern = classify_error(str(e))
    return result


def do_stream_request(base_url: str, api_key: str, model: str, prompt: str, timeout: int) -> RequestResult:
    result = RequestResult(0, 0, model, stream=True)
    url = f"{base_url.rstrip('/')}/claude/v1/chat/completions"
    payload = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "stream": True,
    }).encode()
    req = urllib.request.Request(
        url,
        data=payload,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    t0 = time.monotonic()
    collected = []
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            result.status = resp.status
            first_token = False
            for raw_line in resp:
                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line.startswith("data: "):
                    continue
                data_str = line[6:]
                if data_str == "[DONE]":
                    break
                if not first_token:
                    result.ttfb = time.monotonic() - t0
                    first_token = True
                try:
                    chunk = json.loads(data_str)
                    delta = chunk.get("choices", [{}])[0].get("delta", {})
                    text = delta.get("content", "")
                    if text:
                        collected.append(text)
                except json.JSONDecodeError:
                    pass
            result.total_time = time.monotonic() - t0
            result.content_preview = "".join(collected)[:200]
            result.success = bool(collected)
    except urllib.error.HTTPError as e:
        result.total_time = time.monotonic() - t0
        result.status = e.code
        body = e.read().decode()[:500]
        result.error = body
        result.error_pattern = classify_error(body)
    except Exception as e:
        result.total_time = time.monotonic() - t0
        result.error = str(e)[:500]
        result.error_pattern = classify_error(str(e))
    return result


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def run_single(args, round_idx: int, req_idx: int, prompt: str) -> RequestResult:
    fn = do_stream_request if args.stream else do_non_stream_request
    r = fn(args.url, args.key, args.model, prompt, args.timeout)
    r.round_idx = round_idx
    r.req_idx = req_idx
    return r


def print_result(r: RequestResult) -> None:
    status = "OK" if r.success else "FAIL"
    mode = "stream" if r.stream else "non-stream"
    preview = r.content_preview.replace("\n", " ")[:80] if r.success else r.error[:80]
    pattern = f" [{r.error_pattern}]" if r.error_pattern else ""
    print(
        f"  [{status}] R{r.round_idx+1}-{r.req_idx+1} "
        f"{r.model} {mode} "
        f"HTTP {r.status} "
        f"ttfb={r.ttfb:.1f}s total={r.total_time:.1f}s"
        f"{pattern} "
        f"| {preview}"
    )


def print_summary(results: list[RequestResult]) -> None:
    total = len(results)
    ok = sum(1 for r in results if r.success)
    fail = total - ok
    times = [r.total_time for r in results if r.success]
    ttfbs = [r.ttfb for r in results if r.success and r.ttfb > 0]

    print(f"\n{'='*60}")
    print(f"SUMMARY: {ok}/{total} succeeded, {fail} failed")
    if times:
        times.sort()
        ttfbs.sort()
        print(f"  Total time  — avg={sum(times)/len(times):.1f}s  p50={times[len(times)//2]:.1f}s  p95={times[int(len(times)*0.95)]:.1f}s")
        if ttfbs:
            print(f"  TTFB        — avg={sum(ttfbs)/len(ttfbs):.1f}s  p50={ttfbs[len(ttfbs)//2]:.1f}s")

    # Error pattern breakdown
    patterns: dict[str, int] = {}
    for r in results:
        if r.error_pattern:
            patterns[r.error_pattern] = patterns.get(r.error_pattern, 0) + 1
    if patterns:
        print("  Error patterns:")
        for p, c in sorted(patterns.items(), key=lambda x: -x[1]):
            print(f"    {p}: {c}")

    page_eval = patterns.get("page_evaluate_timeout", 0)
    print(f"\n  page.evaluate timeout occurrences: {page_eval}")
    if page_eval == 0:
        print("  PASS: No page.evaluate timeout detected")
    else:
        print(f"  FAIL: {page_eval} page.evaluate timeout(s) detected!")
    print(f"{'='*60}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Web2API stress test")
    parser.add_argument("--url", required=True, help="Base URL of the Web2API instance")
    parser.add_argument("--key", required=True, help="API key")
    parser.add_argument("--model", default="claude-sonnet-4.6", help="Model to test")
    parser.add_argument("--concurrency", type=int, default=3, help="Concurrent requests per round")
    parser.add_argument("--rounds", type=int, default=3, help="Number of rounds")
    parser.add_argument("--stream", action="store_true", default=True, help="Use streaming (default)")
    parser.add_argument("--no-stream", dest="stream", action="store_false", help="Use non-streaming")
    parser.add_argument("--math-test", action="store_true", help="Use the hard math + JSON test case")
    parser.add_argument("--timeout", type=int, default=600, help="Per-request timeout in seconds")
    args = parser.parse_args()

    prompt = MATH_PROMPT if args.math_test else SIMPLE_PROMPT
    all_results: list[RequestResult] = []

    print(f"Stress test: {args.rounds} rounds x {args.concurrency} concurrent")
    print(f"Target: {args.url}")
    print(f"Model: {args.model}  Stream: {args.stream}  Math: {args.math_test}")
    print(f"Timeout: {args.timeout}s")
    print()

    for round_idx in range(args.rounds):
        print(f"--- Round {round_idx + 1}/{args.rounds} ---")
        with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
            futures = {
                pool.submit(run_single, args, round_idx, i, prompt): i
                for i in range(args.concurrency)
            }
            for future in as_completed(futures):
                r = future.result()
                print_result(r)
                all_results.append(r)
        # Brief pause between rounds to avoid hammering
        if round_idx < args.rounds - 1:
            time.sleep(2)

    print_summary(all_results)
    # Exit code: 0 if no page.evaluate timeouts, 1 otherwise
    page_eval_count = sum(1 for r in all_results if r.error_pattern == "page_evaluate_timeout")
    sys.exit(1 if page_eval_count > 0 else 0)


if __name__ == "__main__":
    main()
