"""
injection_guard.py — Prompt injection detection for KukuiBot.

Two-layer defense:
  1. Fast regex pre-filter for known injection patterns
  2. DeBERTa classifier (deepset/deberta-v3-base-injection) for ML-based detection

Can run standalone as CLI, HTTP server, or be imported as a library.
"""

import json
import logging
import os
import re
import sys
import time
from typing import Optional

logger = logging.getLogger("kukuibot.injection-guard")

# --- Model loading (lazy) ---
_classifier = None
_last_load_error_at = 0.0
_last_load_error_msg = ""
_RETRY_COOLDOWN_SEC = 60.0
_MODEL_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "models", "deberta-v3-base-injection")
_MODEL_NAME = _MODEL_DIR if os.path.isdir(_MODEL_DIR) else "deepset/deberta-v3-base-injection"

# --- Known injection patterns (fast regex pre-filter) ---
INJECTION_PATTERNS = [
    r"ignore\s+(all\s+)?previous\s+instructions",
    r"ignore\s+(all\s+)?prior\s+instructions",
    r"ignore\s+(all\s+)?above\s+instructions",
    r"disregard\s+(all\s+)?previous",
    r"forget\s+(all\s+)?previous",
    r"you\s+are\s+now\s+(a|an)\s+",
    r"new\s+instructions?\s*:",
    r"system\s*:\s*you\s+are",
    r"<\s*system\s*>",
    r"\[\s*system\s*\]",
    r"act\s+as\s+(a|an|if)\s+",
    r"pretend\s+(you\s+are|to\s+be)",
    r"reveal\s+(the\s+)?(system|secret|hidden)\s+prompt",
    r"output\s+(the\s+)?(system|initial)\s+prompt",
    r"repeat\s+(the\s+)?instructions\s+above",
    r"what\s+(are|is)\s+your\s+(system\s+)?instructions",
    r"do\s+not\s+follow\s+(any|the)\s+(previous|above)",
    r"override\s+(the\s+)?(previous|system)",
    r"jailbreak",
    r"DAN\s+mode",
    r"developer\s+mode\s+(enabled|activated)",
]
COMPILED_PATTERNS = [re.compile(p, re.IGNORECASE) for p in INJECTION_PATTERNS]


def _get_classifier():
    """Lazy-load the DeBERTa classifier.

    If loading previously failed, periodically retry (cooldown) so transient
    issues (network hiccup, cache race, first-run deps) can self-heal.
    """
    global _classifier, _last_load_error_at, _last_load_error_msg
    if _classifier == "unavailable":
        # Avoid retry spam, but allow eventual recovery.
        if (time.time() - _last_load_error_at) < _RETRY_COOLDOWN_SEC:
            return None
        _classifier = None

    if _classifier is None:
        try:
            from transformers import pipeline
            logger.info("Loading injection detection model...")
            start = time.time()
            _classifier = pipeline(
                "text-classification",
                model=_MODEL_NAME,
                truncation=True,
                max_length=512,
            )
            logger.info(f"Model loaded in {time.time() - start:.1f}s")
        except Exception as e:
            _last_load_error_at = time.time()
            _last_load_error_msg = str(e)
            logger.warning(f"Failed to load DeBERTa model: {e}")
            _classifier = "unavailable"
    return _classifier if _classifier != "unavailable" else None


def get_guard_diagnostics() -> dict:
    """Expose model load diagnostics for health/debug endpoints."""
    status = "ok" if _classifier not in (None, "unavailable") else (
        "unavailable" if _classifier == "unavailable" else "not_loaded"
    )
    return {
        "status": status,
        "model": _MODEL_NAME,
        "model_dir_exists": os.path.isdir(_MODEL_DIR),
        "retry_cooldown_sec": _RETRY_COOLDOWN_SEC,
        "last_load_error_at": _last_load_error_at,
        "last_load_error": _last_load_error_msg,
    }


