## 2024-05-18 - Missing label 'for' attributes in forms
**Learning:** Many form inputs throughout `frontend/index.html` were wrapped implicitly by labels, or had standalone labels without `for` attributes associating them with the input's `id`.
**Action:** When adding new form inputs or modifying existing forms, ensure all `<label>` elements explicitly link to their corresponding inputs using the `for` attribute and the input's `id`, improving screen reader navigation and increasing the click target size.
