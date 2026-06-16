"""
Main entry point — starts both the Telethon channel monitor and the Aiogram bot.
"""

import asyncio
import logging
import logging.handlers
import sys
import os

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.config import Config
from src.database import Database
from src.channel_monitor import ChannelMonitor
from src.ai_rewriter import AIRewriter
from src.media_processor import MediaProcessor
from src.bot import create_bot, process_new_post, auto_publish_loop, vk_outreach_loop, youtube_clips_loop
from src.content_generator import ContentGenerator
from src.content_scheduler import ContentScheduler
from src.vk_publisher import VKPublisher
from src.max_publisher import MAXPublisher

# Logging setup
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(name)-20s | %(levelname)-7s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.handlers.RotatingFileHandler(
            "data/bot.log",
            maxBytes=10 * 1024 * 1024,  # 10 MB per file
            backupCount=3,
            encoding="utf-8",
        ),
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
        pixabay_key=config.pixabay_api_key,
        unsplash_key=config.unsplash_access_key,
        pexels_key=config.pexels_api_key,
        media_dir=config.media_dir,
    )

    # Create bot
    bot, dp = create_bot(config, db, rewriter, media_proc)
    logger.info("Aiogram bot created")

    # Create content generator & scheduler
    content_gen = ContentGenerator(
        config=config,
        rewriter=rewriter,
        media_processor=media_proc,
    )
    content_scheduler = ContentScheduler(
        config=config,
        bot=bot,
        generator=content_gen,
        db=db,
        rewriter=rewriter,
    )
    logger.info("Content generator & scheduler created")

    # Store scheduler in bot module for /test_content command
    import src.bot as bot_module
    bot_module._content_scheduler = content_scheduler

    # Create VK publisher
    vk_pub = VKPublisher(
        access_token=config.vk_access_token,
        group_id=config.vk_group_id,
        user_token=config.vk_user_token,
    )
    bot_module._vk_publisher = vk_pub
    if vk_pub.enabled:
        logger.info(f"VK crossposting enabled for group {config.vk_group_id}")

    # Create MAX publisher
    max_pub = MAXPublisher(
        bot_token=config.max_bot_token,
        chat_id=config.max_chat_id,
    )
    bot_module._max_publisher = max_pub
    if max_pub.enabled:
        logger.info(f"MAX crossposting enabled for chat {config.max_chat_id}")

    # Create channel monitor
    monitor = ChannelMonitor(config, db)

    # Register callback: when a new post is found, process it
    monitor.on_new_post(process_new_post)

    publish_task = None
    outreach_task = None
    ytclips_task = None
    monitor_started = False

    try:
        # Start Telethon channel monitor (uses bot token, no user session needed)
        try:
            await monitor.start()
            monitor_started = True
            logger.info("Channel monitor started (bot token + polling)")
        except Exception as e:
            logger.warning(f"⚠️ Channel monitor failed to start: {e}")
            logger.info("Bot will continue WITHOUT channel monitoring...")

        # Start auto-publish scheduler (from queue)
        publish_task = asyncio.create_task(auto_publish_loop())
        logger.info(f"Auto-publisher started (interval: {config.publish_interval}s)")

        # Start VK organic-growth outreach loop (dormant unless enabled + targets set)
        outreach_task = asyncio.create_task(vk_outreach_loop())
        if config.vk_competitor_commenting_enabled and config.vk_competitor_targets:
            logger.info(
                f"VK outreach started: {len(config.vk_competitor_targets)} targets, "
                f"{config.vk_competitor_comments_per_day}/day"
            )
        else:
            logger.info("VK outreach loop idle (disabled or no targets configured)")

        # Start YouTube Shorts → VK Clips loop (dormant unless YT_CLIPS_ENABLED)
        ytclips_task = asyncio.create_task(youtube_clips_loop())
        if config.yt_clips_enabled and config.yt_clips_channels:
            logger.info(
                f"YouTube Clips started: {len(config.yt_clips_channels)} channels, "
                f"{config.yt_clips_per_day}/day at {config.yt_clips_slots}"
            )
        else:
            logger.info("YouTube Clips loop idle (disabled)")

        # Start content scheduler (generates unique content on fixed schedule)
        await content_scheduler.start()

        # Run Aiogram bot (this is the main event loop)
        logger.info("Starting Aiogram polling...")
        while True:
            try:
                await dp.start_polling(bot, close_bot_session=False)
                break  # clean stop (KeyboardInterrupt propagated as CancelledError)
            except asyncio.CancelledError:
                break
            except Exception as poll_err:
                logger.warning(f"Polling error ({type(poll_err).__name__}): {poll_err} — restarting in 10s")
                await asyncio.sleep(10)

    except KeyboardInterrupt:
        logger.info("Shutting down (KeyboardInterrupt)")
    except Exception as e:
        logger.error(f"Fatal error: {e}", exc_info=True)
    finally:
        if publish_task:
            publish_task.cancel()
        if outreach_task:
            outreach_task.cancel()
        if ytclips_task:
            ytclips_task.cancel()
        await content_scheduler.stop()
        if monitor_started:
            await monitor.stop()
        await db.close()
        await vk_pub.close()                             # close aiohttp session → no "Unclosed" warning
        if hasattr(max_pub, 'close'):
            await max_pub.close()
        await bot.session.close()
        logger.info("Bot stopped. Goodbye!")


if __name__ == "__main__":
    asyncio.run(main())
