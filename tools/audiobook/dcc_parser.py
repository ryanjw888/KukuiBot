#!/usr/bin/env python3
"""
DCC Book 9 Chapter Parser — Speaker-tagged JSON for TTS voice assignment.

Parses EPUB chapter XHTML, splits into segments, and tags each with:
  - type (system_message, carl_narration, carl_spoken, donut_spoken, etc.)
  - speaker
  - emotion hint

Usage: python3 dcc_parser.py [--chapter N] [--epub PATH]
"""

import json
import os
import re
import sys
import zipfile
from html.parser import HTMLParser

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
DEFAULT_EPUB = "/Users/jarvis/.kukuibot/audiobook/A-Parade-of-Horribles-Generic-2026-02-09.epub"
DEFAULT_CHAPTER = 1
OUTPUT_DIR = "/Users/jarvis/.kukuibot/audiobook"

# ---------------------------------------------------------------------------
# HTML → structured blocks
# ---------------------------------------------------------------------------

class ChapterHTMLParser(HTMLParser):
    """Extract paragraph-level blocks from chapter XHTML, preserving bold/underline semantics."""

    def __init__(self):
        super().__init__()
        self._blocks: list[dict] = []  # {"text": str, "bold": bool, "has_underline_speaker": str|None}
        self._cur_text = ""
        self._in_p = False
        self._in_b = False
        self._in_underline = False
        self._underline_speaker: str | None = None
        self._bold_ratio = 0.0  # fraction of text that is bold
        self._bold_chars = 0
        self._total_chars = 0
        self._skip = False  # skip non-text elements (title, heading divs before text)
        self._in_text_div = False
        self._tag_stack: list[str] = []

    def handle_starttag(self, tag, attrs):
        attr_d = dict(attrs)
        # Only process content inside the main text div
        if tag == "div" and "text" in attr_d.get("class", ""):
            self._in_text_div = True
        if not self._in_text_div:
            return
        self._tag_stack.append(tag)
        if tag == "p":
            cls = attr_d.get("class", "")
            if "scene-break" in cls:
                # scene break → empty separator
                self._blocks.append({"text": "", "bold": False, "underline_speaker": None, "scene_break": True})
                self._skip = True
            else:
                self._in_p = True
                self._cur_text = ""
                self._bold_chars = 0
                self._total_chars = 0
                self._underline_speaker = None
        elif tag == "b":
            self._in_b = True
        elif tag == "span" and "underline" in attr_d.get("class", ""):
            self._in_underline = True
        elif tag == "i":
            pass  # italics don't affect tagging

    def handle_endtag(self, tag):
        if not self._in_text_div:
            return
        if self._tag_stack and self._tag_stack[-1] == tag:
            self._tag_stack.pop()
        if tag == "p":
            if self._in_p and not self._skip:
                text = self._cur_text.strip()
                if text:
                    bold = self._total_chars > 0 and (self._bold_chars / self._total_chars) > 0.5
                    self._blocks.append({
                        "text": text,
                        "bold": bold,
                        "underline_speaker": self._underline_speaker,
                        "scene_break": False,
                    })
            self._in_p = False
            self._skip = False
            self._cur_text = ""
        elif tag == "b":
            self._in_b = False
        elif tag == "span":
            self._in_underline = False

    def handle_data(self, data):
        if not self._in_text_div or not self._in_p or self._skip:
            return
        # Detect underline speaker labels like "Donut:" or "Mordecai:"
        if self._in_underline and self._in_b:
            name = data.strip().rstrip(":")
            if name:
                self._underline_speaker = name
        self._cur_text += data
        n = len(data.strip())
        self._total_chars += n
        if self._in_b:
            self._bold_chars += n


def extract_blocks(epub_path: str, chapter_file: str) -> tuple[str, list[dict]]:
    """Return (chapter_title, list_of_block_dicts) from the EPUB chapter."""
    with zipfile.ZipFile(epub_path) as zf:
        raw = zf.read(chapter_file).decode("utf-8")
    parser = ChapterHTMLParser()
    parser.feed(raw)
    # Extract title from <title> tag
    title_match = re.search(r"<title>(.*?)</title>", raw)
    title = title_match.group(1) if title_match else "Chapter"
    return title, parser._blocks


# ---------------------------------------------------------------------------
# Speaker / type classification
# ---------------------------------------------------------------------------

# Chat-format line: "Name: MESSAGE" where name appears underlined+bold in source
CHAT_SPEAKERS = {
    "donut": "donut_chat",
    "mordecai": "mordecai_chat",
}

