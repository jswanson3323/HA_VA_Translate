"""Constants for the Fallback Conversation Agent integration."""

DOMAIN = "fallback_conversation"

CONF_DEBUG_LEVEL = 'debug_level'
CONF_PRIMARY_AGENT = 'primary_agent'
CONF_FALLBACK_AGENT = 'fallback_agent'
CONF_ENABLE_DIALOG_BYPASS = "enable_dialog_bypass"
CONF_DIALOG_BYPASS_MIN_SCORE = "dialog_bypass_min_score"
CONF_DIALOG_YAML_PATH = "dialog_yaml_path"
CONF_INCLUDE_CONVERSATION_TRIGGER_SCAN = "include_conversation_trigger_scan"

DEBUG_LEVEL_NO_DEBUG = "none"
DEBUG_LEVEL_LOW_DEBUG = "low"
DEBUG_LEVEL_VERBOSE_DEBUG = "verbose"

DEFAULT_NAME = "Fallback Conversation Agent"
DEFAULT_DEBUG_LEVEL = DEBUG_LEVEL_NO_DEBUG
DEFAULT_ENABLE_DIALOG_BYPASS = True
DEFAULT_DIALOG_BYPASS_MIN_SCORE = 0.60
DEFAULT_DIALOG_YAML_PATH = "/config/fallback_conversation/dialog_phrases.yaml"
DEFAULT_INCLUDE_CONVERSATION_TRIGGER_SCAN = False

SERVICE_REBUILD_DIALOG_CATALOG = "rebuild_dialog_catalog"

STRANGE_ERROR_RESPONSES = [
    "not any",
    "geen",
]
