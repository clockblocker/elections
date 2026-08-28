# Elections

This repository now has two maintained surfaces:

- [`proper-app`](proper-app/) — the Bun, React, PostgreSQL, and Python application.
- [`proper-data`](proper-data/) — evidence-linked election datasets and their crawler.

Raw official-source responses are retained under the ignored `data/raw/` directory so
the curated datasets can be reproduced without mixing crawler artifacts into the app.
Generated crawler intermediates may be recreated under `reports/generated/` when needed.

See [`proper-app/README.md`](proper-app/README.md) for native setup and application
commands. Docker is not used.
