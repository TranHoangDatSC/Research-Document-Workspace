"""Turns images, audio and video into text with Gemini, so the AI can answer
questions about them like any other document: OCR + description for images,
a timestamped transcript (+ scene notes for video) for audio/video.

Uses the Gemini Files API (REST, standard library only — same protocol as
the official google-genai SDK): the file is streamed from MinIO to Google in
UPLOAD_CHUNK pieces, so a large video never sits in this server's memory;
the model then reads it by URI, and the uploaded copy is deleted afterwards
(Google would also expire it after 48 hours).

Privacy: the media leaves your server and goes to Google. On the free tier
Google may use it to improve its products. Turn the feature off with
AI_MEDIA_ANALYSIS=false (see .env.example).
"""
import json
import logging
import os
import time
import urllib.error
import urllib.request

from app import llm
from app.repositories import usage as usage_repository

log = logging.getLogger("uvicorn.error")

API = "https://generativelanguage.googleapis.com"
UPLOAD_CHUNK = 8 * 1024 * 1024  # multiple of 256 KiB, as the protocol requires
PROCESSING_TIMEOUT_SECONDS = 600  # video is transcoded by Google before use
GENERATE_TIMEOUT_SECONDS = 600
DEFAULT_MAX_MB = 200

PROMPTS = {
    "image": (
        "Bạn đang chuyển một hình ảnh thành văn bản để lưu vào kho tài liệu nghiên cứu, phục vụ tìm kiếm và hỏi đáp sau này.\n"
        "1) Chép lại NGUYÊN VĂN toàn bộ chữ có trong ảnh (giữ ngôn ngữ gốc, giữ cấu trúc bảng bằng dấu | nếu có).\n"
        "2) Mô tả nội dung ảnh: loại ảnh (biểu đồ, sơ đồ, ảnh chụp, ảnh màn hình...), các thành phần chính, "
        "số liệu và xu hướng đọc được từ biểu đồ.\n"
        "Trình bày bằng tiếng Việt với hai mục '## Chữ trong ảnh' và '## Mô tả'. Không bịa chi tiết không nhìn thấy."
    ),
    "audio": (
        "Chép lời (transcript) đầy đủ đoạn âm thanh này để lưu vào kho tài liệu nghiên cứu.\n"
        "Mỗi đoạn mở đầu bằng mốc thời gian dạng [mm:ss]; nếu có nhiều người nói, ghi 'Người 1:', 'Người 2:'.\n"
        "Giữ ngôn ngữ gốc. Sau transcript, thêm mục '## Tóm tắt' 3-5 câu bằng tiếng Việt. Không bịa đoạn nghe không rõ — ghi [không rõ]."
    ),
    "video": (
        "Chuyển video này thành văn bản để lưu vào kho tài liệu nghiên cứu.\n"
        "## Lời thoại: transcript đầy đủ, mỗi đoạn có mốc [mm:ss], giữ ngôn ngữ gốc.\n"
        "## Diễn biến hình ảnh: theo mốc [mm:ss], mô tả cảnh, chữ/slide xuất hiện trên màn hình (chép nguyên văn), biểu đồ.\n"
        "## Tóm tắt: 3-5 câu bằng tiếng Việt.\n"
        "Không bịa chi tiết không có trong video."
    ),
}


class MediaAIError(Exception):
    def __init__(self, message, status=503):
        self.message = message
        self.status = status
        super().__init__(message)


def enabled():
    return os.environ.get("AI_MEDIA_ANALYSIS", "true").strip().lower() not in ("false", "0", "no", "off")


def max_bytes():
    raw = os.environ.get("AI_MEDIA_MAX_MB", "").strip()
    return (int(raw) if raw.isdigit() and int(raw) > 0 else DEFAULT_MAX_MB) * 1024 * 1024


def _request(method, url, body=None, headers=None, timeout=60):
    req = urllib.request.Request(url, data=body, method=method, headers=headers or {})
    return urllib.request.urlopen(req, timeout=timeout)


def _upload(key, stream, size, mime, display_name):
    """Resumable upload: one 'start' call, then the bytes in chunks, the last
    one with 'finalize'. Returns the File resource ({name, uri, state, ...})."""
    start = _request(
        "POST", f"{API}/upload/v1beta/files?key={key}",
        body=json.dumps({"file": {"display_name": display_name[:120]}}).encode(),
        headers={
            "X-Goog-Upload-Protocol": "resumable",
            "X-Goog-Upload-Command": "start",
            "X-Goog-Upload-Header-Content-Length": str(size),
            "X-Goog-Upload-Header-Content-Type": mime,
            "Content-Type": "application/json",
        },
    )
    with start:
        upload_url = start.headers.get("X-Goog-Upload-URL") or start.headers.get("x-goog-upload-url")
    if not upload_url:
        raise MediaAIError("Gemini không trả về địa chỉ upload")
    offset = 0
    while True:
        chunk = stream.read(UPLOAD_CHUNK)
        last = offset + len(chunk) >= size
        with _request(
            "POST", upload_url, body=chunk, timeout=300,
            headers={
                "Content-Length": str(len(chunk)),
                "X-Goog-Upload-Offset": str(offset),
                "X-Goog-Upload-Command": "upload, finalize" if last else "upload",
            },
        ) as response:
            payload = response.read()
        offset += len(chunk)
        if last:
            return json.loads(payload)["file"]
        if not chunk:
            raise MediaAIError("Tệp ngắn hơn kích thước đã lưu")


