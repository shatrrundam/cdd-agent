"""Core engine.

Pipeline:
  1. Fact base agent builds the company snapshot once (segments, headline financials, peers).
  2. Four section agents each get the snapshot as established facts and a strict topic ownership
     map, so they research new ground instead of re-finding the same numbers.
  3. Editor pass deletes any point that still appears in more than one section.
  4. Synthesis writes the executive summary.

Runs on Gemini's free tier by default; pass provider="claude" for the paid, higher quality version.

Usage from Python:
    from cdd_agent import run_cdd
    result = run_cdd("Asian Paints", context="NSE listed, India, decorative paints", api_key="AIza...")

Command line:
    export GEMINI_API_KEY=AIza...
    python cdd_agent.py "Asian Paints" --context "NSE listed, India" --out report.docx
"""

from __future__ import annotations

import datetime as dt
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable

from prompts import (AGENTS, EDITOR_PROMPT, FACT_BASE_SYSTEM, FACT_BASE_USER, SECTION_SYSTEM,
                     SYNTHESIS_PROMPT)
from providers import domain, make_provider

DEFAULT_PROVIDER = "gemini"
SECTION_ORDER = ["market", "competition", "customer", "target"]
STEPS = ["snapshot"] + SECTION_ORDER + ["editor", "synthesis"]


# ---------------------------------------------------------------- helpers

def _clean(text: str) -> str:
    """House style: no em or en dashes in the report."""
    text = text.replace(" — ", ", ").replace("—", ", ")
    return text.replace(" – ", " to ").replace("–", "-")


def _clean_obj(obj):
    if isinstance(obj, str):
        return _clean(obj)
    if isinstance(obj, list):
        return [_clean_obj(x) for x in obj]
    if isinstance(obj, dict):
        return {k: _clean_obj(v) for k, v in obj.items()}
    return obj


def _extract_json(text: str) -> dict | None:
    fenced = re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", text or "", flags=re.S)
    candidates = fenced[::-1]
    start, end = (text or "").find("{"), (text or "").rfind("}")
    if start != -1 and end > start:
        candidates.append(text[start:end + 1])
    for c in candidates:
        for attempt in (c, re.sub(r",\s*([}\]])", r"\1", c)):
            try:
                return json.loads(attempt)
            except json.JSONDecodeError:
                continue
    return None


def _ctx(context: str) -> str:
    return context.strip() or "listed company"


def _verify_sources(listed, searched, cited):
    """Keep only sources that really came back from web search.

    A listed source passes if its exact URL was returned, or (for Gemini, which returns Google redirect
    links) if its domain matches a domain that search actually returned. Anything else is dropped.
    """
    seen = {**searched, **cited}
    seen_domains = {domain(u) for u in seen} | {domain(t) for t in seen.values()}
    seen_domains.discard("")
    verified, dropped = [], 0
    for s in listed or []:
        url = (s or {}).get("url", "")
        if url in seen or (domain(url) and domain(url) in seen_domains):
            verified.append({"title": s.get("title") or seen.get(url, url), "url": url})
        else:
            dropped += 1
    # add citations (Claude) or grounding sources (Gemini) not already represented
    covered = {domain(v["url"]) for v in verified}
    extra = cited or {u: t for u, t in searched.items() if "vertexaisearch" in u}
    for url, title in extra.items():
        d = domain(title) if "vertexaisearch" in url else domain(url)
        if d not in covered:
            verified.append({"title": title, "url": url})
            covered.add(d)
    return verified, dropped


# ---------------------------------------------------------------- 1. fact base

def run_fact_base(provider, company, context, searches=6) -> dict:
    system = FACT_BASE_SYSTEM.format(company=company, context=_ctx(context), today=dt.date.today().isoformat())
    text, searched, cited, n = provider.research(system, FACT_BASE_USER.format(company=company, searches=searches),
                                                 searches)
    data = _extract_json(text) or {"description": "Snapshot could not be built.", "segments": [], "peers": []}
    data["sources"], dropped = _verify_sources(data.get("sources"), searched, cited)
    data["_meta"] = {"searches": n, "unverified_sources_dropped": dropped}
    return _clean_obj(data)


def _fact_base_for_prompt(fb: dict) -> str:
    return json.dumps({k: v for k, v in fb.items() if k not in ("sources", "_meta")},
                      indent=1, ensure_ascii=False)


