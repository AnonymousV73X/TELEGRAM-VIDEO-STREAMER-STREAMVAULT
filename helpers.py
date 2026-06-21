import re, os
from telethon.tl.types import (
    MessageMediaDocument,
    DocumentAttributeFilename,
    DocumentAttributeVideo,
    DocumentAttributeAudio,
)
from config import VIDEO_EXTS, _MIME_MAP, _safe_mime

# ── Module-level compiled patterns ────────────────────────────────────────────
_EMOJI_STRIP_RE = re.compile(
    r"[^\w\s\-.,'\"!?():;\u00C0-\u024F]",
    re.UNICODE,
)

_STOP_RE = re.compile(
    r"""(?ix)^(
      S\d{1,2}E?\d{0,3}
      | E\d{1,4}
      | Ep\.?\d+
      | Part\.?\d+
      | Season\.?\d+
      | Episode\.?\d*
      | (?:19|20)\d{2}
      | \d{3,4}[pi]
      | 4K | UHD | HDR\d* | HD(?:TV)? | SD
      | BluRay | BDRip | BRRip
      | WEB(?:Rip|DL|-DL)?
      | DVDRip | DVDScr | R5
      | AMZN | NF | DSNP | HMAX | PCOK | SHO | ATVP
      | x26[45] | H\.?26[45] | HEVC | AVC | AV1 | VP9 | MPEG2
      | AAC\d* | AC3 | EAC3 | DDP?5?\.?1 | DTS(?:-HD)?
      | TrueHD | Atmos | FLAC | MP3 | OPUS
      | PROPER | REPACK | EXTENDED | UNRATED | THEATRICAL | FINAL | XXX
      | MULTI | DUAL | FRENCH | HINDI | ENG(?:LISH)? | JAP(?:ANESE)?
      | SUBS? | SUBBED | DUBBED | HardSub
      | REMUX | COMPLETE
      | \[.*\]
      | \(.*\)
    )$""",
    re.VERBOSE | re.IGNORECASE,
)

_ARTICLE = {"the", "a", "an"}
_GLUE = {
    "of",
    "and",
    "in",
    "on",
    "at",
    "to",
    "for",
    "with",
    "from",
    "by",
    "de",
    "la",
    "le",
    "los",
    "las",
    "el",
}

# FIX 2: Hoisted cleanup patterns — reused in derive_album without re-allocation
_CLEANUP_RE = re.compile(r"[\._\-]+")
_SPACE_RE = re.compile(r"\s+")

# Apostrophe restoration for album title normalisation.
# Key = lowercase token with apostrophe stripped; value = canonical form.
# Used so "youre" / "you're" / "Youre" all map to the same album "You're …".
_APOSTROPHE_MAP: dict[str, str] = {
    "youre": "You're",
    "youve": "You've",
    "youll": "You'll",
    "youd": "You'd",
    "theyre": "They're",
    "theyve": "They've",
    "theyll": "They'll",
    "theyd": "They'd",
    "weve": "We've",
    "well": "We'll",
    "wed": "We'd",
    "were": "We're",
    "hes": "He's",
    "shes": "She's",
    "its": "It's",
    "ive": "I've",
    "ill": "I'll",
    "id": "I'd",
    "im": "I'm",
    "whos": "Who's",
    "whats": "What's",
    "thats": "That's",
    "theres": "There's",
    "heres": "Here's",
    "wheres": "Where's",
    "doesnt": "Doesn't",
    "dont": "Don't",
    "didnt": "Didn't",
    "cant": "Can't",
    "wont": "Won't",
    "wouldnt": "Wouldn't",
    "shouldnt": "Shouldn't",
    "couldnt": "Couldn't",
    "isnt": "Isn't",
    "arent": "Aren't",
    "wasnt": "Wasn't",
    "werent": "Weren't",
    "hasnt": "Has't",
    "havent": "Haven't",
    "hadnt": "Hadn't",
    "lets": "Let's",
    "mans": "Man's",
    "womans": "Woman's",
}

# Strip apostrophes for normalisation key comparison
_APOS_STRIP_RE = re.compile(r"['\u2019\u2018`]")


def _restore_apostrophes(token: str) -> str:
    """If token (case-insensitive, apostrophe-stripped) is in the map, return
    the canonical apostrophe form; otherwise return token unchanged."""
    key = _APOS_STRIP_RE.sub("", token).lower()
    return _APOSTROPHE_MAP.get(key, token)


