"""The UI's t() / th() for the node rigs (UX design record 3.8): the frontend
modules call them, the rigs strip the imports - this defines both from the
real dictionaries, in English unless a rig asks for another language.

    from _i18n_js import i18n_js
    harness = i18n_js() + module_body           # or i18n_js("de")
"""
import json
from pathlib import Path

_I18N = Path(__file__).resolve().parents[1] / "frontend" / "i18n"


def dictionary(lang: str = "en") -> dict:
    return json.loads((_I18N / f"{lang}.json").read_text(encoding="utf-8"))


def i18n_js(lang: str = "en") -> str:
    """JS that defines t, th and uiLang as frontend/js/i18n.js does."""
    return """
const __DICTS = {en: %s, %s: %s};
const __LANG = %s;
const __PLURAL = new Intl.PluralRules(__LANG);
const uiLang = () => __LANG;
function t(key, vars) {
  let raw = __DICTS[__LANG]?.[key];
  if (raw == null) raw = __DICTS.en?.[key];
  if (raw == null) return key;
  if (typeof raw === 'object') raw = raw[__PLURAL.select(Number(vars?.n ?? 0))] ?? raw.other ?? '';
  return !vars ? raw : raw.replace(/\\{(\\w+)\\}/g, (m, k) => (k in vars ? String(vars[k]) : m));
}
function th(key, vars) {
  return t(key, vars).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}
""" % (json.dumps(dictionary("en"), ensure_ascii=False), json.dumps(lang),
       json.dumps(dictionary(lang), ensure_ascii=False), json.dumps(lang))


def with_words(src: str, lang: str = "en") -> str:
    """A piece of frontend code plus the text of every dictionary key it
    names in quotes (t('…'), th(cond ? '…' : '…'), a constant holding a
    key), so a check on what a dialog SAYS reads the words wherever they
    live (UX design record 3.8)."""
    import re
    d = dictionary(lang)
    texts = []
    for key in dict.fromkeys(k for k in re.findall(r"'([\w.]+)'", src) if k in d):
        v = d.get(key)
        texts += list(v.values()) if isinstance(v, dict) else [v] if v else []
    return src + "\n" + "\n".join(texts)
