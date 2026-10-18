"""Research bookkeeping: question de-duplication, result cache, source registration and claim verification."""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

from .db import Database, jloads, now
from .providers.base import Source
from .schemas import ResearchReport
from .settings_service import SettingsService
from .store import Store
from .tools.webcheck import Fetcher, excerpt_in_text

_STOP = {"the", "a", "an", "of", "to", "in", "for", "and", "or", "is", "are", "on", "with", "what", "which", "how", "does", "do", "about", "please"}
OUTDATED_AFTER_DAYS = 180
MAX_FETCHES_PER_SUBTASK = 6


def normalize(q: str) -> str:
    words = [w for w in re.findall(r"[a-z0-9]+", q.lower()) if w not in _STOP]
    return " ".join(words)


def qhash(q: str) -> str:
    return hashlib.sha256(normalize(q).encode()).hexdigest()


def _jaccard(a: str, b: str) -> float:
    sa, sb = set(a.split()), set(b.split())
    return len(sa & sb) / len(sa | sb) if sa and sb else 0.0


def parse_date(s: str | None) -> datetime | None:
    if not s:
        return None
    s = s.strip()
    try:
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return d.replace(tzinfo=timezone.utc) if d.tzinfo is None else d.astimezone(timezone.utc)
    except ValueError:
        pass
    for fmt in ("%m/%d/%Y", "%B %d, %Y", "%b %d, %Y", "%d %B %Y"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def domain_of(url: str) -> str:
    return (urlparse(url).hostname or "").lower().removeprefix("www.")


class ResearchService:
    def __init__(self, db: Database, store: Store, settings: SettingsService, fetcher: Fetcher | None, verification_enabled: bool = True):
        self.db = db
        self.store = store
        self.settings = settings
        self.fetcher = fetcher
        self.verification_enabled = verification_enabled

    # ---- cache -------------------------------------------------------------
    def cache_lookup(self, question: str, time_sensitive: bool) -> dict | None:
        lim = self.settings.get_key("limits")
        ttl_h = lim["time_sensitive_ttl_hours"] if time_sensitive else lim["research_cache_ttl_hours"]
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=ttl_h)).isoformat(timespec="milliseconds")
        norm = normalize(question)
        h = qhash(question)
        rows = self.db.all("SELECT * FROM research_cache WHERE created_at>=? ORDER BY id DESC LIMIT 200", (cutoff,))
        for r in rows:
            if r["question_hash"] == h or _jaccard(norm, normalize(r["question"])) >= 0.85:
                # a cached non-time-sensitive answer must not satisfy a time-sensitive question
                if time_sensitive and not r["time_sensitive"]:
                    continue
                self.db.execute("UPDATE research_cache SET hits=hits+1 WHERE id=?", (r["id"],))
                res = jloads(r["result"], {})
                res["cache_created_at"] = r["created_at"]
                return res
        return None

    def cache_store(self, question: str, time_sensitive: bool, result: dict) -> None:
        self.db.execute("INSERT INTO research_cache(question_hash,question,time_sensitive,result,created_at) VALUES(?,?,?,?,?)",
                        (qhash(question), question[:500], int(time_sensitive), json.dumps(result), now()))

    # ---- sources / claims ---------------------------------------------------
    def register(self, task_id: str, subtask_id: str, question: str, report: ResearchReport, provider_sources: list[Source],
                 retrieved_at: str | None = None) -> None:
        """Persist sources and claims. URLs that the search API did not return are kept but marked model_claimed."""
        retrieved_at = retrieved_at or now()
        by_url: dict[str, dict] = {}
        for s in provider_sources:
            by_url[s.url] = {"url": s.url, "title": s.title, "domain": domain_of(s.url), "published": s.published,
                             "last_updated": s.last_updated, "excerpt": s.snippet, "origin": "provider", "retrieved_at": retrieved_at}
        for s in report.sources:
            if s.url in by_url:
                e = by_url[s.url]
                e["title"] = e["title"] or s.title
                e["excerpt"] = e["excerpt"] or s.excerpt
                e["published"] = e["published"] or s.published
            else:
                by_url[s.url] = {"url": s.url, "title": s.title, "domain": s.publisher or domain_of(s.url), "published": s.published,
                                 "last_updated": None, "excerpt": s.excerpt, "origin": "model_claimed", "retrieved_at": retrieved_at,
                                 "check_note": "URL was written by the model and was not returned by the search API"}
        for e in by_url.values():
            self.store.add_source(task_id, subtask_id, question, e)
        for f in report.key_findings:
            note = "" if f.support == "source" else "Agent interpretation; no source states this directly"
            self.store.add_claim(task_id, subtask_id, f.claim, "unverified", f.support, f.source_urls, note)
        for text in report.conflicting_evidence:
            self.store.add_claim(task_id, subtask_id, text, "conflicting", "source", [], "Reported by the research agent as conflicting evidence")

    async def verify(self, task_id: str, subtask_id: str, time_sensitive: bool) -> dict:
        claims = [c for c in self.store.claims(task_id) if c["subtask_id"] == subtask_id and c["status"] == "unverified"]
        sources = {s["url"]: s for s in self.store.sources(task_id) if s["subtask_id"] == subtask_id}
        if not (self.verification_enabled and self.fetcher):
            return {"checked": 0, "reason": "web verification is disabled or unavailable (demo mode); all claims remain unverified"}
        fetched = 0
        for url, s in sources.items():
            if fetched >= MAX_FETCHES_PER_SUBTASK:
                break
            if not s["excerpt"]:
                self.db.execute("UPDATE sources SET check_status='unchecked', check_note=? WHERE id=?", ("no excerpt to compare", s["id"]))
                continue
            fetched += 1
            res = await self.fetcher.fetch(url)
            if not res.ok:
                st, note = "unreachable", res.error
            elif excerpt_in_text(s["excerpt"], res.text):
                st, note = "excerpt_found", "the cited excerpt appears on the live page"
            else:
                st, note = "excerpt_not_found", "page opened but the cited excerpt was not found"
            if s["origin"] == "model_claimed":
                note += "; URL was written by the model and was not returned by the search API"
            s["check_status"] = st
            self.db.execute("UPDATE sources SET check_status=?, check_note=? WHERE id=?", (st, note, s["id"]))
        verified = 0
        for c in claims:
            if c["support"] != "source":
                continue
            linked = [sources[u] for u in c["source_urls"] if u in sources and sources[u]["origin"] == "provider"]
            if not linked:
                self.store.set_claim_status(c["id"], "unverified", "no cited source matches a result returned by the search API")
                continue
            found = [s for s in linked if s["check_status"] == "excerpt_found"]
            if found:
                dates = [d for d in (parse_date(s["last_updated"]) or parse_date(s["published"]) for s in found) if d]
                newest = max(dates) if dates else None
                if time_sensitive and newest and datetime.now(timezone.utc) - newest > timedelta(days=OUTDATED_AFTER_DAYS):
                    self.store.set_claim_status(c["id"], "outdated", f"source dated {newest.date()} is older than {OUTDATED_AFTER_DAYS} days for a time-sensitive question")
                else:
                    note = "cited excerpt found on the live page (proves the source says this, not that the source is right)"
                    if time_sensitive and not newest:
                        note += "; source date unknown"
                    self.store.set_claim_status(c["id"], "verified", note)
                    verified += 1
            elif any(s["check_status"] == "excerpt_not_found" for s in linked):
                self.store.set_claim_status(c["id"], "unverified", "source page opened but the cited excerpt was not found")
            else:
                self.store.set_claim_status(c["id"], "unverified", "source could not be opened or had no excerpt to check")
        return {"checked": fetched, "claims_verified": verified, "claims_total": len(claims)}

    def apply_evaluation(self, task_id: str, subtask_id: str, verdicts: list[dict]) -> None:
        """The evaluator may DOWNGRADE a claim (conflicting/outdated) but can never mark one verified."""
        claims = {c["text"].strip(): c for c in self.store.claims(task_id) if c["subtask_id"] == subtask_id}
        for v in verdicts:
            c = claims.get(v["claim"].strip())
            if not c or v["status"] not in ("conflicting", "outdated"):
                continue
            self.store.set_claim_status(c["id"], v["status"], f"evaluator: {v.get('note', '')}"[:500])
