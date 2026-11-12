"""Model providers. Each exposes the same two calls so the CDD pipeline does not care which one runs.

    research(system, user, max_searches) -> (text, searched, cited, n_searches)
        one web-grounded agent turn; searched/cited map url -> title
    complete(prompt, max_tokens) -> text
        plain call with no web access (editor, synthesis)

Gemini (free tier) is the default. Claude is kept for higher quality runs.
"""

from __future__ import annotations

import re
import time
from urllib.parse import urlparse

GEMINI_DEFAULT_MODEL = "gemini-3-flash-preview"
CLAUDE_DEFAULT_MODEL = "claude-sonnet-5-5"


def domain(url_or_title: str) -> str:
    s = (url_or_title or "").strip().lower()
    host = urlparse(s).netloc if "://" in s else s.split("/")[0]
    return host[4:] if host.startswith("www.") else host


# ---------------------------------------------------------------- Tavily web search (free: 1,000 searches a month)

def tavily_search(api_key: str, query: str, max_results: int = 5) -> list[dict]:
    """One basic Tavily search (1 credit). Returns [{title, url, content}]; empty list on failure."""
    import json as _json
    import urllib.error
    import urllib.request
    body = _json.dumps({"query": query, "search_depth": "basic", "max_results": max_results,
                        "chunks_per_source": 3, "include_answer": False}).encode()
    req = urllib.request.Request("https://api.tavily.com/search", data=body, method="POST",
                                 headers={"Authorization": f"Bearer {api_key}",
                                          "Content-Type": "application/json"})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=40) as r:
                data = _json.loads(r.read().decode())
            return [{"title": x.get("title") or x.get("url"), "url": x.get("url"),
                     "content": (x.get("content") or "")[:1500]} for x in data.get("results") or [] if x.get("url")]
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                raise RuntimeError("Tavily rejected the API key. Check TAVILY_API_KEY in Secrets.") from e
            if e.code == 432 or e.code == 433:
                raise RuntimeError("Tavily free monthly search credits are used up.") from e
            time.sleep(5 * (attempt + 1))
        except Exception:
            time.sleep(5 * (attempt + 1))
    return []


PLAN_QUERIES = """You are planning web searches for a research task. Read the task and return the {n} most useful
search queries as a JSON list of strings, nothing else. Each query under 12 words. Name the company in each
query where relevant. Favour queries that surface primary sources: the latest annual report, investor
presentation, earnings call transcript, stock exchange filings, regulator or industry body statistics, and
listed peers' results. Cover different aspects of the task; do not repeat near-identical queries.

TASK:
{task}"""

WITH_RESULTS = """{task}

WEB SEARCH RESULTS (gathered for you just now; these are your only sources):
{results}

Use only the information above plus clearly stated general knowledge. Do not invent figures. Cite sources by
their exact URL from the list above in the "sources" field. If the results do not cover something, say so
and add it to data_gaps."""


# ---------------------------------------------------------------- Gemini