# ---------------------------------------------------------------- 2. section agents

def run_section(provider, key, company, context, lens, notes, fact_base, search_budget=None) -> dict:
    spec = AGENTS[key]
    system = SECTION_SYSTEM.format(
        company=company, context=_ctx(context), lens=lens, today=dt.date.today().isoformat(),
        notes=f"Analyst notes: {notes.strip()}" if notes.strip() else "",
        fact_base=_fact_base_for_prompt(fact_base),
    )
    started = time.time()
    text, searched, cited, n = provider.research(system, spec["brief"].format(company=company),
                                                 search_budget or spec["max_searches"])
    data = _extract_json(text)
    if data is None:
        data = {
            "headline": "Agent output could not be parsed into the standard structure.",
            "key_findings": [], "analysis": [{"heading": "Raw output", "body": (text or "").strip()[:6000]}],
            "table": None, "red_flags": [], "data_gaps": ["Structured output failed; rerun this section."],
            "confidence": "Low", "confidence_reason": "Output parsing failed.", "sources": [],
        }
    data["sources"], dropped = _verify_sources(data.get("sources"), searched, cited)
    data["_meta"] = {"section": spec["name"], "searches": n, "unverified_sources_dropped": dropped,
                     "seconds": round(time.time() - started), "model": provider.model}
    return _clean_obj(data)


# ---------------------------------------------------------------- 3. editor

EDITABLE = ("headline", "key_findings", "analysis", "red_flags")


def edit_sections(provider, company, fact_base, sections) -> tuple[dict, int]:
    """Remove points repeated across sections. Falls back to the unedited sections on any failure."""
    payload = {k: {f: sections[k].get(f) for f in EDITABLE} for k in SECTION_ORDER if k in sections}
    snapshot_summary = "; ".join(filter(None, [
        fact_base.get("description"),
        "segments: " + ", ".join(s.get("segment", "") for s in fact_base.get("segments") or []),
        "peers: " + ", ".join(fact_base.get("peers") or []),
        "headline financials table for recent fiscal years",
    ]))
    prompt = EDITOR_PROMPT.format(company=company, snapshot_summary=snapshot_summary,
                                  sections=json.dumps(payload, indent=1, ensure_ascii=False))
    try:
        edited = _extract_json(provider.complete(prompt, max_tokens=12000))
    except Exception:
        edited = None
    if not isinstance(edited, dict):
        return _local_dedupe(sections), 0

    removed, out = 0, {}
    for k, sec in sections.items():
        new = dict(sec)
        e = edited.get(k)
        if isinstance(e, dict) and isinstance(e.get("key_findings"), list) \
                and len(e["key_findings"]) >= min(3, len(sec.get("key_findings") or [])):
            for f in EDITABLE:
                if f in e and e[f] is not None:
                    new[f] = e[f]
            try:
                removed += int(e.get("removed_count") or 0)
            except (TypeError, ValueError):
                pass
        out[k] = new
    return _clean_obj(_local_dedupe(out)), removed


def _norm(s: str) -> set:
    return set(re.findall(r"[a-z]+|\d+(?:\.\d+)?", str(s).lower()))


def _local_dedupe(sections: dict, threshold: float = 0.6) -> dict:
    """Safety net: drop a finding or red flag if a near-identical one already appeared in an earlier section."""
    seen: list[set] = []
    out = {}
    for k in SECTION_ORDER:
        if k not in sections:
            continue
        sec = dict(sections[k])
        for field in ("key_findings", "red_flags"):
            kept = []
            for item in sec.get(field) or []:
                toks = _norm(item)
                if not any(toks and len(toks & s) / max(1, min(len(toks), len(s))) >= threshold for s in seen):
                    kept.append(item)
            sec[field] = kept
        for field in ("key_findings", "red_flags"):
            seen.extend(_norm(x) for x in sec.get(field) or [])
        out[k] = sec
    return out


# ---------------------------------------------------------------- 4. synthesis

