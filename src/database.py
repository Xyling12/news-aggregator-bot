"""
Database module — SQLite async operations for posts queue, sources, and settings.
"""

import aiosqlite
import os
from datetime import datetime
from typing import Optional, List, Dict, Any


class Database:
    """Async SQLite database for managing posts, sources, and settings."""

    def __init__(self, db_path: str = "data/bot.db"):
        self.db_path = db_path
        self._db: Optional[aiosqlite.Connection] = None

    async def connect(self):
        """Connect to the database and create tables if they don't exist."""
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        self._db = await aiosqlite.connect(self.db_path)
        self._db.row_factory = aiosqlite.Row
        await self._create_tables()

    async def close(self):
        """Close the database connection."""
        if self._db:
            await self._db.close()

    async def _create_tables(self):
        """Create all necessary tables."""
        await self._db.executescript("""
            CREATE TABLE IF NOT EXISTS sources (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                channel_username TEXT UNIQUE NOT NULL,
                display_name TEXT,
                is_active INTEGER DEFAULT 1,
                added_at TEXT DEFAULT (datetime('now')),
                last_message_id INTEGER DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS posts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_channel TEXT NOT NULL,
                source_message_id INTEGER NOT NULL,
                original_text TEXT NOT NULL,
                rewritten_text TEXT,
                status TEXT DEFAULT 'pending',  -- pending, rewriting, review, approved, rejected, published
                media_type TEXT,                -- photo, video, document, none
                media_file_id TEXT,             -- Telegram file_id of original media
                media_local_path TEXT,          -- Local path to downloaded media
                has_watermark INTEGER DEFAULT 0,
                replacement_media_url TEXT,     -- URL from stock photo API
                created_at TEXT DEFAULT (datetime('now')),
                reviewed_at TEXT,
                published_at TEXT,
                reviewed_by INTEGER,
                UNIQUE(source_channel, source_message_id)
            );

            CREATE TABLE IF NOT EXISTS published (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                post_id INTEGER NOT NULL,
                channel_message_id INTEGER,
                published_at TEXT DEFAULT (datetime('now')),
                FOREIGN KEY (post_id) REFERENCES posts(id)
            );

            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS generated_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                rubric TEXT NOT NULL,
                topic TEXT NOT NULL,
                created_at TEXT DEFAULT (datetime('now'))
            );

            CREATE INDEX IF NOT EXISTS idx_posts_status ON posts(status);
            CREATE INDEX IF NOT EXISTS idx_posts_source ON posts(source_channel);
            CREATE INDEX IF NOT EXISTS idx_posts_status_created ON posts(status, created_at);
            CREATE INDEX IF NOT EXISTS idx_posts_created ON posts(created_at);
            CREATE INDEX IF NOT EXISTS idx_generated_history_rubric ON generated_history(rubric);
        """)
        await self._db.commit()

    # ── Sources ──────────────────────────────────────────────────────────

    async def add_source(self, channel_username: str, display_name: str = None) -> int:
        """Add a source channel. Returns the source ID."""
        cursor = await self._db.execute(
            "INSERT OR IGNORE INTO sources (channel_username, display_name) VALUES (?, ?)",
            (channel_username, display_name or channel_username),
        )
        await self._db.commit()
        return cursor.lastrowid

    async def remove_source(self, channel_username: str):
        """Remove a source channel."""
        await self._db.execute(
            "UPDATE sources SET is_active = 0 WHERE channel_username = ?",
            (channel_username,),
        )
        await self._db.commit()

    async def get_active_sources(self) -> List[Dict[str, Any]]:
        """Get all active source channels."""
        cursor = await self._db.execute(
            "SELECT * FROM sources WHERE is_active = 1"
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def update_last_message_id(self, channel_username: str, message_id: int):
        """Update the last processed message ID for a source channel."""
        await self._db.execute(
            "UPDATE sources SET last_message_id = ? WHERE channel_username = ?",
            (message_id, channel_username),
        )
        await self._db.commit()

    async def get_last_message_id(self, channel_username: str) -> int:
        """Get the last processed message ID for a source channel."""
        cursor = await self._db.execute(
            "SELECT last_message_id FROM sources WHERE channel_username = ?",
            (channel_username,),
        )
        row = await cursor.fetchone()
        return row["last_message_id"] if row else 0

    # ── Posts ─────────────────────────────────────────────────────────────

    async def add_post(
        self,
        source_channel: str,
        source_message_id: int,
        original_text: str,
        media_type: str = "none",
        media_file_id: str = None,
        media_local_path: str = None,
    ) -> Optional[int]:
        """Add a new post to the queue. Returns post ID or None if duplicate."""
        try:
            cursor = await self._db.execute(
                """INSERT INTO posts 
                   (source_channel, source_message_id, original_text, media_type, media_file_id, media_local_path)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (source_channel, source_message_id, original_text, media_type, media_file_id, media_local_path),
            )
            await self._db.commit()
            return cursor.lastrowid
        except aiosqlite.IntegrityError:
            return None  # Duplicate post

    async def update_post_rewrite(self, post_id: int, rewritten_text: str):
        """Update the rewritten text for a post."""
        await self._db.execute(
            "UPDATE posts SET rewritten_text = ?, status = 'review' WHERE id = ?",
            (rewritten_text, post_id),
        )
        await self._db.commit()

    async def update_post_status(self, post_id: int, status: str, reviewed_by: int = None, rewritten_text: str = None):
        """Update the status of a post. Optionally save rewritten_text (used to store rejection reason)."""
        if status in ("approved", "rejected"):
            if rewritten_text is not None:
                await self._db.execute(
                    "UPDATE posts SET status = ?, reviewed_at = datetime('now'), reviewed_by = ?, rewritten_text = ? WHERE id = ?",
                    (status, reviewed_by, rewritten_text, post_id),
                )
            else:
                await self._db.execute(
                    "UPDATE posts SET status = ?, reviewed_at = datetime('now'), reviewed_by = ? WHERE id = ?",
                    (status, reviewed_by, post_id),
                )
        elif status == "published":
            await self._db.execute(
                "UPDATE posts SET status = ?, published_at = datetime('now') WHERE id = ?",
                (status, post_id),
            )
        else:
            await self._db.execute(
                "UPDATE posts SET status = ? WHERE id = ?",
                (status, post_id),
            )
        await self._db.commit()

    async def update_post_text(self, post_id: int, new_text: str):
        """Update the rewritten text (after admin edit)."""
        await self._db.execute(
            "UPDATE posts SET rewritten_text = ? WHERE id = ?",
            (new_text, post_id),
        )
        await self._db.commit()

    async def update_post_media(self, post_id: int, has_watermark: bool = False, replacement_url: str = None):
        """Update media info for a post."""
        await self._db.execute(
            "UPDATE posts SET has_watermark = ?, replacement_media_url = ? WHERE id = ?",
            (1 if has_watermark else 0, replacement_url, post_id),
        )
        await self._db.commit()

    async def get_post(self, post_id: int) -> Optional[Dict[str, Any]]:
        """Get a single post by ID."""
        cursor = await self._db.execute("SELECT * FROM posts WHERE id = ?", (post_id,))
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def get_posts_by_status(self, status: str, limit: int = 20) -> List[Dict[str, Any]]:
        """Get posts by status."""
        cursor = await self._db.execute(
            "SELECT * FROM posts WHERE status = ? ORDER BY created_at DESC LIMIT ?",
            (status, limit),
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def get_pending_posts(self, limit: int = 10) -> List[Dict[str, Any]]:
        """Get posts pending AI rewrite."""
        return await self.get_posts_by_status("pending", limit)

    async def get_review_posts(self, limit: int = 20) -> List[Dict[str, Any]]:
        """Get posts ready for admin review."""
        return await self.get_posts_by_status("review", limit)

    async def get_approved_posts(self, limit: int = 10) -> List[Dict[str, Any]]:
        """Get approved posts ready for publishing."""
        return await self.get_posts_by_status("approved", limit)

    async def get_oldest_approved_post(self) -> Optional[Dict[str, Any]]:
        """Get the single oldest approved post for scheduled publishing."""
        cursor = await self._db.execute(
            "SELECT * FROM posts WHERE status = 'approved' ORDER BY created_at ASC LIMIT 1"
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def get_stats(self) -> Dict[str, int]:
        """Get post statistics."""
        stats = {}
        for status in ("pending", "rewriting", "review", "approved", "rejected", "published"):
            cursor = await self._db.execute(
                "SELECT COUNT(*) as count FROM posts WHERE status = ?", (status,)
            )
            row = await cursor.fetchone()
            stats[status] = row["count"]
        return stats

    async def get_recent_texts(self, hours: int = 24, limit: int = 100) -> List[str]:
        """Get original texts of recent posts for deduplication."""
        cursor = await self._db.execute(
            """SELECT original_text FROM posts 
               WHERE created_at > datetime('now', ?) AND status != 'rejected'
               ORDER BY created_at DESC LIMIT ?""",
            (f"-{hours} hours", limit),
        )
        rows = await cursor.fetchall()
        return [row["original_text"] for row in rows]

    async def get_texts_by_status(self, statuses: List[str], hours: int = 48, limit: int = 100) -> List[str]:
        """Get original texts of posts with specified statuses within a time window.
        Used for deduplication of incoming posts against queue and published posts.
        """
        placeholders = ",".join("?" * len(statuses))
        cursor = await self._db.execute(
            f"""SELECT original_text FROM posts
               WHERE status IN ({placeholders})
               AND created_at > datetime('now', ?)
               ORDER BY created_at DESC LIMIT ?""",
            (*statuses, f"-{hours} hours", limit),
        )
        rows = await cursor.fetchall()
        return [row["original_text"] for row in rows]

    async def get_rewritten_texts_by_status(self, statuses: List[str], hours: int = 48, limit: int = 100) -> List[str]:
        """Get rewritten texts of posts with specified statuses within a time window.
        Used for second-level deduplication after AI rewrite — catches same news rewritten differently.
        """
        placeholders = ",".join("?" * len(statuses))
        cursor = await self._db.execute(
            f"""SELECT rewritten_text FROM posts
               WHERE status IN ({placeholders})
               AND rewritten_text IS NOT NULL
               AND created_at > datetime('now', ?)
               ORDER BY created_at DESC LIMIT ?""",
            (*statuses, f"-{hours} hours", limit),
        )
        rows = await cursor.fetchall()
        return [row["rewritten_text"] for row in rows]


    # ── Published ────────────────────────────────────────────────────────

    async def add_published(self, post_id: int, channel_message_id: int):
        """Record a published post."""
        await self._db.execute(
            "INSERT INTO published (post_id, channel_message_id) VALUES (?, ?)",
            (post_id, channel_message_id),
        )
        await self._db.commit()

    async def get_today_published_texts(self) -> List[str]:
        """Get rewritten texts of posts published today (for daily digest)."""
        cursor = await self._db.execute(
            """SELECT p.rewritten_text FROM posts p
               JOIN published pub ON pub.post_id = p.id
               WHERE pub.published_at > datetime('now', '-16 hours')
               AND p.rewritten_text IS NOT NULL
               ORDER BY pub.published_at DESC"""
        )
        rows = await cursor.fetchall()
        return [row["rewritten_text"] for row in rows]

    async def has_recent_topic_post(self, keywords: List[str], hours: int = 4) -> bool:
        """Return True if any published/approved/queued post in the last N hours
        contains at least one of the given keywords in its original or rewritten text.
        Used for topic-based cooldowns (e.g. weather: no more than once per 4 hours).
        """
        placeholders = " OR ".join(
            ["(original_text LIKE ? OR rewritten_text LIKE ?)"] * len(keywords)
        )
        params = []
        for kw in keywords:
            like = f"%{kw}%"
            params += [like, like]
        params.append(f"-{hours} hours")
        cursor = await self._db.execute(
            f"""SELECT 1 FROM posts
               WHERE ({placeholders})
               AND status IN ('published', 'approved', 'pending', 'rewriting')
               AND created_at > datetime('now', ?)
               LIMIT 1""",
            params,
        )
        row = await cursor.fetchone()
        return row is not None

    # ── Settings ─────────────────────────────────────────────────────────

    async def get_setting(self, key: str, default: str = None) -> Optional[str]:
        """Get a setting value."""
        cursor = await self._db.execute(
            "SELECT value FROM settings WHERE key = ?", (key,)
        )
        row = await cursor.fetchone()
        return row["value"] if row else default

    async def set_setting(self, key: str, value: str):
        """Set a setting value."""
        await self._db.execute(
            "INSERT OR REPLACE INTO settings (key, value, updated_at) VALUES (?, ?, datetime('now'))",
            (key, value),
        )
        await self._db.commit()

    # ── Analytics ─────────────────────────────────────────────────────────

    async def get_weekly_stats(self) -> Dict[str, Any]:
        """Get weekly statistics for the analytics report."""
        result = {"total": 0, "approved": 0, "rejected": 0, "published": 0, "by_source": {}}

        # Total posts this week
        cursor = await self._db.execute(
            "SELECT COUNT(*) as count FROM posts WHERE created_at > datetime('now', '-7 days')"
        )
        row = await cursor.fetchone()
        result["total"] = row["count"]

        # By status
        for status in ("approved", "rejected", "published"):
            cursor = await self._db.execute(
                "SELECT COUNT(*) as count FROM posts WHERE status = ? AND created_at > datetime('now', '-7 days')",
                (status,),
            )
            row = await cursor.fetchone()
            result[status] = row["count"]

        # By source channel
        cursor = await self._db.execute(
            """SELECT source_channel, COUNT(*) as count FROM posts 
               WHERE created_at > datetime('now', '-7 days')
               GROUP BY source_channel ORDER BY count DESC"""
        )
        rows = await cursor.fetchall()
        result["by_source"] = {row["source_channel"]: row["count"] for row in rows}

        return result

    # ── Generated History ────────────────────────────────────────────────

    async def add_generated_history(self, rubric: str, topic: str):
        """Record a generated topic to prevent repetition."""
        await self._db.execute(
            "INSERT INTO generated_history (rubric, topic) VALUES (?, ?)",
            (rubric, topic),
        )
        await self._db.commit()

    async def get_recent_generated_topics(self, rubric: str, limit: int = 60) -> List[str]:
        """Get recently generated topics for a rubric (e.g. last 60 times = 2 months)."""
        cursor = await self._db.execute(
            "SELECT topic FROM generated_history WHERE rubric = ? ORDER BY created_at DESC LIMIT ?",
            (rubric, limit),
        )
        rows = await cursor.fetchall()
        return [row["topic"] for row in rows]
