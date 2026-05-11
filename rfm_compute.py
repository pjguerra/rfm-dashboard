"""
Cómputo RFM por segmento — Enacero Inox
Ventanas data-driven:
  B2B_RECURRENTES : 180 días
  RICZA           : 720 días (24 meses)
  INMEPRO         : 720 días (24 meses)
  TALLERES        : 548 días (18 meses)

R: umbral absoluto anclado al p75 del ciclo inter-compra por segmento
   (= "compró dentro del tiempo que cubre el 75% de las recompras normales")
F, M: top-20% percentil dentro del segmento
"""

import sys, os, warnings
warnings.filterwarnings("ignore")
sys.path.insert(0, os.path.dirname(__file__))

import numpy as np
import pandas as pd
import xmlrpc.client
from datetime import datetime
from google.cloud import bigquery
from google.oauth2 import service_account

pd.set_option("display.float_format", "{:,.1f}".format)

from config import ODOO_URL, ODOO_DB, ODOO_USER, ODOO_PASSWORD

BQ_PROJECT = "inventory-forecast-2024"
BQ_DATASET = "enacero_rfm"
BQ_KEY     = os.path.join(os.path.dirname(__file__), "inventory-forecast-2024-29821d1322f8.json")

def ensure_tables(bq):
    ds_ref = f"{BQ_PROJECT}.{BQ_DATASET}"
    try:
        bq.get_dataset(ds_ref)
    except Exception:
        ds = bigquery.Dataset(ds_ref)
        ds.location = "US"
        bq.create_dataset(ds)
        print(f"  ✓ Dataset {BQ_DATASET} creado")

    snap_id = f"{ds_ref}.rfm_snapshots"
    try:
        bq.get_table(snap_id)
    except Exception:
        bq.create_table(bigquery.Table(snap_id, schema=[
            bigquery.SchemaField("snapshot_date",    "DATE"),
            bigquery.SchemaField("partner_id",        "INT64"),
            bigquery.SchemaField("partner_name",      "STRING"),
            bigquery.SchemaField("am_name",           "STRING"),
            bigquery.SchemaField("is_active_am",      "BOOL"),
            bigquery.SchemaField("segment",           "STRING"),
            bigquery.SchemaField("window_days",       "INT64"),
            bigquery.SchemaField("active_in_window",  "BOOL"),
            bigquery.SchemaField("R_days",            "INT64"),
            bigquery.SchemaField("F",                 "INT64"),
            bigquery.SchemaField("M_gtq",             "FLOAT64"),
            bigquery.SchemaField("R_score",           "INT64"),
            bigquery.SchemaField("F_score",           "INT64"),
            bigquery.SchemaField("M_score",           "INT64"),
            bigquery.SchemaField("tier",              "STRING"),
        ]))
        print(f"  ✓ Tabla rfm_snapshots creada")

    orders_id = f"{ds_ref}.orders"
    try:
        bq.get_table(orders_id)
    except Exception:
        bq.create_table(bigquery.Table(orders_id, schema=[
            bigquery.SchemaField("order_id",   "INT64"),
            bigquery.SchemaField("partner_id", "INT64"),
            bigquery.SchemaField("order_date", "TIMESTAMP"),
            bigquery.SchemaField("amount_gtq", "FLOAT64"),
            bigquery.SchemaField("segment",    "STRING"),
        ]))
        print(f"  ✓ Tabla orders creada")

    seg_id = f"{ds_ref}.client_segments"
    try:
        bq.get_table(seg_id)
    except Exception:
        bq.create_table(bigquery.Table(seg_id, schema=[
            bigquery.SchemaField("partner_id",   "INT64"),
            bigquery.SchemaField("partner_name", "STRING"),
            bigquery.SchemaField("segment",      "STRING"),
        ]))
        print(f"  ✓ Tabla client_segments creada")

    thresh_id = f"{ds_ref}.am_m_thresholds"
    try:
        bq.get_table(thresh_id)
    except Exception:
        bq.create_table(bigquery.Table(thresh_id, schema=[
            bigquery.SchemaField("snapshot_date",      "DATE"),
            bigquery.SchemaField("am_name",            "STRING"),
            bigquery.SchemaField("segment",            "STRING"),
            bigquery.SchemaField("m_threshold_gtq",    "FLOAT64"),
            bigquery.SchemaField("n_active_clients",   "INT64"),
            bigquery.SchemaField("n_above_threshold",  "INT64"),
        ]))
        print(f"  ✓ Tabla am_m_thresholds creada")