# Known dialogue-tag patterns mapping to speakers
CARL_SAID_RE = re.compile(
    r"""\b(I\s+(said|asked|called|shouted|yelled|whispered|muttered|replied|demanded|added|started|continued|answered|suggested|told))\b""",
    re.I,
)
DONUT_SAID_RE = re.compile(
    r"""\b(Donut\s+(said|asked|called|shouted|yelled|whispered|demanded|replied|purred|meowed|hissed|squealed|exclaimed|declared|insisted))\b""",
    re.I,
)
OTHER_SAID_RE = re.compile(
    r"""(?:^|\.\s+|,?\s*[""\u201d]\s*)([A-Z][\w\s]*?)\s+(said|asked|called|shouted|yelled|whispered|gasped|bellowed|growled|grunted|sputtered|cried|screamed|panted)\b""",
    re.I,
)
# Named speaker patterns for specific characters in mixed paragraphs
NAMED_SPEAKER_RE = re.compile(
    r"""\b(Hedy|Waldrip\s+Chris|Donut|Mordecai)\s+(said|asked|called|shouted|yelled|whispered|gasped|bellowed|growled|grunted|sputtered|cried|screamed|panted|demanded|insisted|declared|exclaimed)\b""",
    re.I,
)

# System-message indicators (bold OR non-bold opening lines)
SYSTEM_PATTERNS = [
    re.compile(r"^(Welcome,?\s+Crawler|Time to Level Collapse|Views:|Followers:|Favorites:|Leaderboard Rank:|Bounty:|Congrats,?\s+Crawler|Remaining Crawlers:|Entering your|Warning:)", re.I),
    re.compile(r"^(Your party leader must|Mechanical has been chosen|This message is from a deceased)", re.I),
]

# System messages that can appear without bold formatting
SYSTEM_NONBOLD_PATTERNS = [
    re.compile(r"^Welcome,?\s+Crawler", re.I),
]

# Description box — bold blocks starting with a name + race/class pattern
DESCRIPTION_RE = re.compile(r"^[\w\s]+\.\s+(Gremlin|Human|Elf|Dwarf|Orc|Naga|Kobold|Goblin|Cat|Feline)", re.I)
DESCRIPTION_CONTINUATION_RE = re.compile(r"^(This is an? |Warning: Just because|.+ is not allowed to enter|.+ is an expert in)")


def _extract_quoted(text: str) -> list[str]:
    """Extract quoted dialogue segments from text."""
    # Match both straight and curly quotes
    return re.findall(r'["\u201c](.*?)["\u201d]', text)


def _infer_speaker_from_context(text: str, prev_blocks: list[dict]) -> str | None:
    """Try to determine who is speaking quoted dialogue."""
    # Check for named characters first (most specific)
    m = NAMED_SPEAKER_RE.search(text)
    if m:
        return m.group(1).strip().lower()
    # Check dialogue tags within the same paragraph
    if CARL_SAID_RE.search(text):
        return "carl"
    if DONUT_SAID_RE.search(text):
        return "donut"
    m = OTHER_SAID_RE.search(text)
    if m:
        name = m.group(1).strip()
        # Skip pronouns
        if name.lower() not in ("he", "she", "it", "they", "the", "a", "an"):
            return name.lower()
    return None


# Content-based speaker clues for pure dialogue lines
DONUT_CONTENT_RE = re.compile(r"^Carl,|\bCarl\b[,!?]|them,\s+Carl|\bMongo\b|\batop the leaderboard\b|\bI choose you\b|unshaven|my goodness|I'm quite sorry|sounds obscene|quit fighting|upper paw|legless bitch", re.I)
CARL_CONTENT_RE = re.compile(r"\bhappy for you\b|\bwhat did you pick\b|\buh,?\s+I think\b|\bwhat about\b", re.I)
HEDY_CONTENT_RE = re.compile(r"\bpet spells\b|\bTwinkle Toes\b|\bHeal Critter\b|good as new|it's the truth", re.I)
WALDRIP_CONTENT_RE = re.compile(r"\bCharm Wombat\b|\bCloud Cheetah\b|don't tell them|wait,?\s+wait", re.I)


def _infer_speaker_from_content(text: str) -> str | None:
    """Guess speaker from dialogue content itself."""
    if DONUT_CONTENT_RE.search(text):
        return "donut"
    if CARL_CONTENT_RE.search(text):
        return "carl"
    if HEDY_CONTENT_RE.search(text):
        return "hedy"
    if WALDRIP_CONTENT_RE.search(text):
        return "waldrip chris"
    return None


