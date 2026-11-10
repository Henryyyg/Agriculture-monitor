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

page=st.sidebar.radio("Navigation",["Overview","Grain Stocks","WASDE Explorer","Historical Data","Data Connections"])
data=load()
if page=="Grain Stocks":
    show_grain_stocks()
elif page=="Data Connections":
    st.subheader("Import historical WASDE CSV")
    st.markdown("Download the official historical CSV from [USDA](https://www.usda.gov/oce/commodity-markets/wasde/historical-wasde-report-data) and upload it below.")
    st.markdown("### Automatic USDA download (no API key)")
    st.caption("Downloads the original monthly WASDE CSV directly from USDA. The first import requires checking the field mapping; no manual file download is needed.")
    raw = None
    try:
        links = discover_wasde_csvs()
        if links:
            chosen = st.selectbox("Official USDA report", range(len(links)), format_func=lambda i: f"{links[i][0]} — {links[i][1].split('/')[-1]}")
            if st.button("Download selected USDA report", type="primary"):
                st.session_state["wasde_download"] = download_wasde_csv(links[chosen][1])
                st.session_state["wasde_download_url"] = links[chosen][1]
                st.success("Downloaded from USDA. Check the mapping below, then import.")
        else:
            st.warning("USDA page did not expose any CSV links. Manual upload is still available.")
    except Exception as exc:
        st.warning(f"Automatic download unavailable: {exc}. You can still upload a CSV.")
    if "wasde_download" in st.session_state:
        raw = st.session_state["wasde_download"]
        st.caption("Source: " + st.session_state.get("wasde_download_url", "USDA"))
        if st.button("Clear downloaded report"):
            st.session_state.pop("wasde_download", None)
            st.rerun()
    uploaded=st.file_uploader("Or upload a USDA CSV",type="csv")
    if uploaded is not None:
        try:
            raw=pd.read_csv(uploaded,low_memory=False)
        except Exception as exc:
            st.error(f"Unable to read CSV: {exc}")
    if raw is not None:
        try:
            st.dataframe(raw.head(8),use_container_width=True)
            names=list(raw.columns)
            def field(label,keywords):
                matches=[i for i,n in enumerate(names) if any(k in n.lower() for k in keywords)]
                return st.selectbox(label,names,index=matches[0] if matches else 0)
            a,b=st.columns(2)
            with a:
                crop_col=field("Commodity",["commodity","crop"])
                date_col=field("Report date",["report date","release date","date"])
                year_col=field("Marketing year",["market year","marketing year","year"])
            with b:
                region_col=field("Region",["region","country"])
                metric_col=field("Metric",["attribute","metric","item"])
                value_col=field("Value",["value","amount"])
            unit_col=st.selectbox("Unit column (optional)",["(none)"]+names)
            if st.button("Validate and import",type="primary"):
                df=pd.DataFrame({
                  "crop":raw[crop_col].astype(str).str.strip().str.lower().map(
                    {"corn":"Corn","maize":"Corn","soybeans":"Soybeans","soybean":"Soybeans","wheat":"Wheat"}),
                  "region":raw[region_col].astype(str),
                  "report_date":pd.to_datetime(raw[date_col],errors="coerce").dt.strftime("%Y-%m-%d"),
                  "marketing_year":raw[year_col].astype(str),
                  "metric":raw[metric_col].astype(str),
                  "unit":raw[unit_col].astype(str) if unit_col!="(none)" else "As reported",
                  "value":pd.to_numeric(raw[value_col].astype(str).str.replace(",","",regex=False),errors="coerce")
                }).dropna(subset=["crop","report_date","value"])
                if df.empty:
                    st.error("No valid observations. Check the column mappings.")
                else:
                    df.to_sql("incoming",connection(),if_exists="replace",index=False)
                    connection().execute("INSERT OR REPLACE INTO observations SELECT * FROM incoming")
                    connection().execute("DROP TABLE incoming")
                    connection().commit()
                    st.success(f"Imported {len(df):,} observations")
                    st.rerun()
        except Exception as e:
            st.error(f"CSV import error: {e}")
    st.info("Quarterly Grain Stocks is now available in the Grain Stocks sidebar tab. WASDE still uses the separate public USDA download workflow.")
elif data.empty:
    st.info("No data imported yet. Use Data Connections to upload historical USDA WASDE data.")
elif page=="Overview":
    for crop in CROPS:
        with st.expander(crop,expanded=True):
            sub=data[data.crop==crop]
            if sub.empty:
                st.write("No observations")
            else:
                latest=sub.report_date.max()
                st.caption(f"Latest imported report: {latest}")
                st.dataframe(sub[sub.report_date==latest],hide_index=True,use_container_width=True)
elif page=="WASDE Explorer":
    crop=st.selectbox("Commodity",CROPS)
    sub=data[data.crop==crop]
    if sub.empty:
        st.warning("No data for this commodity")
    else:
        a,b,c=st.columns(3)
        with a: region=st.selectbox("Region",sorted(sub.region.unique()))
        sub=sub[sub.region==region]
        with b: year=st.selectbox("Marketing year",sorted(sub.marketing_year.unique(),reverse=True))
        sub=sub[sub.marketing_year==year]
        with c: metric=st.selectbox("Metric",sorted(sub.metric.unique()))
        sub=sub[sub.metric==metric]
        unit=st.selectbox("Unit",sorted(sub.unit.unique()))
        sub=sub[sub.unit==unit].sort_values("report_date")
        st.plotly_chart(px.line(sub,x="report_date",y="value",markers=True,title=f"{crop} · {metric} · {year} ({unit})"),use_container_width=True)
        if not sub.empty:
            last=sub.iloc[-1]
            previous=sub.iloc[-2] if len(sub)>1 else None
            x,y,z=st.columns(3)
            x.metric("Latest",f"{last.value:,.2f}")
            y.metric("Previous release",f"{previous.value:,.2f}" if previous is not None else "—")
            z.metric("Change",f"{last.value-previous.value:+,.2f}" if previous is not None else "—")
        st.dataframe(sub,hide_index=True,use_container_width=True)
else:
    chosen=st.multiselect("Commodities",CROPS,default=CROPS)
    filtered=data[data.crop.isin(chosen)]
    st.dataframe(filtered,hide_index=True,use_container_width=True)
    st.download_button("Download CSV",filtered.to_csv(index=False),"usda_history.csv","text/csv")
    buf=io.BytesIO()
    with pd.ExcelWriter(buf,engine="openpyxl") as writer:
        filtered.to_excel(writer,index=False,sheet_name="USDA")
    st.download_button("Download Excel",buf.getvalue(),"usda_history.xlsx")
