"""Anaconda 2026 brand colors, typography and Material UI theme."""

from __future__ import annotations

from pathlib import Path

ASSETS = Path(__file__).parent / "assets"
LOGO = ASSETS / "anaconda-logo.png"
FAVICON = ASSETS / "favicon.png"

GREEN = {
    50: "#F3FFE0", 100: "#DEFFAA", 200: "#C1FF60", 300: "#9CF215", 400: "#5FE419",
    500: "#08CA4A", 600: "#07AB41", 700: "#068F35", 800: "#056B29", 900: "#035420", 950: "#023E18",
}  # fmt: skip
LILAC = {
    50: "#E5E1FF", 100: "#CCC6FF", 200: "#B1A6FF", 300: "#A296FE", 400: "#9183FF",
    500: "#6D5BF6", 600: "#594AC8", 700: "#443998", 800: "#2E2667", 900: "#1F1945", 950: "#171236",
}  # fmt: skip
NEUTRAL = {
    50: "#F7F8F9", 100: "#E4E6EB", 200: "#D5D8DF", 300: "#C9CCD5", 400: "#B8BDC8",
    500: "#A6ABB7", 600: "#898D96", 700: "#6A6D74", 800: "#4B4D52", 900: "#2C2D30", 950: "#0C0C0C",
}  # fmt: skip
ERROR = "#D00100"

HEADING_FONT = '"Space Grotesk", Arial, sans-serif'
BODY_FONT = '"Inter", Arial, sans-serif'
MONO_FONT = '"Space Mono", monospace'

#: All three families are free on Google Fonts; Arial applies when they are missing.
FONTS_CSS = (
    "https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700"
    "&family=Space+Grotesk:wght@500;600;700&family=Space+Mono:wght@400;700&display=swap"
)

_HEADINGS = {
    "fontFamily": HEADING_FONT,
    "fontWeight": 500,
    "letterSpacing": "-0.02em",
    "lineHeight": 1.35,
}


def _mode(dark: bool) -> dict:
    # White text never goes on Green 500, and Neutral 950 fails on Lilac 500, so
    # each palette color carries an explicit contrast text color.
    if dark:
        palette = {
            "primary": {"main": LILAC[300], "contrastText": NEUTRAL[950]},
            "secondary": {"main": GREEN[500], "contrastText": NEUTRAL[950]},
            "error": {"main": "#F2504F", "contrastText": NEUTRAL[950]},
            "success": {"main": GREEN[500], "contrastText": NEUTRAL[950]},
            "background": {"default": LILAC[900], "paper": LILAC[800]},
            "text": {"primary": "#FFFFFF", "secondary": NEUTRAL[400]},
            "divider": LILAC[700],
        }
        bar = LILAC[950]
    else:
        palette = {
            "primary": {"main": LILAC[500], "contrastText": "#FFFFFF"},
            "secondary": {"main": GREEN[500], "contrastText": NEUTRAL[950]},
            "error": {"main": ERROR, "contrastText": "#FFFFFF"},
            "success": {"main": GREEN[800], "contrastText": "#FFFFFF"},
            "background": {"default": "#FFFFFF", "paper": "#FFFFFF"},
            "text": {"primary": NEUTRAL[950], "secondary": NEUTRAL[700]},
            "divider": NEUTRAL[300],
        }
        bar = LILAC[900]
    return {
        "palette": palette,
        "shape": {"borderRadius": 8},
        "typography": {
            "fontFamily": BODY_FONT,
            "fontSize": 14,
            "body1": {"fontSize": "1rem", "lineHeight": 1.7},
            "body2": {"fontSize": "0.875rem", "lineHeight": 1.7},
            **{f"h{i}": dict(_HEADINGS) for i in range(1, 7)},
            "overline": {
                "fontFamily": HEADING_FONT,
                "fontWeight": 500,
                "letterSpacing": "0.15em",
                "lineHeight": 1.5,
            },
            "button": {"fontFamily": BODY_FONT, "textTransform": "none", "fontWeight": 600},
        },
        "components": {
            # The header is dark in both modes so the green logo keeps its contrast.
            "MuiAppBar": {
                "styleOverrides": {
                    "root": {"backgroundColor": bar, "color": "#FFFFFF", "backgroundImage": "none"}
                }
            },
            "MuiPaper": {"styleOverrides": {"root": {"backgroundImage": "none"}}},
        },
    }


#: ``theme_config`` for ``pmui.Page`` and other Material UI components.
THEME_CONFIG = {"light": _mode(False), "dark": _mode(True)}

#: Code is set in Space Mono; Markdown panes and tabulator cells inherit it.
RAW_CSS = f"code, pre, kbd {{ font-family: {MONO_FONT}; }}"

#: Page-level typography for the standalone HTML export, which has no Material UI shell.
STANDALONE_CSS = (
    RAW_CSS
    + f" body {{ font-family: {BODY_FONT}; color: {NEUTRAL[950]}; }}"
    + f" h1, h2, h3, h4 {{ font-family: {HEADING_FONT};"
    + " font-weight: 500; letter-spacing: -0.02em; }"
)
