"""The link in every first email (writer.cta).

The page is chosen here, from what Harvey knows about the business, never
by the model: the first configured link whose keywords match the
prospect's industry, else one matching the company's name or description,
else the default. ``{domain}`` in the link becomes the company's website
domain, so a visit (``?ref=acme.example``) can be traced back to it.

The model only decides where the link sits: it writes the placeholder
``{{cta_url}}`` and ``place_cta`` swaps in the real link. A draft that left
the placeholder out still gets the link, on its own line before the
sign-off; a second placeholder is dropped, so the email has one link.
"""

import re
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

from harvey.config import CTA_DOMAIN_TAG
from harvey.signature import is_sign_off as _is_sign_off

CTA_TOKEN = "{{cta_url}}"
CTA_TOKEN_RE = re.compile(r"\{\{\s*cta_url\s*\}\}", re.IGNORECASE)

# What the Writer is told when a link is configured. Replaces the built-in
# "no links in email 1" for this one link and nothing else.
CTA_RULE = """- Put the link placeholder {{cta_url}} in this email exactly once, on its own
  line, after a few plain words that say what's there (for example "There's a
  short overview here:"). Harvey swaps it for a link to the right page on our
  site. It is this email's one link and replaces the rule against links in
  email 1. Write no other link, URL or web address, and keep the one question."""
CTA_SEQUENCE_RULE = """- Email 1 has the link placeholder {{cta_url}} exactly once, on its own line,
  after a few plain words that say what's there. Harvey swaps it for a link to
  our site; it replaces the rule against links in email 1. Emails 2 and 3 have
  no link. Never write any other link, URL or web address."""



def cta_config(config):
    writer = getattr(config, "writer", None)
    cta = getattr(writer, "cta", None)
    return cta if getattr(cta, "enabled", False) else None


def _matches(keywords, text: str) -> bool:
    text = (text or "").lower()
    return any(re.search(r"(?<![a-z0-9])" + re.escape(k), text) for k in keywords if k)


def choose_link(cta, industry_texts: list[str], other_texts: list[str]) -> str:
    """The link template for a business, or "" when nothing fits and there's
    no default. Industry wins over name and description: "a freight
    forwarder" says more than a homepage that mentions freight once."""
    if cta is None:
        return ""
    for texts in (industry_texts, other_texts):
        joined = " \n ".join(t for t in texts if t)
        if not joined:
            continue
        for link in cta.links:
            if _matches(link.keywords, joined):
                return link.url
    return cta.default or ""


def build_link(template: str, domain: str) -> str:
    """``template`` with ``{domain}`` filled in. With no domain known, a query
    parameter that held it is dropped rather than left empty."""
    if not template or CTA_DOMAIN_TAG not in template:
        return template
    if domain:
        return template.replace(CTA_DOMAIN_TAG, quote(domain, safe=".-"))
    parts = urlsplit(template)
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
             if CTA_DOMAIN_TAG not in v]
    path = parts.path.replace(CTA_DOMAIN_TAG, "")
    return urlunsplit((parts.scheme, parts.netloc, path, urlencode(query), parts.fragment))


def ref_domain(company=None, email: str = "") -> str:
    """The company's website domain, else the email's domain unless it's a
    free mailbox (``ref=gmail.com`` would say nothing about who visited)."""
    from harvey.state import FREE_MAIL_DOMAINS
    domain = (getattr(company, "domain", "") or "").strip().lower()
    if not domain:
        domain = (email or "").rpartition("@")[2].strip().lower()
        if domain in FREE_MAIL_DOMAINS:
            domain = ""
    if domain.startswith("www."):
        domain = domain[4:]
    return domain


def link_for(config, prospect, company=None) -> str:
    """The finished link for this prospect's first email, or "" (no CTA)."""
    cta = cta_config(config)
    if cta is None:
        return ""
    industry = [getattr(prospect, "industry", ""), getattr(company, "industry", "")]
    other = [getattr(prospect, "company", ""), getattr(company, "name", ""),
             getattr(company, "description", "")]
    template = choose_link(cta, industry, other)
    return build_link(template, ref_domain(company, getattr(prospect, "email", "")))


async def link_for_prospect(state, config, prospect) -> str:
    if cta_config(config) is None or prospect is None:
        return ""
    company = None
    if getattr(prospect, "company_id", ""):
        try:
            company = await state.get_company(prospect.company_id)
        except Exception:
            company = None
    return link_for(config, prospect, company)


def _drop_token_lines(text: str, strip: bool = True) -> str:
    """Remove placeholders; a line left holding nothing but the placeholder goes too."""
    lines = []
    for line in text.split("\n"):
        if CTA_TOKEN_RE.search(line):
            line = CTA_TOKEN_RE.sub("", line).rstrip()
            if not line.strip():
                continue
        lines.append(line)
    text = re.sub(r"\n{3,}", "\n\n", "\n".join(lines))
    return text.strip() if strip else text


def drop_cta(text: str) -> str:
    """Text with no link placeholder left in it (follow-ups, subjects)."""
    if not text or not CTA_TOKEN_RE.search(text):
        return text
    return _drop_token_lines(text)



def place_cta(body: str, url: str) -> str:
    """``body`` with its link in place: the first placeholder becomes ``url``,
    any others are dropped, and a body with none gets ``url`` on its own line
    before the sign-off. No ``url``: the placeholders are just removed."""
    body = (body or "").strip()
    if not url:
        return drop_cta(body)
    match = CTA_TOKEN_RE.search(body)
    if match:
        head, tail = body[:match.start()], body[match.end():]
        if CTA_TOKEN_RE.search(tail):
            tail = _drop_token_lines(tail, strip=False)
        return (head + url + tail).strip()
    paragraphs = re.split(r"\n\s*\n", body)
    if len(paragraphs) > 1 and _is_sign_off(paragraphs[-1]):
        return "\n\n".join(paragraphs[:-1] + [url, paragraphs[-1]])
    return f"{body}\n\n{url}"
