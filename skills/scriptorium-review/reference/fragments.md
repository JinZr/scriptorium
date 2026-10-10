# Bounded responses and fragments

Successful JSON responses for `task claim`, `show`, `read`, `search`, `nav`, `page`, and `export` are at most 7,000
UTF-8 bytes, including the envelope. Requested line, character, and match counts are ceilings, not promises.

## Frozen inputs

- `task show ATTEMPT_ID --part prompt|schema|source-map|brief|example` returns `text`, a character `offset`,
  `total_chars`, the part's `digest`, and a `next_command`.
- Follow `next_command` until null and concatenate `text` in offset order without separators. A fragment can end
  inside a JSON string; parse only the complete text.
- `--offset N` continues explicitly from an earlier fragment. Offsets count Unicode characters, not UTF-8 bytes.
- Frozen inputs stay readable after the attempt finishes. `read`, `search`, `page`, and `nav` need an active
  attempt.
- A part the attempt does not have fails instead of returning text: `brief` for a run without one, and `example`
  (`example_unavailable`) when no example was frozen with the task: revision and verification tasks, tasks
  prepared before examples existed, and schema shapes the example builder cannot fill. The overview's `inputs`
  lists `example` only when one is stored.

## Reading sources

- `task read` returns `next_line` and `next_offset`. On a long line, `next_line` can stay the same while
  `next_offset` grows. Follow `next_command`; never increment the line yourself, which would skip the line's tail.
- `--end-line B` makes continuations stop after line B instead of at the end of the file.
- `--anchor` covers only lines returned whole, from their first character. A line split across responses is never
  covered; build its anchor from the source map digest and verbatim text you read.
- `task search` pages with `--cursor`; follow `next_command` to see every match.
- `task page --text` pages its text layer with `--offset` and the returned `--text-digest`.

## Submission diagnostics

- `task submit` and `task submit --check` responses are not bounded. A long `data.validation_report.issues` list
  can exceed what the host displays.
- If the host might truncate it, redirect the response to a file (`... --check > check.json`) and read every issue
  from that file before editing the answer.

## Commands

- Run every `next_command` unchanged. If you use a wrapper, replace only the executable.
- `--help` output is not retrieval and does not count as reading the manuscript.
- The CLI records what it returned, not what the host displayed or understood.
