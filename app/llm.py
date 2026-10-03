"""Minimal HTTP client for a free-tier chat LLM (Gemini or an OpenAI-compatible
API). Standard library only (urllib) — one HTTP call does not need an SDK.
Configured entirely from environment variables; see .env.example.

Every call sends a separate system instruction and an explicit temperature
(taken from the active domain, see app/domains/). Without them Gemini runs at
its default ~1.0 and answers drift in tone, length and layout between calls.

The free tier returns 503 ("model overloaded") fairly often at busy times,
and a given key can also hit its own per-minute quota (429). Neither means
the *question* failed, so `ask()` rotates through every configured model and
every configured key (keys outer, models inner — 503 is a model problem, 429
is a key problem) and only gives up after every combination has failed.

Models are tried one at a time, in the configured order, not raced in
parallel: racing spent one request of every model's free quota per question
(hitting 429 sooner) and returned whichever model was fastest — usually the
weakest — so answer quality changed from one question to the next.
"""
import json
import logging
import os
import time
import urllib.error
import urllib.request

from app.repositories import usage as usage_repository

log = logging.getLogger("uvicorn.error")

DEFAULT_MODELS = {
    # "-latest" are Google's own self-updating aliases — safer as a default
    # than pinning an exact version, which WILL be deprecated eventually.
    "gemini": ["gemini-flash-latest", "gemini-2.5-flash", "gemini-pro-latest"],
    "openai": ["gpt-4o-mini", "gpt-4o"],
}

# Answers are now longer and structured (and may carry the full text of the
# selected documents), so 15s cut off legitimate replies. 503/429 come back in
# well under a second, so this only bounds a genuinely hung request.
DEFAULT_TIMEOUT_SECONDS = 45


class LLMError(Exception):
    def __init__(self, message):
        self.message = message
        super().__init__(message)


def _timeout():
    try:
        return float(os.environ.get("LLM_TIMEOUT_SECONDS", "") or DEFAULT_TIMEOUT_SECONDS)
    except ValueError:
        return DEFAULT_TIMEOUT_SECONDS


def _post_json(url, payload, headers):
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"), method="POST",
        headers={"Content-Type": "application/json", **headers},
    )
    with urllib.request.urlopen(req, timeout=_timeout()) as response:
        return json.loads(response.read())


def _gemini_generation_config(temperature):
    config = {}
    if temperature is not None:
        config["temperature"] = temperature
    # Opt-in: thinking models (2.5 / "-latest") spend seconds reasoning even on
    # simple questions. A budget caps that, but the accepted field differs
    # between model generations — unset means "leave the model's default".
    budget = os.environ.get("LLM_THINKING_BUDGET", "").strip()
    if budget.lstrip("-").isdigit():
        config["thinkingConfig"] = {"thinkingBudget": int(budget)}
    return config


def _call_gemini(api_key, model, prompt, system=None, temperature=None, history=()):
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"
    contents = [{"role": role, "parts": [{"text": text}]} for role, text in history]
    contents.append({"role": "user", "parts": [{"text": prompt}]})
    payload = {"contents": contents}
    if system:
        payload["systemInstruction"] = {"parts": [{"text": system}]}
    config = _gemini_generation_config(temperature)
    if config:
        payload["generationConfig"] = config
    data = _post_json(url, payload, {})
    try:
        candidate = data["candidates"][0]
        # Thinking models may return a "thought" part before the answer.
        text = "".join(p.get("text", "") for p in candidate["content"]["parts"] if not p.get("thought"))
    except (KeyError, IndexError, TypeError):
        raise LLMError("Gemini trả về phản hồi không đúng định dạng mong đợi") from None
    if not text.strip():
        reason = candidate.get("finishReason", "?")
        raise LLMError(f"Gemini trả về câu trả lời rỗng (finishReason={reason})")
    usage = data.get("usageMetadata") or {}
    return text, {
        "input": usage.get("promptTokenCount"),
        "output": usage.get("candidatesTokenCount"),
        "thinking": usage.get("thoughtsTokenCount"),
        "cached": usage.get("cachedContentTokenCount"),
    }


def _openai_base_url():
    """https://api.openai.com/v1 unless overridden — any server speaking the
    same chat-completions protocol works here, which covers more than OpenAI
    itself: Ollama, LM Studio, vLLM and most local-model runners all expose
    an OpenAI-compatible endpoint, so pointing LLM_BASE_URL at one (e.g.
    http://localhost:11434/v1 for Ollama) is enough to use it — no code
    change, just LLM_PROVIDER=openai + this setting in .env or /admin/settings."""
    return os.environ.get("LLM_BASE_URL", "").strip().rstrip("/") or "https://api.openai.com/v1"


