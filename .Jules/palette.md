## 2024-08-22 - Add Focus Visible Styles
**Learning:** The application lacks global `:focus-visible` styles for interactive elements like buttons and links, relying heavily on default browser outlines or having none at all. This creates an accessibility barrier for keyboard navigation.
**Action:** Always verify keyboard focus state visibility across all interactive elements (buttons, links, custom toggles) and add explicit `:focus-visible` styles using the app's primary accent color (e.g., `--amber`) with a sufficient outline offset.
