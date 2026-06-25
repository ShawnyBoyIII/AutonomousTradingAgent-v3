from dotenv import load_dotenv

from trading_bot.cli.app import app


def main() -> None:
    load_dotenv()
    app()


if __name__ == "__main__":
    main()