COMPANY_IDS = [1, 5]
REF_DATE    = datetime.today().strftime("%Y-%m-%d")
BATCH       = 2000

# Ventanas por segmento (días hacia atrás desde REF_DATE)
WINDOWS = {
    "B2B_RECURRENTES": 180,
    "RICZA":           720,
    "INMEPRO":         720,
    "TALLERES":        548,
    "AYCO":            720,
}

R_THRESH = {
    "B2B_RECURRENTES": 31,   # p90 ciclo = 31d
    "RICZA":           181,  # p90 ciclo = 181d
    "INMEPRO":         171,  # p90 ciclo = 171d
    "TALLERES":        167,  # p90 ciclo = 167d
    "AYCO":            106,  # p90 ciclo = 106d
}

F_TH = {
    "B2B_RECURRENTES": 0.60,
    "RICZA":           0.60,
    "INMEPRO":         0.60,
    "TALLERES":        0.60,
    "AYCO":            0.60,
}

SEG_ORDER = ["B2B_RECURRENTES","RICZA","INMEPRO","TALLERES","AYCO"]

print(f"\n{'='*65}")
print(f"  CÓMPUTO RFM POR SEGMENTO — {REF_DATE}")
print(f"{'='*65}\n")

# ── cliente BigQuery ──────────────────────────────────────────────────────────
creds = service_account.Credentials.from_service_account_file(BQ_KEY)
bq    = bigquery.Client(project=BQ_PROJECT, credentials=creds)
ensure_tables(bq)

# ── conexión Odoo ─────────────────────────────────────────────────────────────
common = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/common")
uid    = common.authenticate(ODOO_DB, ODOO_USER, ODOO_PASSWORD, {})
if not uid:
    raise RuntimeError("Autenticación fallida")
models = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/object")
print(f"  UID={uid}  OK\n")

def sr(model, domain, fields, order="id", company_ids=None):
    ctx = {"allowed_company_ids": company_ids or COMPANY_IDS}
    rows, offset = [], 0
    while True:
        chunk = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, model,
                                  "search_read", [domain],
                                  {"fields": fields, "limit": BATCH,
                                   "offset": offset, "order": order,
                                   "context": ctx})
        if not chunk: break
        rows.extend(chunk)
        if len(chunk) < BATCH: break
        offset += BATCH
    return rows

def mid(v):
    return v[0] if isinstance(v, (list, tuple)) and v else (v if isinstance(v, int) else None)

def mname(v):
    return v[1] if isinstance(v, (list, tuple)) and len(v) >= 2 else None

# ── cargar segmentos ──────────────────────────────────────────────────────────
segs = bq.query(
    f"SELECT partner_id, partner_name, segment FROM `{BQ_PROJECT}.{BQ_DATASET}.client_segments`"
).to_dataframe()
segs["partner_id"] = segs["partner_id"].astype(int)
seg_map  = dict(zip(segs["partner_id"], segs["segment"]))
name_map = dict(zip(segs["partner_id"], segs["partner_name"]))
print(f"► Segmentos cargados: {len(segs):,} clientes")

# AMs con contrato activo en empresas 1, 5 o 7
print("► Obteniendo AMs con contrato activo...")
contracts   = sr("hr.contract", [("state","=","open"),("company_id","in",[1,5,7])],
                 ["employee_id"], company_ids=[1,5,7])
emp_ids_act = list({mid(c["employee_id"]) for c in contracts if c.get("employee_id")})
raw_emp_act = sr("hr.employee", [("id","in",emp_ids_act)], ["id","user_id"],
                 company_ids=[1,5,7])
active_am_names = {mname(e["user_id"]) for e in raw_emp_act if e.get("user_id")}
active_am_names.discard(None)
print(f"  AMs activos: {len(active_am_names)}\n")

