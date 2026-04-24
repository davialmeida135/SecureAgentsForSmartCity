from __future__ import annotations

import json
import os
from typing import Any, Dict, List

import pandas as pd
import streamlit as st

LOG_FILE = os.getenv("JSON_LOG_FILE", "logs/traces.jsonl")


@st.cache_data(ttl=2)
def _load_logs(path: str) -> pd.DataFrame:
    rows: List[Dict[str, Any]] = []
    if not os.path.exists(path):
        return pd.DataFrame()

    with open(path, "r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return pd.DataFrame(rows)


st.set_page_config(page_title="Smart City MAPE-K Trace Dashboard", layout="wide")
st.title("Smart City MAPE-K Trace Dashboard")

frame = _load_logs(LOG_FILE)
if frame.empty:
    st.warning(f"No logs found at: {LOG_FILE}")
    st.stop()

trace_options = sorted(
    frame.get("traceId", pd.Series(dtype=str)).dropna().unique().tolist()
)
selected_trace = st.selectbox("Trace ID", options=trace_options)
subset = frame[frame.get("traceId") == selected_trace].copy()

st.subheader("Trace Timeline")
st.dataframe(
    subset[
        [
            c
            for c in [
                "timestamp",
                "component",
                "level",
                "message",
                "risk_level",
                "approval_mode",
                "reason",
            ]
            if c in subset.columns
        ]
    ],
    use_container_width=True,
)

st.subheader("Stage Summary")
components = subset.get("component", pd.Series(dtype=str)).value_counts().reset_index()
components.columns = ["component", "events"]
st.table(components)

st.subheader("Raw JSON Records")
st.json(subset.to_dict(orient="records"))
