## 2024-09-12 - Decorative SVG cleanup
**Learning:** Screen readers announce SVGs as interactive or noisy elements by default unless hidden. It's common for icon-only buttons to contain `<svg>` tags. If the button has an `aria-label`, the internal SVG needs `aria-hidden="true"` to prevent redundant/confusing announcements.
**Action:** When adding inline SVGs inside functional buttons or next to descriptive text, always add `aria-hidden="true"`.
