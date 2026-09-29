"""Minimal HTTP client for a free-tier chat LLM (Gemini or an OpenAI-compatible
API). Standard library only (urllib) — one HTTP call does not need an SDK.
Configured entirely from environment variables; see .env.example.

The free tier returns 503 ("model overloaded") fairly often at busy times,
and a given key can also hit its own per-minute quota (429). Neither means
the *question* failed, so `ask()` rotates through every configured model and
every configured key (in that order — 503 is a model problem, 429 is a key
problem) and only gives up after every combination has failed.
"""
import concurrent.futures
import json
import logging
import os
import urllib.error
import urllib.request

log = logging.getLogger("uvicorn.error")

DEFAULT_MODELS = {
    # "-latest" are Google's own self-updating aliases — safer as a default
    # than pinning an exact version, which WILL be deprecated eventually.
    "gemini": ["gemini-flash-latest", "gemini-2.5-flash", "gemini-pro-latest"],
    "openai": ["gpt-4o-mini", "gpt-4o"],
}

# Halved from the old 30s: the free tier either answers quickly or is
# overloaded, and a shorter timeout gets to the (usually working) next
# model/key sooner instead of sitting on a lost cause.
REQUEST_TIMEOUT_SECONDS = 15


class LLMError(Exception):
    def __init__(self, message):
        self.message = message
        super().__init__(message)


def _call_gemini(api_key, model, prompt):
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"
    payload = json.dumps({"contents": [{"parts": [{"text": prompt}]}]}).encode("utf-8")
    req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_SECONDS) as response:
        data = json.loads(response.read())
    try:
        return data["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError):
        raise LLMError("Gemini trả về phản hồi không đúng định dạng mong đợi") from None


def _call_openai(api_key, model, prompt):
    url = "https://api.openai.com/v1/chat/completions"
    payload = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
    }).encode("utf-8")
    req = urllib.request.Request(
        url, data=payload, method="POST",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
    )
    with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_SECONDS) as response:
        data = json.loads(response.read())
    try:
        return data["choices"][0]["message"]["content"]
    except (KeyError, IndexError):
        raise LLMError("OpenAI trả về phản hồi không đúng định dạng mong đợi") from None


_PROVIDERS = {
    "gemini": _call_gemini,
    "openai": _call_openai,
}


def _split_env_list(value):
    return [item.strip() for item in value.replace("\n", ",").split(",") if item.strip()]


def current_provider():
    return os.environ.get("LLM_PROVIDER", "").strip().lower()


def available_models():
    """Models to offer in the UI dropdown, in rotation order."""
    provider = current_provider()
    configured = _split_env_list(os.environ.get("LLM_MODELS", ""))
    if configured:
        return configured
    single = os.environ.get("LLM_MODEL", "").strip()
    if single:
        return [single]
    return list(DEFAULT_MODELS.get(provider, []))


def _api_keys():
    configured = _split_env_list(os.environ.get("LLM_API_KEYS", ""))
    if configured:
        return configured
    single = os.environ.get("LLM_API_KEY", "").strip()
    return [single] if single else []


def _attempt(call, key, model, prompt):
    """Runs one (key, model) combination; never raises — errors come back as data
    so callers (in particular the parallel racer below) don't need a try/except
    per future."""
    try:
        return True, call(key, model, prompt), model, None
    except LLMError as exc:
        return False, None, model, exc.message
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:200]
        return False, None, model, f"model={model} HTTP {exc.code}: {detail}"
    except urllib.error.URLError as exc:
        return False, None, model, f"model={model}: không kết nối được ({exc.reason})"


def _race_models(call, key, models, prompt, provider):
    """Tries every model for one key at once instead of one at a time. A 503 on
    one model doesn't mean the others are overloaded too, so waiting for them
    sequentially (the old behaviour) only adds latency for nothing. Returns as
    soon as one succeeds; the rest keep running in a background thread but are
    never awaited, so a single slow/timed-out model can't hold up the response.
    """
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=len(models))
    last_error = None
    attempts = 0
    try:
        futures = {executor.submit(_attempt, call, key, model, prompt): model for model in models}
        for future in concurrent.futures.as_completed(futures):
            attempts += 1
            ok, answer, model, error = future.result()
            if ok:
                return attempts, answer, model, None
            last_error = error
            log.info("llm_attempt_failed provider=%s model=%s", provider, model)
    finally:
        executor.shutdown(wait=False, cancel_futures=True)
    return attempts, None, None, last_error


def ask(prompt, preferred_model=None):
    """Returns (answer_text, model_used). Rotates keys outer, models inner —
    but within one key, every remaining model is raced in parallel rather
    than tried one at a time (see _race_models)."""
    provider = current_provider()
    keys = _api_keys()
    if not provider or not keys:
        raise LLMError(
            "LLM_PROVIDER/LLM_API_KEY(S) chưa được cấu hình trong .env "
            "(xem .env.example để biết cách lấy API key miễn phí)"
        )
    call = _PROVIDERS.get(provider)
    if call is None:
        raise LLMError(f"LLM_PROVIDER không hỗ trợ: {provider!r} (chỉ 'gemini' hoặc 'openai')")

    models = available_models()
    if not models:
        raise LLMError("Chưa cấu hình LLM_MODEL/LLM_MODELS trong .env")

    last_error = None
    attempts = 0
    for key in keys:
        remaining = models
        if preferred_model:
            # Tried alone, not raced: racing it against the fallback models could
            # return a *different* model's answer even though the person's pick
            # would have worked fine too.
            attempts += 1
            ok, answer, model, error = _attempt(call, key, preferred_model, prompt)
            if ok:
                return answer, model
            last_error = error
            log.info("llm_attempt_failed provider=%s model=%s", provider, preferred_model)
            remaining = [m for m in models if m != preferred_model]
        if not remaining:
            continue
        used, answer, model, error = _race_models(call, key, remaining, prompt, provider)
        attempts += used
        if answer is not None:
            return answer, model
        last_error = error or last_error

    raise LLMError(
        f"Đã thử {attempts} lượt (mô hình × key) đều thất bại. Lỗi gần nhất: {last_error}"
    )
