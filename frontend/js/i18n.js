// ── The UI's words, in the user's language (UX design record 3.8) ───────────
// The dictionaries are frontend/i18n/en.json and de.json: flat keys to text,
// a plural as {"one": …, "other": …}, {name} for a value. Both are loaded
// before any view renders (this module awaits them at import), so t() is
// synchronous everywhere. The language is the user's setting (Settings →
// Account), else the one they write to the curator in - /api/auth/status
// "ui_language"; before sign-in, the browser's. The server reads the same
// files to name the UI to the curator (services/i18n, services/app_context).
// Markup carries data-i18n="key" (text) and data-i18n-title /
// data-i18n-aria-label / data-i18n-placeholder; its English is the
// dictionary's own, so the page reads right before the first applyStatic().
export const LANGUAGES = ['en', 'de'];
const DICTS = {};
async function _load(lang) {
  try {
    const r = await fetch(`/i18n/${lang}.json`, {cache: 'no-cache'});
    DICTS[lang] = r.ok ? await r.json() : {};
  } catch (e) {
    console.warn(`[i18n] ${lang} dictionary not loaded - English stands in`, e);
    DICTS[lang] = {};
  }
}
await Promise.all(LANGUAGES.map(_load));

let _lang = (globalThis.navigator?.language || 'en').toLowerCase().startsWith('de') ? 'de' : 'en';
let _plural = new Intl.PluralRules(_lang);
export const uiLang = () => _lang;

/** The text of `key`, English where the language lacks it, the key itself
 *  where both do. `vars.n` picks a plural form; `{name}` slots take vars. */
export function t(key, vars) {
  let raw = DICTS[_lang]?.[key];
  if (raw == null) raw = DICTS.en?.[key];
  if (raw == null) return key;
  if (typeof raw === 'object') raw = raw[_plural.select(Number(vars?.n ?? 0))] ?? raw.other ?? '';
  return !vars ? raw : raw.replace(/\{(\w+)\}/g, (m, k) => (k in vars ? String(vars[k]) : m));
}

/** t() for HTML: the text escaped (its values may be titles). */
export function th(key, vars) {
  return t(key, vars).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

const ATTRS = ['title', 'aria-label', 'placeholder'];
/** Write the markup's words in the current language. */
export function applyStatic(root = document) {
  root.querySelectorAll('[data-i18n]').forEach(el => { el.textContent = t(el.dataset.i18n); });
  for (const a of ATTRS) {
    root.querySelectorAll(`[data-i18n-${a}]`).forEach(el => el.setAttribute(a, t(el.getAttribute(`data-i18n-${a}`))));
  }
}

/** Switch the UI language ("en" / "de"); anything else is English. */
export function setLanguage(lang) {
  _lang = LANGUAGES.includes(lang) ? lang : 'en';
  _plural = new Intl.PluralRules(_lang);
  if (globalThis.document) {
    document.documentElement.lang = _lang;
    applyStatic(document);
    // For words a module sets itself (the phone's chat placeholder, …).
    globalThis.dispatchEvent?.(new Event('i18n:changed'));
  }
}
if (globalThis.document) setLanguage(_lang);