# AM: user_id en res.partner (se completa con fallback de órdenes más abajo)
all_pids = segs["partner_id"].tolist()
raw_am   = sr("res.partner", [("id","in", all_pids)], ["id","user_id"])
am_map   = {int(p["id"]): mname(p.get("user_id")) for p in raw_am}  # None si vacío

# ── extraer órdenes (ventana máxima = 720 días) ───────────────────────────────
MAX_WINDOW = max(WINDOWS.values())
ref    = pd.to_datetime(REF_DATE)
d_from = (ref - pd.Timedelta(days=MAX_WINDOW)).strftime("%Y-%m-%d 00:00:00")
d_to   = ref.strftime("%Y-%m-%d 23:59:59")

print(f"► Extrayendo órdenes (ventana máxima {MAX_WINDOW} días)...")
raw_so = sr("sale.order",
            [("state","in",["sale","done"]),("date_order",">=",d_from),
             ("date_order","<=",d_to),("company_id","in",COMPANY_IDS)],
            ["id","date_order","partner_id","currency_id","amount_untaxed","user_id"])
print(f"  Órdenes brutas: {len(raw_so):,}")

# resolver commercial_partner_id
pids_raw = sorted({mid(o["partner_id"]) for o in raw_so if o.get("partner_id")})
raw_p    = sr("res.partner",[("id","in",pids_raw)],["id","commercial_partner_id"])
pmap     = {int(p["id"]): (mid(p.get("commercial_partner_id")) or int(p["id"])) for p in raw_p}

# fallback AM: vendedor de la última orden por partner
print("  Resolviendo AM por última orden (fallback)...")
seen_am = set()
for o in sorted(raw_so, key=lambda x: x["date_order"], reverse=True):
    raw_pid = mid(o.get("partner_id"))
    if not raw_pid: continue
    parent_pid = pmap.get(raw_pid, raw_pid)
    if parent_pid in seen_am: continue
    seen_am.add(parent_pid)
    if not am_map.get(parent_pid):
        am_map[parent_pid] = mname(o.get("user_id"))
for pid in all_pids:
    if not am_map.get(pid):
        am_map[pid] = "Sin asignar"
assigned = sum(1 for v in am_map.values() if v != "Sin asignar")
print(f"  AMs distintos: {len({v for v in am_map.values() if v != 'Sin asignar'})}  |  "
      f"Clientes con AM: {assigned:,} / {len(all_pids):,}")

# FX → GTQ (tasa más reciente por moneda)
gtq_rec  = sr("res.currency",[("name","=","GTQ")],["id"])
GTQ_ID   = int(gtq_rec[0]["id"]) if gtq_rec else None
rates_raw = sr("res.currency.rate",
               [("name","<=",d_to),("company_id","in",COMPANY_IDS)],
               ["currency_id","name","rate","company_id"], order="name asc")
rates_df = pd.DataFrame([{
    "currency_id": int(mid(r["currency_id"])),
    "rate": float(r["rate"] or 1.0),
    "date": pd.to_datetime(r["name"]),
    "company_id": mid(r["company_id"]) or 0,
} for r in rates_raw if mid(r["currency_id"])])
latest_rate = (rates_df[rates_df["company_id"].isin(COMPANY_IDS)]
               .sort_values("date").drop_duplicates("currency_id", keep="last")
               .set_index("currency_id")["rate"])
gtq_rate = latest_rate.get(GTQ_ID, 1.0)

def to_gtq(currency_id, amount):
    r_from = float(latest_rate.get(currency_id, 1.0))
    if r_from == 0: r_from = 1.0
    return float(amount) * gtq_rate / r_from

orders_raw = pd.DataFrame([{
    "order_id":    int(o["id"]),
    "order_date":  pd.to_datetime(o["date_order"]),
    "partner_id":  pmap.get(mid(o["partner_id"]), mid(o["partner_id"])),
    "currency_id": mid(o["currency_id"]),
    "amount_gtq":  to_gtq(mid(o["currency_id"]), o.get("amount_untaxed", 0)),
} for o in raw_so if o.get("partner_id")])

