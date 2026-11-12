"""Streamlit admin dashboard: pipeline, AI rules configurator, webhook simulator.

Run from the repository root:
    streamlit run dashboard/app.py
"""
from __future__ import annotations

import hmac
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402
import pandas as pd  # noqa: E402
import streamlit as st  # noqa: E402

from shared.config import get_config  # noqa: E402
from shared.schemas import DEFAULT_SETTINGS, INTENTS, LEAD_STATUSES  # noqa: E402

cfg = get_config()
st.set_page_config(page_title="Speed-to-Lead Engine", page_icon="⚡", layout="wide")


# --------------------------------------------------------------------------------------
# Auth
# --------------------------------------------------------------------------------------
def require_login() -> None:
    password = cfg.dashboard_password
    if not password:
        if cfg.is_production:
            st.error("DASHBOARD_PASSWORD is not set. Add it in your Railway variables to unlock the dashboard.")
            st.stop()
        st.sidebar.warning("Dev mode: no DASHBOARD_PASSWORD set.")
        return
    if st.session_state.get("auth_ok"):
        if st.sidebar.button("Sign out"):
            st.session_state.auth_ok = False
            st.rerun()
        return
    st.title("⚡ Speed-to-Lead Engine")
    with st.form("login"):
        entered = st.text_input("Dashboard password", type="password")
        submitted = st.form_submit_button("Sign in")
    if submitted:
        if hmac.compare_digest(entered.encode(), password.encode()):
            st.session_state.auth_ok = True
            st.rerun()
        st.error("Incorrect password.")
    st.stop()


require_login()


# --------------------------------------------------------------------------------------
# Data access
# --------------------------------------------------------------------------------------
@st.cache_resource
def get_db():
    if not (cfg.supabase_url and cfg.supabase_service_key):
        return None
    from supabase import create_client

    return create_client(cfg.supabase_url, cfg.supabase_service_key)


db = get_db()
if db is None:
    st.title("⚡ Speed-to-Lead Engine")
    st.error("Supabase is not configured. Set SUPABASE_URL and SUPABASE_SERVICE_KEY, then reload.")
    st.stop()


@st.cache_data(ttl=10)
def load_leads() -> pd.DataFrame:
    rows = db.table("leads").select("*").order("created_at", desc=True).limit(1000).execute().data
    return pd.DataFrame(rows)


def load_settings() -> dict:
    merged = dict(DEFAULT_SETTINGS)
    try:
        res = db.table("agency_settings").select("*").eq("id", 1).limit(1).execute()
        if res.data:
            merged.update({k: v for k, v in res.data[0].items() if v is not None})
    except Exception as exc:  # noqa: BLE001
        st.warning(f"Could not load settings: {exc}")
    return merged


def fmt_seconds(value: float | None) -> str:
    if value is None or pd.isna(value):
        return "n/a"
    return f"{value:.0f}s" if value < 120 else f"{value / 60:.1f} min"


settings = load_settings()
st.title("⚡ Speed-to-Lead Engine")
st.caption(f"Client workspace: **{settings['agency_name']}**")

tab_pipeline, tab_brain, tab_sim = st.tabs(["📊 Live Lead Pipeline", "🧠 AI Brain & Rules", "🧪 Webhook Simulator"])