def album_key(name: str) -> str:
    """Normalised comparison key: lowercase, apostrophes stripped, extra spaces collapsed.
    Use for grouping/matching album names regardless of apostrophe or case variants.
    All tokens are lowercased so 'Spy X Family' and 'Spy x Family' share the same key.
    """
    return _SPACE_RE.sub(" ", _APOS_STRIP_RE.sub("", name).lower()).strip()


def _canonical_album_name(name: str) -> str:
    """Return a canonical display name from a raw album string.
    Tokens that are all-uppercase short words (like 'X', 'vs') are kept uppercase;
    other tokens are title-cased. This ensures 'Spy x Family' → 'Spy X Family'."""
    if not name:
        return name
    out = []
    for tok in name.split():
        # Single letters or short all-caps abbreviations → uppercase
        if len(tok) <= 2 or tok.isupper():
            out.append(tok.upper())
        else:
            out.append(tok[0].upper() + tok[1:])
    return " ".join(out)


def _canonicalize_album(name: str) -> str:
    """Restore apostrophes in a user-set or stale auto-derived album name
    without changing casing or word order. Safe to call on manual overrides."""
    if not name:
        return name
    return " ".join(_restore_apostrophes(tok) for tok in name.split())


# FIX 3: Hoisted _parse_caption patterns — were re-looked-up on every call
_CAP_FILENAME = re.compile(r"Filename:\s*(\S+)")
_CAP_QUALITY = re.compile(r"Quality:\s*(\S+)")
_CAP_DURATION = re.compile(r"Duration:\s*([^\n|]+)")
_CAP_SIZE = re.compile(r"Size:\s*([^\n|]+)")


# ── FIX 1: Single-pass attribute extractor ────────────────────────────────────
# Previously callers (cache.py) invoked is_video / get_filename / get_duration /
# get_video_attrs separately — each walked doc.attributes independently.
# This replaces up to 4 linear scans with 1.
def get_doc_attrs(msg):
    """Single-pass extraction: (filename, mime, width, height, duration_s).
    Returns (None, 'video/mp4', 0, 0, 0) when document is absent."""
    fname = None
    w = h = dur = 0
    if not (msg.media and hasattr(msg.media, "document")):
        return fname, "video/mp4", w, h, dur
    doc = msg.media.document
    mime = getattr(doc, "mime_type", None) or "video/mp4"
    for attr in doc.attributes:
        if isinstance(attr, DocumentAttributeFilename):
            fname = attr.file_name
        elif isinstance(attr, DocumentAttributeVideo):
            w, h, dur = attr.w, attr.h, int(attr.duration or 0)
        elif isinstance(attr, DocumentAttributeAudio) and not dur:
            dur = int(attr.duration or 0)
    return fname, mime, w, h, dur


def is_video(msg):
    if not msg.media or not isinstance(msg.media, MessageMediaDocument):
        return False
    doc = msg.media.document
    mime = getattr(doc, "mime_type", "") or ""
    if mime.startswith("video/"):
        return True
    for attr in doc.attributes:
        if isinstance(attr, DocumentAttributeFilename):
            return os.path.splitext(attr.file_name)[1].lower() in VIDEO_EXTS
    return False


def get_filename(msg):
    if msg.media and hasattr(msg.media, "document"):
        for attr in msg.media.document.attributes:
            if isinstance(attr, DocumentAttributeFilename):
                return attr.file_name
    return f"video_{msg.id}.mp4"


def get_mime(msg):
    if msg.media and hasattr(msg.media, "document"):
        return getattr(msg.media.document, "mime_type", None) or "video/mp4"
    return "video/mp4"


def get_size(msg):
    if msg.media and hasattr(msg.media, "document"):
        return getattr(msg.media.document, "size", 0) or 0
    return 0


def get_duration(msg):
    if msg.media and hasattr(msg.media, "document"):
        for attr in msg.media.document.attributes:
            if isinstance(attr, (DocumentAttributeVideo, DocumentAttributeAudio)):
                return getattr(attr, "duration", 0) or 0
    return 0


def get_video_attrs(msg):
    """Return (width, height, duration_s) from Telegram document attributes."""
    if not msg.media or not hasattr(msg.media, "document"):
        return None, None, None
    for attr in msg.media.document.attributes:
        if isinstance(attr, DocumentAttributeVideo):
            return attr.w, attr.h, attr.duration
    return None, None, None


