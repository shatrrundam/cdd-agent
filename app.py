"""Streamlit front end for the CDD agent."""

import json
import re
import threading
import time

import streamlit as st

import datetime as dt

from cdd_agent import SECTION_ORDER, STEPS, run_cdd
from providers import CLAUDE_DEFAULT_MODEL, GEMINI_DEFAULT_MODEL
from prompts import AGENTS
from report import build_report

st.set_page_config(page_title="CDD Agent", page_icon="📊", layout="wide")


def secret(name, default=""):
    try:
        return st.secrets.get(name, default)
    except Exception:
        return default


# ---------- optional password gate (set APP_PASSWORD in secrets to protect your API credit) ----------
pw = secret("APP_PASSWORD")
if pw and not st.session_state.get("authed"):
    st.title("CDD Agent")
    entered = st.text_input("Access password", type="password")
    if entered == pw:
        st.session_state.authed = True
        st.rerun()
    elif entered:
        st.error("Wrong password")
    st.stop()

# ---------- sidebar ----------
@st.cache_resource
def run_counter():
    """Shared across all visitors of the deployed app; resets when the app restarts."""
    return {"date": "", "count": 0, "lock": threading.Lock()}


DAILY_LIMIT = int(secret("DAILY_RUN_LIMIT", 20) or 20)
PER_SESSION_LIMIT = int(secret("SESSION_RUN_LIMIT", 3) or 3)


def runs_left():
    c = run_counter()
    today = dt.date.today().isoformat()
    with c["lock"]:
        if c["date"] != today:
            c["date"], c["count"] = today, 0
        return DAILY_LIMIT - c["count"]


def claim_run():
    c = run_counter()
    with c["lock"]:
        c["count"] += 1


ENGINES = {"Gemini (free)": "gemini", "Claude (paid, higher quality)": "claude"}
# Only offer Claude when the owner has put a Claude key in secrets, so visitors never see a paid option
available = [e for e in ENGINES if ENGINES[e] == "gemini" or secret("ANTHROPIC_API_KEY")]

with st.sidebar:
    st.header("Settings")
    engine = ENGINES[st.selectbox("AI engine", available)]
    key_name = "GEMINI_API_KEY" if engine == "gemini" else "ANTHROPIC_API_KEY"
    api_key = secret(key_name) or st.text_input(
        "Gemini API key" if engine == "gemini" else "Anthropic API key", type="password",
        help="Free Gemini key from aistudio.google.com. Stored only for this browser session.")
    default_model = secret("GEMINI_MODEL", GEMINI_DEFAULT_MODEL) if engine == "gemini" \
        else secret("CLAUDE_MODEL", CLAUDE_DEFAULT_MODEL)
    search_key = None
    if engine == "gemini":
        search_key = secret("TAVILY_API_KEY") or st.text_input(
            "Tavily API key (web search)", type="password",
            help="Free at tavily.com, 1,000 searches a month. Needed if your Gemini project has no free Google Search.")
    model = st.text_input("Model", value=default_model)
    parallel = st.toggle("Run agents in parallel", value=(engine == "claude"),
                         help="Off by default on the free tier to stay inside per-minute limits.")
    budget = st.slider("Max web searches per agent", 3, 8, 6)
    st.caption("A run takes about 5 to 15 minutes.")
    if secret(key_name):
        st.caption(f"Runs left today on this demo: {max(0, runs_left())} of {DAILY_LIMIT}")

# ---------- inputs ----------
st.title("Commercial Due Diligence Agent")
st.write("Outside-in CDD on a listed company from public data: market, competition, customers and "
         "the target, with a red-flag summary and a Word report.")

c1, c2 = st.columns([2, 3])
company = c1.text_input("Company", placeholder="e.g. Asian Paints")
context = c2.text_input("Context (exchange, country, sector)",
                        placeholder="e.g. NSE listed, India, decorative paints")
c3, c4 = st.columns([2, 3])
lens = c3.selectbox("Investor perspective", [
    "financial investor (PE fund) considering a significant stake",
    "strategic acquirer considering a takeover",
    "public market investor building a position",
    "lender assessing credit risk",
])
notes = c4.text_input("Focus or notes (optional)",
                      placeholder="e.g. focus on the B2B segment; check the Middle East expansion")

using_owner_key = bool(secret(key_name))
session_runs = st.session_state.get("session_runs", 0)
blocked = using_owner_key and (runs_left() <= 0 or session_runs >= PER_SESSION_LIMIT)

run = st.button("Run CDD", type="primary", disabled=not (company and api_key) or blocked)
if not api_key:
    st.info("Add a free Gemini API key in the sidebar to start (get one at aistudio.google.com).")
elif blocked:
    st.warning("This demo has reached its run limit for now. You can paste your own free Gemini key "
               "in the sidebar, or try again tomorrow.")
