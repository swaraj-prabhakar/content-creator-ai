"""Instagram Reels poster (standard library only). Runs inside GitHub Actions in the queue repo, and locally.

Queue = DRAFT releases in the queue repo (drafts are invisible to the public and their assets aren't downloadable
without a token). Each draft release:
  tag   reel-<run name>
  body  JSON {"kind": "reel", "status": "queued|posted|failed", "publish_at": UTC ISO, "caption": ..., "thumb_offset_ms",
              "youtube_url", "attempts", "media_id", "permalink", "error"}
  asset the video (final_youtube.mp4)

Each run: check the Instagram token, then for every queued item due within LEAD minutes: download the video, create a
resumable REELS container, upload, wait for processing, sleep until publish_at, publish, record the permalink in the
release body (status posted) and delete the video asset. A caption that is already on the account is not posted again.

Env: IG_USER_ID, IG_ACCESS_TOKEN (EAA... = Facebook-login token -> graph.facebook.com; IG... -> graph.instagram.com),
GITHUB_TOKEN, GITHUB_REPOSITORY (owner/name), optional DRY_RUN=1, LEAD_MIN (default 20).
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

API_VER = "v21.0"
MAX_ATTEMPTS = 3


def _graph(token: str) -> str:
    return f"https://graph.facebook.com/{API_VER}" if token.startswith("EAA") else f"https://graph.instagram.com/{API_VER}"


def _req(method: str, url: str, *, params: dict | None = None, data: bytes | None = None, headers: dict | None = None,
         timeout: int = 120):
    if params:
        url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
    r = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            body = resp.read()
            return resp.status, (json.loads(body) if body and body[:1] in b"{[" else body)
    except urllib.error.HTTPError as e:
        body = e.read()
        try:
            return e.code, json.loads(body)
        except ValueError:
            return e.code, {"error": {"message": body[:300].decode("utf-8", "replace")}}


class IGError(RuntimeError):
    pass


class Instagram:
    def __init__(self, user_id: str, token: str):
        self.uid, self.token, self.base = user_id, token, _graph(token)

    def call(self, method: str, path: str, **params):
        code, j = _req(method, f"{self.base}/{path}", params={**params, "access_token": self.token})
        if code >= 400 or (isinstance(j, dict) and "error" in j):
            err = j.get("error", {}) if isinstance(j, dict) else {}
            raise IGError(f"{path}: {err.get('message', j)} (code {err.get('code')}, sub {err.get('error_subcode')})")
        return j

    def whoami(self) -> dict:
        return self.call("GET", self.uid, fields="id,username,media_count")

    def recent_captions(self, n: int = 15) -> list[str]:
        return [m.get("caption") or "" for m in self.call("GET", f"{self.uid}/media", fields="caption", limit=n).get("data", [])]

    def upload_reel(self, video_path: str, caption: str, thumb_offset_ms: int = 200) -> str:
        """Create a resumable REELS container and upload the file. Returns the container id once FINISHED."""
        c = self.call("POST", f"{self.uid}/media", media_type="REELS", upload_type="resumable", caption=caption,
                      share_to_feed="true", thumb_offset=str(thumb_offset_ms))
        size = os.path.getsize(video_path)
        with open(video_path, "rb") as fh:
            data = fh.read()
        code, j = _req("POST", c["uri"], data=data, timeout=600,
                       headers={"Authorization": f"OAuth {self.token}", "offset": "0", "file_size": str(size)})
        if code >= 400 or not (isinstance(j, dict) and j.get("success")):
            raise IGError(f"upload failed ({code}): {j}")
        for _ in range(120):                                   # up to ~10 min of processing
            st = self.call("GET", c["id"], fields="status_code,status")
            if st.get("status_code") == "FINISHED":
                return c["id"]
            if st.get("status_code") in ("ERROR", "EXPIRED"):
                raise IGError(f"processing {st.get('status_code')}: {st.get('status')}")
            time.sleep(5)
        raise IGError("processing timed out")

    def publish(self, container: str) -> tuple[str, str]:
        for i in range(6):
            try:
                mid = self.call("POST", f"{self.uid}/media_publish", creation_id=container)["id"]
                break
            except IGError as e:
                if i == 5 or "9007" not in str(e) and "not ready" not in str(e).lower():
                    raise
                time.sleep(10)
        link = self.call("GET", mid, fields="permalink").get("permalink", "")
        return mid, link


class Queue:
    """Draft releases of the queue repo, via the GitHub REST API."""
    API = "https://api.github.com"

    def __init__(self, repo: str, token: str):
        self.repo, self.h = repo, {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                                   "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "reels-queue"}

    def _call(self, method, path, body=None, **params):
        code, j = _req(method, f"{self.API}/repos/{self.repo}/{path}", params=params or None, headers=self.h,
                       data=json.dumps(body).encode() if body is not None else None)
        if code >= 400:
            raise RuntimeError(f"GitHub {method} {path}: {code} {j}")
        return j

    def items(self) -> list[dict]:
        out = []
        for rel in self._call("GET", "releases", per_page=100):
            try:
                meta = json.loads(rel.get("body") or "")
            except ValueError:
                continue
            if isinstance(meta, dict) and meta.get("kind") == "reel":
                out.append({"release": rel, "meta": meta})
        return out

    def save(self, item: dict, title: str | None = None):
        rel = item["release"]
        body = {"body": json.dumps(item["meta"], indent=1, ensure_ascii=False)}
        if title:
            body["name"] = title
        item["release"] = self._call("PATCH", f"releases/{rel['id']}", body)

    def download(self, asset: dict, dest: str):
        """Assets redirect to a signed storage URL that must be fetched WITHOUT the GitHub auth header."""
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *a, **k):
                return None
        opener = urllib.request.build_opener(NoRedirect)
        req = urllib.request.Request(f"{self.API}/repos/{self.repo}/releases/assets/{asset['id']}",
                                     headers={**self.h, "Accept": "application/octet-stream"})
        try:
            resp = opener.open(req, timeout=120)
            data = resp.read()
        except urllib.error.HTTPError as e:
            if e.code not in (301, 302, 303, 307, 308):
                raise
            with urllib.request.urlopen(e.headers["Location"], timeout=600) as r2:
                data = r2.read()
        with open(dest, "wb") as fh:
            fh.write(data)

    def delete_asset(self, asset: dict):
        self._call("DELETE", f"releases/assets/{asset['id']}")


def _utc(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc)


def run(dry: bool = False, lead_min: int = 20) -> int:
    ig = Instagram(os.environ["IG_USER_ID"].strip(), os.environ["IG_ACCESS_TOKEN"].strip())
    q = Queue(os.environ["GITHUB_REPOSITORY"], os.environ["GITHUB_TOKEN"])
    me = ig.whoami()                                         # fails loudly (-> email) if the token stopped working
    print(f"Instagram @{me.get('username')} ok ({me.get('media_count')} posts)")
    now = datetime.now(timezone.utc)
    items = [i for i in q.items() if i["meta"].get("status") in ("queued", "failed")
             and i["meta"].get("attempts", 0) < MAX_ATTEMPTS]
    due = sorted((i for i in items if _utc(i["meta"]["publish_at"]) <= now + timedelta(minutes=lead_min)),
                 key=lambda i: i["meta"]["publish_at"])
    later = [i["meta"]["publish_at"] for i in items if i not in due]
    print(f"queue: {len(due)} due now, {len(later)} later {sorted(later)[:5]}")
    failures = 0
    for item in due:
        meta, rel = item["meta"], item["release"]
        name = rel.get("tag_name")
        video = next((a for a in rel.get("assets", []) if a["name"].endswith(".mp4")), None)
        print(f"- {name}: publish_at {meta['publish_at']}, video {video and video['name']} "
              f"({video and round(video['size'] / 1e6, 1)} MB)")
        if dry:
            continue
        head = meta["caption"].strip()[:80]
        if head and any(c.strip().startswith(head) for c in ig.recent_captions()):
            meta.update(status="posted", note="already on the account (not posted twice)")
            q.save(item, title=f"posted: {name}")
            print("  already posted - marked done")
            continue
        if not video:
            meta.update(status="failed", error="no .mp4 asset on the release", attempts=MAX_ATTEMPTS)
            q.save(item); failures += 1
            continue
        meta["attempts"] = meta.get("attempts", 0) + 1
        try:
            with tempfile.TemporaryDirectory() as tmp:
                path = os.path.join(tmp, video["name"])
                q.download(video, path)
                container = ig.upload_reel(path, meta["caption"], int(meta.get("thumb_offset_ms", 200)))
            wait = (_utc(meta["publish_at"]) - datetime.now(timezone.utc)).total_seconds()
            if wait > 0:
                print(f"  uploaded; waiting {wait / 60:.1f} min until the slot")
                time.sleep(wait)
            mid, link = ig.publish(container)
            meta.update(status="posted", media_id=mid, permalink=link, error=None,
                        posted_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
            q.save(item, title=f"posted: {name}")
            q.delete_asset(video)
            print(f"  posted {link}")
        except Exception as e:                               # keep going with the rest; fail the job at the end
            meta.update(status="failed", error=str(e)[:500])
            q.save(item)
            failures += 1
            print(f"  FAILED (attempt {meta['attempts']}/{MAX_ATTEMPTS}): {e}")
    return 1 if failures else 0


def next_due() -> datetime | None:
    """publish_at of the earliest reel still waiting."""
    q = Queue(os.environ["GITHUB_REPOSITORY"], os.environ["GITHUB_TOKEN"])
    ts = [_utc(i["meta"]["publish_at"]) for i in q.items() if i["meta"].get("status") in ("queued", "failed")
          and i["meta"].get("attempts", 0) < MAX_ATTEMPTS]
    return min(ts) if ts else None


def relay(hours: float, lead_min: int = 20, poll_min: int = 10) -> int:
    """Stay alive for `hours`, posting each reel when it comes due (GitHub's cron is best-effort and may never fire,
    so the workflow re-dispatches itself after this returns). Errors are logged and retried, never fatal."""
    deadline, failures = time.time() + hours * 3600, 0
    while True:
        try:
            failures += run(lead_min=lead_min)
            nxt = next_due()
        except Exception as e:
            print(f"run failed: {e}"); failures += 1; nxt = None
        left = deadline - time.time()
        if left <= 60:
            break
        wake = poll_min * 60 if nxt is None else (nxt - timedelta(minutes=lead_min) - datetime.now(timezone.utc)).total_seconds()
        wake = max(30.0, min(wake, poll_min * 6 * 60 if nxt else poll_min * 60, left))
        print(f"next reel {nxt and nxt.isoformat()}; sleeping {wake / 60:.1f} min")
        time.sleep(wake)
        if time.time() >= deadline:
            break
    return 1 if failures else 0


if __name__ == "__main__":
    dry, lead = os.getenv("DRY_RUN", "0") in ("1", "true"), int(os.getenv("LEAD_MIN", "20"))
    hours = float(os.getenv("RELAY_HOURS", "0") or 0)
    sys.exit(relay(hours, lead) if hours > 0 and not dry else run(dry=dry, lead_min=lead))
