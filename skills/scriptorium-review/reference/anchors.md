# Evidence anchors

Every location comes from this attempt's exported files. Check each output with `task submit --check` before
submitting it.

## Text sources

```json
{
  "source_path": "main.tex",
  "start_line": 3,
  "end_line": 3,
  "source_digest": "<sources[].source_digest from source-map.json>",
  "quoted_text": "verbatim text from those lines"
}
```

- Find the source in `source-map.json`. `source_path` is its bare `sources[].source_path`, not the `read_path`;
  `source_digest` is copied from the same entry. Read the text at `read_path` in the export directory.
- `start_line` and `end_line` are 1-based and inclusive. A line ends only at `\n`, `\r\n`, or `\r`; a form feed or
  Unicode line separator stays inside its line, so count lines with that rule rather than a generic splitter.
  `line_count` in the source map bounds the range.
- `quoted_text` must appear verbatim within those lines. Copy it from the exported file; you may shorten it to the
  decisive part.
- Only sources with `text_anchorable: true` take line anchors. Cite figures and other binary sources by page.
- Never quote the `task page --text` layer or `navigation.json` as the source text; check the source lines.

## Rendered pages

```json
{"source_path": "manuscript.pdf", "page": 7}
```

- `page` is the 1-based **global** page of `manuscript.pdf`.
- With `task page --document supplement.tex --number 1`, `document_page` is the local position and the returned
  `page` is the global one. Cite the returned global page, never a local number or printed page label.
- `source-map.json` lists `compiled_pdf.documents`, each with `entrypoint`, `start_page`, and `page_count`,
  including supplements compiled separately and assembled after the main document.

## Checking

`task submit ATTEMPT_ID --input-digest D --file answer.json --check` validates every anchor against the frozen
sources without recording anything. Issue codes such as `evidence.source_quote_mismatch`,
`evidence.source_digest_mismatch`, `evidence.range_out_of_bounds`, and `evidence.page_out_of_bounds` name the rule
an anchor broke; `expected` and `actual` show the difference.
