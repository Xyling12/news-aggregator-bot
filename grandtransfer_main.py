"""
Entry point for the grandtransfer-bot container (VK content for vk.com/grandtransfer).
Run as: python grandtransfer_main.py
"""

import asyncio
from src.transfer_scheduler import main

if __name__ == "__main__":
    asyncio.run(main())