def fast_pattern_check(text: str) -> list[dict]:
    """Quick regex scan for known injection patterns."""
    matches = []
    for pattern in COMPILED_PATTERNS:
        for m in pattern.finditer(text):
            matches.append({
                "pattern": pattern.pattern,
                "match": m.group(),
                "position": m.start(),
            })
    return matches


def _chunk_text(text: str, chunk_size: int = 512, overlap: int = 64) -> list[str]:
    """Split text into overlapping chunks for analysis."""
    words = text.split()
    if len(words) <= chunk_size:
        return [text]
    chunks = []
    start = 0
    while start < len(words):
        end = min(start + chunk_size, len(words))
        chunks.append(" ".join(words[start:end]))
        start += chunk_size - overlap
    return chunks


def scan_text(text: str, threshold: float = 0.85, source: str = "unknown") -> dict:
    """
    Scan text for prompt injection.

    Args:
        text: Text to scan
        threshold: Confidence threshold for INJECTION classification
        source: Label for logging (e.g. "web_search", "web_fetch", "user_paste")

    Returns:
        dict with verdict (LEGIT|INJECTION|SUSPICIOUS), confidence, details
    """
    if not text or len(text.strip()) < 20:
        return {"verdict": "LEGIT", "confidence": 1.0, "method": "too_short"}

    result = {
        "verdict": "LEGIT",
        "confidence": 1.0,
        "pattern_matches": [],
        "model_results": [],
        "method": "combined",
        "source": source,
    }

    # Phase 1: Fast regex
    pattern_matches = fast_pattern_check(text)
    result["pattern_matches"] = pattern_matches

    # Phase 2: DeBERTa model
    classifier = _get_classifier()
    if classifier:
        chunks = _chunk_text(text, 512)
        worst_injection_score = 0.0
        for i, chunk in enumerate(chunks):
            if not chunk.strip():
                continue
            pred = classifier(chunk)[0]
            label = pred["label"]
            score = pred["score"]
            result["model_results"].append({
                "chunk": i,
                "label": label,
                "score": round(score, 4),
                "preview": chunk[:100] + ("..." if len(chunk) > 100 else ""),
            })
            if label == "INJECTION":
                worst_injection_score = max(worst_injection_score, score)

        # Verdict logic
        if source == "web_fetch":
            # Raw page content: model alone is sufficient (higher risk surface)
            if worst_injection_score >= 0.95:
                result["verdict"] = "INJECTION"
                result["confidence"] = round(worst_injection_score, 4)
        else:
            # Search results / other: require pattern + high model confidence (avoid false positives)
            if pattern_matches and worst_injection_score >= 0.99:
                result["verdict"] = "INJECTION"
                result["confidence"] = round(worst_injection_score, 4)
                result["method"] = "combined_pattern_and_model"
            elif worst_injection_score >= threshold:
                result["verdict"] = "INJECTION"
                result["confidence"] = round(worst_injection_score, 4)
            elif pattern_matches and worst_injection_score >= 0.5:
                result["verdict"] = "INJECTION"
                result["confidence"] = round(max(worst_injection_score, 0.75), 4)
                result["method"] = "combined_pattern_boost"
            elif pattern_matches:
                result["verdict"] = "SUSPICIOUS"
                result["confidence"] = round(worst_injection_score, 4)
                result["method"] = "pattern_only"
            else:
                result["confidence"] = round(1.0 - worst_injection_score, 4)
    else:
        # Model unavailable — fall back to regex only
        result["method"] = "regex_only"
        if pattern_matches:
            result["verdict"] = "SUSPICIOUS"
            result["confidence"] = 0.6

    return result