def synthesise(provider, company, context, lens, fact_base, sections) -> dict:
    slim = {"company_snapshot": {k: v for k, v in fact_base.items() if k not in ("sources", "_meta")}}
    slim.update({k: {kk: v for kk, v in sec.items() if kk not in ("sources", "_meta", "analysis")}
                 for k, sec in sections.items()})
    prompt = SYNTHESIS_PROMPT.format(company=company, context=_ctx(context), lens=lens,
                                     today=dt.date.today().isoformat(),
                                     findings=json.dumps(slim, indent=1, ensure_ascii=False))
    data = _extract_json(provider.complete(prompt, max_tokens=6000)) or {
        "verdict": "Not assessed", "verdict_reason": "Synthesis output could not be parsed.",
        "executive_summary": [], "top_red_flags": [], "value_creation_levers": [],
        "questions_for_management": [], "next_steps": [],
    }
    return _clean_obj(data)


# ---------------------------------------------------------------- orchestration

def _failed_section(k, err, model):
    return {"headline": "This section failed to run.", "key_findings": [], "analysis": [], "table": None,
            "red_flags": [], "data_gaps": [f"Agent error: {err[:300]}"], "confidence": "Low",
            "confidence_reason": "Agent did not complete.", "sources": [],
            "_meta": {"section": AGENTS[k]["name"], "searches": 0, "seconds": 0, "model": model,
                      "unverified_sources_dropped": 0}}


def run_cdd(company: str, context: str = "", lens: str = "financial investor (PE fund)",
            notes: str = "", api_key: str | None = None, provider: str = DEFAULT_PROVIDER,
            model: str | None = None, parallel: bool | None = None, search_budget: int | None = None,
            on_progress: Callable[[str, str], None] | None = None) -> dict:
    """Run the full CDD. on_progress(step, status) fires for each step in STEPS."""
    import os
    key = api_key or os.environ.get("GEMINI_API_KEY" if provider == "gemini" else "ANTHROPIC_API_KEY", "")
    llm = make_provider(provider, key, model)
    if parallel is None:
        parallel = llm.parallel_ok
    progress = on_progress or (lambda k, s: None)

    progress("snapshot", "running")
    fact_base = run_fact_base(llm, company, context)
    progress("snapshot", "done")

    sections, errors = {}, {}

    def job(k):
        progress(k, "running")
        return run_section(llm, k, company, context, lens, notes, fact_base, search_budget)

    if parallel:
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = {pool.submit(job, k): k for k in SECTION_ORDER}
            for f in as_completed(futures):
                k = futures[f]
                try:
                    sections[k] = f.result()
                    progress(k, "done")
                except Exception as e:
                    errors[k] = str(e)
                    progress(k, "failed")
    else:
        for k in SECTION_ORDER:
            try:
                sections[k] = job(k)
                progress(k, "done")
            except Exception as e:
                errors[k] = str(e)
                progress(k, "failed")
    for k, err in errors.items():
        sections[k] = _failed_section(k, err, llm.model)
    sections = {k: sections[k] for k in SECTION_ORDER}

    progress("editor", "running")
    sections, removed = edit_sections(llm, company, fact_base, sections)
    progress("editor", "done")

    progress("synthesis", "running")
    summary = synthesise(llm, company, context, lens, fact_base, sections)
    progress("synthesis", "done")

    return {"company": company, "context": context, "lens": lens, "notes": notes,
            "date": dt.date.today().isoformat(), "model": f"{llm.name}: {llm.model}",
            "duplicates_removed": removed, "fact_base": fact_base, "summary": summary, "sections": sections}


if __name__ == "__main__":
    import argparse
    from report import build_report

    p = argparse.ArgumentParser(description="High-level CDD on a listed company from public data")
    p.add_argument("company")
    p.add_argument("--context", default="", help="exchange, country, sector, e.g. 'NSE listed, India, paints'")
    p.add_argument("--lens", default="financial investor (PE fund)")
    p.add_argument("--notes", default="")
    p.add_argument("--provider", default=DEFAULT_PROVIDER, choices=["gemini", "claude"])
    p.add_argument("--model", default=None)
    p.add_argument("--out", default=None)
    a = p.parse_args()

    res = run_cdd(a.company, a.context, a.lens, a.notes, provider=a.provider, model=a.model,
                  on_progress=lambda k, s: print(f"[{k}] {s}"))
    out = a.out or f"CDD_{re.sub(r'[^A-Za-z0-9]+', '_', a.company)}.docx"
    with open(out, "wb") as fh:
        fh.write(build_report(res))
    with open(out.replace(".docx", ".json"), "w") as fh:
        json.dump(res, fh, indent=2, ensure_ascii=False)
    print("Saved", out)
