"""
YouTube Shorts → VK Clips fetcher.

Downloads a fresh, short, vertical clip from a curated list of local Izhevsk
YouTube channels for re-posting to VK Clips. Direct download from the server IP
(no proxy needed — datacenter proxies trip YouTube's bot check, the host IP works).

Safety filters: vertical only, < ~95s, and a sensitive-topic title blocklist so
tragedies/accidents never get re-posted. Each video id is remembered to avoid repeats.
"""
import asyncio
import json
import logging
import os
import subprocess
from typing import Optional

logger = logging.getLogger(__name__)

# Never re-post clips whose title mentions these — even from trusted channels
SENSITIVE_WORDS = [
    "стрельб", "стрелял", "погиб", "погибш", "теракт", "авари", "дтп", "суицид",
    "взрыв", "убий", "труп", "смерт", "смертельн", "жертв", "насил", "изнасил",
    "пожар", "утонул", "утоплен", "трагед", "расстрел", "нож", "зарезал",
]


class YouTubeClips:
    """Finds and downloads one fresh vertical short from approved channels."""

    def __init__(self, channels: list, seen_path: str, max_seen: int = 800,
                 max_age_days: int = 75):
        self.channels = list(channels or [])
        self.seen_path = seen_path
        self.max_seen = max_seen
        self.max_age_days = max_age_days  # skip clips older than this (avoids off-season)
        self._seen = self._load_seen()

    # ── seen-set persistence (survives restarts / redeploys via data volume) ──
    def _load_seen(self) -> set:
        try:
            with open(self.seen_path, "r", encoding="utf-8") as f:
                return set(json.load(f))
        except Exception:
            return set()

    def _save_seen(self):
        try:
            os.makedirs(os.path.dirname(self.seen_path), exist_ok=True)
            with open(self.seen_path, "w", encoding="utf-8") as f:
                json.dump(list(self._seen)[-self.max_seen:], f, ensure_ascii=False)
        except Exception as e:
            logger.warning(f"YT clips: could not save seen set: {e}")

    async def _run(self, args: list, timeout: int = 120) -> tuple:
        """Run yt-dlp (binary, with python module fallback). Returns (code, output)."""
        def _call():
            for base in (["yt-dlp"], ["python", "-m", "yt_dlp"]):
                try:
                    p = subprocess.run(
                        base + args, capture_output=True, text=True, timeout=timeout
                    )
                    return p.returncode, (p.stdout or "") + (p.stderr or "")
                except FileNotFoundError:
                    continue
                except subprocess.TimeoutExpired:
                    return 1, "timeout"
            return 1, "yt-dlp not found"
        return await asyncio.to_thread(_call)

    @staticmethod
    def _is_sensitive(title: str) -> bool:
        t = (title or "").lower()
        return any(w in t for w in SENSITIVE_WORDS)

    def _channel_url(self, ch: str) -> str:
        if ch.startswith("http"):
            return ch
        if ch.startswith("UC"):
            return f"https://www.youtube.com/channel/{ch}/shorts"
        return f"https://www.youtube.com/@{ch.lstrip('@')}/shorts"

    async def fetch_one(self, tmp_dir: str) -> Optional[dict]:
        """Return {"path","title","channel","id"} for a downloaded clip, or None."""
        import random

        # 1) Collect fresh, non-sensitive candidate ids from all channels
        candidates = []  # (id, title, channel)
        channels = list(self.channels)
        random.shuffle(channels)
        for ch in channels:
            code, out = await self._run(
                ["--flat-playlist", "--no-warnings", "--playlist-end", "20",
                 "--print", "%(id)s\x1f%(title)s\x1f%(channel)s", self._channel_url(ch)],
                timeout=90,
            )
            if code != 0:
                logger.debug(f"YT clips: list failed for {ch}")
                continue
            for line in out.splitlines():
                parts = line.split("\x1f", 2)
                if len(parts) < 2:
                    continue
                vid = parts[0].strip()
                title = parts[1].strip()
                channel = parts[2].strip() if len(parts) > 2 else ch
                if not vid or vid in self._seen:
                    continue
                if self._is_sensitive(title):
                    continue
                candidates.append((vid, title, channel))

        if not candidates:
            return None
        random.shuffle(candidates)

        # 2) Validate (vertical + short + RECENT) and download the first that fits
        from datetime import datetime, timedelta
        for vid, title, channel in candidates[:12]:
            self._seen.add(vid)  # mark seen so we don't re-evaluate next time
            meta_code, meta = await self._run(
                ["--no-warnings", "--print",
                 "%(duration)s\x1f%(width)s\x1f%(height)s\x1f%(upload_date)s\x1f%(channel)s",
                 "--simulate", f"https://www.youtube.com/watch?v={vid}"],
                timeout=60,
            )
            if meta_code != 0 or not meta.strip():
                continue
            try:
                parts = meta.strip().splitlines()[0].split("\x1f")
                dur, w, h = float(parts[0]), int(parts[1]), int(parts[2])
                upload_date = parts[3] if len(parts) > 3 else ""
                ch_name = parts[4].strip() if len(parts) > 4 else channel
            except Exception:
                continue
            if dur < 4 or dur > 95 or h <= w:   # short + vertical only
                continue
            # Recency: skip stale clips (e.g. a winter video posted in summer)
            if upload_date.isdigit() and len(upload_date) == 8:
                try:
                    age_days = (datetime.now() - datetime.strptime(upload_date, "%Y%m%d")).days
                    if age_days > self.max_age_days:
                        logger.debug(f"YT clips: skip {vid} — {age_days}d old (stale season)")
                        continue
                except Exception:
                    pass

            out_path = os.path.join(tmp_dir, f"yt_{vid}.mp4")
            dl_code, _ = await self._run(
                ["--no-warnings", "-f", "bv*[height<=1920]+ba/b[ext=mp4]/b",
                 "--merge-output-format", "mp4", "-o", out_path,
                 f"https://www.youtube.com/watch?v={vid}"],
                timeout=240,
            )
            if dl_code == 0 and os.path.exists(out_path) and os.path.getsize(out_path) > 50_000:
                self._save_seen()
                clean_ch = ch_name if ch_name and ch_name not in ("NA", "None") else ""
                logger.info(f"YT clips: downloaded {vid} ({dur:.0f}s, {w}x{h}) from {clean_ch or '?'}")
                return {"path": out_path, "title": title, "channel": clean_ch, "id": vid}

        self._save_seen()
        return None
