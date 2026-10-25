"""Declare all global constants."""

COMMANDS = {
    "start": "Check whether I am alive",
    "forward": "Set a new forward",
    "remove": "Remove an existing forward",
    "help": "Learn usage",
}

REGISTER_COMMANDS = True

KEEP_LAST_MANY = 10000

CONFIG_FILE_NAME = "nb.config.json"
CONFIG_ENV_VAR_NAME = "NB_CONFIG"
FAILED_FORWARD_LOG_FILE_NAME = "nb.failed.jsonl"