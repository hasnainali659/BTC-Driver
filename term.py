"""
Terminal color helpers — Windows-safe.

Strategy:
  1. If colorama is installed, use it (handles every Windows console).
  2. Otherwise on Windows, nudge the console into VT mode (works on
     Windows 10+ cmd/PowerShell/Windows Terminal).
  3. Colors auto-disable when output is piped/redirected or NO_COLOR is set.

Usage:
    from term import paint, score_color, BOLD, GREEN, RED, YELLOW, CYAN, DIM
    print(paint("BULLISH", GREEN, bold=True))
    print(paint(f"{score:+.2f}", score_color(score)))
"""
import os
import sys

RESET = "\033[0m"
BOLD_CODE = "\033[1m"
DIM = "\033[2m"
RED = "\033[31m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
BLUE = "\033[34m"
MAGENTA = "\033[35m"
CYAN = "\033[36m"
WHITE = "\033[37m"
BOLD = BOLD_CODE  # alias

_enabled = None


def _detect() -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    if not hasattr(sys.stdout, "isatty") or not sys.stdout.isatty():
        return False
    if os.name == "nt":
        try:
            import colorama
            colorama.just_fix_windows_console()
            return True
        except ImportError:
            pass
        # Windows 10+ supports ANSI once VT processing is enabled.
        # os.system('') is the classic trick that flips it on.
        os.system("")
    return True


def colors_enabled() -> bool:
    global _enabled
    if _enabled is None:
        _enabled = _detect()
    return _enabled


def paint(text, color: str = "", bold: bool = False, dim: bool = False) -> str:
    """Wrap text in ANSI codes (no-op when colors are disabled)."""
    text = str(text)
    if not colors_enabled():
        return text
    prefix = color
    if bold:
        prefix += BOLD_CODE
    if dim:
        prefix += DIM
    if not prefix:
        return text
    return f"{prefix}{text}{RESET}"


def score_color(score: float, neutral_band: float = 0.05) -> str:
    """Green for bullish, red for bearish, dim-less neutral."""
    if score > neutral_band:
        return GREEN
    if score < -neutral_band:
        return RED
    return DIM


def direction_color(direction: str) -> str:
    d = str(direction).lower()
    if d == "bullish":
        return GREEN
    if d == "bearish":
        return RED
    if d == "neutral":
        return YELLOW
    return DIM


def confidence_color(conf: str) -> str:
    c = str(conf).lower()
    if c == "high":
        return GREEN
    if c == "medium":
        return YELLOW
    return DIM


def pct_color(value: float, good_above: float, ok_above: float) -> str:
    """Color a metric: green above good, yellow above ok, red below."""
    if value >= good_above:
        return GREEN
    if value >= ok_above:
        return YELLOW
    return RED