# --------------------------------------------------------------------------------------
# Tab 1: pipeline
# --------------------------------------------------------------------------------------
with tab_pipeline:
    top_l, top_r = st.columns([6, 1])
    with top_r:
        if st.button("🔄 Refresh", use_container_width=True):
            load_leads.clear()
            st.rerun()

    try:
        df = load_leads()
    except Exception as exc:  # noqa: BLE001
        st.error(f"Could not load leads: {exc}")
        st.stop()

    if df.empty:
        st.info("No leads yet. Use the Webhook Simulator tab to send your first test lead.")
    else:
        for col in ("lead_score", "response_seconds", "budget_max"):
            if col in df:
                df[col] = pd.to_numeric(df[col], errors="coerce")
        threshold = int(settings.get("hot_lead_threshold") or 8)

        k1, k2, k3, k4 = st.columns(4)
        k1.metric("Total leads", len(df))
        k2.metric("Average score", f"{df['lead_score'].mean():.1f}" if df["lead_score"].notna().any() else "n/a")
        k3.metric(f"Hot leads (score ≥ {threshold})", int((df["lead_score"] >= threshold).sum()))
        k4.metric("Avg response time", fmt_seconds(df["response_seconds"].mean()))

        f1, f2, f3 = st.columns(3)
        score_range = f1.slider("Lead score", 1, 10, (1, 10))
        intents = f2.multiselect("Intent", INTENTS, default=INTENTS)
        statuses = f3.multiselect("Status", LEAD_STATUSES, default=LEAD_STATUSES)

        mask = df["status"].isin(statuses)
        mask &= df["intent"].isin(intents) | df["intent"].isna()
        mask &= df["lead_score"].between(*score_range) | df["lead_score"].isna()
        filtered = df[mask].copy()

        try:
            created = pd.to_datetime(filtered["created_at"], utc=True).dt.tz_convert(cfg.display_timezone)
        except Exception:  # noqa: BLE001
            created = pd.to_datetime(filtered["created_at"], utc=True)
        filtered["received"] = created.dt.strftime("%d %b %H:%M")

        view = filtered[
            ["id", "received", "full_name", "intent", "lead_score", "status", "budget_estimate",
             "preferred_location", "email_status", "response_seconds", "phone", "email", "property_interest"]
        ].reset_index(drop=True)

        st.caption("Change a lead's **Status** directly in the table, then click *Save status changes*.")
        edited = st.data_editor(
            view,
            hide_index=True,
            use_container_width=True,
            key="lead_editor",
            disabled=[c for c in view.columns if c != "status"],
            column_config={
                "id": None,
                "received": "Received",
                "full_name": "Name",
                "intent": "Intent",
                "lead_score": st.column_config.ProgressColumn("Score", min_value=0, max_value=10, format="%d"),
                "status": st.column_config.SelectboxColumn("Status", options=LEAD_STATUSES, required=True),
                "budget_estimate": "Budget",
                "preferred_location": "Location",
                "email_status": "Email",
                "response_seconds": st.column_config.NumberColumn("Response (s)", format="%.0f"),
                "phone": "Phone",
                "email": "Email address",
                "property_interest": "Interest",
            },
        )
        if st.button("💾 Save status changes", type="primary"):
            changed = edited[edited["status"] != view["status"]]
            for _, row in changed.iterrows():
                db.table("leads").update({"status": row["status"]}).eq("id", row["id"]).execute()
            load_leads.clear()
            st.success(f"Updated {len(changed)} lead(s).") if len(changed) else st.info("No changes to save.")
            if len(changed):
                st.rerun()

        with st.expander("🔍 Lead details (AI summary, reply sent, original message)"):
            if filtered.empty:
                st.write("No leads match the filters.")
            else:
                options = {f"{r.full_name} · {r.received}": r.id for r in filtered.itertuples()}
                chosen = st.selectbox("Choose a lead", list(options))
                lead = filtered[filtered["id"] == options[chosen]].iloc[0]
                c1, c2 = st.columns(2)
                with c1:
                    st.markdown(f"**Score:** {lead.get('lead_score')} / 10  \n**Why:** {lead.get('score_reason') or 'n/a'}")
                    summary = lead.get("summary")
                    if isinstance(summary, list):
                        st.markdown("**Agent summary**\n" + "\n".join(f"- {s}" for s in summary))
                    st.markdown("**Original message**")
                    st.write(lead.get("inquiry_message") or "—")
                with c2:
                    st.markdown(f"**Reply sent:** {lead.get('email_subject') or 'n/a'}")
                    st.text(lead.get("email_body") or "")
                    st.caption(
                        f"AI mode: {lead.get('ai_mode') or 'n/a'} · Processing: {lead.get('processing_status')} · "
                        f"Alert: {lead.get('alert_channels') or 'none'}"
                    )
                    if lead.get("error_log"):
                        st.warning(lead["error_log"])