def _wait_until_active(key, file):
    deadline = time.monotonic() + PROCESSING_TIMEOUT_SECONDS
    while file.get("state") == "PROCESSING":
        if time.monotonic() > deadline:
            raise MediaAIError("Gemini xử lý tệp quá lâu, thử lại sau")
        time.sleep(3)
        with _request("GET", f"{API}/v1beta/{file['name']}?key={key}") as response:
            file = json.loads(response.read())
    if file.get("state") != "ACTIVE":
        raise MediaAIError(f"Gemini không đọc được tệp này (trạng thái {file.get('state')})", status=422)
    return file


def _delete(key, file):
    try:
        with _request("DELETE", f"{API}/v1beta/{file['name']}?key={key}"):
            pass
    except (urllib.error.URLError, OSError):
        log.info("media_ai_cleanup_failed file=%s", file.get("name"))  # expires on its own in 48 h


def _record_usage(model, ok, started, usage=None, error=None):
    """Best-effort, same as llm.py's own _record_usage: a down MongoDB must
    never fail the analysis itself, only leave this attempt off the admin
    stats page (app/services/usage_stats.py)."""
    try:
        usage_repository.record("media_ai", "gemini", model, ok, (time.monotonic() - started) * 1000, usage=usage, error=error)
    except Exception as exc:
        log.warning("media_ai_usage_record_failed error=%s", type(exc).__name__)


def _generate(key, model, file, kind):
    payload = {
        "contents": [{"role": "user", "parts": [
            {"file_data": {"mime_type": file["mimeType"], "file_uri": file["uri"]}},
            {"text": PROMPTS[kind]},
        ]}],
        "generationConfig": {"temperature": 0.1},
    }
    with _request(
        "POST", f"{API}/v1beta/models/{model}:generateContent?key={key}",
        body=json.dumps(payload).encode(), headers={"Content-Type": "application/json"},
        timeout=GENERATE_TIMEOUT_SECONDS,
    ) as response:
        data = json.loads(response.read())
    try:
        candidate = data["candidates"][0]
        text = "".join(p.get("text", "") for p in candidate["content"]["parts"] if not p.get("thought"))
    except (KeyError, IndexError, TypeError):
        raise MediaAIError("Gemini trả về phản hồi không đúng định dạng") from None
    if not text.strip():
        raise MediaAIError(f"Gemini không trả về nội dung (finishReason={candidate.get('finishReason', '?')})", status=422)
    usage_data = data.get("usageMetadata") or {}
    usage = {
        "input": usage_data.get("promptTokenCount"), "output": usage_data.get("candidatesTokenCount"),
        "thinking": usage_data.get("thoughtsTokenCount"), "cached": usage_data.get("cachedContentTokenCount"),
    }
    return text, usage


def analyze(kind, open_stream, size, mime, display_name):
    """kind: "image" | "audio" | "video". open_stream(): a fresh readable
    stream of the file (called again if a key has to be retried).
    Returns (text, model). Raises MediaAIError."""
    if not enabled():
        raise MediaAIError("Phân tích ảnh/âm thanh/video bằng AI đang tắt (AI_MEDIA_ANALYSIS=false)", status=422)
    if llm.current_provider() != "gemini":
        raise MediaAIError("Phân tích ảnh/âm thanh/video cần LLM_PROVIDER=gemini", status=422)
    keys = llm._api_keys()
    models = llm.available_models()
    if not keys or not models:
        raise MediaAIError("Chưa cấu hình LLM_API_KEY / LLM_MODEL trong .env", status=422)
    if size > max_bytes():
        raise MediaAIError(f"Tệp quá {max_bytes() // (1024 * 1024)} MiB để phân tích bằng AI (đặt AI_MEDIA_MAX_MB để nâng)", status=422)

    last_error = "không rõ"
    # An uploaded file belongs to the key's Google project, so models rotate
    # within a key and a new key means a new upload (429 is per key).
    for key in keys:
        file = None
        try:
            stream = open_stream()
            try:
                file = _upload(key, stream, size, mime, display_name)
            finally:
                close = getattr(stream, "close", None)
                if close:
                    close()
            file = _wait_until_active(key, file)
            for model in models:
                started = time.monotonic()
                try:
                    text, usage = _generate(key, model, file, kind)
                    log.info("media_ai_done kind=%s model=%s size_bytes=%s ms=%d", kind, model, size, (time.monotonic() - started) * 1000)
                    _record_usage(model, True, started, usage=usage)
                    return text, model
                except urllib.error.HTTPError as exc:
                    detail = exc.read().decode("utf-8", "replace")[:200]
                    last_error = f"model={model} HTTP {exc.code}: {detail}"
                    _record_usage(model, False, started, error=f"HTTP {exc.code}: {detail}")
                    if exc.code == 429:
                        break  # this key is out of quota: next key
                    if exc.code == 400:
                        raise MediaAIError(f"Gemini từ chối tệp: {last_error}", status=422) from None
        except urllib.error.HTTPError as exc:
            last_error = f"HTTP {exc.code}: {exc.read().decode('utf-8', 'replace')[:200]}"
        except (urllib.error.URLError, OSError) as exc:
            last_error = f"không kết nối được ({exc})"
        finally:
            if file and file.get("name"):
                _delete(key, file)
    raise MediaAIError(f"Không phân tích được bằng AI. Lỗi gần nhất: {last_error}")
