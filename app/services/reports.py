"""报告服务 - 读取本地 Markdown 报告"""
import os
from pathlib import Path
from datetime import datetime

REPORTS_DIR = Path(__file__).parent.parent.parent / "reports"


def get_available_dates() -> list[str]:
    """获取所有有报告的日期"""
    if not REPORTS_DIR.exists():
        return []
    dates = set()
    for f in REPORTS_DIR.glob("*_ai_report.md"):
        # Extract date from filename like 20260706_ai_report.md
        name = f.stem
        if name.endswith("_ai_report"):
            date_str = name.replace("_ai_report", "")
            if len(date_str) == 8:
                formatted = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:8]}"
                dates.add(formatted)
    return sorted(dates, reverse=True)


def get_report(date_str: str) -> dict | None:
    """读取指定日期的报告"""
    date_key = date_str.replace("-", "")
    filename = f"{date_key}_ai_report.md"
    filepath = REPORTS_DIR / filename
    if not filepath.exists():
        return None

    content = filepath.read_text(encoding="utf-8")
    # Simple markdown to HTML conversion
    html = md_to_html(content)
    stat = filepath.stat()
    updated = datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S")
    return {"combined": html, "updated_at": updated}


def md_to_html(md: str) -> str:
    """Simple markdown to HTML"""
    lines = md.split("\n")
    html_lines = []
    in_list = False
    in_code = False

    for line in lines:
        stripped = line.strip()

        if stripped.startswith("```"):
            if in_code:
                html_lines.append("</code></pre>")
                in_code = False
            else:
                html_lines.append("<pre><code>")
                in_code = True
            continue

        if in_code:
            html_lines.append(line)
            continue

        if not stripped:
            if in_list:
                html_lines.append("</ul>")
                in_list = False
            html_lines.append("")
            continue

        if stripped.startswith("# "):
            if in_list:
                html_lines.append("</ul>")
                in_list = False
            html_lines.append(f"<h3>{stripped[2:]}</h3>")
        elif stripped.startswith("## "):
            if in_list:
                html_lines.append("</ul>")
                in_list = False
            html_lines.append(f"<h3>{stripped[3:]}</h3>")
        elif stripped.startswith("### "):
            if in_list:
                html_lines.append("</ul>")
                in_list = False
            html_lines.append(f"<h4>{stripped[4:]}</h4>")
        elif stripped.startswith("#### "):
            if in_list:
                html_lines.append("</ul>")
                in_list = False
            html_lines.append(f"<h4>{stripped[5:]}</h4>")
        elif stripped.startswith("- ") or stripped.startswith("* "):
            if not in_list:
                html_lines.append("<ul>")
                in_list = True
            text = stripped[2:]
            text = format_inline(text)
            html_lines.append(f"<li>{text}</li>")
        elif stripped.startswith("> "):
            if in_list:
                html_lines.append("</ul>")
                in_list = False
            text = format_inline(stripped[2:])
            html_lines.append(f'<p class="ai-callout">{text}</p>')
        else:
            if in_list:
                html_lines.append("</ul>")
                in_list = False
            text = format_inline(stripped)
            # Detect special patterns
            if "结论" in stripped or "总原则" in stripped:
                html_lines.append(f'<p class="ai-callout">{text}</p>')
            elif stripped.startswith("风险") or stripped.startswith("禁止"):
                html_lines.append(f'<p class="ai-risk-text">{text}</p>')
            elif stripped.startswith("目标") or stripped.startswith("动作"):
                html_lines.append(f'<p class="ai-action-text">{text}</p>')
            else:
                html_lines.append(f'<p class="ai-text">{text}</p>')

    if in_list:
        html_lines.append("</ul>")

    return "\n".join(html_lines)


def format_inline(text: str) -> str:
    """Format inline markdown"""
    import re
    # Bold
    text = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', text)
    # Code
    text = re.sub(r'`(.+?)`', r'<code>\1</code>', text)
    # Links
    text = re.sub(r'\[(.+?)\]\((.+?)\)', r'<a href="\2" target="_blank">\1</a>', text)
    return text
