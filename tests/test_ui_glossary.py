"""The UI's WORDS rules, pinned (the DESIGN LANGUAGE block in app.css).

No UI text names the judge, the arr, the custodian or a bake, and none shows
an env-var style name. The dictionaries are loaded as JSON and their values
checked; nothing greps source.

    python tests/test_ui_glossary.py
"""
from _harness import check  # noqa: I001 - first: temp stores
from _i18n_js import dictionary

import re

# The one place a variable name is an instruction the user must type: setup
# hints that tell them to set it on their Ollama server.
_ALLOWED_NAMES = {"OLLAMA_HOST", "OLLAMA_KV_CACHE_TYPE", "OLLAMA_FLASH_ATTENTION"}

_PLACEHOLDER = re.compile(r"\{[^}]*\}")  # {judge} names a value, not a word
_ENV_NAME = re.compile(r"\b[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+\b")
_EN_BAD = re.compile(  # "Arr apps" is right; only the lower-case and ALL-CAPS forms are not
    # "tick" as the scheduler's beat, not "tick the box"
    r"(?i:\bjudg(?:e|es|ed|ing|ement|ements)\b|\bphase \d|\bcustodians?\b|\bbak(?:e|es|ed|ing)\b|\b(?:next|each|every|this|the) tick\b)"
    # "an arr", "your arr", "in arr": a lower-case arr anywhere (placeholders
    # are stripped first; "Arr apps" keeps its capital), and no config file.
    r"|\barrs?\b|\bARR\b|\*arr\b|\.env\b"
)
_DE_BAD = re.compile(r"\bRichter\w*|\bNodes?\b|\bstreich\w*|\bPhase \d|\bCustodian\w*|\bBake\w*|\bgebacken\b|\bbacken\b")


def _texts(d):
    for key, v in d.items():
        for form in (v.values() if isinstance(v, dict) else [v]):
            yield key, _PLACEHOLDER.sub("", form)


def _offenders(d, rx):
    bad = []
    for key, text in _texts(d):
        hits = [m.group(0) for m in rx.finditer(text)]
        hits += [n for n in _ENV_NAME.findall(text) if n not in _ALLOWED_NAMES]
        if hits:
            bad.append((key, hits))
    return bad


EN, DE = dictionary("en"), dictionary("de")

bad = _offenders(EN, _EN_BAD)
check("English UI text: no judge/judgement, Phase N, arr, ARR, *arr, custodian, bake, tick, .env, env-var names",
      not bad, str(bad[:8]))
bad = _offenders(DE, _DE_BAD)
check("German UI text: no Richter, Node, streichen, Phase N, Custodian, Bake, env-var names", not bad, str(bad[:8]))
check("the env-var allowlist is the Ollama-server variables only",
      all(n.startswith("OLLAMA_") for n in _ALLOWED_NAMES))

check.done()
