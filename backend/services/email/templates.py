"""Shared formal HTML/plain-text shell for every transactional AIVA email.

Email clients strip most CSS, so everything here is table-based with inline
styles only. Remote images are frequently blocked by default, so the layout
must stay intact (and the brand still readable) when they do not load.
"""

from __future__ import annotations

import html

from backend.config import get_settings

# Navy from the GoChat247 / AIVA marks; the lighter blue matches the app's --primary.
BRAND_NAVY = "#1b4b82"
BRAND_BLUE = "#0070c7"
INK = "#1f2937"
MUTED = "#64748b"
BORDER = "#e2e8f0"
CANVAS = "#f4f6f8"

_FONT = "Arial, 'Helvetica Neue', Helvetica, sans-serif"
_MONO = "'SFMono-Regular', Consolas, 'Liberation Mono', Menlo, monospace"

# Served from the frontend's public/ folder (Vite copies it to the site root).
_GOCHAT_LOGO_FILE = "GoChat247_blue_transparent.png"
_GOAI_LOGO_FILE = "GoAI_logo.png"


def _gochat_logo_url() -> str:
    settings = get_settings()
    configured = (settings.email_logo_url or "").strip()
    if configured:
        return configured
    return f"{settings.frontend_url.rstrip('/')}/{_GOCHAT_LOGO_FILE}"


def _goai_logo_url() -> str:
    settings = get_settings()
    configured = (settings.email_footer_logo_url or "").strip()
    if configured:
        return configured
    return f"{settings.frontend_url.rstrip('/')}/{_GOAI_LOGO_FILE}"


def _aiva_logo_url() -> str:
    return (get_settings().email_aiva_logo_url or "").strip()


def _aiva_wordmark_html() -> str:
    """The AIVA mark: the supplied artwork when configured, else a text wordmark.

    The text fallback is deliberate — it is the same navy wordmark, but it
    survives image blocking and stays crisp on high-DPI screens.
    """
    url = _aiva_logo_url()
    if url:
        return (
            f'<img src="{html.escape(url, quote=True)}" alt="AIVA" height="26" '
            f'style="display:block;height:26px;width:auto;border:0;outline:none;'
            f'text-decoration:none;">'
        )
    return (
        f'<span style="font-family:{_FONT};font-size:25px;line-height:26px;'
        f'font-weight:bold;letter-spacing:2px;color:{BRAND_NAVY};">AIVA</span>'
    )


def _header_html(eyebrow: str | None) -> str:
    logo = _gochat_logo_url()
    eyebrow_cell = ""
    if eyebrow:
        eyebrow_cell = (
            f'<td align="right" valign="middle" style="font-family:{_FONT};font-size:11px;'
            f'line-height:16px;letter-spacing:1.5px;text-transform:uppercase;color:{MUTED};">'
            f"{html.escape(eyebrow)}</td>"
        )
    return f"""\
<tr>
<td style="padding:24px 32px;border-bottom:3px solid {BRAND_NAVY};">
<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%">
<tr>
<td valign="middle" style="width:52px;padding-right:14px;">
<img src="{html.escape(logo, quote=True)}" width="48" height="48" alt="GoChat247"
style="display:block;width:48px;height:48px;border:0;outline:none;text-decoration:none;
font-family:{_FONT};font-size:10px;line-height:14px;color:{BRAND_NAVY};">
</td>
<td valign="middle" style="border-left:1px solid {BORDER};padding-left:12px;">
{_aiva_wordmark_html()}
</td>
{eyebrow_cell}
</tr>
</table>
</td>
</tr>"""


