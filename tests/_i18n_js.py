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