def scan_and_filter(text: str, source: str = "external") -> str:
    """
    Full two-pass content security pipeline:
      1. DeBERTa first pass (fast regex + ML)
      2. If flagged → Spark sub-agent (sandboxed, no tools) for intelligent assessment

    Returns original text if clean, blocked message if dangerous.
    """
    if not text or len(text.strip()) < 20:
        return text

    result = scan_text(text, source=source)

    if result["verdict"] == "LEGIT":
        return text

    # Flagged — escalate to Spark for second-pass assessment
    logger.info(f"[injection-guard] First pass flagged {result['verdict']} in {source}, escalating to Spark")
    try:
        from spark_guard import assess_inbound
        assessment = assess_inbound(text, result)
        action = assessment.get("action", "BLOCK")
        reason = assessment.get("reason", "")

        if action == "ALLOW":
            logger.info(f"[injection-guard] Spark allowed: {reason}")
            return text
        elif action == "REDACT":
            logger.warning(f"[injection-guard] Spark redacted: {reason}")
            return f"[CONTENT PARTIALLY REDACTED: {reason}]\n{text}"
        else:  # BLOCK
            logger.warning(f"[injection-guard] Spark blocked: {reason}")
            return f"[CONTENT BLOCKED: {reason}]"
    except Exception as e:
        # Spark failed — fall back to first-pass verdict
        logger.warning(f"[injection-guard] Spark assessment failed ({e}), using first-pass verdict")
        confidence = result["confidence"]
        if result["verdict"] == "INJECTION":
            return f"[CONTENT BLOCKED: Prompt injection detected in {source} (confidence: {confidence:.1%}). Filtered for safety.]"
        return f"[NOTE: Content contains suspicious patterns but may be legitimate.]\n{text}"


# --- HTTP Server mode ---

def serve(port: int = 8079, threshold: float = 0.85):
    """Run as standalone HTTP API server."""
    from http.server import HTTPServer, BaseHTTPRequestHandler

    # Pre-load the model
    _get_classifier()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            if self.path == "/scan":
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length)
                try:
                    data = json.loads(body)
                    text = data.get("text", "")
                    t = data.get("threshold", threshold)
                    source = data.get("source", "api")
                    result = scan_text(text, threshold=t, source=source)
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(json.dumps(result).encode())
                except Exception as e:
                    self.send_response(500)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(json.dumps({"error": str(e)}).encode())
            else:
                self.send_response(404)
                self.end_headers()

        def do_GET(self):
            if self.path == "/health":
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"status": "ok", "model": _MODEL_NAME}).encode())
            else:
                self.send_response(404)
                self.end_headers()

        def log_message(self, format, *args):
            logger.debug(f"[http] {args[0]}")

    server = HTTPServer(("127.0.0.1", port), Handler)
    print(f"Injection Guard API running on http://127.0.0.1:{port}", file=sys.stderr)
    server.serve_forever()


# --- CLI ---

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Prompt Injection Guard")
    parser.add_argument("text", nargs="?", help="Text to scan")
    parser.add_argument("--file", "-f", help="File to scan")
    parser.add_argument("--stdin", action="store_true", help="Read from stdin")
    parser.add_argument("--json", "-j", action="store_true", help="JSON output")
    parser.add_argument("--threshold", "-t", type=float, default=0.85)
    parser.add_argument("--serve", action="store_true", help="Run as HTTP server")
    parser.add_argument("--port", type=int, default=8079)
    args = parser.parse_args()

    if args.serve:
        serve(port=args.port, threshold=args.threshold)
        sys.exit(0)

    if args.file:
        with open(args.file) as f:
            text = f.read()
    elif args.stdin:
        text = sys.stdin.read()
    elif args.text:
        text = args.text
    else:
        parser.print_help()
        sys.exit(2)

    result = scan_text(text, threshold=args.threshold)
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        v = result["verdict"]
        c = result["confidence"]
        emoji = "🟢" if v == "LEGIT" else "🔴" if v == "INJECTION" else "🟡"
        print(f"{emoji} {v} (confidence: {c:.1%})")
        if result["pattern_matches"]:
            print(f"  Pattern matches: {len(result['pattern_matches'])}")
            for m in result["pattern_matches"][:3]:
                print(f"    - \"{m['match']}\" at pos {m['position']}")

    sys.exit(0 if result["verdict"] == "LEGIT" else 1)