def _quality_from_dims(w, h):
    if not w or not h:
        return ""
    short = min(w, h)
    if short >= 2160:
        return "4K"
    if short >= 1440:
        return "1440p"
    if short >= 1080:
        return "1080p"
    if short >= 720:
        return "720p"
    if short >= 480:
        return "480p"
    if short >= 360:
        return "360p"
    return f"{short}p"


def _strip_emojis(text):
    return _SPACE_RE.sub(" ", _EMOJI_STRIP_RE.sub(" ", text)).strip()


def _clean_title(caption, filename):
    if caption and caption.strip():
        t = _strip_emojis(caption.strip())
        if t:
            return t
    name = os.path.splitext(filename)[0]
    return _strip_emojis(_CLEANUP_RE.sub(" ", name).strip()) or name


def derive_album(caption, filename):
    """
    Smart album grouping: extract the series/show title from a messy filename
    or caption. Handles articles, multi-word titles, junk suffixes, and
    apostrophe variants (youre / you're / Youre all → "You're …").
    """
    src = (
        caption.strip()
        if caption and caption.strip()
        else os.path.splitext(filename)[0]
    )
    src = _SPACE_RE.sub(
        " ", _CLEANUP_RE.sub(" ", _EMOJI_STRIP_RE.sub(" ", src))
    ).strip()
    if not src:
        return "Uncategorised"

    # Normalise apostrophe variants before tokenising so "you're" and "youre"
    # produce the same token stream.
    src = _APOS_STRIP_RE.sub("", src)

    tokens = src.split()
    title_tokens: list[str] = []
    has_alpha = False

    for tok in tokens:
        if _STOP_RE.match(tok):
            break
        is_number = bool(re.fullmatch(r"\d+", tok))
        if is_number:
            if has_alpha:
                break
            title_tokens.append(tok)
            continue
        title_tokens.append(tok)
        has_alpha = True

    while title_tokens and title_tokens[-1].lower() in (_ARTICLE | _GLUE):
        title_tokens.pop()

    if not title_tokens:
        return "Uncategorised"
    if len(title_tokens) == 1 and title_tokens[0].lower() in _ARTICLE:
        return "Uncategorised"

    # Restore apostrophes and apply title-casing per token.
    out_tokens: list[str] = []
    for i, tok in enumerate(title_tokens):
        restored = _restore_apostrophes(tok)
        if restored != tok:
            # _restore_apostrophes already provides canonical casing
            out_tokens.append(restored)
        else:
            # Title-case: lower glue/articles mid-title, capitalise otherwise
            # Single-letter tokens (like 'x' between words) → uppercase always
            low = tok.lower()
            if len(tok) == 1:
                out_tokens.append(tok.upper())
            elif i > 0 and low in (_ARTICLE | _GLUE):
                out_tokens.append(low)
            else:
                out_tokens.append(tok[0].upper() + tok[1:] if tok else tok)
    return " ".join(out_tokens)


def _parse_season(filename: str) -> int:
    """Return season number from filename like S02E05, or 0."""
    m = re.search(r"[Ss](\d{1,2})[Ee]\d", filename)
    if m:
        return int(m.group(1))
    m = re.search(r"\b(\d{1,2})[xX]\d{2}\b", filename)
    if m:
        return int(m.group(1))
    return 0


def _fmt_size(b):
    if not b:
        return ""
    for u in ("B", "KB", "MB", "GB", "TB"):
        if b < 1024:
            return f"{b:.1f} {u}"
        b /= 1024
    return f"{b:.1f} PB"


def _fmt_dur(s):
    if not s:
        return ""
    h, r = divmod(int(s), 3600)
    m, sec = divmod(r, 60)
    return f"{h}h {m}m" if h else (f"{m}m {sec}s" if m else f"{sec}s")


def _parse_caption(caption):
    """Extract structured fields from bot-style captions."""
    # FIX 3: Patterns hoisted to module level — were re-looked-up on every call.
    # Also short-circuits immediately on empty/None caption.
    if not caption:
        return {"filename": None, "quality": None, "duration": None, "size": None}
    return {
        "filename": (m := _CAP_FILENAME.search(caption)) and m.group(1).strip() or None,
        "quality": (m := _CAP_QUALITY.search(caption)) and m.group(1).strip() or None,
        "duration": (m := _CAP_DURATION.search(caption)) and m.group(1).strip() or None,
        "size": (m := _CAP_SIZE.search(caption)) and m.group(1).strip() or None,
    }
