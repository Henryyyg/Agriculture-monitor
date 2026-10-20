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

page=st.sidebar.radio("Navigation",["Overview","WASDE Explorer","Historical Data","Data Connections"])
data=load()
if page=="Data Connections":
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
    st.info("Quarterly Grain Stocks API and release-day ingestion are planned for the next version.")
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
