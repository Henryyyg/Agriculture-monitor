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
    if not response.ok:
        raise ValueError(f"USDA download returned HTTP {response.status_code}.")
    if len(response.content) > 20_000_000:
        raise ValueError("Download exceeds 20 MB safety limit.")
    try:
        return pd.read_csv(io.BytesIO(response.content), low_memory=False, encoding="utf-8-sig")
    except UnicodeDecodeError:
        return pd.read_csv(io.BytesIO(response.content), low_memory=False, encoding="latin-1")


# USDA NASS Quick Stats: quarterly US grain stocks, using Streamlit Secrets.
NASS_URL = "https://quickstats.nass.usda.gov/api/api_GET/"
NASS_CROPS = {"Corn": "CORN", "Soybeans": "SOYBEANS", "Wheat": "WHEAT"}

def get_nass_key():
    # Strip accidental whitespace and quote characters copied from email.
    return str(st.secrets.get("USDA_NASS_API_KEY", "")).strip().strip('\\\"').strip("'").strip()

def test_nass_connection(api_key):
    # Deliberately avoid echoing requests exceptions, URLs or response bodies:
    # requests embeds query-string API keys in HTTP errors.
    if not api_key:
        return "Missing key in this app's Streamlit Secrets."
    try:
        response = requests.get(NASS_URL, params={
            "key": api_key, "commodity_desc": "CORN",
            "agg_level_desc": "NATIONAL", "year": "2023",
            "format": "JSON"
        }, timeout=25)
    except requests.RequestException:
        return "Unable to contact USDA. Check connectivity and try again."
    if response.status_code == 401:
        return "HTTP 401: USDA rejected this key. Check the NASS Quick Stats email, and verify the secret is saved to this deployment."
    if response.status_code == 403:
        return "HTTP 403: USDA denied access to this request."
    if response.status_code != 200:
        return f"USDA returned HTTP {response.status_code}. Authentication could not be confirmed."
    try:
        payload = response.json()
        if isinstance(payload, dict) and payload.get("error"):
            return "USDA accepted the request but returned a query error. The key may be valid."
    except ValueError:
        return "USDA returned HTTP 200, but not valid JSON."
    return "Success: USDA accepted the API key (HTTP 200)."



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
    if response.status_code == 401:
        raise ValueError("USDA rejected the NASS API key (HTTP 401). Check or regenerate the key in Streamlit Secrets.")
    if not response.ok:
        raise ValueError(f"USDA Quick Stats returned HTTP {response.status_code}.")
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
    api_key = get_nass_key()
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



st.sidebar.caption("USDA Agricultural Monitor")
start_year = st.sidebar.selectbox("Grain Stocks history from", [2020, 2015, 2010, 2023])
if st.sidebar.button("Refresh USDA data"):
    st.cache_data.clear()
    st.rerun()
st.subheader("USDA API connection")
if st.button("Test NASS API connection"):
    st.info(test_nass_connection(get_nass_key()))
st.subheader("Quarterly Grain Stocks — United States")
st.caption("NASS Quick Stats · National stocks · Latest reported quarter, not a WASDE forecast")
api_key = st.secrets.get("USDA_NASS_API_KEY", "")
if not api_key:
    st.error("Add USDA_NASS_API_KEY in Streamlit Cloud → Settings → Secrets to load quarterly Grain Stocks.")
else:
    cols = st.columns(3)
    for col, crop in zip(cols, CROPS):
        with col:
            st.markdown(f"**{crop}**")
            try:
                with st.spinner(f"Loading {crop}..."):
                    frame = nass_stocks(crop, start_year, api_key)
                if frame.empty:
                    st.warning("No matching NASS series returned")
                    continue
                # Keep the aggregate on/off-farm total; do not combine with its components.
                if "short_desc" in frame:
                    descriptions = frame["short_desc"].astype(str)
                    total = frame[descriptions.str.contains("STOCKS, MEASURED IN ALL POSITIONS", case=False, na=False)]
                    if total.empty:
                        total = frame[~descriptions.str.contains("ON FARM|OFF FARM", case=False, na=False)]
                    if not total.empty:
                        frame = total
                if "unit_desc" in frame and frame["unit_desc"].nunique() > 1:
                    frame = frame[frame["unit_desc"] == frame["unit_desc"].mode().iloc[0]]
                frame = frame.drop_duplicates(subset=["date", "short_desc"]).sort_values("date")
                latest = frame.iloc[-1]
                unit = latest.get("unit_desc", "as reported")
                st.metric("Latest", f"{latest.value_numeric:,.0f}", help=f"{latest.date.date()} · {unit}")
                st.caption(f"Quarter: {latest.date.date()} · {unit}")
                st.plotly_chart(px.line(frame, x="date", y="value_numeric", markers=True,
                    labels={"date":"Quarter","value_numeric":unit}), use_container_width=True, key=f"nass_{crop}")
            except Exception as exc:
                st.error(f"USDA request failed: {exc}")

st.divider()
st.subheader("Monthly WASDE Ending Stocks — United States")
st.caption("Corn, Soybeans and Wheat · Monthly estimate vintages, grouped by marketing year")
cols = st.columns(3)
for col, crop in zip(cols, CROPS):
    with col:
        st.markdown(f"**{crop}**")
        st.metric("Latest estimate", "Not connected")
st.divider()
st.subheader("Monthly WASDE Ending Stocks — Global")
st.caption("World totals · Million metric tons · Monthly estimate vintages")
cols = st.columns(3)
for col, crop in zip(cols, CROPS):
    with col:
        st.markdown(f"**{crop}**")
        st.metric("Latest estimate", "Not connected")
st.info("WASDE historical data is publicly downloadable without a key, but its automatic parser is not yet implemented. These placeholders deliberately do not show invented or mismatched values.")
