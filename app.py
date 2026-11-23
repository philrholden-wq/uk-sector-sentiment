"""
app.py - Streamlit dashboard for the UK consumer sentiment collector.

Reads each sector folder in data/ (data/<sector>/history_index.json and its
date-stamped CSVs). A sidebar dropdown switches between sectors.
Run locally:   streamlit run app.py
Preview demo:  DATA_DIR=demo_data streamlit run app.py
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import altair as alt
import pandas as pd
import streamlit as st

DATA_ROOT = Path(os.getenv("DATA_DIR", "data"))

st.set_page_config(page_title="UK Consumer Sentiment", layout="wide")

SOURCE_LABELS = {"bluesky": "Bluesky", "youtube": "YouTube", "reddit_brave": "Reddit (via Brave)", "demo": "Demo"}


def src_label(s: str) -> str:
    return SOURCE_LABELS.get(s, s)


# -----------------------------------------------------------------------------
# Data loading. The cache key includes file modification times, so a new commit
# from GitHub Actions shows up as soon as Streamlit Cloud pulls it.
# -----------------------------------------------------------------------------
def discover_sectors() -> dict[str, Path]:
    """Sector name -> folder. Also accepts the older flat layout (CSVs directly in data/)."""
    if not DATA_ROOT.exists():
        return {}
    found: dict[str, Path] = {}
    for folder in sorted(p for p in DATA_ROOT.iterdir() if p.is_dir()) + [DATA_ROOT]:
        if not any(folder.glob("sentiment_*.csv")):
            continue
        name = folder.name.replace("-", " ").capitalize() if folder != DATA_ROOT else "UK consumer sentiment"
        try:
            name = json.loads((folder / "history_index.json").read_text(encoding="utf-8")).get("sector", name)
        except (OSError, json.JSONDecodeError):
            pass
        found[name if name not in found else f"{name} ({folder.name})"] = folder
    return dict(sorted(found.items(), key=lambda kv: kv[0].lower()))


def _fingerprint(folder: Path) -> tuple:
    if not folder.exists():
        return ()
    return tuple(sorted((p.name, p.stat().st_mtime) for p in folder.iterdir() if p.is_file()))


@st.cache_data(show_spinner=False)
def load_index(folder: str, _fp: tuple) -> tuple[str, pd.DataFrame]:
    DATA_DIR = Path(folder)
    INDEX_PATH = DATA_DIR / "history_index.json"
    sector = "UK consumer sentiment"
    runs: list[dict] = []
    if INDEX_PATH.exists():
        try:
            payload = json.loads(INDEX_PATH.read_text(encoding="utf-8"))
            sector = payload.get("sector", sector)
            runs = payload.get("runs", [])
        except json.JSONDecodeError:
            runs = []
    if not runs:  # fall back to scanning CSVs if the index is missing or broken
        for csv in sorted(DATA_DIR.glob("sentiment_*.csv")):
            m = re.match(r"sentiment_(\d{4}-\d{2}-\d{2})\.csv$", csv.name)
            if m:
                df = pd.read_csv(csv)
                n = len(df)
                pos = int((df["sentiment"] == "positive").sum()) if n else 0
                neg = int((df["sentiment"] == "negative").sum()) if n else 0
                runs.append({"run_date": m.group(1), "file": csv.name, "total_mentions": n,
                             "positive_pct": round(100 * pos / n, 1) if n else None,
                             "negative_pct": round(100 * neg / n, 1) if n else None,
                             "net_sentiment": round(100 * (pos - neg) / n, 1) if n else None})
    idx = pd.DataFrame(runs)
    if not idx.empty:
        idx["run_date"] = pd.to_datetime(idx["run_date"])
        idx = idx[idx["file"].map(lambda f: (DATA_DIR / f).exists())]
        idx = idx.sort_values("run_date").reset_index(drop=True)
    return sector, idx


@st.cache_data(show_spinner=False)
def load_run(folder: str, file_name: str, _fp: tuple) -> pd.DataFrame:
    text_cols = ("brands", "matched_terms", "text", "community", "context", "url")
    df = pd.read_csv(Path(folder) / file_name, dtype={"item_id": str, **{c: str for c in text_cols}})
    for col in text_cols:
        if col in df:
            df[col] = df[col].fillna("")
    if "created_utc" in df:
        df["created_utc"] = pd.to_datetime(df["created_utc"], errors="coerce", utc=True)
    return df


def uk_date(d) -> str:
    """UK long date, e.g. 'Monday 23 November 2026'."""
    d = pd.Timestamp(d)
    return f"{d.strftime('%A')} {d.day} {d.strftime('%B %Y')}"


def short_date(d) -> str:
    """UK date without weekday, e.g. '23 November 2026'."""
    d = pd.Timestamp(d)
    return f"{d.day} {d.strftime('%B %Y')}"


def fmt_pct(v) -> str:
    return "n/a" if v is None or pd.isna(v) else f"{v:.1f}%"


def fmt_net(v) -> str:
    return "n/a" if v is None or pd.isna(v) else f"{v:+.1f}"


def delta(cur, prev):
    if prev is None or cur is None or pd.isna(cur) or pd.isna(prev):
        return None
    return round(float(cur) - float(prev), 1)


# -----------------------------------------------------------------------------
# Empty state
# -----------------------------------------------------------------------------
sectors = discover_sectors()
with st.sidebar:
    st.header("Sector")
    if sectors:
        sector_choice = st.selectbox("Market sector", list(sectors), index=0,
                                     help="Add sectors from GitHub: Actions > Run workflow. See README.")
        DATA_DIR = sectors[sector_choice]
    else:
        DATA_DIR = DATA_ROOT
fp = _fingerprint(DATA_DIR)
sector, index = load_index(str(DATA_DIR), fp) if sectors else ("", pd.DataFrame())

if index.empty:
    st.title("UK Consumer Sentiment")
    st.info(
        f"No runs found in `{DATA_ROOT}/`. Run `python collector.py` with your credentials, "
        "or preview the dashboard with demo data:\n\n"
        "```\npython collector.py --demo --data-dir demo_data\nDATA_DIR=demo_data streamlit run app.py\n```"
    )
    st.stop()

# -----------------------------------------------------------------------------
# Sidebar: run selector and filters
# -----------------------------------------------------------------------------
runs_desc = index.sort_values("run_date", ascending=False).reset_index(drop=True)
labels = [
    f"{short_date(r.run_date)}{' (latest)' if i == 0 else ''}"
    for i, r in enumerate(runs_desc.itertuples())
]
with st.sidebar:
    st.header("Run")
    choice = st.selectbox("Date-stamped run", options=range(len(labels)),
                          format_func=lambda i: labels[i], index=0)
    selected = runs_desc.iloc[choice]
    previous = runs_desc.iloc[choice + 1] if choice + 1 < len(runs_desc) else None

df = load_run(str(DATA_DIR), selected["file"], fp)

with st.sidebar:
    st.header("Filters")
    st.caption("Filters apply to the post tables and brand chart, not the headline KPIs.")
    sources = sorted(df["source"].dropna().unique()) if not df.empty else []
    f_sources = st.multiselect("Source", sources, default=sources, format_func=src_label)
    communities = sorted(df["community"].dropna().unique()) if not df.empty else []
    f_comms = st.multiselect("Community / subreddit", communities, placeholder="All communities")
    brand_list = sorted({b.strip() for s in df.get("brands", pd.Series(dtype=str)) for b in s.split(";") if b.strip()})
    f_brands = st.multiselect("Brand", brand_list, placeholder="All brands")
    f_text = st.text_input("Text contains", placeholder="e.g. branch, app, fee")
    f_min = st.slider("Minimum sentiment strength |compound|", 0.0, 1.0, 0.05, 0.05)
    f_top = st.slider("Posts per table", 5, 100, 20, 5)
    st.divider()
    st.caption(f"Data folder: `{DATA_DIR}/` · {len(index)} saved runs")

# -----------------------------------------------------------------------------
# Header and KPIs
# -----------------------------------------------------------------------------
st.title(f"UK Consumer Sentiment: {sector}")
gen = selected.get("generated_at_utc")
st.caption(
    f"Run: {uk_date(selected['run_date'])}"
    + (f" · collected {pd.Timestamp(gen).strftime('%H:%M')} UTC" if isinstance(gen, str) and gen else "")
    + (f" · sources: {', '.join(src_label(x) for x in selected['sources'])}" if isinstance(selected.get("sources"), list) else "")
    + " · scored with VADER"
)
warnings = selected.get("warnings")
if isinstance(warnings, list) and warnings:
    with st.expander(f"{len(warnings)} collector warning(s) for this run"):
        for w in warnings:
            st.write(f"- {w}")

p = previous if previous is not None else {}
k1, k2, k3, k4 = st.columns(4)
k1.metric("Total Mentions", f"{int(selected['total_mentions']):,}",
          delta=None if previous is None else int(selected["total_mentions"] - previous["total_mentions"]))
k2.metric("Positive %", fmt_pct(selected.get("positive_pct")),
          delta=delta(selected.get("positive_pct"), p.get("positive_pct")))
k3.metric("Negative %", fmt_pct(selected.get("negative_pct")),
          delta=delta(selected.get("negative_pct"), p.get("negative_pct")), delta_color="inverse")
k4.metric("Net Sentiment Score", fmt_net(selected.get("net_sentiment")),
          delta=delta(selected.get("net_sentiment"), p.get("net_sentiment")),
          help="Positive % minus Negative %. Ranges from -100 (all negative) to +100 (all positive).")
if previous is not None:
    st.caption(f"Changes are compared with the previous run on {uk_date(previous['run_date'])}.")

# Per-source breakdown for the selected run. Uses the summary saved at run time
# (so it still matches the KPIs after old YouTube rows are pruned).
bs = selected.get("by_source")
if isinstance(bs, dict) and bs:
    breakdown = pd.DataFrame([
        {"Source": src_label(k), "Mentions": v.get("mentions"), "Positive %": v.get("positive_pct"),
         "Negative %": v.get("negative_pct"), "Net Sentiment": v.get("net_sentiment")}
        for k, v in bs.items()])
elif not df.empty:
    g = df.groupby("source")["sentiment"]
    breakdown = pd.DataFrame({
        "Source": [src_label(s) for s in g.size().index],
        "Mentions": g.size().values,
        "Positive %": (100 * g.apply(lambda s: (s == "positive").mean())).round(1).values,
        "Negative %": (100 * g.apply(lambda s: (s == "negative").mean())).round(1).values,
    })
    breakdown["Net Sentiment"] = (breakdown["Positive %"] - breakdown["Negative %"]).round(1)
else:
    breakdown = pd.DataFrame()
if not breakdown.empty:
    st.subheader("Breakdown by source")
    st.dataframe(breakdown.sort_values("Mentions", ascending=False), hide_index=True, width="stretch",
                 column_config={"Net Sentiment": st.column_config.NumberColumn(format="%+.1f")})
if selected.get("youtube_pruned") is True:
    st.caption("YouTube comments from this run have been deleted under YouTube's 30-day storage rule. "
               "Its YouTube figures above are kept; its posts tables no longer include YouTube.")

# -----------------------------------------------------------------------------
# Trend chart across all runs
# -----------------------------------------------------------------------------
st.subheader("Net Sentiment trend across all runs")
trend = index.dropna(subset=["net_sentiment"]).copy()
if len(trend) == 0:
    st.info("No scored runs yet.")
else:
    trend["label"] = trend["run_date"].map(uk_date)
    trend["selected"] = trend["run_date"] == selected["run_date"]
    # Put one tick on each run date (up to 12 runs), otherwise let Altair choose.
    tick_values = ([int(t.timestamp() * 1000) for t in trend["run_date"]] if len(trend) <= 12 else None)
    base = alt.Chart(trend).encode(
        x=alt.X("run_date:T", title=None,
                axis=alt.Axis(format="%d %b %Y", labelAngle=0, values=tick_values, grid=False)),
        y=alt.Y("net_sentiment:Q", title="Net Sentiment Score"),
        tooltip=[alt.Tooltip("label:N", title="Run"),
                 alt.Tooltip("net_sentiment:Q", title="Net sentiment", format="+.1f"),
                 alt.Tooltip("positive_pct:Q", title="Positive %", format=".1f"),
                 alt.Tooltip("negative_pct:Q", title="Negative %", format=".1f"),
                 alt.Tooltip("total_mentions:Q", title="Mentions", format=",")],
    )
    zero = alt.Chart(pd.DataFrame({"y": [0]})).mark_rule(strokeDash=[4, 4], opacity=0.5).encode(y="y:Q")
    line = base.mark_line(point=False, strokeWidth=2.5)
    points = base.mark_circle().encode(
        size=alt.condition("datum.selected", alt.value(160), alt.value(50)),
        opacity=alt.condition("datum.selected", alt.value(1), alt.value(0.7)),
    )
    chart = zero + line + points

    # Optional per-source lines, from the by_source summaries in history_index.json
    if "by_source" in index.columns and st.toggle("Show each source as a separate line", value=False):
        per_src = [
            {"run_date": r.run_date, "source": src_label(src), "net_sentiment": v.get("net_sentiment"),
             "mentions": v.get("mentions")}
            for r in index.itertuples() if isinstance(r.by_source, dict)
            for src, v in r.by_source.items() if v.get("net_sentiment") is not None
        ]
        if per_src:
            src_df = pd.DataFrame(per_src)
            src_lines = alt.Chart(src_df).mark_line(point=True, strokeDash=[5, 3], opacity=0.8).encode(
                x="run_date:T", y="net_sentiment:Q",
                color=alt.Color("source:N", title="Source"),
                tooltip=["source", alt.Tooltip("run_date:T", format="%d %b %Y", title="Run"),
                         alt.Tooltip("net_sentiment:Q", format="+.1f", title="Net"), "mentions"])
            chart = chart + src_lines
    st.altair_chart(chart.properties(height=300), width="stretch")
    st.caption("Solid line: all sources combined. Larger dot: the run selected in the sidebar.")
    if len(trend) == 1:
        st.caption("The trend line appears once a second run is saved.")

# -----------------------------------------------------------------------------
# Filtered posts
# -----------------------------------------------------------------------------
if df.empty:
    st.warning("This run found no matching mentions.")
    st.stop()

view = df[df["source"].isin(f_sources)]
if f_comms:
    view = view[view["community"].isin(f_comms)]
if f_brands:
    pattern = "|".join(re.escape(b) for b in f_brands)
    view = view[view["brands"].str.contains(pattern, regex=True)]
if f_text:
    view = view[view["text"].str.contains(re.escape(f_text), case=False, regex=True)]
view = view[view["compound"].abs() >= f_min]

# Brand breakdown (filtered)
brand_rows = view.assign(brand=view["brands"].str.split(";")).explode("brand")
brand_rows["brand"] = brand_rows["brand"].str.strip()
brand_rows = brand_rows[brand_rows["brand"] != ""]
if not brand_rows.empty:
    st.subheader("Net sentiment by brand (filtered)")
    by_brand = (brand_rows.groupby("brand")
                .agg(mentions=("sentiment", "size"),
                     pos=("sentiment", lambda s: (s == "positive").sum()),
                     neg=("sentiment", lambda s: (s == "negative").sum()))
                .reset_index())
    by_brand["net"] = (100 * (by_brand["pos"] - by_brand["neg"]) / by_brand["mentions"]).round(1)
    by_brand = by_brand[by_brand["mentions"] >= 3]
    if by_brand.empty:
        st.caption("Not enough mentions per brand to chart (minimum 3).")
    else:
        bar = alt.Chart(by_brand).mark_bar().encode(
            x=alt.X("net:Q", title="Net Sentiment Score"),
            y=alt.Y("brand:N", sort="-x", title=None),
            color=alt.condition("datum.net >= 0", alt.value("#2e7d32"), alt.value("#c62828")),
            tooltip=["brand", alt.Tooltip("net:Q", format="+.1f"), "mentions"],
        ).properties(height=max(160, 26 * len(by_brand)))
        st.altair_chart(bar, width="stretch")
        st.caption("Brands with fewer than 3 mentions are hidden.")

st.subheader("Top posts from this run")
st.caption(f"{len(view):,} of {len(df):,} mentions match the current filters.")

view = view.assign(source_label=view["source"].map(src_label))
table_cols = ["compound", "text", "source_label", "community", "context", "brands", "created_utc", "engagement", "url"]
col_cfg = {
    "compound": st.column_config.ProgressColumn("Sentiment", min_value=-1.0, max_value=1.0, format="%.2f"),
    "text": st.column_config.TextColumn("Text", width="large"),
    "source_label": st.column_config.TextColumn("Source"),
    "community": st.column_config.TextColumn("Community"),
    "context": st.column_config.TextColumn("Video / thread", help="YouTube video title or Reddit thread title"),
    "brands": st.column_config.TextColumn("Brands"),
    "created_utc": st.column_config.DatetimeColumn("Posted (UTC)", format="DD/MM/YYYY HH:mm"),
    "engagement": st.column_config.NumberColumn(
        "Engagement", help="Bluesky likes + reposts + replies, or YouTube comment likes. Not available for Reddit search results."),
    "url": st.column_config.LinkColumn("Link", display_text="Open"),
}
group_by_source = st.toggle("Break tables down by source", value=True)


def show_table(frame: pd.DataFrame, polarity: str) -> None:
    subset = frame[frame["sentiment"] == polarity]
    subset = subset.nlargest(f_top, "compound") if polarity == "positive" else subset.nsmallest(f_top, "compound")
    if subset.empty:
        st.info(f"No {polarity} mentions match the filters.")
    else:
        st.dataframe(subset[table_cols], column_config=col_cfg, hide_index=True, width="stretch")


for tab, polarity in zip(st.tabs(["Most positive", "Most negative"]), ["positive", "negative"]):
    with tab:
        if view.empty:
            st.info("No mentions match the current filters. Try lowering the minimum sentiment strength.")
        elif group_by_source:
            for src in sorted(view["source"].unique(), key=src_label):
                part = view[view["source"] == src]
                n_pol = int((part["sentiment"] == polarity).sum())
                st.markdown(f"**{src_label(src)}** · {n_pol:,} {polarity} of {len(part):,} mentions")
                show_table(part, polarity)
        else:
            show_table(view, polarity)

st.download_button("Download filtered rows (CSV)", view.drop(columns="source_label").to_csv(index=False).encode("utf-8"),
                   file_name=f"filtered_{DATA_DIR.name}_{pd.Timestamp(selected['run_date']).date().isoformat()}.csv",
                   mime="text/csv")

st.caption(
    "Method: VADER compound score ≥ 0.05 is positive, ≤ -0.05 is negative, otherwise neutral. "
    "VADER is fast and free but misses sarcasm and context, so treat small week-to-week changes with caution. "
    "Authors are stored as one-way hashes. Reddit results are powered by the Brave Search API."
)
