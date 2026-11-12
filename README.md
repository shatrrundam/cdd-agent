# CDD Agent

An AI agent that runs a high-level commercial due diligence on a listed company using live public data, and writes a Word report.

How it avoids repetition across sections:

1. A snapshot agent researches the company basics once (segments, headline financials, peer set). These are printed in a Company Snapshot page and handed to every section agent as already established.
2. Each section agent has a strict list of topics it owns and topics it must skip, and is told not to restate the snapshot.
3. An editor pass reads all four sections and deletes any point that appears in more than one, keeping it only in the section that owns the topic. A word-overlap check runs after it as a safety net.

Four research agents run in parallel, one per CDD workstream, each with its own brief and web search budget:

| Agent | Covers |
|---|---|
| Market | Market definition, size and growth (ranges across sources), drivers, regulation, profit pools |
| Competition | 5 to 8 peers benchmarked on scale, growth, margins and share; basis of competition; barriers; consolidation |
| Customer | Segment and channel mix, concentration, stickiness, purchase criteria, pricing power, sentiment |
| Target | Business model, 3 to 5 year financials, growth decomposition, guidance track record, moats, governance signals |

A synthesis step then reads all four and produces a verdict on the growth story, an executive summary, a red-flag table, value creation levers, questions for management, and next steps for full CDD.

Output: a formatted Word report (cover, executive summary, section scorecard, red flags, four detailed sections with tables, management questions, sources, methodology) plus the raw JSON.

## Design choices that matter

- **Primary sources first.** Each agent is told to start from annual reports, investor presentations, earnings call transcripts and exchange filings before press or industry reports.
- **Source verification.** Any source the model lists that did not actually come back from a web search is dropped, so the appendix cannot contain invented links.
- **Conflicting numbers shown as ranges**, not silently resolved.
- **Confidence rating and data gaps per section**, so the reader knows what is thin.
- **Synthesis cannot add new numbers**; it only works from what the agents found.

## Engine

Runs free by default on Google's Gemini API (Gemini 3 Flash with Google Search grounding). Get a key at aistudio.google.com, no card needed. Claude can be switched on for higher quality runs by adding `ANTHROPIC_API_KEY` to secrets; visitors never see the paid option unless you add that key.

Free tier notes: limits are set per Google Cloud project and shown in AI Studio; search grounding has a monthly free allowance; Google may use free tier inputs to improve its products, so only run it on public company information.

## Run locally

```bash
pip install -r requirements.txt
cp .streamlit/secrets.toml.example .streamlit/secrets.toml   # then add your Gemini key
streamlit run app.py
```

Or from the command line:

```bash
export GEMINI_API_KEY=AIza...
python cdd_agent.py "Asian Paints" --context "NSE listed, India, decorative paints" --out asian_paints.docx
```

## Deploy on Streamlit Community Cloud

1. Push this folder to a GitHub repo (the `.gitignore` keeps your secrets out).
2. On share.streamlit.io, create an app from the repo with `app.py` as the entry point.
3. In App settings > Secrets, paste the contents of `secrets.toml.example` with your real Gemini key.
4. `DAILY_RUN_LIMIT` (default 20) and `SESSION_RUN_LIMIT` (default 3) stop a busy public link from using up your free quota. Visitors who hit the limit can paste their own free key.

## Time

A run makes seven model calls and around 30 searches. On the free tier the agents run one after another to stay within per-minute limits, so expect roughly 10 to 15 minutes.

## Limitations

- Customer analysis uses public proxies, not interviews or surveys.
- No access to paywalled data (broker research, Capital IQ, Statista, Gartner).
- Figures should be checked against the audited accounts before use in a client deliverable.
- This is a desk research accelerator, not a substitute for full CDD.

## Files

- `app.py` Streamlit interface
- `cdd_agent.py` orchestration, web search agents, synthesis, CLI
- `prompts.py` all research briefs and the output schema (edit here to change the methodology)
- `providers.py` Gemini (free) and Claude (paid) engines behind one interface
- `report.py` Word report builder
