"""Marking a link click by hand (channels.email.follow_ups: clicked).

The CTA link carries ``?ref=<company domain>``, so a click shows up wherever
the operator watches visits (analytics, an edge or server log). Recording it
here, with ``harvey clicked <domain>`` or the dashboard's Clicked button,
does what a click read from the lead sheet does: the person is marked as
clicked and email 2 is queued, through the usual approval, cap and window.
"""

import re
from datetime import datetime, timezone
from urllib.parse import unquote

from harvey.cta import ref_domain
from harvey.sheet_sync import CLICKED_KEY


def _host(value: str) -> str:
    value = (value or "").strip().lower()
    value = re.sub(r"^[a-z][a-z0-9+.-]*://", "", value)
    value = re.split(r"[/?#:\s]", value, maxsplit=1)[0].strip(".")
    if value.startswith("www."):
        value = value[4:]
    return value if "." in value else ""


def click_target(text: str) -> str:
    """What was clicked, from a domain, an address, or a pasted link with
    ``?ref=``: 'https://ours.example/?ref=acme.example' -> 'acme.example'."""
    text = (text or "").strip()
    ref = re.search(r"[?&]ref=([^&#\s]+)", text)
    if ref:
        text = unquote(ref.group(1))
    if "@" in text and "://" not in text:
        return text.lower()
    return _host(text)


async def contacted(state) -> list[tuple]:
    """Everyone whose first email went out: (prospect, ref domain, clicked on)."""
    seen, out = set(), []
    for item in await state.get_sent_sequence_items(1):
        pid = item.get("prospect_id")
        if not pid or pid in seen:
            continue
        seen.add(pid)
        prospect = await state.get_prospect(pid)
        if prospect is None:
            continue
        company = await state.get_company(prospect.company_id) if prospect.company_id else None
        out.append((prospect, ref_domain(company, prospect.email),
                    await state.get_setting(CLICKED_KEY + pid)))
    return out


async def find(state, target: str) -> list:
    """The contacted prospects a click on ``target`` (domain or address) is from."""
    if not target:
        return []
    return [p for p, domain, _ in await contacted(state)
            if (p.email or "").lower() == target or domain == target]


async def mark_clicked(state, sender, config, prospect) -> str:
    """Record the click and queue email 2. Returns what happened, in words."""
    from harvey.config import follow_up_mode

    key = CLICKED_KEY + prospect.id
    first = not await state.get_setting(key)
    if first:
        await state.set_setting(key, datetime.now(timezone.utc).date().isoformat())
        await state.log_action(action_type="link_clicked", agent="user",
                               details={"prospect_id": prospect.id, "email": prospect.email})
    mode = follow_up_mode(config)
    if mode != "clicked":
        return ("marked clicked" if first else "already marked clicked") + (
            f" (follow_ups is '{mode}', so no email 2 is queued for clicks)")
    queued = await sender.stage_follow_up(prospect.id) if sender is not None else None
    if queued:
        return "marked clicked; email 2 is waiting in the Outbox for your approval"
    existing = await state.get_sequence_item(prospect.id, 2)
    if existing and existing.get("status") in ("pending_review", "approved", "sending", "sent"):
        return f"already marked; email 2 is {existing['status'].replace('_', ' ')}"
    return (("marked clicked" if first else "already marked clicked")
            + f"; no email 2 queued (status: {prospect.status}, e.g. they replied, "
              "opted out, bounced or a colleague answered)")
