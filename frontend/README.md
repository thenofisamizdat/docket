# Docket board frontend (React + Vite)

Source for the Docket board SPA that the backend serves at `/docket`. This lives
in the repo so the shipped UI is **rebuilt from source** — it is the single source
of truth, not a prebuilt blob that drifts.

## Build

```
cd frontend
npm install          # first time only
npm run build        # writes straight into ../src/docket_dev/web/dist (the served bundle)
```

`vite.config.js` sets `base: '/docket/'` and `build.outDir` to the Python package's
`web/dist`, so a build immediately updates what `docket serve` ships. Commit the
regenerated `src/docket_dev/web/dist/` alongside your source changes.

## Dev

```
npm run dev          # http://localhost:5175, proxies /api → a local `docket serve` (:8011)
```

Styling is Tailwind (`tailwind.config.js` + `postcss.config.js`) — both are
required for the build to emit the full stylesheet.
