"""
Main entry point — starts both the Telethon channel monitor and the Aiogram bot.
"""

import asyncio
import logging
import sys
import os

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.config import Config
from src.database import Database
from src.channel_monitor import ChannelMonitor
from src.ai_rewriter import AIRewriter
from src.media_processor import MediaProcessor
from src.bot import create_bot, process_new_post, auto_publish_loop

# Logging setup
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(name)-20s | %(levelname)-7s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("data/bot.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger("main")


async def main():
    """Main application entry point."""
    logger.info("=" * 60)
    logger.info("🤖 News Aggregator Bot starting...")
    logger.info("=" * 60)

    # Load config
    config = Config.from_env()
    errors = config.validate()
    if errors:
        for err in errors:
            logger.error(f"Config error: {err}")
        logger.error("Fix the .env file and restart.")
        sys.exit(1)

    logger.info(f"Config loaded: {len(config.source_channels)} source(s), {len(config.admin_ids)} admin(s)")

    # Ensure directories exist
    os.makedirs("data", exist_ok=True)
    os.makedirs(config.media_dir, exist_ok=True)

    # Initialize components
    db = Database(config.db_path)
    await db.connect()
    logger.info("Database connected")

    rewriter = AIRewriter(config)
    media_proc = MediaProcessor(
        unsplash_key=config.unsplash_access_key,
        media_dir=config.media_dir,
    )

    # Create bot
    bot, dp = create_bot(config, db, rewriter, media_proc)
    logger.info("Aiogram bot created")

    # Create channel monitor
    monitor = ChannelMonitor(config, db)

    # Register callback: when a new post is found, process it
    monitor.on_new_post(process_new_post)

    try:
        # Start Telethon client
        await monitor.start()
        logger.info("Channel monitor started")

        # Fetch recent posts from all sources (catch-up)
        for channel in config.source_channels:
            posts = await monitor.fetch_recent_posts(channel, limit=3)
            if posts:
                logger.info(f"Fetched {len(posts)} recent posts from @{channel}")

        # Start auto-publish scheduler
        publish_task = asyncio.create_task(auto_publish_loop())
        logger.info(f"Auto-publisher started (interval: {config.publish_interval}s)")

        # Run Aiogram bot (this is the main event loop)
        logger.info("Starting Aiogram polling...")
        await dp.start_polling(bot, close_bot_session=False)

    except KeyboardInterrupt:
        logger.info("Shutting down (KeyboardInterrupt)")
    except Exception as e:
        logger.error(f"Fatal error: {e}", exc_info=True)
    finally:
        publish_task.cancel()
        await monitor.stop()
        await db.close()
        await bot.session.close()
        logger.info("Bot stopped. Goodbye!")


if __name__ == "__main__":
    asyncio.run(main())
