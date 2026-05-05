import streamlit as st
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import os
from google.cloud import bigquery
from google.oauth2 import service_account

st.set_page_config(
    page_title="RFM — Enacero Inox",
    page_icon="📊",
    layout="wide",
)

TIER_RANK = {
    "RFM": 7, "RF": 6, "RM": 6, "FM": 6,
    "R": 4, "F": 3, "M": 3,
    "otros": 2, "inactivo": 1,
}
TIER_ORDER  = ["RFM","RF","RM","FM","R","F","M","otros","inactivo"]
TIER_COLORS = {
    "RFM":      "#1a7f3c",
    "RF":       "#2ecc71",
    "RM":       "#27ae60",
    "FM":       "#f39c12",
    "R":        "#3498db",
    "F":        "#9b59b6",
    "M":        "#e67e22",
    "otros":    "#bdc3c7",
    "inactivo": "#e74c3c",
}

BASE       = os.path.dirname(__file__)
BQ_PROJECT = "inventory-forecast-2024"
BQ_DATASET = "enacero_rfm"
BQ_KEY     = os.path.join(BASE, "inventory-forecast-2024-29821d1322f8.json")

@st.cache_resource
def get_bq():
    if "gcp_service_account" in st.secrets:
        creds = service_account.Credentials.from_service_account_info(
            dict(st.secrets["gcp_service_account"])
        )
    else:
        creds = service_account.Credentials.from_service_account_file(BQ_KEY)
    return bigquery.Client(project=BQ_PROJECT, credentials=creds)

@st.cache_data(ttl=300)
def load_data():
    bq = get_bq()
    query = f"""
    WITH latest AS (
      SELECT MAX(snapshot_date) AS d
      FROM `{BQ_PROJECT}.{BQ_DATASET}.rfm_snapshots`
    ),
    prev AS (
      SELECT MAX(snapshot_date) AS d
      FROM `{BQ_PROJECT}.{BQ_DATASET}.rfm_snapshots`
      WHERE snapshot_date < (SELECT d FROM latest)
    ),
    cur AS (
      SELECT s.*
      FROM `{BQ_PROJECT}.{BQ_DATASET}.rfm_snapshots` s
      WHERE s.snapshot_date = (SELECT d FROM latest)
    ),
    prv AS (
      SELECT partner_id, tier AS tier_prev
      FROM `{BQ_PROJECT}.{BQ_DATASET}.rfm_snapshots`
      WHERE snapshot_date = (SELECT d FROM prev)
    )
    SELECT c.*, p.tier_prev
    FROM cur c
    LEFT JOIN prv p USING (partner_id)
    """
    df = bq.query(query).to_dataframe()

    df["tier_rank"]      = df["tier"].map(TIER_RANK).fillna(0)
    df["tier_rank_prev"] = df["tier_prev"].map(TIER_RANK).fillna(0)

    def movimiento(row):
        if pd.isna(row["tier_prev"]):
            return "nuevo"
        delta = row["tier_rank"] - row["tier_rank_prev"]
        if delta > 0: return "↑ subió"
        if delta < 0: return "↓ bajó"
        return "= estable"

    df["movimiento"] = df.apply(movimiento, axis=1)
    return df

@st.cache_data(ttl=300)
def load_orders():
    bq = get_bq()
    query = f"""
    SELECT order_id, partner_id, order_date, amount_gtq, segment
    FROM `{BQ_PROJECT}.{BQ_DATASET}.orders`
    """
    df = bq.query(query).to_dataframe()
    df["order_date"] = pd.to_datetime(df["order_date"], utc=True).dt.tz_localize(None)
    return df

df     = load_data()
orders = load_orders()

# rango disponible en orders
if not orders.empty:
    min_date = orders["order_date"].min().date()
    max_date = orders["order_date"].max().date()
else:
    min_date = pd.Timestamp.today().date() - pd.Timedelta(days=720)
    max_date = pd.Timestamp.today().date()

