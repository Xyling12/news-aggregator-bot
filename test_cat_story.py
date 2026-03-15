import asyncio
import logging
from src.story_generator import StoryGenerator

logging.basicConfig(level=logging.INFO)

async def test():
    sg = StoryGenerator()
    cat_bytes = await sg.generate_cat_story()
    if cat_bytes:
        with open("test_cat_story.jpg", "wb") as f:
            f.write(cat_bytes)
        print("Done. Saved to test_cat_story.jpg")
    else:
        print("Failed.")

if __name__ == "__main__":
    asyncio.run(test())