class GeminiProvider:
    name = "Gemini (free tier)"
    default_model = GEMINI_DEFAULT_MODEL
    parallel_ok = False  # free tier per-minute limits are tight

    def __init__(self, api_key: str, model: str | None = None, search_key: str | None = None):
        from google import genai
        from google.genai import types
        self.types = types
        # With a Tavily key, Tavily does the searching and Gemini only reads and writes (works on projects
        # with no free Google Search grounding). Without one, Gemini's built-in Google Search is used.
        self.search_key = search_key
        self.name = "Gemini + Tavily search (free tiers)" if search_key else "Gemini (free tier)"
        self.client = genai.Client(api_key=api_key)
        self.candidates = self._candidate_models(model or self.default_model)
        self.model = self.candidates[0]
        self.tried = []  # models dropped because they had no free quota
        self.log = []    # why each model was dropped, for a useful error message

    def _candidate_models(self, wanted: str) -> list[str]:
        """Google renames, retires and paywalls models often. Build an ordered list of Flash models this key
        can call: the requested one first (if available), then stable Flash models, then previews, then lite."""
        try:
            available = []
            for m in self.client.models.list():
                name = (m.name or "").replace("models/", "")
                actions = getattr(m, "supported_actions", None) or []
                if actions and "generateContent" not in actions:
                    continue
                available.append(name)
        except Exception:
            return [wanted]  # cannot list models; try the requested one as is
        skip = ("image", "tts", "audio", "live", "embedding", "thinking", "exp", "8b", "robotics", "native")
        flash = [n for n in available if "flash" in n and not any(s in n for s in skip)]

        def rank(n):
            ver = re.search(r"gemini-(\d+(?:\.\d+)?)", n)
            v = float(ver.group(1)) if ver else 0.0
            # full Flash before lite, stable before preview, newest version first, aliases last
            return ("lite" not in n, "preview" not in n, "latest" not in n, v)
        ordered = sorted(flash, key=rank, reverse=True)
        out = ([wanted] if wanted in available else []) + [n for n in ordered if n != wanted]
        return out or [wanted]

    @staticmethod
    def _quota_info(e) -> dict:
        """Read Google's structured quota details: which quota was hit, its value, and the retry delay."""
        import json as _json
        raw = _json.dumps(getattr(e, "details", None) or {}) + " " + str(getattr(e, "message", "") or e)
        ids = re.findall(r'"quotaId":\s*"([^"]+)"', raw)
        values = re.findall(r'"quotaValue":\s*"?(\d+)', raw)
        delay = re.search(r'"retryDelay":\s*"(\d+)', raw)
        low = raw.lower()
        return {
            "ids": ids,
            "zero": any(v == "0" for v in values) or "limit: 0" in low,
            "daily": any("perday" in i.lower() for i in ids),
            "minute": any("perminute" in i.lower() for i in ids),
            "search": any("search" in i.lower() or "grounding" in i.lower() for i in ids) or "grounding" in low,
            "delay": int(delay.group(1)) if delay else 20,
            "msg": str(getattr(e, "message", "") or e)[:300],
        }

    def _next_model(self) -> bool:
        self.tried.append(self.model)
        rest = [m for m in self.candidates if m not in self.tried]
        if rest:
            self.model = rest[0]
            return True
        return False

    def _call(self, contents, config):
        from google.genai import errors
        waits, last = 0, None
        while True:
            try:
                return self.client.models.generate_content(model=self.model, contents=contents, config=config)
            except errors.APIError as e:
                code = getattr(e, "code", 0)
                if code not in (404, 429, 500, 503):
                    raise RuntimeError(f"Gemini error {code} on {self.model}: {str(e)[:300]}") from e
                q = self._quota_info(e) if code == 429 else {"ids": [], "minute": False, "delay": 20,
                                                              "msg": str(e)[:300]}
                last = (code, q)
                # short wait for a per-minute limit or a temporary outage, at most twice per model
                if (q["minute"] and not q["zero"] or code in (500, 503)) and waits < 2:
                    waits += 1
                    time.sleep(min(60, q["delay"] + 2))
                    continue
                # anything else (no free allowance, daily cap, retired model, unknown quota):
                # each model has its own quota, so move to the next one
                waits = 0
                self.log.append(f"{self.model}: {code} {', '.join(q.get('ids') or []) or q['msg'][:90]}")
                if self._next_model():
                    continue
                raise RuntimeError(self._diagnose(config)) from e

    def _diagnose(self, config) -> str:
        """Every model failed. Find out whether plain calls work and only Google Search is blocked."""
        uses_search = bool(getattr(config, "tools", None))
        verdict = ""
        if uses_search:
            for m in self.candidates:
                try:
                    self.client.models.generate_content(model=m, contents="Reply with OK.",
                                                        config=self.types.GenerateContentConfig(max_output_tokens=5))
                    verdict = (f"DIAGNOSIS: plain requests work on {m}, but requests with Google Search are refused. "
                               "This key's project has no free Google Search grounding allowance. ")
                    break
                except Exception:
                    continue
            if not verdict:
                verdict = ("DIAGNOSIS: even plain requests without Google Search are refused on every model, so this "
                           "project has no usable free quota at all (often a new project, or one linked to billing). ")
        return verdict + "Per model: " + " | ".join(self.log) + ". Check https://ai.dev/rate-limit"

    def research(self, system, user, max_searches):
        if self.search_key:
            return self._research_tavily(system, user, max_searches)
        t = self.types
        user = user + f"\n\nUse Google Search as needed (aim for no more than about {max_searches} searches)."
        cfg = t.GenerateContentConfig(system_instruction=system, temperature=0.3,
                                      tools=[t.Tool(google_search=t.GoogleSearch())])
        resp = self._call(user, cfg)
        text = resp.text or ""
        searched, n = {}, 0
        for cand in resp.candidates or []:
            gm = getattr(cand, "grounding_metadata", None)
            if not gm:
                continue
            n += len(gm.web_search_queries or [])
            for ch in gm.grounding_chunks or []:
                web = getattr(ch, "web", None)
                if web and web.uri:
                    # Gemini returns a Google redirect link; the title is the source's domain
                    searched[web.uri] = web.title or web.uri
        return text, searched, {}, n

    def _research_tavily(self, system, user, max_searches):
        """Plan queries with Gemini, run them on Tavily, then have Gemini write from the results."""
        n = max(3, min(int(max_searches), 8))
        plan = self.complete(f"{system}\n\n" + PLAN_QUERIES.format(n=n, task=user), max_tokens=1500)
        m = re.search(r"\[.*\]", plan or "", flags=re.S)
        try:
            import json as _json
            queries = [q for q in _json.loads(m.group(0)) if isinstance(q, str)][:n] if m else []
        except Exception:
            queries = []
        if not queries:  # fall back to a simple query from the task's first line
            queries = [re.sub(r"\s+", " ", user.split("\n")[0])[:120]]

        searched, blocks, seen = {}, [], set()
        for q in queries:
            for r in tavily_search(self.search_key, q):
                if r["url"] in seen:
                    continue
                seen.add(r["url"])
                searched[r["url"]] = r["title"]
                blocks.append(f"[{len(blocks) + 1}] {r['title']}\nURL: {r['url']}\n{r['content']}")
        results = "\n\n".join(blocks)[:60000] or "No results were returned."
        cfg = self.types.GenerateContentConfig(system_instruction=system, temperature=0.3, max_output_tokens=8192)
        text = self._call(WITH_RESULTS.format(task=user, results=results), cfg).text or ""
        return text, searched, {}, len(queries)

    def complete(self, prompt, max_tokens=8000):
        cfg = self.types.GenerateContentConfig(temperature=0.2, max_output_tokens=max_tokens)
        return self._call(prompt, cfg).text or ""