orders_raw["segment"] = orders_raw["partner_id"].map(seg_map)
orders_raw = orders_raw.dropna(subset=["segment"])
print(f"  Órdenes con segmento: {len(orders_raw):,}")


# ── cómputo RFM por segmento ──────────────────────────────────────────────────
def quintile_score(series, ascending=True):
    """Convierte serie a quintil 1-5. ascending=True → mayor valor = mayor score."""
    r = series.rank(pct=True, method="average")
    if not ascending:
        r = 1 - r
    bins   = [0, 0.20, 0.40, 0.60, 0.80, 1.01]
    labels = [1, 2, 3, 4, 5]
    return pd.cut(r, bins=bins, labels=labels, include_lowest=True).astype(int)

def assign_tier(row, f_th):
    # R: umbral absoluto precalculado en columna high_R
    # M: umbral absoluto por AM precalculado en columna high_M
    r = bool(row["high_R"])
    f = row["F_pct"] >= f_th
    m = bool(row["high_M"])
    if r and f and m: return "RFM"
    if r and f:       return "RF"
    if r and m:       return "RM"
    if f and m:       return "FM"
    if r:             return "R"
    if f:             return "F"
    if m:             return "M"
    return "otros"

all_rfm = []
threshold_records = []

for seg in SEG_ORDER:
    window = WINDOWS[seg]
    cutoff = ref - pd.Timedelta(days=window)

    # clientes de este segmento
    seg_clients = segs[segs["segment"] == seg]["partner_id"].tolist()

    # órdenes dentro de la ventana
    seg_orders = orders_raw[
        (orders_raw["segment"] == seg) &
        (orders_raw["order_date"] >= cutoff)
    ].copy()

    # agregar por cliente: R, F, M
    agg = seg_orders.groupby("partner_id").agg(
        last_order  = ("order_date", "max"),
        F           = ("order_id", "nunique"),
        M_gtq       = ("amount_gtq", "sum"),
    ).reset_index()
    agg["R_days"] = (ref - agg["last_order"]).dt.days

    # clientes sin actividad en la ventana
    active_ids = set(agg["partner_id"].tolist())
    inactive   = [pid for pid in seg_clients if pid not in active_ids]
    if inactive:
        inactive_df = pd.DataFrame({
            "partner_id": inactive,
            "last_order": pd.NaT,
            "F": 0,
            "M_gtq": 0.0,
            "R_days": window,   # máxima recency = tamaño de ventana
        })
        agg = pd.concat([agg, inactive_df], ignore_index=True)

    agg["active_in_window"] = agg["F"] > 0
    n_active   = agg["active_in_window"].sum()
    n_inactive = (~agg["active_in_window"]).sum()

    # percentiles (solo activos para el rank, inactivos quedan al fondo)
    active_mask = agg["active_in_window"]
    agg["R_pct"] = 0.0
    agg["F_pct"] = 0.0
    agg["M_pct"] = 0.0

    if active_mask.sum() > 1:
        # R: menor R_days = más reciente = mejor → invertir rank
        agg.loc[active_mask, "R_pct"] = (
            agg.loc[active_mask, "R_days"]
            .rank(pct=True, method="average", ascending=True)  # días: menor es mejor
            .apply(lambda x: 1 - x)
        )
        agg.loc[active_mask, "F_pct"] = agg.loc[active_mask, "F"].rank(pct=True, method="average")
        # M_pct: ranking informativo dentro del segmento (solo para M_score)
        agg.loc[active_mask, "M_pct"] = agg.loc[active_mask, "M_gtq"].rank(pct=True, method="average")

    # am_name necesario antes de calcular el umbral M por AM
    agg["am_name"] = agg["partner_id"].map(am_map).fillna("Sin asignar")

    # M umbral fijo por AM: p60 de M_gtq de clientes activos de ese AM
    # (= el mínimo que separa el top 40% de la cartera de cada vendedor)
    am_m_thresh = (
        agg[active_mask]
        .groupby("am_name")["M_gtq"]
        .quantile(0.60)
        .rename("m_threshold")
        .reset_index()
    )
    agg = agg.merge(am_m_thresh, on="am_name", how="left")
    agg["high_M"] = agg["active_in_window"] & (agg["M_gtq"] >= agg["m_threshold"].fillna(float("inf")))

    # registrar umbrales para BigQuery
    for _, row in am_m_thresh.iterrows():
        am = row["am_name"]
        thresh = float(row["m_threshold"])
        n_act  = int(((agg["am_name"] == am) & agg["active_in_window"]).sum())
        n_above = int(((agg["am_name"] == am) & agg["high_M"]).sum())
        threshold_records.append({
            "am_name":           am,
            "segment":           seg,
            "m_threshold_gtq":   thresh,
            "n_active_clients":  n_act,
            "n_above_threshold": n_above,
        })

    # quintiles 1-5
    agg["R_score"] = pd.cut(agg["R_pct"], bins=[0,0.20,0.40,0.60,0.80,1.01],
                             labels=[1,2,3,4,5], include_lowest=True).astype("Int64")
    agg["F_score"] = pd.cut(agg["F_pct"], bins=[0,0.20,0.40,0.60,0.80,1.01],
                             labels=[1,2,3,4,5], include_lowest=True).astype("Int64")
    agg["M_score"] = pd.cut(agg["M_pct"], bins=[0,0.20,0.40,0.60,0.80,1.01],
                             labels=[1,2,3,4,5], include_lowest=True).astype("Int64")

    # R absoluto: "reciente" = compró dentro del p75 del ciclo inter-compra del segmento
    r_thresh = R_THRESH[seg]
    agg["high_R"] = agg["active_in_window"] & (agg["R_days"] <= r_thresh)

    # tier
    agg["tier"] = agg.apply(lambda r: assign_tier(r, F_TH[seg])
                             if r["active_in_window"] else "inactivo", axis=1)

    agg["segment"]      = seg
    agg["window_days"]  = window
    agg["partner_name"] = agg["partner_id"].map(name_map)

    all_rfm.append(agg)

    # ── resumen del segmento ──────────────────────────────────────────────────
    active_df = agg[agg["active_in_window"]]
    tier_counts = agg["tier"].value_counts()

    print(f"{'═'*65}")
    print(f"  {seg}  |  ventana {window}d  |  "
          f"{n_active} activos  /  {n_inactive} inactivos en ventana")
    print(f"{'─'*65}")
    print(f"  {'':5}  {'R_days':>8}  {'F':>6}  {'M_gtq':>12}")
    for stat, fn in [("p25", lambda s: s.quantile(0.25)),
                     ("med", lambda s: s.median()),
                     ("p75", lambda s: s.quantile(0.75)),
                     ("p90", lambda s: s.quantile(0.90))]:
        r = fn(active_df["R_days"])
        f = fn(active_df["F"])
        m = fn(active_df["M_gtq"])
        print(f"  {stat:>5}  {r:>8.0f}  {f:>6.1f}  Q{m:>11,.0f}")

    pct_high_r = agg["high_R"].sum() / len(agg) * 100
    pct_high_m = agg["high_M"].sum() / len(agg) * 100
    print(f"\n  Tiers  (R ≤ {r_thresh}d → {agg['high_R'].sum()} recientes = {pct_high_r:.0f}%"
          f"  |  F top-{(1-F_TH[seg])*100:.0f}%"
          f"  |  M umbral por AM → {agg['high_M'].sum()} = {pct_high_m:.0f}%):")
    tier_order = ["RFM","RF","RM","FM","R","F","M","otros","inactivo"]
    for t in tier_order:
        n = tier_counts.get(t, 0)
        if n:
            pct = n / len(agg) * 100
            bar = "█" * int(pct / 2)
            print(f"    {t:>8}  {bar:<20} {pct:>5.1f}%  ({n:,})")
    print()

