"""Minimal HTTP client for a free-tier chat LLM (Gemini or an OpenAI-compatible
API). Standard library only (urllib) — one HTTP call does not need an SDK.
Configured entirely from environment variables; see .env.example.
"""
import json
import os
import urllib.error
import urllib.request


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
    "gemini": (_call_gemini, "gemini-2.0-flash"),
    "openai": (_call_openai, "gpt-4o-mini"),
}


def ask(prompt):
    provider = os.environ.get("LLM_PROVIDER", "").strip().lower()
    api_key = os.environ.get("LLM_API_KEY", "").strip()
    if not provider or not api_key:
        raise LLMError(
            "LLM_PROVIDER/LLM_API_KEY chưa được cấu hình trong .env "
            "(xem .env.example để biết cách lấy API key miễn phí)"
        )
    entry = _PROVIDERS.get(provider)
    if entry is None:
        raise LLMError(f"LLM_PROVIDER không hỗ trợ: {provider!r} (chỉ 'gemini' hoặc 'openai')")
    call, default_model = entry
    model = os.environ.get("LLM_MODEL", "").strip() or default_model

    try:
        return call(api_key, model, prompt)
    except LLMError:
        raise
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:300]
        raise LLMError(f"Lời gọi LLM thất bại (HTTP {exc.code}): {detail}") from None
    except urllib.error.URLError as exc:
        raise LLMError(f"Không kết nối được tới dịch vụ LLM: {exc.reason}") from None
