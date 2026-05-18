import logging

RESET  = "\033[0m"

# Color map: tag → ANSI code for the whole line
TAG_COLORS = {
    "[WEBHOOK]":    "\033[1;96m",   # bold bright-cyan   — incoming events
    "[ALERT]":      "\033[1;93m",   # bold bright-yellow — alert details
    "[GRAPH]":      "\033[94m",     # bright-blue        — state machine
    "[LLM]":        "\033[95m",     # bright-magenta     — AI reasoning
    "[TOOL/ES]":    "\033[96m",     # bright-cyan        — Elasticsearch
    "[TOOL/PM]":    "\033[93m",     # bright-yellow      — Prometheus
    "[TOOL/PCF]":   "\033[92m",     # bright-green       — PCF Config API
    "[FIX]":        "\033[1;91m",   # bold bright-red    — actions applied
    "[VERIFY]":     "\033[32m",     # green (overridden per ✓/✗ below)
    "[ESCALATION]": "\033[1;91m",   # bold bright-red    — human required
    "[AGENT]":      "\033[97m",     # bright-white       — summary lines
}


class AIOpsFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        base = super().format(record)
        msg  = record.getMessage()

        for tag, color in TAG_COLORS.items():
            if tag in msg:
                if tag == "[VERIFY]":
                    color = "\033[1;92m" if "✓" in msg else "\033[1;91m"
                # Bold-color the tag, normal-color the rest of the line
                colored = base.replace(tag, f"\033[1m{color}{tag}{RESET}{color}", 1)
                return colored + RESET

        if record.levelno >= logging.ERROR:
            return f"\033[91m{base}{RESET}"
        if record.levelno >= logging.WARNING:
            return f"\033[33m{base}{RESET}"
        return base