# ── armar DataFrame final ─────────────────────────────────────────────────────
rfm_df = pd.concat(all_rfm, ignore_index=True)
rfm_df["is_active_am"] = rfm_df["am_name"].isin(active_am_names)
cols_out = ["partner_id","partner_name","am_name","is_active_am","segment","window_days",
            "active_in_window","R_days","F","M_gtq",
            "R_score","F_score","M_score","tier"]

print(f"{'='*65}")
print(f"  RESUMEN GLOBAL")
print(f"{'='*65}")
print(f"  Clientes totales:   {len(rfm_df):,}")
print(f"  Activos en ventana: {rfm_df['active_in_window'].sum():,}")
print(f"  Inactivos:          {(~rfm_df['active_in_window']).sum():,}")
print(f"\n  Distribución de tiers:")
for t, n in rfm_df["tier"].value_counts().items():
    print(f"    {t:>10}  {n:>5,}  ({n/len(rfm_df)*100:.1f}%)")

# ── subir a BigQuery ──────────────────────────────────────────────────────────
print(f"\n{'='*65}")
print(f"  SUBIENDO A BIGQUERY — {BQ_PROJECT}.{BQ_DATASET}")
print(f"{'='*65}")

# rfm_snapshots: borrar snapshot de hoy si existe, luego APPEND
bq.query(f"""
    DELETE FROM `{BQ_PROJECT}.{BQ_DATASET}.rfm_snapshots`
    WHERE snapshot_date = '{REF_DATE}'
""").result()