# ---------------------------------------------------------------- Claude

class ClaudeProvider:
    name = "Claude (paid)"
    default_model = CLAUDE_DEFAULT_MODEL
    parallel_ok = True

    def __init__(self, api_key: str, model: str | None = None):
        import anthropic
        self.anthropic = anthropic
        self.client = anthropic.Anthropic(api_key=api_key)
        self.model = model or self.default_model

    def _call(self, **kwargs):
        a = self.anthropic
        for attempt in range(4):
            try:
                return self.client.messages.create(model=self.model, **kwargs)
            except (a.RateLimitError, a.APIConnectionError, a.InternalServerError):
                if attempt == 3:
                    raise
                time.sleep(15 * (attempt + 1))

    def research(self, system, user, max_searches):
        tools = [{"type": "web_search_20250305", "name": "web_search", "max_uses": max_searches}]
        messages = [{"role": "user", "content": user}]
        text, searched, cited, n = "", {}, {}, 0
        for _ in range(6):  # follow pause_turn continuations on long searches
            resp = self._call(max_tokens=8000, system=system, messages=messages, tools=tools)
            for block in resp.content:
                btype = getattr(block, "type", "")
                if btype == "text":
                    text += block.text
                    for c in (getattr(block, "citations", None) or []):
                        if getattr(c, "url", None):
                            cited[c.url] = getattr(c, "title", None) or c.url
                elif btype == "server_tool_use":
                    n += 1
                elif btype == "web_search_tool_result" and isinstance(getattr(block, "content", None), list):
                    for r in block.content:
                        if getattr(r, "url", None):
                            searched[r.url] = getattr(r, "title", None) or r.url
            if resp.stop_reason != "pause_turn":
                break
            messages = messages + [{"role": "assistant",
                                    "content": [b.model_dump(exclude_none=True) for b in resp.content]}]
        return text, searched, cited, n

    def complete(self, prompt, max_tokens=8000):
        resp = self._call(max_tokens=max_tokens, messages=[{"role": "user", "content": prompt}])
        return "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")


PROVIDERS = {"gemini": GeminiProvider, "claude": ClaudeProvider}


def make_provider(name: str, api_key: str, model: str | None = None, search_key: str | None = None):
    if name == "gemini":
        return GeminiProvider(api_key=api_key, model=model or None, search_key=search_key or None)
    return PROVIDERS[name](api_key=api_key, model=model or None)
