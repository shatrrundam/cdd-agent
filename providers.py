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


# ---------------------------------------------------------------- Gemini

class GeminiProvider:
    name = "Gemini (free tier)"
    default_model = GEMINI_DEFAULT_MODEL
    parallel_ok = False  # free tier per-minute limits are tight

    def __init__(self, api_key: str, model: str | None = None):
        from google import genai
        from google.genai import types
        self.types = types
        self.client = genai.Client(api_key=api_key)
        self.candidates = self._candidate_models(model or self.default_model)
        self.model = self.candidates[0]
        self.tried = []  # models dropped because they had no free quota

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
                if self._next_model():
                    continue
                code, q = last
                hint = (" The Google Search quota appears to be the limit." if q.get("search") else "")
                raise RuntimeError(
                    f"No Gemini model with free quota worked for this key (tried {', '.join(self.tried)}). "
                    f"Last error {code}: {q['msg']} Quota: {', '.join(q['ids']) or 'not stated'}.{hint} "
                    f"Check per-model free limits at https://ai.dev/rate-limit") from e

    def research(self, system, user, max_searches):
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


def make_provider(name: str, api_key: str, model: str | None = None):
    return PROVIDERS[name](api_key=api_key, model=model or None)