# ── SIDEBAR ───────────────────────────────────────────────────────────────────
with st.sidebar:
    st.image("https://img.icons8.com/color/96/combo-chart.png", width=60)
    st.title("Filtros")

    active_ams = sorted(df[df["is_active_am"]]["am_name"].dropna().unique().tolist())
    ams = ["Todos"] + active_ams
    am_col, am_clear = st.columns([4, 1])
    with am_col:
        selected_am = st.selectbox("Account Manager", ams,
                                   index=ams.index(st.session_state.get("am", "Todos")))
    with am_clear:
        st.write("")
        if st.button("✕", key="clear_am", help="Limpiar"):
            st.session_state["am"] = "Todos"
            st.rerun()
    st.session_state["am"] = selected_am

    segs = ["Todos"] + sorted(df["segment"].unique().tolist())
    selected_seg = st.selectbox("Segmento", segs)

    all_tiers = [t for t in TIER_ORDER if t in df["tier"].unique()]
    selected_tiers = st.multiselect("Tier", options=all_tiers, default=all_tiers)

    st.divider()
    st.subheader("Período de análisis")
    default_start = max(min_date, (pd.Timestamp.today() - pd.Timedelta(days=365)).date())
    date_from = st.date_input("Desde", value=default_start,
                               min_value=min_date, max_value=max_date)
    date_to   = st.date_input("Hasta", value=max_date,
                               min_value=min_date, max_value=max_date)
    st.caption("Afecta Volumen, Frecuencia y Recencia. El Tier RFM es fijo.")

    st.divider()
    last_snap = str(df["snapshot_date"].max()) if "snapshot_date" in df.columns else "—"
    st.caption(f"Última actualización: {last_snap}")
    if st.button("🔄 Recargar datos"):
        st.cache_data.clear()
        st.rerun()

# ── MÉTRICAS DEL PERÍODO SELECCIONADO ────────────────────────────────────────
period_end = pd.Timestamp(date_to)
ord_period = orders[
    (orders["order_date"].dt.date >= date_from) &
    (orders["order_date"].dt.date <= date_to)
]
if not ord_period.empty:
    period_metrics = ord_period.groupby("partner_id").agg(
        M_periodo  = ("amount_gtq", "sum"),
        F_periodo  = ("order_id",   "nunique"),
        last_ord_p = ("order_date", "max"),
    ).reset_index()
    period_metrics["R_periodo"] = (period_end - period_metrics["last_ord_p"]).dt.days
    period_metrics = period_metrics.drop(columns=["last_ord_p"])
else:
    period_metrics = pd.DataFrame(columns=["partner_id","M_periodo","F_periodo","R_periodo"])

df_view = df.merge(period_metrics, on="partner_id", how="left")
df_view["M_periodo"] = df_view["M_periodo"].fillna(0)
df_view["F_periodo"] = df_view["F_periodo"].fillna(0).astype(int)
df_view["R_periodo"] = df_view["R_periodo"].fillna(
    (period_end - pd.Timestamp(min_date)).days).astype(int)

# ── FILTRAR ───────────────────────────────────────────────────────────────────
view = df_view.copy()
if selected_am != "Todos":
    view = view[view["am_name"] == selected_am]
if selected_seg != "Todos":
    view = view[view["segment"] == selected_seg]
if selected_tiers:
    view = view[view["tier"].isin(selected_tiers)]

# ── TÍTULO ────────────────────────────────────────────────────────────────────
title_parts = []
if selected_am != "Todos":   title_parts.append(selected_am)
if selected_seg != "Todos":  title_parts.append(selected_seg)
st.title("Dashboard RFM — " + (" · ".join(title_parts) if title_parts else "Todos los clientes"))

# ── KPIs ──────────────────────────────────────────────────────────────────────
k1, k2, k3, k4, k5 = st.columns(5)
n_total           = len(view)
n_rfm             = int((view["tier"] == "RFM").sum())
n_subieron        = int((view["movimiento"] == "↑ subió").sum())
vol_periodo       = view["M_periodo"].sum()
n_activos_periodo = int((view["F_periodo"] > 0).sum())

k1.metric("Clientes",              f"{n_total:,}")
k2.metric("Compraron en período",  f"{n_activos_periodo:,}",
          f"{n_activos_periodo/n_total*100:.0f}%" if n_total else "")
