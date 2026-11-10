import io
import sqlite3
import requests
from pathlib import Path
import pandas as pd
import plotly.express as px
import streamlit as st

st.set_page_config(page_title="Newsquawk Agricultural Monitor",page_icon="🌾",layout="wide")
st.title("🌾 Newsquawk Agricultural Monitor")
st.caption("Historical USDA data · Prototype · Not a live release feed")
CROPS=["Corn","Soybeans","Wheat"]
DB=Path(__file__).with_name("agriculture.sqlite3")

@st.cache_resource
def connection():
    con=sqlite3.connect(DB,check_same_thread=False)
    con.execute("""CREATE TABLE IF NOT EXISTS observations(
      crop TEXT,region TEXT,report_date TEXT,marketing_year TEXT,
      metric TEXT,unit TEXT,value REAL,
      PRIMARY KEY(crop,region,report_date,marketing_year,metric,unit))""")
    con.commit()
    return con

def load():
    return pd.read_sql_query("SELECT * FROM observations ORDER BY report_date",connection())


# Official USDA monthly CSV links are discovered from the publication page,
# rather than hard-coding release dates or file names.
from html.parser import HTMLParser
from urllib.parse import urljoin

WASDE_ARCHIVE = "https://www.usda.gov/oce/commodity-markets/wasde/historical-wasde-report-data"