def _detect_emotion(text: str, seg_type: str) -> str:
    """Simple keyword/pattern emotion detection."""
    t = text.lower()

    if seg_type == "system_message":
        if "warning" in t:
            return "urgent"
        if "congrats" in t or "welcome" in t:
            return "neutral"
        return "neutral"

    if seg_type == "donut_chat":
        if "omg" in t or "!" in text and text.count("!") >= 2:
            return "excited"
        if "help" in t or "hurry" in t:
            return "urgent"
        return "dramatic"

    if seg_type == "donut_spoken":
        if "!" in text:
            return "excited"
        if "?" in text:
            return "dramatic"
        return "dramatic"

    if seg_type == "mordecai_chat":
        if "holy shit" in t:
            return "shocked"
        if "nothing good" in t:
            return "somber"
        return "neutral"

    # General emotion keywords
    if any(w in t for w in ["exploded", "blam", "gah", "yowl"]):
        return "shocked"
    if any(w in t for w in ["sadness", "dead", "deceased", "rest well"]):
        return "somber"
    if any(w in t for w in ["triumph", "ha!", "hurray"]):
        return "excited"
    if any(w in t for w in ["worried", "warning", "hurry"]):
        return "urgent"
    if any(w in t for w in ["bellowed", "shouted", "yelled", "bitch", "jackass", "bastard"]):
        return "angry"
    if any(w in t for w in ["luxury", "wished", "crazy asshole"]):
        return "somber"
    if any(w in t for w in ["sarcas", "happy for you"]):
        return "sarcastic"
    if any(w in t for w in ["incredulous", "my goodness"]):
        return "amused"
    if "?" in text and seg_type in ("carl_spoken", "npc_dialogue"):
        return "neutral"
    if "!" in text:
        return "excited"

    return "neutral"