k3.metric("Tier RFM",              f"{n_rfm:,}",
          f"{n_rfm/n_total*100:.1f}%" if n_total else "")
k4.metric("Subieron de tier",      f"{n_subieron:,}")
k5.metric("Volumen GTQ (período)", f"Q{vol_periodo:,.0f}")

st.divider()

# ── FILA 1: distribución de tiers + movimiento ────────────────────────────────
col_a, col_b = st.columns(2)

with col_a:
    st.subheader("Distribución de tiers")
    tier_ct = (view["tier"].value_counts()
               .reindex([t for t in TIER_ORDER if t in view["tier"].unique()])
               .reset_index())
    tier_ct.columns = ["tier", "n"]
    fig_tier = px.bar(
        tier_ct, x="tier", y="n", color="tier",
        color_discrete_map=TIER_COLORS,
        text="n", labels={"n": "Clientes", "tier": ""},
    )
    fig_tier.update_traces(textposition="outside")
    fig_tier.update_layout(showlegend=False, margin=dict(t=20,b=0))
    st.plotly_chart(fig_tier, use_container_width=True)

with col_b:
    st.subheader("Movimiento vs período anterior")
    if view["tier_prev"].notna().any():
        mov_ct = view["movimiento"].value_counts().reset_index()
        mov_ct.columns = ["movimiento", "n"]
        color_map = {"↑ subió": "#1a7f3c", "= estable": "#3498db",
                     "↓ bajó": "#e74c3c", "nuevo": "#bdc3c7"}
        fig_mov = px.bar(
            mov_ct, x="movimiento", y="n", color="movimiento",
            color_discrete_map=color_map, text="n",
            labels={"n": "Clientes", "movimiento": ""},
        )
        fig_mov.update_traces(textposition="outside")
        fig_mov.update_layout(showlegend=False, margin=dict(t=20,b=0))
        st.plotly_chart(fig_mov, use_container_width=True)
    else:
        st.info("Sin datos de período anterior todavía. Aparecerá luego del próximo cálculo quincenal.")

# ── FILA 2: distribución por segmento (solo si AM seleccionado) ───────────────
if selected_am != "Todos":
    st.subheader(f"Tiers por segmento — {selected_am}")
    seg_tier = (view.groupby(["segment","tier"])
                .size().reset_index(name="n"))
    fig_seg = px.bar(
        seg_tier, x="segment", y="n", color="tier",
        color_discrete_map=TIER_COLORS,
        category_orders={"tier": TIER_ORDER},
        labels={"n": "Clientes", "segment": "", "tier": "Tier"},
        barmode="stack",
    )
    fig_seg.update_layout(margin=dict(t=20,b=0))
    st.plotly_chart(fig_seg, use_container_width=True)
    st.divider()

# ── TABLA DE CLIENTES ─────────────────────────────────────────────────────────
st.subheader("Cartera de clientes")

table_cols = ["partner_name","am_name","segment","tier","tier_prev",
              "movimiento","R_periodo","F_periodo","M_periodo"]
if selected_am != "Todos":
    table_cols = [c for c in table_cols if c != "am_name"]

display = (view[table_cols + ["tier_rank"]]
           .sort_values(["tier_rank", "M_periodo"], ascending=[False, False])
           .drop(columns=["tier_rank"])
           .rename(columns={
               "partner_name": "Cliente",
               "am_name":      "AM",
               "segment":      "Segmento",
               "tier":         "Tier",
               "tier_prev":    "Tier anterior",
               "movimiento":   "Movimiento",
               "R_periodo":    "Recencia (días)",
               "F_periodo":    "Órdenes",
               "M_periodo":    "Volumen GTQ",
           }))

display["Volumen GTQ"] = display["Volumen GTQ"].apply(lambda x: f"Q{x:,.0f}")

st.dataframe(
    display,
    use_container_width=True,
    height=500,
    column_config={
        "Tier": st.column_config.TextColumn(width="small"),
        "Movimiento": st.column_config.TextColumn(width="small"),
    },
)

st.caption(f"{len(display):,} clientes mostrados")
