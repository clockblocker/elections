# 2021 research workbench

The browser client for issues #9–#12. It expects the read-only exploration API at `/api`; set `VITE_API_URL` to use a different origin.

```sh
npm install
npm run dev
```

With the Compose stack running, install Playwright's Chromium once and run the
rendered browser flow against the reconciled local dataset:

```sh
npx playwright install chromium
npm run test:e2e
```

Override the target with `PLAYWRIGHT_BASE_URL` when the UI is not on port 45173.

The analytical state (filters and selection) is encoded in the URL. Point size, grid visibility, and contrast are cosmetic preferences kept in local storage. Chart navigation is entirely local after point data loads. CSV and JSON exports include the API source version and active filters; the JSON also records the displayed formulas.

If the API is unavailable, the error screen can open a deterministic demonstration snapshot. It is visibly labelled and is never confused with reconciled source data.

## Interaction and limitations

- Drag to pan, use a wheel or trackpad to zoom, and use Shift+drag (or the Box tool) for box zoom.
- Hover picking is client-side and exact coordinate overlaps are exposed as a candidate list.
- “Fit selected” zooms around the pinned UIK-party observation.
- PNG captures the WebGL layer. CSV contains only the currently loaded/visible observations.
- Missing commission fields remain explicit in the evidence panel.
- The client treats API reconciliation status as authoritative; it does not independently recalculate national source totals.
