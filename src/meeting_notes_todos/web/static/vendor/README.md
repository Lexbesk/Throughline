# Vendored client libraries

Served locally at `/static/vendor/` so the app keeps working offline. To update,
replace the file with a newer minified build and note the version here.

| File            | Library   | Version | License                | Source |
|-----------------|-----------|---------|------------------------|--------|
| `marked.min.js` | marked    | 15.0.12 | MIT                    | https://cdn.jsdelivr.net/npm/marked@15.0.12/marked.min.js |
| `purify.min.js` | DOMPurify | 3.2.6   | Apache-2.0 OR MPL-2.0  | https://cdn.jsdelivr.net/npm/dompurify@3.2.6/dist/purify.min.js |

Used only to render the chat assistant's natural-language replies: markdown is
parsed with marked, then sanitized with DOMPurify before insertion (model output
is never inserted as raw HTML). If either file fails to load, the UI falls back
to plain-text rendering.