def _footer_html(footer_note: str | None) -> str:
    extra = ""
    if footer_note:
        extra = (
            f'<p style="margin:0 0 8px;font-family:{_FONT};font-size:12px;'
            f'line-height:18px;color:{MUTED};">{html.escape(footer_note)}</p>'
        )
    goai = _goai_logo_url()
    return f"""\
<tr>
<td align="center" style="padding:24px 32px 28px;border-top:1px solid {BORDER};background-color:#fafbfc;">
<img src="{html.escape(goai, quote=True)}" width="170" alt="GoAI &mdash; Elevate your business with AI"
style="display:block;width:170px;height:auto;margin:0 auto 14px;border:0;outline:none;
text-decoration:none;font-family:{_FONT};font-size:11px;line-height:16px;color:{MUTED};">
{extra}
<p style="margin:0 0 8px;font-family:{_FONT};font-size:12px;line-height:18px;color:{MUTED};">
This is an automated message from AIVA. Please do not reply to this email.
</p>
<p style="margin:0;font-family:{_FONT};font-size:12px;line-height:18px;color:{MUTED};">
AIVA &mdash; a <strong style="color:{BRAND_NAVY};">GoChat247</strong> product.
&copy; GoChat247. All rights reserved.
</p>
</td>
</tr>"""


def _details_html(details: list[tuple[str, str]]) -> str:
    rows = []
    for label, value in details:
        rows.append(
            f'<tr>'
            f'<td valign="top" style="padding:9px 16px 9px 0;border-bottom:1px solid {BORDER};'
            f'font-family:{_FONT};font-size:13px;line-height:20px;color:{MUTED};'
            f'white-space:nowrap;">{html.escape(label)}</td>'
            f'<td valign="top" style="padding:9px 0;border-bottom:1px solid {BORDER};'
            f'font-family:{_FONT};font-size:13px;line-height:20px;color:{INK};'
            f'font-weight:bold;">{html.escape(value)}</td>'
            f'</tr>'
        )
    return (
        '<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%" '
        'style="margin:0 0 24px;border-collapse:collapse;">'
        f'{"".join(rows)}'
        "</table>"
    )


def _callout_html(value: str, label: str | None) -> str:
    label_html = ""
    if label:
        label_html = (
            f'<p style="margin:0 0 10px;font-family:{_FONT};font-size:11px;line-height:16px;'
            f'letter-spacing:1.5px;text-transform:uppercase;color:{MUTED};">'
            f"{html.escape(label)}</p>"
        )
    return f"""\
<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%"
style="margin:0 0 24px;border-collapse:separate;">
<tr>
<td align="center" style="padding:22px 16px;background-color:#f4f8fc;border:1px solid {BORDER};border-radius:6px;">
{label_html}
<div style="font-family:{_MONO};font-size:32px;line-height:38px;font-weight:bold;
letter-spacing:8px;color:{BRAND_NAVY};">{html.escape(value)}</div>
</td>
</tr>
</table>"""


def _block_html(label: str | None, text: str) -> str:
    label_html = ""
    if label:
        label_html = (
            f'<p style="margin:0 0 8px;font-family:{_FONT};font-size:13px;line-height:20px;'
            f'font-weight:bold;color:{INK};">{html.escape(label)}</p>'
        )
    # HTML collapses runs of spaces, which would flatten stack-trace indentation.
    safe = html.escape(text).replace(" ", "&nbsp;").replace("\n", "<br>")
    return f"""\
{label_html}
<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%"
style="margin:0 0 24px;border-collapse:separate;">
<tr>
<td style="padding:14px 16px;background-color:#f8fafc;border:1px solid {BORDER};border-radius:6px;
border-left:3px solid {BRAND_NAVY};font-family:{_MONO};font-size:12px;line-height:19px;
color:{INK};word-break:break-word;">{safe}</td>
</tr>
</table>"""


def _cta_html(label: str, url: str) -> str:
    safe_url = html.escape(url, quote=True)
    return f"""\
<table role="presentation" cellpadding="0" cellspacing="0" border="0" style="margin:0 0 8px;">
<tr>
<td align="center" bgcolor="{BRAND_NAVY}" style="border-radius:6px;">
<a href="{safe_url}" target="_blank"
style="display:inline-block;padding:12px 28px;font-family:{_FONT};font-size:14px;line-height:20px;
font-weight:bold;color:#ffffff;text-decoration:none;border-radius:6px;">{html.escape(label)}</a>
</td>
</tr>
</table>
<p style="margin:0 0 4px;font-family:{_FONT};font-size:12px;line-height:18px;color:{MUTED};">
If the button does not work, copy this link into your browser:<br>
<a href="{safe_url}" style="color:{BRAND_BLUE};text-decoration:underline;word-break:break-all;">{html.escape(url)}</a>
</p>"""