rfm_bq = rfm_df[cols_out].copy()
rfm_bq["snapshot_date"] = pd.to_datetime(REF_DATE).date()
for col in ["R_score", "F_score", "M_score"]:
    rfm_bq[col] = rfm_bq[col].astype("float64").fillna(0).astype("int64")
rfm_bq["R_days"]           = rfm_bq["R_days"].fillna(0).astype("int64")
rfm_bq["F"]                = rfm_bq["F"].fillna(0).astype("int64")
rfm_bq["is_active_am"]     = rfm_bq["is_active_am"].astype(bool)
rfm_bq["active_in_window"] = rfm_bq["active_in_window"].astype(bool)

job = bq.load_table_from_dataframe(
    rfm_bq, f"{BQ_PROJECT}.{BQ_DATASET}.rfm_snapshots",
    job_config=bigquery.LoadJobConfig(write_disposition="WRITE_APPEND"))
job.result()
print(f"  ✓ rfm_snapshots: {len(rfm_bq):,} filas  (snapshot {REF_DATE})")

# orders: TRUNCATE + RELOAD (ventana {MAX_WINDOW}d)
orders_bq = orders_raw[["order_id","partner_id","order_date","amount_gtq","segment"]].copy()
orders_bq["order_id"]   = orders_bq["order_id"].astype("int64")
orders_bq["partner_id"] = orders_bq["partner_id"].astype("int64")
orders_bq["amount_gtq"] = orders_bq["amount_gtq"].astype("float64")

job = bq.load_table_from_dataframe(
    orders_bq, f"{BQ_PROJECT}.{BQ_DATASET}.orders",
    job_config=bigquery.LoadJobConfig(write_disposition="WRITE_TRUNCATE"))
job.result()
print(f"  ✓ orders: {len(orders_bq):,} filas  (ventana {MAX_WINDOW}d)")

# am_m_thresholds: borrar snapshot de hoy si existe, luego APPEND
thresh_df = pd.DataFrame(threshold_records)
thresh_df["snapshot_date"] = pd.to_datetime(REF_DATE).date()
thresh_df["m_threshold_gtq"]   = thresh_df["m_threshold_gtq"].astype("float64")
thresh_df["n_active_clients"]  = thresh_df["n_active_clients"].astype("int64")
thresh_df["n_above_threshold"] = thresh_df["n_above_threshold"].astype("int64")

bq.query(f"""
    DELETE FROM `{BQ_PROJECT}.{BQ_DATASET}.am_m_thresholds`
    WHERE snapshot_date = '{REF_DATE}'
""").result()

job = bq.load_table_from_dataframe(
    thresh_df, f"{BQ_PROJECT}.{BQ_DATASET}.am_m_thresholds",
    job_config=bigquery.LoadJobConfig(write_disposition="WRITE_APPEND"))
job.result()
print(f"  ✓ am_m_thresholds: {len(thresh_df):,} filas  (snapshot {REF_DATE})")

print(f"\n✓ Cómputo RFM completado.")
