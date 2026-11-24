# UK Consumer Sentiment Dashboard

A free, open-source pipeline that tracks UK consumer sentiment about one or more market sectors (default: **UK high street banking**) across **Bluesky**, **YouTube** and **Reddit**, with no Reddit API key. It scores every mention with VADER, stores each weekly run as a date-stamped CSV in this repository, and shows the results in a Streamlit dashboard.

```
GitHub Actions (every Monday 08:00 GMT)
   └─ collector.py ──► Bluesky (atproto)           posts
                   ├─► YouTube Data API v3         comments on UK-relevant videos
                   └─► Brave Search API            Reddit thread titles + descriptions
        │  deduplicate → VADER score → UK filter
        ▼
   data/<sector>/sentiment_YYYY-MM-DD.csv  +  data/<sector>/history_index.json   (committed)
        │
        ▼
   app.py on Streamlit Community Cloud (updates automatically when the commit lands)
```

## What is in the repository

| File | Purpose |
|---|---|
| `collector.py` | Collects, deduplicates, scores and saves each run, for every sector. |
| `sectors/` | One JSON file per market sector: brands, aliases and phrases. Add sectors from GitHub's Run workflow form. |
| `app.py` | Streamlit dashboard: sector and run selectors, KPIs, trend chart, a breakdown by source and filterable top positive and negative tables. |
| `.github/workflows/scheduled_run.yml` | Runs the collector weekly, adds a form for new sectors, and commits the new CSVs. |
| `requirements.txt` | Python dependencies (Python 3.11 or later). |
| `.env.example` | Template for local environment variables. |
| `data/` | Run output, one folder per sector, plus `api_usage.json` (Brave request count). The dashboard reads this folder. |

## Why Reddit is collected through search

