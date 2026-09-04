import logging

from dotenv import load_dotenv

from ocean_helper_bot.bot import run
from ocean_helper_bot.config import Settings


def main() -> None:
    load_dotenv()
    logging.basicConfig(
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        level=logging.INFO,
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    run(Settings.from_environment())


if __name__ == "__main__":
    main()
