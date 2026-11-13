"""Count the English UI words a frontend module still writes itself (UX
design record 3.8): string literals - '…', "…", `…` with ${…} and tags taken
out - that read as an English phrase: two words or more, the first one
capitalised or one of them an everyday English word. Comments, console
lines, selectors, i18n keys and CSS class lists do not count. A heuristic for
a ratchet (tests/test_i18n_ratchet.py), not a parser: it only has to go down
as the words move into frontend/i18n/en.json.
"""
import re
from pathlib import Path

JS = Path(__file__).resolve().parents[1] / "frontend" / "js"
STOP = {"the", "a", "an", "to", "of", "in", "is", "are", "your", "you", "not", "no", "and", "or", "for",
        "on", "with", "this", "it", "be", "has", "was", "will", "can", "yet", "from", "by", "at", "as",
        "all", "more", "new", "open", "show", "add", "save", "saved", "delete", "keep", "loading", "failed"}
_SKIP_LINE = re.compile(r"^\s*(//|\*|/\*)|console\.|querySelector|getElementById|addEventListener|import |"
                        r"classList|setAttribute\('(?:class|id|role|aria-(?:hidden|live|busy|expanded|haspopup))")
_LIT = re.compile(r"'(?:[^'\\\n]|\\.)*'|\"(?:[^\"\\\n]|\\.)*\"|`(?:[^`\\]|\\.)*`", re.S)
_WORD = re.compile(r"[A-Za-z][A-Za-z']*")


def _texts(lit: str) -> list:
    body = lit[1:-1]
    if lit[0] == "`":
        # ${…} out (balanced enough for this code base), then the tags
        while "${" in body:
            i = body.index("${")
            depth, j = 0, i + 1
            while j < len(body):
                depth += body[j] == "{"
                depth -= body[j] == "}"
                if depth == 0:
                    break
                j += 1
            body = body[:i] + " \u0000 " + body[j + 1:]
    body = re.sub(r"<[^>]*>", "\u0000", body)
    return [seg for seg in body.split("\u0000") if seg.strip()]


_CODE = ("${", "=>", "===", "&&", "||", "()", "[", ":not(")
_TAGS = {"a", "button", "input", "select", "textarea", "div", "span", "summary", "details", "label"}


def english(seg: str) -> bool:
    if re.fullmatch(r"[\w.\-/:#?&=%]+", seg.strip()):        # a key, a path, a class, an id
        return False
    if any(c in seg for c in _CODE):                          # code, not words
        return False
    words = [w for w in _WORD.findall(seg) if len(w) > 1 or w in ("a", "I")]
    if len(words) < 2 or sum(len(w) > 1 for w in words) < 2:
        return False
    if all(w.lower() in _TAGS for w in words):                # 'button, a, input' - a selector
        return False
    tokens = seg.split()
    if all(re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)+|[a-z0-9]+", tok) for tok in tokens) \
            and any("-" in tok for tok in tokens):
        return False                                           # 'btn btn-secondary btn-sm'
    return words[0][0].isupper() or any(w.lower() in STOP for w in words)


def count(path: Path) -> int:
    n = 0
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if _SKIP_LINE.search(line):
            continue
        line = re.sub(r"\s//\s.*$", "", line)                 # a trailing comment
        for m in _LIT.finditer(line):
            n += sum(english(seg) for seg in _texts(m.group(0)))
    return n


def counts(names) -> dict:
    return {name: count(JS / name) for name in names}