Reddit closed self-service API keys in November 2025 and stopped accepting new requests for public API access on 31 October 2026. Remaining public API access ends in March 2027 ([Reddit, r/redditdev](https://www.reddit.com/r/redditdev/comments/1wubcvf/moving_data_api_apps_to_the_developer_platform/)). This project therefore never calls Reddit. It asks the Brave Search API for `site:reddit.com` results and keeps thread titles and descriptions from Brave's index. For each search term it runs one targeted query for each of r/UKPersonalFinance and r/AskUK, plus one general `site:reddit.com` query with "UK".

Search snippets are thinner than full threads. You get the title and a short excerpt, not the comments, and there is no upvote count. Treat the Reddit line as a directional signal.

## Free-tier costs

| Source | What you need | Free allowance | This project's weekly use |
|---|---|---|---|
| Bluesky | A free account and an [app password](https://bsky.app/settings/app-passwords) | Free | Banking: 27 to about 400 search requests a run |
| YouTube Data API v3 | A Google Cloud project and an API key (no billing needed) | 100 `search.list` calls a day, plus 10,000 units a day for other calls ([Google quota table](https://developers.google.com/youtube/data/determine_quota_cost)) | Up to 20 searches and about 200 comment calls |
| Brave Search API | A Brave Search API key on the Search plan | No free plan. Includes $5 of free credit each month, about 1,000 requests at $5 per 1,000, and needs a card ([Brave pricing](https://api-dashboard.search.brave.com/documentation/pricing)) | Banking: 81 requests a run, 324 to 405 a month (four or five Mondays). Each extra weekly sector adds (brands + phrases) × 3 |
| GitHub Actions | A GitHub repository | Free for public repositories, with included minutes for private ones | One run of about 2 to 5 minutes a week |
| Streamlit Community Cloud | A free account at [share.streamlit.io](https://share.streamlit.io) | Free. Apps sleep after a spell without visitors and wake on the next visit | Always available |

**Brave is free in practice, not by contract.** The weekly budget sits well inside the $5 monthly credit, but Brave needs a card on file, and usage beyond the credit is billed. Set your prepaid top-up to $0 so it cannot charge you. Brave's FAQ also says storing results needs a custom plan ([Brave Search API](https://brave.com/search/api/)), and this project stores descriptions in Git. See "Compliance and privacy". The collector counts its Brave requests in `data/api_usage.json` and stops at 900 a calendar month (change with the `BRAVE_MONTHLY_LIMIT` repository variable). Brave's billing month may not match the calendar month exactly, so also set a spending limit in the Brave dashboard.

---

## Setup

### 1. Create the repository

1. Create a new GitHub repository (private is recommended; see "Compliance and privacy").
2. Add every file from this project, keeping the folder layout (including `.github/workflows/`, `sectors/` and `data/.gitkeep`).

### 2. Get your free keys

**Bluesky**
1. Sign in to Bluesky, then go to Settings → Privacy and security → App passwords.
2. Create an app password. Never use your main password.
3. Note your handle, for example `yourname.bsky.social`.

**YouTube**
1. Open [console.cloud.google.com](https://console.cloud.google.com) and create a project.
2. Go to APIs & Services → Library, then search for **YouTube Data API v3** and click Enable.
3. Go to APIs & Services → Credentials → Create credentials → API key.
4. Optional but recommended: edit the key and, under API restrictions, restrict it to YouTube Data API v3.

**Brave Search API**
1. Sign up at [api-dashboard.search.brave.com](https://api-dashboard.search.brave.com) and subscribe to the **Search** plan.
2. Add a card (Brave uses it for verification), and set the prepaid amount to $0 so you only use the free monthly credit.
3. Go to API Keys and create a key. This is your `SEARCH_API_KEY`. The collector sends it in the `X-Subscription-Token` header to `https://api.search.brave.com/res/v1/web/search`.

Each source is optional. The collector runs whichever sources have keys and logs a warning for the others. It stops with an error only if none are set.

### 3. Test locally (optional, recommended)

```bash
python -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -r requirements.txt

# Preview the dashboard with synthetic data. No keys needed.
for d in 2026-11-02 2026-11-09 2026-11-16 2026-11-23; do
  python collector.py --demo --data-dir demo_data --run-date $d
done
DATA_DIR=demo_data streamlit run app.py

# Live run with your keys
cp .env.example .env                 # then fill in the values
set -a; source .env; set +a
python collector.py
streamlit run app.py
```

`demo_data/` and `.env` are git-ignored, so neither reaches GitHub.

### 4. Add your keys to GitHub

In your repository, go to Settings → Secrets and variables → Actions → **Secrets** → New repository secret, and add:

| Secret | Value |
|---|---|
| `BLUESKY_HANDLE` | e.g. `yourname.bsky.social` |
| `BLUESKY_APP_PASSWORD` | your Bluesky app password |
| `YOUTUBE_API_KEY` | your Google API key |
| `SEARCH_API_KEY` | your Brave Search API key |

Optional **Variables** (same page, Variables tab): `LOOKBACK_DAYS`, the period each run covers for every source (default `30`, a rolling month), and `BRAVE_MONTHLY_LIMIT`, the monthly ceiling on Brave requests across all sectors (default `900`).

### 5. Allow the workflow to commit

Go to Settings → Actions → General → Workflow permissions and select **Read and write permissions**. The workflow also requests `contents: write`, but some accounts and organisations block it by default.

### 6. Run it once by hand

Open the Actions tab, select **Weekly sentiment collection**, then click **Run workflow**. When it finishes you should see a new commit such as `data: sentiment run 2026-11-23`, which adds `data/uk-high-street-banking/sentiment_2026-11-23.csv` and updates that folder's `history_index.json`. The run page shows a summary table for each source and lists any warnings.

### 7. Deploy the dashboard

1. Sign in at [share.streamlit.io](https://share.streamlit.io) with GitHub. For a private repository, grant private access under Settings → Linked accounts ([Streamlit docs](https://docs.streamlit.io/deploy/streamlit-community-cloud/get-started/connect-your-github-account)).
2. Click Create app, choose your repository and branch, and set the main file to `app.py`.
3. Deploy. The dashboard needs no secrets because it only reads `data/`.

Streamlit Community Cloud picks up each new commit automatically ([Streamlit docs](https://docs.streamlit.io/deploy/streamlit-community-cloud/manage-your-app)), so the weekly commit refreshes the live dashboard with no database.

---

## Adding sectors

Each sector is a file in `sectors/`, and its runs go to `data/<file name>/`. The dashboard has a **Market sector** dropdown at the top of the sidebar.

### From GitHub (no code)

1. Open the **Actions** tab, select **Weekly sentiment collection**, then click **Run workflow**.
2. Leave **sector** as `all` and fill in the new-sector fields, for example:

| Field | Example |
|---|---|
| New sector name | `UK supermarkets` |
| Brands | `Tesco*, Sainsbury's*\|Sainsburys, Asda*, Morrisons*, Waitrose*, Marks & Spencer*\|M&S, Co-op Food, Aldi, Lidl, Iceland Foods*\|Iceland, Ocado*` |
| Sector phrases | `supermarket, food shop, grocery prices, self-checkout, meal deal, loyalty card` |
| Subreddits | `AskUK, CasualUK` |
| Add the new sector to the Monday run | ticked |

3. Click **Run workflow**. The run saves `sectors/uk-supermarkets.json`, collects the first week of data and commits both. Streamlit picks up the commit and the sector appears in the dropdown.

Brand syntax:

- Commas separate brands. The first name is shown on the dashboard and used as the search query.
- `|` adds aliases: `Marks & Spencer|M&S`. Straight and curly apostrophes are matched automatically.
- `*` marks a name that is unambiguous outside the UK, so it counts as UK by itself (Tesco yes; Aldi, Lidl and Iceland no). Unmarked brands are kept only when the post also contains a UK signal such as £, UK or a UK city.
- Avoid everyday words as the first name. Use `Three UK|Three Mobile` rather than `Three`, and `Iceland Foods*|Iceland` rather than `Iceland`.

To rerun one existing sector, put its file name (for example `uk-supermarkets`) in **sector** and leave the new-sector fields empty. Running the form again with the same sector name overwrites that sector's settings.

### Budget per sector

Every weekly sector uses Brave requests. A sector with 12 brands and 6 phrases needs 18 × 3 = 54 requests a run (capped at 90).

| Weekly sectors | Brave requests a month | Inside the $5 credit? |
|---|---|---|
| Banking only | 324 to 405 | Yes |
| Banking + one sector of 18 searches | 540 to 675 | Yes |
| Banking + two such sectors | 756 to 945 | Only just. The 900 limit may cut the last run of the month short |

YouTube allows 100 searches a day and each sector uses up to 20, so up to four sectors fit comfortably in one Monday run. For one-off comparisons, untick "Add the new sector to the Monday run" and rerun the sector by hand when you need it.

### Editing or pausing a sector

Open the sector's file in `sectors/` on GitHub and click the pencil icon. Each brand is one line:

```json
"Tesco": {"aliases": ["Tesco"], "uk_unique": true},
```

- `"include_in_schedule": false` keeps the sector and its history but leaves it out of the Monday run.
- `extra_uk_signals` and `extra_uk_subreddits` add sector-specific UK filters on top of the shared lists in `collector.py`. Banking adds `sort code`, `FCA` and `ISA`, for example.
- `brave_target_subreddits` sets up to three subreddits for targeted Reddit searches.
- To delete a sector, delete its file in `sectors/` and its folder in `data/`.

Changing a sector's brands changes what is counted, so the trend line before and after the edit is not strictly comparable.

### Upgrading from the single-sector version

The first run of this version moves the existing `data/sentiment_*.csv` files and `data/history_index.json` into `data/uk-high-street-banking/`, and creates `sectors/uk-high-street-banking.json` if it is missing. Your earlier history is kept.

## How the collector works

| Source | Query | UK filter | Row type |
|---|---|---|---|
| Bluesky | Each brand and sector term, `lang=en`, since the lookback cut-off | Must contain a UK signal or a `uk_unique` brand | `post` |
| YouTube | `search.list` with `regionCode=GB`, `relevanceLanguage=en`, `publishedAfter` the cut-off | The video's title, description or channel must contain a UK signal. `regionCode=GB` only means "viewable in the UK", so it is not enough on its own | `comment` (top comments, which inherit the video's brand if they name none) |
| Reddit via Brave | `"<term>" site:reddit.com/r/UKPersonalFinance`, `"<term>" site:reddit.com/r/AskUK` and `"<term>" UK site:reddit.com`, with `country=GB` and a date range matching the lookback | The URL must be a Reddit thread. The subreddit must be on the UK list, or the text must have a UK signal | `thread_snippet` |

Deduplication removes repeats of the same item, the same Reddit thread found by several queries or URL forms (`old.reddit`, `www.reddit`), and identical text across sources.

Sentiment uses VADER's standard thresholds: compound ≥ 0.05 is positive, ≤ -0.05 is negative, and anything in between is neutral. **Net Sentiment Score = Positive % − Negative %**, which runs from −100 to +100.

### Output format

`data/<sector>/sentiment_YYYY-MM-DD.csv`, one row per mention:

`run_date, collected_at_utc, source, item_type, item_id, community, context, brands, matched_terms, text, created_utc, engagement, url, author_hash, compound, pos, neu, neg, sentiment`

`data/<sector>/history_index.json` holds one summary per run (totals, percentages, net score and a breakdown by source). The trend chart reads it. If it is ever deleted or damaged, rebuild it with `python collector.py --rebuild-index`.

Running the collector twice on the same day overwrites that day's file and index entry.

---

## Limitations

- **VADER is fast and free but shallow.** It misses sarcasm and context, and it scores some clear complaints as neutral. For example, "closed yet another branch on our high street" scores 0.0. Read week-to-week moves of a few points as noise, and check the posts tables before drawing conclusions.
- **The data is not a representative sample** of UK consumers. Bluesky and YouTube commenters skew younger and more online, and search engines decide which Reddit threads appear.
- **Reddit descriptions are short.** Brave's `page_age` date is a best estimate and is sometimes missing, and there are no upvote counts.
- **GitHub schedules are best effort.** Runs can start late at busy times. GitHub can also disable scheduled workflows in public repositories after 60 days with no repository activity. If runs stop, re-enable the workflow in the Actions tab.
- **Each run is a rolling 30-day snapshot.** Weekly runs overlap, so a post can appear in up to five consecutive runs. That is intended for a rolling measure, but it smooths the trend: a sudden change shows up gradually. Do not add mention counts across runs. Set `LOOKBACK_DAYS` to `7` for separate weekly windows. The dashboard warns when two compared runs cover different periods.
- **08:00 GMT is 09:00 BST** in summer, because GitHub cron always runs in UTC.

## Compliance and privacy

- **YouTube storage rule.** YouTube's Developer Policies require stored API data, such as comment text, to be refreshed or deleted within 30 days ([YouTube Developer Policies, III.E.4](https://developers.google.com/youtube/terms/developer-policies)). The collector removes YouTube rows from any CSV that is 21 or more days old, which keeps every stored copy under 30 days on a weekly schedule. Change this with `YOUTUBE_RETENTION_DAYS`. The run's aggregate figures stay in `history_index.json`. Storing derived metrics such as sentiment beyond 30 days formally needs YouTube's approval through its quota extension process ([YouTube derived-metrics policy](https://developers.google.com/youtube/terms/derived-metrics-policy)). For long-term commercial use, apply for that approval or set the YouTube key aside.
- **Git history keeps deleted rows.** Pruning removes YouTube rows from the current files, but earlier versions remain in Git history. Treat the repository as private. If strict deletion matters, periodically squash the data history.
- **UK GDPR.** Public posts can still be personal data. The collector stores only a one-way hash of each author, never names or handles, but the post text and links remain. Use a private repository, collect only what you need, and remove a post promptly if someone asks.
- **Brave terms.** Brave says storing results needs a custom plan, and its free monthly credit is reported to depend on attributing Brave Search in your project ([Implicator](https://www.implicator.ai/brave-drops-free-search-api-tier-puts-all-developers-on-metered-billing.md)). The dashboard footer credits Brave. Before you use this commercially or publish the repository, check Brave's terms or contact Brave about storage rights.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `No sources configured` | No secrets reached the workflow. Check the secret names exactly. |
| `Bluesky login failed` | Use an app password and the full handle, e.g. `name.bsky.social`. |
| YouTube `403 quotaExceeded` | The daily quota resets at midnight Pacific Time. Reduce `YOUTUBE_MAX_SEARCHES`. |
| Brave `402` / `429` errors | The monthly credit is used up or you are rate-limited. Lower `BRAVE_MONTHLY_LIMIT`. |
| Brave `401` / `403` | The key is wrong, or the Search plan is not active on it. |
| The workflow push fails with 403 | Complete setup step 5 (Read and write permissions). |
| The dashboard says "No runs found" | No CSV has been committed to `data/` yet. Run the workflow once by hand. |
| `Unknown sector` | The **sector** field must match a file name in `sectors/` without `.json`, or be `all`. |
| `Brave monthly limit reached` | The 900-request ceiling was hit. Reddit results resume next month. Untick some sectors' `include_in_schedule`, or raise `BRAVE_MONTHLY_LIMIT` if you accept charges. |
| `New sector not saved` | Fill in at least the Brands or the Sector phrases field. |

## Licence

MIT. Use, adapt and share freely.
