# Evidence anchors

Every location must come from this attempt's frozen source map (`task show ATTEMPT_ID --part source-map`).

## Text sources

```json
{
  "source_path": "main.tex",
  "start_line": 3,
  "end_line": 3,
  "source_digest": "<sources[].source_digest from the source map>",
  "quoted_text": "verbatim text from those lines"
}
```

- `source_path` is the bare path from `sources[].source_path`, not the `read_path`.
- `start_line` and `end_line` are 1-based and inclusive.
- `quoted_text` must appear verbatim within those lines of the frozen source.
- `task read ... --anchor` returns a ready anchor. You may shorten its `quoted_text` to the decisive part, keeping
  the path, digest, and line range.
- Only sources with `text_anchorable: true` take line anchors. Cite figures and other binary sources by page.

## Rendered pages

```json
{"source_path": "manuscript.pdf", "page": 7}
```

- `page` is the 1-based **global** page of `manuscript.pdf`.
- With `task page --document supplement.tex --number 1`, `document_page` is the local position and the returned
  `page` is the global one. Cite the returned global page, never a local number or printed page label.
- `source-map.json` lists any `compiled_pdf.documents`, each with `entrypoint`, `start_page`, and `page_count`. These
  include supplements compiled separately and assembled after the main document. Older bundles without this index
  use global page numbers throughout.
- The `task page --text` layer is a reading aid; never quote it as evidence.

## Paths for retrieval

- Prefer the exact `read_path` with `task read` or `task search --path`. A `read_path` wins over a colliding source
  name, and bare source names work when unambiguous.
- Bare `manifest.json`, `navigation.json`, and `source-map.json` select generated metadata. Use the source map's
  `read_path` to reach a manuscript source with one of those names.
- A raw search can match comments or inactive alternatives. Confirm that a passage belongs to the compiled
  manuscript before treating it as a claim.

Validation issue codes such as `evidence.source_quote_mismatch`, `evidence.source_digest_mismatch`,
`evidence.range_out_of_bounds`, and `evidence.page_out_of_bounds` name which of these rules an anchor broke.
