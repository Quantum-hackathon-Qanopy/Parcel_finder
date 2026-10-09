# web/ — the public demo site

A static site: a landing page explaining the project and its caveats, plus the
monitoring dashboard. No build step, no server, no Earth Engine credentials.

```
web/
  index.html      landing page
  dashboard.html  the generated monitoring dashboard (placeholder until replaced)
```

## Publishing the real dashboard

`src/monitor.py` writes `outputs/monitoring_dashboard.html`. That file embeds all of
its data as JSON and draws its basemap from Esri World Imagery, so it is
self-contained and keeps working offline from the repository. Copy it over the
placeholder:

```bash
cp outputs/monitoring_dashboard.html web/dashboard.html
```

Do **not** publish `outputs/selection_map.html` the same way. It is built by geemap
and references Earth Engine tile URLs that expire after a few days, so it goes blank
some time after it is generated. Regenerate it immediately before showing it, or link
to it with that caveat stated.

## Deploying

From the repository root:

```bash
npx vercel deploy web --prod
```

The first run asks which account and project to use and then prints the URL. Nothing
here depends on the GitHub integration, so it works whether or not the Vercel GitHub
App has been granted access to the organisation.

To deploy automatically on every push instead, install the Vercel GitHub App for the
`Quantum-hackathon-Qanopy` organisation, grant it this repository, and set the project
root directory to `web`. Note that `outputs/` is gitignored, so the dashboard has to
be committed under `web/` for a push-triggered build to include it.

## Editing

Both pages are single files with inline CSS. The palette matches the pitch deck:

| | |
| --- | --- |
| background | `#fafaf7` |
| panel | `#f0f2ee` |
| ink | `#1b3a2f` |
| muted text | `#4a5a52` |
| accent | `#bf6b2c` |

The figures on the landing page are the ones in the project README's limitations
section. If the monitoring window moves or the matching changes, update both together
so the site and the repository never disagree.