def render_email(
    *,
    title: str,
    intro: str,
    preheader: str | None = None,
    eyebrow: str | None = None,
    details: list[tuple[str, str]] | None = None,
    callout: str | None = None,
    callout_label: str | None = None,
    block_label: str | None = None,
    block_text: str | None = None,
    note: str | None = None,
    cta_label: str | None = None,
    cta_url: str | None = None,
    footer_note: str | None = None,
) -> str:
    """Wrap message content in the standard AIVA letterhead.

    All values are plain text and are escaped here; callers must not pass HTML.
    """
    parts: list[str] = [
        f'<h1 style="margin:0 0 12px;font-family:{_FONT};font-size:20px;line-height:28px;'
        f'font-weight:bold;color:{BRAND_NAVY};">{html.escape(title)}</h1>',
        f'<p style="margin:0 0 24px;font-family:{_FONT};font-size:14px;line-height:22px;'
        f'color:{INK};">{html.escape(intro)}</p>',
    ]
    if callout:
        parts.append(_callout_html(callout, callout_label))
    if details:
        parts.append(_details_html(details))
    if block_text:
        parts.append(_block_html(block_label, block_text))
    if note:
        parts.append(
            f'<p style="margin:0 0 24px;font-family:{_FONT};font-size:13px;line-height:20px;'
            f'color:{MUTED};">{html.escape(note)}</p>'
        )
    if cta_label and cta_url:
        parts.append(_cta_html(cta_label, cta_url))

    preheader_html = ""
    if preheader:
        preheader_html = (
            '<div style="display:none;max-height:0;overflow:hidden;mso-hide:all;'
            'font-size:1px;line-height:1px;color:#ffffff;opacity:0;">'
            f"{html.escape(preheader)}</div>"
        )

    return f"""\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="light only">
<meta name="supported-color-schemes" content="light only">
<title>{html.escape(title)}</title>
</head>
<body style="margin:0;padding:0;background-color:{CANVAS};">
{preheader_html}
<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%"
style="background-color:{CANVAS};">
<tr>
<td align="center" style="padding:32px 16px;">
<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="600"
style="width:600px;max-width:100%;background-color:#ffffff;border:1px solid {BORDER};
border-radius:8px;border-collapse:separate;overflow:hidden;">
{_header_html(eyebrow)}
<tr>
<td style="padding:32px;">
{"".join(parts)}
</td>
</tr>
{_footer_html(footer_note)}
</table>
</td>
</tr>
</table>
</body>
</html>"""


def render_text(
    *,
    title: str,
    intro: str,
    details: list[tuple[str, str]] | None = None,
    callout: str | None = None,
    callout_label: str | None = None,
    block_label: str | None = None,
    block_text: str | None = None,
    note: str | None = None,
    cta_label: str | None = None,
    cta_url: str | None = None,
    footer_note: str | None = None,
) -> str:
    """Plain-text twin of :func:`render_email`, kept in the same formal register."""
    lines: list[str] = ["AIVA", "=" * 60, "", title, "", intro, ""]
    if callout:
        lines.append(f"{callout_label or 'Code'}: {callout}")
        lines.append("")
    if details:
        width = max(len(label) for label, _ in details)
        for label, value in details:
            lines.append(f"{label.ljust(width)}  {value}")
        lines.append("")
    if block_text:
        if block_label:
            lines.append(f"{block_label}:")
        lines.append(block_text)
        lines.append("")
    if note:
        lines.append(note)
        lines.append("")
    if cta_label and cta_url:
        lines.append(f"{cta_label}: {cta_url}")
        lines.append("")
    lines.append("-" * 60)
    if footer_note:
        lines.append(footer_note)
    lines.append("This is an automated message from AIVA. Please do not reply to this email.")
    lines.append("AIVA - a GoChat247 product. (c) GoChat247. All rights reserved.")
    return "\n".join(lines) + "\n"