class WasdeLinks(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []
        self.href = None
        self.label = []
    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self.href = dict(attrs).get("href")
            self.label = []
    def handle_data(self, data):
        if self.href is not None:
            self.label.append(data)
    def handle_endtag(self, tag):
        if tag == "a" and self.href:
            url = urljoin(WASDE_ARCHIVE, self.href)
            if url.lower().split("?")[0].endswith(".csv") and "wasde" in url.lower():
                self.links.append(((" ".join(self.label)).strip(), url))
            self.href = None

@st.cache_data(ttl=3600, show_spinner=False)
def discover_wasde_csvs():
    response = requests.get(WASDE_ARCHIVE, timeout=35, headers={"User-Agent":"Newsquawk-Agricultural-Monitor/0.2"})
    response.raise_for_status()
    parser = WasdeLinks()
    parser.feed(response.text)
    return list(dict.fromkeys(parser.links))

@st.cache_data(ttl=3600, show_spinner=False)
def download_wasde_csv(url):
    if not url.startswith(("https://www.usda.gov/", "https://usda.gov/")):
        raise ValueError("Only official USDA download URLs are allowed.")
    response = requests.get(url, timeout=65)
    response.raise_for_status()
    if len(response.content) > 20_000_000:
        raise ValueError("Download exceeds 20 MB safety limit.")
    try:
        return pd.read_csv(io.BytesIO(response.content), low_memory=False, encoding="utf-8-sig")
    except UnicodeDecodeError:
        return pd.read_csv(io.BytesIO(response.content), low_memory=False, encoding="latin-1")


# USDA NASS Quick Stats: quarterly US grain stocks, using Streamlit Secrets.
NASS_URL = "https://quickstats.nass.usda.gov/api/api_GET/"
NASS_CROPS = {"Corn": "CORN", "Soybeans": "SOYBEANS", "Wheat": "WHEAT"}

@st.cache_data(ttl=21600, show_spinner=False)
def nass_stocks(crop, start_year, api_key):
    params = {
        "key": api_key,
        "commodity_desc": NASS_CROPS[crop],
        "statisticcat_desc": "STOCKS",
        "agg_level_desc": "NATIONAL",
        "source_desc": "SURVEY",
        "year__GE": str(start_year),
        "format": "JSON",
    }
    response = requests.get(NASS_URL, params=params, timeout=75)
    response.raise_for_status()
    payload = response.json()
    if "error" in payload:
        raise ValueError(str(payload["error"]))
    df = pd.DataFrame(payload.get("data", []))
    if df.empty:
        return df
    if "short_desc" in df:
        df = df[df["short_desc"].astype(str).str.contains("STOCKS", case=False, na=False)]
    if "freq_desc" in df:
        df = df[df["freq_desc"].eq("QUARTERLY")]
    if "domain_desc" in df:
        df = df[df["domain_desc"].eq("TOTAL")]
    if "reference_period_desc" in df:
        df = df[df["reference_period_desc"].isin(["MAR", "JUN", "SEP", "DEC"])]
    df = df.copy()
    df["value_numeric"] = pd.to_numeric(df["Value"].astype(str).str.replace(",", "", regex=False).str.replace("(D)", "", regex=False), errors="coerce")
    df["year_numeric"] = pd.to_numeric(df["year"], errors="coerce")
    months = {"MAR": 3, "JUN": 6, "SEP": 9, "DEC": 12}
    df["month_numeric"] = df["reference_period_desc"].map(months)
    df = df.dropna(subset=["value_numeric", "year_numeric", "month_numeric"])
    df["date"] = pd.to_datetime(dict(year=df["year_numeric"].astype(int), month=df["month_numeric"].astype(int), day=1)) + pd.offsets.MonthEnd(0)
    return df.sort_values("date")

def show_grain_stocks():
    st.subheader("USDA Quarterly Grain Stocks")
    st.caption("Official NASS Quick Stats · US national stocks · Survey data. Figures are historical observations, not live release headlines.")
    api_key = st.secrets.get("USDA_NASS_API_KEY", "")
    if not api_key:
        st.error("Add USDA_NASS_API_KEY to Streamlit Community Cloud → App settings → Secrets.")
        return
    a, b = st.columns(2)
    with a:
        crop = st.selectbox("Commodity", CROPS, key="nass_crop")
    with b:
        start_year = st.selectbox("History from", [2010, 2015, 2020, 2023], key="nass_start")
    try:
        with st.spinner("Retrieving USDA Grain Stocks..."):
            df = nass_stocks(crop, start_year, api_key)
    except Exception as exc:
        st.error(f"USDA Quick Stats request failed: {exc}")
        st.info("Check that the NASS key is saved correctly in Streamlit Secrets. If the key is new, retry shortly.")
        return
    if df.empty:
        st.warning("USDA returned no matching quarterly national stocks observations for these filters.")
        return
    descriptions = sorted(df["short_desc"].dropna().unique())
    description = st.selectbox("USDA series", descriptions)
    df = df[df["short_desc"] == description].copy()
    if "unit_desc" in df:
        units = sorted(df["unit_desc"].dropna().unique())
        if len(units) > 1:
            unit = st.selectbox("Units", units)
            df = df[df["unit_desc"] == unit]
    df = df.sort_values("date")
    unit_label = str(df["unit_desc"].iloc[-1]) if "unit_desc" in df and not df.empty else "as reported"
    if not df.empty:
        last = df.iloc[-1]
        st.metric("Latest USDA observation", f"{last.value_numeric:,.0f} {unit_label}", help=str(last.date.date()))
        st.plotly_chart(px.line(df, x="date", y="value_numeric", markers=True,
            labels={"date": "Reference quarter", "value_numeric": unit_label},
            title=f"{crop} — quarterly national grain stocks"), use_container_width=True)
        columns = [c for c in ["date", "Value", "unit_desc", "short_desc", "reference_period_desc", "year"] if c in df]
        st.dataframe(df[columns].sort_values("date", ascending=False), hide_index=True, use_container_width=True)
        st.download_button("Download Grain Stocks CSV", df[columns].to_csv(index=False),
                           f"usda_{crop.lower()}_grain_stocks.csv", "text/csv")


st.sidebar.caption("USDA data connections")
page = st.sidebar.radio("Navigation", ["Grain Stocks", "WASDE"])
if page == "Grain Stocks":
    show_grain_stocks()
else:
    st.subheader("WASDE — historical supply and demand")
    st.info("WASDE original-release historical data is published by USDA as downloadable datasets, not through the NASS Quick Stats API. No manual upload is required here; an automated WASDE vintage-data connector is still being developed.")
    st.markdown("[Official USDA WASDE historical data](https://www.usda.gov/oce/commodity-markets/wasde/historical-wasde-report-data)")
    st.markdown("[USDA FAS PSD API — separate API key required](https://apps.fas.usda.gov/opendatawebV2/)")
    st.caption("The PSD API supplies revised historical balances; original WASDE release vintages are a separate dataset. Do not treat them as interchangeable.")