# --------------------------------------------------------------------------------------
# Tab 2: AI brain
# --------------------------------------------------------------------------------------
with tab_brain:
    st.subheader("AI Brain & Rules Configurator")
    st.caption("Changes apply to the very next incoming lead. No code needed.")
    with st.form("settings_form"):
        c1, c2 = st.columns(2)
        agency_name = c1.text_input("Agency name", settings["agency_name"])
        agent_name = c2.text_input("Name emails are written as", settings["agent_name"])
        c3, c4, c5 = st.columns(3)
        agent_email = c3.text_input("Reply-to email (agent)", settings["agent_email"])
        agency_phone = c4.text_input("Agency phone", settings["agency_phone"])
        agency_website = c5.text_input("Website", settings["agency_website"])
        c6, c7, c8, c9 = st.columns(4)
        brand_color = c6.color_picker("Brand colour", settings["brand_color"])
        currency = c7.text_input("Currency", settings["currency"], max_chars=5)
        min_budget = c8.number_input("Minimum budget", min_value=0, value=int(settings["min_budget"]), step=10000)
        threshold_new = c9.slider("Hot-lead score threshold", 1, 10, int(settings["hot_lead_threshold"]))
        target_locations = st.text_area(
            "Target locations (comma separated)", settings["target_locations"],
            help="Leads asking about these areas score higher.",
        )
        email_signoff = st.text_area("Email sign-off", settings["email_signoff"], height=90)
        custom_instructions = st.text_area(
            "Custom AI instructions", settings["custom_instructions"], height=120,
            help="Tone, things to always mention, things to never say.",
        )
        save = st.form_submit_button("Save settings", type="primary")

    if save:
        new_values = {
            "id": 1,
            "agency_name": agency_name.strip() or DEFAULT_SETTINGS["agency_name"],
            "agent_name": agent_name.strip(),
            "agent_email": agent_email.strip(),
            "agency_phone": agency_phone.strip(),
            "agency_website": agency_website.strip(),
            "brand_color": brand_color,
            "currency": currency.strip().upper() or "USD",
            "min_budget": int(min_budget),
            "hot_lead_threshold": int(threshold_new),
            "target_locations": target_locations.strip(),
            "email_signoff": email_signoff,
            "custom_instructions": custom_instructions,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        try:
            db.table("agency_settings").upsert(new_values, on_conflict="id").execute()
            st.success("Settings saved. The AI will use them for the next lead.")
        except Exception as exc:  # noqa: BLE001
            st.error(f"Could not save settings: {exc}")

# --------------------------------------------------------------------------------------
# Tab 3: simulator
# --------------------------------------------------------------------------------------
PRESETS = {
    "🔥 Hot buyer": {
        "full_name": "Priya Sharma", "phone": "+1 555 010 2030", "property_interest": "3-bed apartment in Downtown",
        "inquiry_message": "We are pre-approved up to $650k and want to buy a 3 bedroom apartment in Downtown this month. Can we view something this week?",
    },
    "🏠 Seller": {
        "full_name": "Daniel Okafor", "phone": "+1 555 010 4455", "property_interest": "Sell my house in Riverside",
        "inquiry_message": "I'd like a valuation for my 4 bedroom house in Riverside. Planning to list in the next two months.",
    },
    "🔑 Cold renter": {
        "full_name": "Sam Lee", "phone": "", "property_interest": "Studio rental",
        "inquiry_message": "Looking at rents sometime later this year, maybe $900 per month.",
    },
    "💼 Investor": {
        "full_name": "Maria Gonzalez", "phone": "+1 555 010 7788", "property_interest": "Rental portfolio in Westfield",
        "inquiry_message": "I invest in rental properties and have about $1.2M to deploy across 3-4 units in Westfield. Interested in yields above 6%.",
    },
}

with tab_sim:
    st.subheader("Live Webhook Simulator")
    st.caption(f"Sends a real POST request to `{cfg.backend_url}/webhook/new-lead`, exactly like a website form would.")

    hc1, hc2 = st.columns([1, 3])
    if hc1.button("Check backend health"):
        try:
            health = httpx.get(f"{cfg.backend_url}/health", timeout=10)
            hc2.json(health.json())
        except Exception as exc:  # noqa: BLE001
            hc2.error(f"Backend unreachable: {exc}")

    preset_name = st.selectbox("Demo scenario", list(PRESETS))
    p = PRESETS[preset_name]
    with st.form("sim_form"):
        s1, s2 = st.columns(2)
        full_name = s1.text_input("Full name", p["full_name"], key=f"n_{preset_name}")
        email = s2.text_input("Email (replies are sent here, use your own for demos)", "", key=f"e_{preset_name}")
        s3, s4 = st.columns(2)
        phone = s3.text_input("Phone", p["phone"], key=f"p_{preset_name}")
        interest = s4.text_input("Property interest", p["property_interest"], key=f"i_{preset_name}")
        message = st.text_area("Inquiry message", p["inquiry_message"], height=110, key=f"m_{preset_name}")
        send = st.form_submit_button("🚀 Send test lead", type="primary")

    if send:
        if not email.strip():
            st.error("Please enter an email address.")
        else:
            body = {
                "full_name": full_name, "email": email.strip(), "phone": phone,
                "property_interest": interest, "inquiry_message": message, "source": "dashboard-simulator",
            }
            try:
                resp = httpx.post(
                    f"{cfg.backend_url}/webhook/new-lead",
                    json=body,
                    headers={"X-API-Key": get_config().webhook_api_key},
                    timeout=30,
                )
                if resp.status_code == 202:
                    st.success("Lead accepted. Give it a few seconds, then hit Refresh on the pipeline tab.")
                    load_leads.clear()
                else:
                    st.error(f"Backend replied {resp.status_code}")
                st.json(resp.json())
            except Exception as exc:  # noqa: BLE001
                st.error(f"Could not reach the backend at {cfg.backend_url}: {exc}")

    with st.expander("Connect a real website form"):
        st.code(
            f"""curl -X POST {cfg.backend_url}/webhook/new-lead \\
  -H "Content-Type: application/json" \\
  -H "X-API-Key: <your WEBHOOK_API_KEY>" \\
  -d '{{"full_name":"Jane Doe","email":"jane@example.com","phone":"+15550100",
       "property_interest":"2-bed condo","inquiry_message":"Looking to buy soon."}}'""",
            language="bash",
        )
