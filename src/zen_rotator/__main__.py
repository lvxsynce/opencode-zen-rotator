import argparse
import logging

import uvicorn

from .config import Settings, default_config
from .gateway import create_app


def main():
    parser = argparse.ArgumentParser(description="Local gateway for Zen free models")
    parser.add_argument("--config", default=str(default_config()))
    args = parser.parse_args()
    try:
        settings = Settings.load(args.config)
    except ValueError as error:
        parser.error(str(error))
    logging.basicConfig(level=logging.INFO)
    uvicorn.run(create_app(settings), host="127.0.0.1", port=settings.port, access_log=False)


if __name__ == "__main__":
    main()
