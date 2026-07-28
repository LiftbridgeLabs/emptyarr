import requests
from typing import List, Dict


def _post(webhook_url: str, payload: dict):
    if not webhook_url:
        return
    # Validate it's actually a Discord webhook URL to prevent SSRF
    if not webhook_url.startswith("https://discord.com/api/webhooks/") and \
       not webhook_url.startswith("https://discordapp.com/api/webhooks/"):
        return
    try:
        requests.post(webhook_url, json=payload, timeout=10)
    except Exception:
        pass


def _check_fields(checks: Dict) -> list:
    return [
        {
            "name":   name,
            "value":  ("✅ " if c["pass"] else "❌ ") + c["detail"],
            "inline": False,
        }
        for name, c in checks.items()
    ]


def _build_tv_tree(items: List[Dict]) -> dict:
    tree: dict = {}
    for ep in (item for item in items if item.get("type") == "episode"):
        show   = ep.get("grandparent_title") or ep.get("parent_title") or "Unknown Show"
        s_num  = ep.get("parent_index", "")
        season = f"Season {s_num}" if s_num else (ep.get("parent_title") or "Unknown Season")
        ep_num = ep.get("index", "")
        label  = f"Ep {ep_num} \u2013 {ep['title']}" if ep_num else ep["title"]
        tree.setdefault(show, {}).setdefault(season, []).append((int(ep_num) if str(ep_num).isdigit() else 999, label))
    for show in tree:
        for season in tree[show]:
            tree[show][season].sort(key=lambda x: x[0])
            tree[show][season] = [label for _, label in tree[show][season]]
    for s in (item for item in items if item.get("type") == "season"):
        show   = s.get("parent_title") or s.get("grandparent_title") or "Unknown Show"
        s_num  = s.get("index", "") or s.get("parent_index", "")
        season = f"Season {s_num}" if s_num else s["title"]
        tree.setdefault(show, {}).setdefault(season, [])
    for sh in (item for item in items if item.get("type") == "show"):
        tree.setdefault(sh["title"], {})
    return tree


def _season_number(label: str) -> int:
    parts = label.split()
    return int(parts[-1]) if parts and parts[-1].isdigit() else 999


def _format_tv_tree(items: List[Dict]) -> str:
    """Build a hierarchical show → season → episode listing for Discord."""
    tree = _build_tv_tree(items)
    lines = []
    for show_name in sorted(tree):
        lines.append(f"**{show_name}**")
        for season in sorted(tree[show_name], key=_season_number):
            lines.append(f"\u00a0\u00a0{season}")
            for ep in tree[show_name][season]:
                lines.append(f"\u00a0\u00a0\u00a0\u00a0\u2022 {ep}")
    return "\n".join(lines)


def _item_lines(items: List[Dict], limit: int, noun: str = "") -> List[str]:
    lines = []
    for item in items[:limit]:
        year = f" ({item['year']})" if item.get("year") else ""
        lines.append(f"• {item['title']}{year}")
    if len(items) > limit:
        suffix = f" {noun}" if noun else ""
        lines.append(f"_…and {len(items) - limit} more{suffix}_")
    return lines


def _removed_item_lines(items: List[Dict]) -> List[str]:
    tv_items = [
        item for item in items
        if item.get("type") in ("episode", "season", "show")
    ]
    movies = [item for item in items if item.get("type") == "movie"]
    if not tv_items and not movies:
        return _item_lines(items, 15)
    lines = [_format_tv_tree(tv_items)] if tv_items else []
    if movies:
        if lines:
            lines.append("")
        lines.extend(_item_lines(movies, 20, "movies"))
    return lines


def _append_embed_body(description: str, lines: List[str]) -> str:
    if not lines:
        return description
    body = "\n".join(lines)
    if len(description) + len(body) + 2 > 4000:
        body = body[:4000 - len(description) - 20] + "\n_…(truncated)_"
    return f"{description}\n\n{body}"


def notify_emptied(webhook_url: str, instance_name: str, library_name: str,
                   removed_items: List[Dict], checks: Dict, breakdown: str = ""):
    """Fired when trash was actually emptied (items removed)."""
    if not webhook_url:
        return

    count       = len(removed_items)
    description = f"Emptied **{breakdown or f'{count} item(s)'}** from trash."

    description = _append_embed_body(
        description, _removed_item_lines(removed_items),
    )

    _post(webhook_url, {"embeds": [{
        "title":       f"✅ emptyarr — {instance_name} / {library_name}",
        "description": description,
        "color":       0x3ecf8e,
        "fields":      _check_fields(checks),
    }]})


def notify_clean(webhook_url: str, instance_name: str, library_name: str,
                 checks: Dict):
    """Fired when run succeeded but trash was already empty."""
    if not webhook_url:
        return
    _post(webhook_url, {"embeds": [{
        "title":       f"✅ emptyarr — {instance_name} / {library_name}",
        "description": "Trash was already empty — nothing to remove.",
        "color":       0x3ecf8e,
        "fields":      _check_fields(checks),
    }]})


def notify_health_fail(webhook_url: str, instance_name: str, library_name: str,
                       failed_checks: Dict, all_checks: Dict):
    """Fired when health checks failed — trash empty was skipped."""
    if not webhook_url:
        return
    failed_list = "\n".join(
        f"• **{n}**: {c['detail']}" for n, c in failed_checks.items()
    )
    _post(webhook_url, {"embeds": [{
        "title":       f"⚠️ emptyarr — {instance_name} / {library_name}",
        "description": f"Health checks failed — trash empty skipped.\n\n**Failed:**\n{failed_list}",
        "color":       0xf06565,
        "fields":      _check_fields(all_checks),
    }]})


def notify_error(webhook_url: str, instance_name: str, library_name: str,
                 error: str, checks: Dict):
    """Fired when emptyTrash API call failed."""
    if not webhook_url:
        return
    _post(webhook_url, {"embeds": [{
        "title":       f"🔴 emptyarr — {instance_name} / {library_name} error",
        "description": f"emptyTrash failed:\n```{error}```",
        "color":       0xe74c3c,
        "fields":      _check_fields(checks),
    }]})


def notify_skip(webhook_url: str, instance_name: str,
                library_name: str, reason: str):
    """Fired when run was skipped (scheduling paused, config error, etc)."""
    if not webhook_url:
        return
    _post(webhook_url, {"embeds": [{
        "title":       f"⏭️ emptyarr — {instance_name} / {library_name} skipped",
        "description": f"**Reason:** {reason}",
        "color":       0xe8a045,
    }]})
