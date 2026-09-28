"""Minimal HTTP client for a free-tier chat LLM (Gemini or an OpenAI-compatible
API). Standard library only (urllib) — one HTTP call does not need an SDK.
Configured entirely from environment variables; see .env.example.

The free tier returns 503 ("model overloaded") fairly often at busy times,
and a given key can also hit its own per-minute quota (429). Neither means
the *question* failed, so `ask()` rotates through every configured model and
every configured key (in that order — 503 is a model problem, 429 is a key
problem) and only gives up after every combination has failed.
"""
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


class LLMError(Exception):
    def __init__(self, message):
        self.message = message
        super().__init__(message)


def _call_gemini(api_key, model, prompt):
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"
    payload = json.dumps({"contents": [{"parts": [{"text": prompt}]}]}).encode("utf-8")
    req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=30) as response:
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
    with urllib.request.urlopen(req, timeout=30) as response:
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


def ask(prompt, preferred_model=None):
    """Returns (answer_text, model_used). Rotates models first, then keys."""
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
    if preferred_model:
        # A model the person picked in the UI goes first; the rest stay as fallback.
        models = [preferred_model] + [m for m in models if m != preferred_model]
    if not models:
        raise LLMError("Chưa cấu hình LLM_MODEL/LLM_MODELS trong .env")

    last_error = None
    attempts = 0
    # Model is the inner loop: a 503 means THIS model is overloaded, which a
    # different key won't fix, but a different model usually will. Only once
    # every model has failed for a key do we move on to the next key (that
    # key may simply be out of quota).
    for key in keys:
        for model in models:
            attempts += 1
            try:
                return call(key, model, prompt), model
            except LLMError as exc:
                last_error = exc.message
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", errors="replace")[:200]
                last_error = f"model={model} HTTP {exc.code}: {detail}"
                log.info("llm_attempt_failed provider=%s model=%s status=%s", provider, model, exc.code)
            except urllib.error.URLError as exc:
                last_error = f"model={model}: không kết nối được ({exc.reason})"

    raise LLMError(
        f"Đã thử {attempts} lượt (mô hình × key) đều thất bại. Lỗi gần nhất: {last_error}"
    )
