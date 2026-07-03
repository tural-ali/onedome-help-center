# OneDome Help Center

Public help center for OneDome, generated as a static site from the Confluence
**Customer Support KB** (CSKB) space and deployed to Cloudflare Pages.

- **Source of truth:** Confluence space `CSKB` (id `591855635`) on
  `onedome.atlassian.net`. Editors manage articles in Confluence; the site
  rebuilds from it.
- **Design:** onedome.com branding (navy nav, black footer, brand yellow
  `#FBBB30`, Proxima Nova / Inter) with the help.onedome.com layout
  (hero + search, topic cards, article views).
- **Privacy:** the build authenticates to Confluence server-side (in CI) and
  emits fully public static HTML. The Confluence space itself stays private.
- **Publishing:** only `current` (published) CSKB pages are included; drafts are
  skipped. Hierarchy = Home → categories → sections → articles (by page depth).

## Build locally

```bash
pip install -r requirements.txt
export ATLASSIAN_EMAIL=you@onedome.com
export ATLASSIAN_API_TOKEN=...          # Confluence API token
python build.py                          # writes dist/index.html
python3 -m http.server -d dist 8791      # preview at http://localhost:8791
```

## Deployment

GitHub Actions (`.github/workflows/deploy.yml`) runs:
- **daily at 06:00 UTC** (keeps the site in sync with CSKB),
- on **push to main**, and
- **manually** (workflow_dispatch).

It builds `dist/` and deploys to the Cloudflare Pages project
`onedome-help-center`.

### Required GitHub secrets
| Secret | Purpose |
|---|---|
| `ATLASSIAN_EMAIL` | Confluence service-account email |
| `ATLASSIAN_API_TOKEN` | Confluence API token |
| `CLOUDFLARE_API_TOKEN` | Cloudflare token with Pages: Edit |
| `CLOUDFLARE_ACCOUNT_ID` | Cloudflare account id |

Optional repo **variables**: `CONFLUENCE_DOMAIN` (default `onedome.atlassian.net`),
`CSKB_SPACE_ID` (default `591855635`).

## Custom domain

Intended to live at **help.onedome.com**. DNS for `onedome.com` is on AWS
Route 53, so the cutover is: add `help.onedome.com` as a custom domain on the
Cloudflare Pages project, then point the Route 53 `help.onedome.com` CNAME at the
Pages domain (currently it CNAMEs to `onedome.zendesk.com`). Do this only after
reviewing the temporary `*.pages.dev` URL.

## Optional: "Popular articles"

Add the Confluence label `promoted` to any CSKB article to feature it in the
Popular articles section on the home page.
