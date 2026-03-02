"""
Media Processor — handles watermark detection, media downloading, and stock photo search.
"""

import logging
import os
from typing import Optional, List, Tuple

import aiohttp
import numpy as np
from PIL import Image

logger = logging.getLogger(__name__)


class MediaProcessor:
    """Processes media files: watermark detection and stock photo search."""

    def __init__(self, unsplash_key: str = "", media_dir: str = "media"):
        self.unsplash_key = unsplash_key
        self.media_dir = media_dir
        os.makedirs(media_dir, exist_ok=True)

    def detect_watermark(self, image_path: str) -> Tuple[bool, float]:
        """
        Simple watermark detection using corner analysis.
        Checks if corners have semi-transparent overlays typical of watermarks.
        
        Returns:
            Tuple of (has_watermark: bool, confidence: float 0.0-1.0)
        """
        try:
            import cv2

            img = cv2.imread(image_path)
            if img is None:
                return False, 0.0

            h, w = img.shape[:2]
            gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)

            # Check corners for typical watermark patterns
            corner_size = min(h, w) // 5
            corners = [
                gray[:corner_size, :corner_size],          # top-left
                gray[:corner_size, w - corner_size:],       # top-right
                gray[h - corner_size:, :corner_size],       # bottom-left
                gray[h - corner_size:, w - corner_size:],   # bottom-right
            ]

            # Calculate variance in corners - low variance + high brightness = potential watermark
            watermark_indicators = 0
            for corner in corners:
                mean_val = np.mean(corner)
                std_val = np.std(corner)
                # Watermarks often show as semi-transparent overlays
                # They tend to have consistent patterns (low std relative to mean)
                if std_val < 30 and mean_val > 200:
                    watermark_indicators += 1

            # Check for text-like patterns using edge detection
            edges = cv2.Canny(gray, 100, 200)
            bottom_strip = edges[int(h * 0.85):, :]
            edge_density_bottom = np.mean(bottom_strip) / 255.0

            # Many watermarks are in the bottom portion
            if edge_density_bottom > 0.15:
                watermark_indicators += 1

            has_watermark = watermark_indicators >= 2
            confidence = min(watermark_indicators / 4.0, 1.0)

            logger.info(f"Watermark detection for {image_path}: "
                       f"detected={has_watermark}, confidence={confidence:.2f}")
            return has_watermark, confidence

        except ImportError:
            logger.warning("OpenCV not available, skipping watermark detection")
            return False, 0.0
        except Exception as e:
            logger.error(f"Watermark detection failed: {e}")
            return False, 0.0

    async def search_stock_photo(self, keywords: List[str], count: int = 3) -> List[dict]:
        """
        Search for stock photos using Unsplash API.
        
        Returns:
            List of dicts with 'url', 'thumb_url', 'description', 'author'
        """
        if not self.unsplash_key:
            logger.warning("Unsplash API key not configured")
            return []

        query = " ".join(keywords[:3])
        results = []

        try:
            async with aiohttp.ClientSession() as session:
                url = "https://api.unsplash.com/search/photos"
                params = {
                    "query": query,
                    "per_page": count,
                    "orientation": "landscape",
                }
                headers = {
                    "Authorization": f"Client-ID {self.unsplash_key}",
                }

                async with session.get(url, params=params, headers=headers) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        for photo in data.get("results", []):
                            results.append({
                                "url": photo["urls"]["regular"],
                                "thumb_url": photo["urls"]["thumb"],
                                "description": photo.get("description", ""),
                                "author": photo["user"]["name"],
                                "unsplash_link": photo["links"]["html"],
                            })
                        logger.info(f"Found {len(results)} stock photos for '{query}'")
                    else:
                        error = await resp.text()
                        logger.error(f"Unsplash API returned {resp.status}: {error}")

        except Exception as e:
            logger.error(f"Stock photo search failed: {e}")

        return results

    async def download_stock_photo(self, photo_url: str, filename: str) -> Optional[str]:
        """Download a stock photo and save it locally."""
        try:
            filepath = os.path.join(self.media_dir, filename)
            async with aiohttp.ClientSession() as session:
                async with session.get(photo_url) as resp:
                    if resp.status == 200:
                        content = await resp.read()
                        with open(filepath, "wb") as f:
                            f.write(content)
                        logger.info(f"Downloaded stock photo: {filepath}")
                        return filepath
                    else:
                        logger.error(f"Failed to download photo: {resp.status}")
        except Exception as e:
            logger.error(f"Photo download failed: {e}")
        return None

    def resize_for_telegram(self, image_path: str, max_size: int = 1280) -> str:
        """Resize image to fit Telegram's limits (max 1280px on longest side)."""
        try:
            img = Image.open(image_path)
            w, h = img.size

            if max(w, h) > max_size:
                ratio = max_size / max(w, h)
                new_size = (int(w * ratio), int(h * ratio))
                img = img.resize(new_size, Image.LANCZOS)

                # Save resized
                resized_path = image_path.rsplit(".", 1)
                resized_path = f"{resized_path[0]}_resized.{resized_path[1]}" if len(resized_path) > 1 else f"{image_path}_resized"
                img.save(resized_path, quality=90)
                logger.info(f"Resized image: {w}x{h} -> {new_size[0]}x{new_size[1]}")
                return resized_path

            return image_path
        except Exception as e:
            logger.error(f"Image resize failed: {e}")
            return image_path
