"""Loads config.yaml and .env, and sets up logging. Everything else imports from here."""
import logging
import os
import yaml
from dotenv import load_dotenv

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

load_dotenv(os.path.join(PROJECT_ROOT, ".env"))


def load_config():
    """Read config.yaml from the project root and return it as a plain dict."""
    path = os.path.join(PROJECT_ROOT, "config.yaml")
    with open(path) as f:
        return yaml.safe_load(f)


def get_env(name, default=None):
    """Return an environment variable (loaded from .env), or default if unset/empty."""
    value = os.environ.get(name)
    if value is None or value == "":
        return default
    return value


def project_path(relative_path):
    """Turn a path relative to the project root into an absolute path."""
    return os.path.join(PROJECT_ROOT, relative_path)


def setup_logging(log_file=None):
    """Send log messages to the console, and also to log_file if given."""
    handlers = [logging.StreamHandler()]
    if log_file:
        os.makedirs(os.path.dirname(log_file), exist_ok=True)
        handlers.append(logging.FileHandler(log_file))
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=handlers,
        force=True,
    )


if __name__ == "__main__":
    config = load_config()
    print("Config loaded. Themes:")
    for theme in config["universe"]["themes"]:
        print(f"  - {theme['name']}")
    print("SEC_USER_AGENT set:", get_env("SEC_USER_AGENT") is not None)