def classify_blocks(blocks: list[dict]) -> list[dict]:
    """Convert raw blocks into tagged segments."""
    segments = []
    seg_id = 0
    in_description = False
    prev_speaker = None  # track last known dialogue speaker for attribution

    i = 0
    while i < len(blocks):
        block = blocks[i]
        if block.get("scene_break"):
            in_description = False
            i += 1
            continue

        text = block["text"]
        bold = block["bold"]
        uline_speaker = block.get("underline_speaker")

        # ------ Chat-format lines (underline speaker in bold) ------
        if uline_speaker:
            speaker_key = uline_speaker.lower().replace(" ", "_")
            # Strip the "Speaker:" prefix from text for cleaner output
            chat_text = re.sub(r"^\s*" + re.escape(uline_speaker) + r"\s*:\s*", "", text).strip()
            if not chat_text:
                chat_text = text.strip()

            if speaker_key in CHAT_SPEAKERS:
                seg_type = CHAT_SPEAKERS[speaker_key]
            else:
                seg_type = "npc_dialogue"

            # Some chat lines from Mordecai lack the all-caps style — still mordecai_chat
            if speaker_key == "mordecai":
                seg_type = "mordecai_chat"

            seg_id += 1
            emotion = _detect_emotion(chat_text, seg_type)
            segments.append({
                "id": seg_id,
                "type": seg_type,
                "speaker": speaker_key.replace("_", " "),
                "text": chat_text,
                "emotion": emotion,
            })
            i += 1
            continue

        # ------ Bold system / description blocks ------
        if bold:
            # Check if it's a description box
            if DESCRIPTION_RE.match(text) or (in_description and DESCRIPTION_CONTINUATION_RE.match(text)):
                in_description = True
                seg_id += 1
                segments.append({
                    "id": seg_id,
                    "type": "description_box",
                    "speaker": "system",
                    "text": text,
                    "emotion": "neutral",
                })
                i += 1
                continue

            # Check system message patterns
            is_system = any(p.match(text) for p in SYSTEM_PATTERNS)
            if is_system or (bold and not _extract_quoted(text)):
                in_description = False
                seg_id += 1
                emotion = _detect_emotion(text, "system_message")
                segments.append({
                    "id": seg_id,
                    "type": "system_message",
                    "speaker": "system",
                    "text": text,
                    "emotion": emotion,
                })
                i += 1
                continue

        # Reset description tracking on non-bold text
        if not bold:
            in_description = False

        # ------ Non-bold system messages (e.g. "Welcome, Crawler...") ------
        if not bold and any(p.match(text) for p in SYSTEM_NONBOLD_PATTERNS):
            seg_id += 1
            emotion = _detect_emotion(text, "system_message")
            segments.append({
                "id": seg_id,
                "type": "system_message",
                "speaker": "system",
                "text": text,
                "emotion": emotion,
            })
            i += 1
            continue

        # ------ Quoted dialogue lines ------
        quotes = _extract_quoted(text)
        if quotes:
            # Determine speaker from dialogue tags in this paragraph
            speaker = _infer_speaker_from_context(text, blocks[:i])

            # Try content-based inference (addresses "Carl", mentions Mongo, etc.)
            if speaker is None:
                quote_text = quotes[0] if quotes else text
                speaker = _infer_speaker_from_content(quote_text)

            # If no explicit tag, look at surrounding narration for "she said" / "Donut said"
            if speaker is None and i + 1 < len(blocks):
                next_block = blocks[i + 1]
                next_text = next_block["text"]
                # Only use next block if it's narration that describes the current speaker
                # (e.g., "she said" / "Donut said" right after a pure dialogue line)
                # But NOT if the next block itself is dialogue starting with a quote
                next_stripped = next_text.strip()
                next_starts_with_quote = next_stripped and next_stripped[0] in '"\u201c'
                if not next_starts_with_quote:
                    # "she said" / "Donut said" refers to THIS line's speaker
                    if DONUT_SAID_RE.search(next_text):
                        speaker = "donut"
                    elif re.search(r"\bshe\s+(said|asked|called|gasped|purred|declared)", next_text, re.I):
                        speaker = "donut"
                    # But "I said" in next block means NEXT speaker is Carl, not this one
                    m_other = OTHER_SAID_RE.search(next_text)
                    if m_other and speaker is None:
                        speaker = m_other.group(1).strip().lower()

            # Dialogue alternation: if still unknown, alternate from prev_speaker
            if speaker is None:
                if prev_speaker == "carl":
                    speaker = "donut"
                elif prev_speaker == "donut":
                    speaker = "carl"
                else:
                    speaker = "carl"

            # Pure dialogue line (entire paragraph is a quote)
            stripped = text.strip()
            is_pure_dialogue = (
                (stripped.startswith('"') or stripped.startswith('\u201c'))
                and (stripped.endswith('"') or stripped.endswith('\u201d'))
            )

            if is_pure_dialogue:
                # Map speaker to type
                if speaker == "carl":
                    seg_type = "carl_spoken"
                elif speaker == "donut":
                    seg_type = "donut_spoken"
                else:
                    seg_type = "npc_dialogue"

                # Clean the quote marks
                clean = stripped.strip('"\u201c\u201d').strip()
                seg_id += 1
                emotion = _detect_emotion(clean, seg_type)
                segments.append({
                    "id": seg_id,
                    "type": seg_type,
                    "speaker": speaker,
                    "text": clean,
                    "emotion": emotion,
                })
                prev_speaker = speaker
            else:
                # Mixed narration + dialogue: keep as narration with inline quotes
                seg_id += 1
                seg_type = "carl_narration"
                # But if it contains a dialogue tag for someone else speaking, tag it as npc
                if speaker and speaker not in ("carl", "donut"):
                    # It's narration containing NPC dialogue — split if useful
                    # For now, tag as narration (Carl is narrating what they said)
                    pass
                emotion = _detect_emotion(text, seg_type)
                segments.append({
                    "id": seg_id,
                    "type": seg_type,
                    "speaker": "carl",
                    "text": text,
                    "emotion": emotion,
                })
                prev_speaker = speaker

            i += 1
            continue

        # ------ Default: Carl narration ------
        seg_id += 1
        emotion = _detect_emotion(text, "carl_narration")
        segments.append({
            "id": seg_id,
            "type": "carl_narration",
            "speaker": "carl",
            "text": text,
            "emotion": emotion,
        })
        i += 1

    return segments


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    import argparse
    ap = argparse.ArgumentParser(description="DCC EPUB chapter parser for TTS")
    ap.add_argument("--epub", default=DEFAULT_EPUB, help="Path to EPUB file")
    ap.add_argument("--chapter", type=int, default=DEFAULT_CHAPTER, help="Chapter number")
    args = ap.parse_args()

    chapter_file = f"OEBPS/chapter-{args.chapter:03d}.xhtml"
    title, blocks = extract_blocks(args.epub, chapter_file)

    segments = classify_blocks(blocks)

    output = {
        "chapter": args.chapter,
        "title": title,
        "segments": segments,
    }

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    out_path = os.path.join(OUTPUT_DIR, f"chapter_{args.chapter:03d}_tagged.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)

    print(f"Wrote {len(segments)} segments to {out_path}")

    # Print distribution
    from collections import Counter
    dist = Counter(s["type"] for s in segments)
    print("\nType distribution:")
    for t, c in dist.most_common():
        print(f"  {t}: {c}")

    return output


if __name__ == "__main__":
    main()