st.caption("Draft desk research from public sources. Not investment advice.")

# ---------- run ----------
if run:
    state = {k: "queued" for k in STEPS}
    box = {}
    if using_owner_key:
        claim_run()
        st.session_state.session_runs = session_runs + 1

    def worker():
        try:
            box["result"] = run_cdd(company, context, lens, notes, api_key=api_key, provider=engine,
                                    model=model, parallel=parallel, search_budget=budget,
                                    search_key=search_key or None,
                                    on_progress=lambda k, s: state.__setitem__(k, s))
        except Exception as e:
            box["error"] = str(e)

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    icons = {"queued": "⏳", "running": "🔎", "done": "✅", "failed": "⚠️"}
    names = {"snapshot": "Company snapshot", **{k: AGENTS[k]["name"] for k in SECTION_ORDER},
             "editor": "Removing repetition across sections", "synthesis": "Executive summary"}
    started = time.time()
    with st.status("Researching...", expanded=True) as status:
        slot = st.empty()
        while t.is_alive():
            mins, secs = divmod(int(time.time() - started), 60)
            slot.markdown("\n".join(f"{icons[state[k]]} {names[k]}: {state[k]}" for k in state)
                          + f"\n\nElapsed {mins}m {secs:02d}s")
            time.sleep(1)
        slot.markdown("\n".join(f"{icons[state[k]]} {names[k]}: {state[k]}" for k in state))
        if "error" in box:
            status.update(label="Run failed", state="error")
            st.error(box["error"])
        else:
            status.update(label="Done", state="complete", expanded=False)
            st.session_state.result = box["result"]

# ---------- results ----------
res = st.session_state.get("result")
if res:
    s = res["summary"]
    fname = re.sub(r"[^A-Za-z0-9]+", "_", res["company"]).strip("_")
    d1, d2, _ = st.columns([1, 1, 3])
    d1.download_button("Download Word report", build_report(res), file_name=f"CDD_{fname}.docx",
                       mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                       type="primary")
    d2.download_button("Download raw JSON", json.dumps(res, indent=2, ensure_ascii=False),
                       file_name=f"CDD_{fname}.json", mime="application/json")

    st.subheader(f"{res['company']}: growth story {s.get('verdict', 'n/a').lower()}")
    st.write(s.get("verdict_reason", ""))

    tabs = st.tabs(["Summary", "Snapshot"] + [AGENTS[k]["name"] for k in SECTION_ORDER]
                   + ["Management questions"])
    with tabs[1]:
        fb = res.get("fact_base", {})
        st.write(fb.get("description", ""))
        if fb.get("segments"):
            st.dataframe(fb["segments"], use_container_width=True, hide_index=True)
        fin = fb.get("financials") or {}
        if fin.get("columns"):
            cols = fin["columns"]
            st.dataframe([dict(zip(cols, (list(r) + [""] * len(cols))[:len(cols)])) for r in fin.get("rows", [])],
                         use_container_width=True, hide_index=True)
        if fb.get("peers"):
            st.caption("Peer set: " + ", ".join(fb["peers"]))
    with tabs[0]:
        for b in s.get("executive_summary", []):
            st.markdown(f"- {b}")
        if s.get("top_red_flags"):
            st.markdown("**Red flags**")
            st.dataframe([{"Issue": f.get("issue"), "Section": f.get("section"),
                           "Severity": f.get("severity"), "Why it matters": f.get("why_it_matters")}
                          for f in s["top_red_flags"]], use_container_width=True, hide_index=True)
    for i, k in enumerate(SECTION_ORDER, start=2):
        sec = res["sections"][k]
        with tabs[i]:
            st.markdown(f"**{sec.get('headline', '')}**")
            st.caption(f"Confidence: {sec.get('confidence')}. {sec.get('confidence_reason', '')}")
            for f in sec.get("key_findings", []):
                st.markdown(f"- {f}")
            tbl = sec.get("table")
            if isinstance(tbl, dict) and tbl.get("columns"):
                st.markdown(f"**{tbl.get('title', '')}**")
                cols = tbl["columns"]
                st.dataframe([dict(zip(cols, (list(r) + [""] * len(cols))[:len(cols)]))
                              for r in tbl.get("rows", [])], use_container_width=True, hide_index=True)
            for part in sec.get("analysis", []):
                with st.expander(part.get("heading", "Detail")):
                    st.write(part.get("body", ""))
            if sec.get("red_flags"):
                st.warning("\n".join(f"- {x}" for x in sec["red_flags"]))
            if sec.get("sources"):
                with st.expander(f"Sources ({len(sec['sources'])})"):
                    for src in sec["sources"]:
                        st.markdown(f"- [{src['title']}]({src['url']})")
    with tabs[-1]:
        for i, q in enumerate(s.get("questions_for_management", []), start=1):
            st.markdown(f"{i}. {q}")
