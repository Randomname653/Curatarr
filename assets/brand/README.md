# Curatarr brand files

All rights reserved. The Curatarr name, logo and wordmark are not licensed
under the AGPL-3.0 that covers the code (see the License section of the
main README). A fork uses its own name and logo.

| File | What it is |
|---|---|
| `curatarr-mark` | The mark: the C with the diamond. Favicon, tray, phone header |
| `curatarr-mark-dark` | The mark in one dark colour, for light backgrounds |
| `curatarr-mark-mono` | The mark in white, the source of the monochrome app icon |
| `curatarr-mark-classic` | The classic mark: the C in a four-part ring |
| `curatarr-logo` | Mark + "Curatarr", for dark backgrounds (sidebar, login, README) |
| `curatarr-logo-dark` | The same for light backgrounds |
| `curatarr-logo-inline` | The mark standing in for the C ("Ⓒuratarr") |
| `curatarr-logo-inline-dark` | The same for light backgrounds |
| `curatarr-app-icon` | The app icon, full bleed: dark tile, ruby diamond |
| `curatarr-app-icon-classic` | The classic app icon |

Each comes as SVG and as a 1024 px PNG master. `python scripts/make_icon.py`
renders the favicon, the PWA and phone icons, the Windows `.ico` and the
social preview from these files.

Colours: amber `#EFB02C`, light diamond `#FFE3A4`, ruby `#E62C33`, tile
`#1F242B`.

For a dashboard (Homarr, Homepage and the like) that asks for an icon URL:
`https://raw.githubusercontent.com/Randomname653/Curatarr/main/frontend/icons/icon-transparent-512.png`
(cut out; `icon-512.png` is the dark tile)
