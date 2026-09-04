# Index declaration documents in place

The 2026 declaration corpus combines individual CEC report-77 files, consolidated
federal party-list files, and heterogeneous regional commission publications.
`declarations/corpus.json` is therefore a metadata-only index that references exact
documents at their source-owned paths, reports duplicate evidence without merging it,
and keeps incomplete or ambiguous coverage explicit instead of copying documents or
claiming a candidate-complete normalized dataset. This preserves source fidelity and
avoids false equivalence between documents with different granularity, at the cost of
requiring consumers to follow paths and parse the original formats.