def _call_openai(api_key, model, prompt, system=None, temperature=None, history=()):
    messages = [{"role": "system", "content": system}] if system else []
    messages.extend(
        {"role": "assistant" if role == "model" else "user", "content": text} for role, text in history
    )
    messages.append({"role": "user", "content": prompt})
    payload = {"model": model, "messages": messages}
    if temperature is not None:
        payload["temperature"] = temperature
    data = _post_json(
        f"{_openai_base_url()}/chat/completions", payload,
        {"Authorization": f"Bearer {api_key}"},
    )
    try:
        text = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError):
        raise LLMError("OpenAI trả về phản hồi không đúng định dạng mong đợi") from None
    usage = data.get("usage") or {}
    return text, {"input": usage.get("prompt_tokens"), "output": usage.get("completion_tokens")}


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


def _normalize_history(history):
    """[(role, text)] with role "user"/"model": drops empty turns (an empty
    part is a Gemini 400) and leading model turns (history must open with the
    user), and merges same-role neighbours since roles must alternate."""
    turns = []
    for role, text in history or ():
        text = (text or "").strip()
        if not text or role not in ("user", "model"):
            continue
        if not turns and role == "model":
            continue
        if turns and turns[-1][0] == role:
            turns[-1] = (role, turns[-1][1] + "\n\n" + text)
        else:
            turns.append((role, text))
    # The new question is a user turn, so history must end on the model.
    if turns and turns[-1][0] == "user":
        turns.pop()
    return turns


def _record_usage(source, provider, model, started, ok, usage=None, error=None):
    """Best-effort, like every other storage write in this app: a down
    MongoDB must never fail the actual question, it only means this one
    attempt is missing from the admin stats page (app/services/usage_stats.py)."""
    try:
        usage_repository.record(source, provider, model, ok, (time.monotonic() - started) * 1000, usage=usage, error=error)
    except Exception as exc:
        log.warning("llm_usage_record_failed error=%s", type(exc).__name__)


def _attempt(call, key, model, prompt, system, temperature, history, source, provider):
    """Runs one (key, model) combination; never raises — errors come back as data."""
    started = time.monotonic()
    try:
        text, usage = call(key, model, prompt, system=system, temperature=temperature, history=history)
    except LLMError as exc:
        _record_usage(source, provider, model, started, False, error=exc.message)
        return None, f"model={model}: {exc.message}"
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:200]
        _record_usage(source, provider, model, started, False, error=f"HTTP {exc.code}: {detail}")
        return None, f"model={model} HTTP {exc.code}: {detail}"
    except urllib.error.URLError as exc:
        _record_usage(source, provider, model, started, False, error=f"URLError: {exc.reason}")
        return None, f"model={model}: không kết nối được ({exc.reason})"
    except TimeoutError:
        _record_usage(source, provider, model, started, False, error="timeout")
        return None, f"model={model}: quá {_timeout():.0f}s không phản hồi"
    log.info(
        "llm_usage model=%s ms=%d input_tokens=%s output_tokens=%s thinking_tokens=%s cached_tokens=%s",
        model, (time.monotonic() - started) * 1000,
        usage.get("input"), usage.get("output"), usage.get("thinking"), usage.get("cached"),
    )
    _record_usage(source, provider, model, started, True, usage=usage)
    return text, None


def ask(prompt, preferred_model=None, system=None, temperature=None, history=None, source="ask"):
    """Returns (answer_text, model_used). Keys outer, models inner, one at a
    time; `preferred_model` (the person's pick in the UI) goes first.
    `history`: earlier turns as [(role, text)], role "user" or "model".
    `source`: a short label ("ask", "graph", ...) recorded with every
    attempt's usage (app/repositories/usage.py) — which feature spent the
    tokens, for the admin stats page's per-source breakdown."""
    history = _normalize_history(history)
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
    if preferred_model:
        models = [preferred_model] + [m for m in models if m != preferred_model]

    last_error = None
    attempts = 0
    for key in keys:
        for model in models:
            attempts += 1
            answer, error = _attempt(call, key, model, prompt, system, temperature, history, source, provider)
            if answer is not None:
                return answer, model
            last_error = error
            log.info("llm_attempt_failed provider=%s model=%s", provider, model)

    raise LLMError(
        f"Đã thử {attempts} lượt (mô hình × key) đều thất bại. Lỗi gần nhất: {last_error}"
    )
