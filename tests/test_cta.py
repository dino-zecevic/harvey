"""writer.cta: one link in every first email, picked by Harvey, placed by the model.

The page is chosen from what Harvey knows about the business (never by the
model), {domain} becomes the company's website domain, and the email ends
up with exactly one link whether the draft placed the placeholder, forgot
it, or repeated it.
"""

from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from harvey.config import CtaConfig, EmailChannelConfig, WriterConfig
from harvey.cta import (
    CTA_TOKEN, build_link, choose_link, drop_cta, link_for, place_cta, ref_domain,
)
from harvey.gate import pre_send_check

FREIGHT = "https://ours.example/freight/?ref={domain}"
DATA = "https://ours.example/data/?ref={domain}"
HOME = "https://ours.example/?ref={domain}"


def cta():
    return CtaConfig(default=HOME, links=[
        {"url": FREIGHT, "keywords": ["freight", "logistic", "haulage"]},
        {"url": DATA, "keywords": ["accounting", "bookkeep"]},
    ])


def cfg(c=None):
    return SimpleNamespace(writer=SimpleNamespace(cta=c if c is not None else cta()))


# ── choosing the page ──

def test_the_industry_picks_the_page():
    assert choose_link(cta(), ["Freight forwarding"], []) == FREIGHT
    assert choose_link(cta(), ["Bookkeeping services"], []) == DATA


def test_industry_beats_a_mention_in_the_description():
    # An accountant whose homepage mentions logistics clients is still an accountant.
    assert choose_link(cta(), ["Accounting"], ["We serve logistics firms"]) == DATA


def test_the_description_is_used_when_the_industry_says_nothing():
    assert choose_link(cta(), ["Services"], ["Haulage across the north"]) == FREIGHT


def test_nothing_matching_gets_the_default():
    assert choose_link(cta(), ["Dentist"], ["Smiles"]) == HOME
    assert choose_link(CtaConfig(links=[{"url": DATA, "keywords": ["tax"]}]), ["Dentist"], []) == ""


def test_keywords_match_at_the_start_of_a_word_only():
    c = CtaConfig(links=[{"url": DATA, "keywords": ["tax advis"]}])
    assert choose_link(c, ["Tax advisers"], []) == DATA
    assert choose_link(c, ["Syntax advisory"], []) == ""


# ── the link itself ──

def test_domain_fills_the_ref():
    assert build_link(FREIGHT, "acme.test") == "https://ours.example/freight/?ref=acme.test"


def test_no_domain_drops_the_ref_instead_of_leaving_it_empty():
    assert build_link(FREIGHT, "") == "https://ours.example/freight/"
    assert build_link("https://ours.example/?a=1&ref={domain}", "") == "https://ours.example/?a=1"


def test_ref_is_the_company_website_not_a_free_mailbox():
    assert ref_domain(SimpleNamespace(domain="www.acme.test"), "jo@gmail.com") == "acme.test"
    assert ref_domain(None, "jo@acme.test") == "acme.test"
    assert ref_domain(None, "jo@gmail.com") == ""


def test_link_for_a_prospect():
    prospect = SimpleNamespace(industry="Logistics", company="Acme", email="jo@acme.test")
    company = SimpleNamespace(domain="acme.test", industry="", name="Acme", description="")
    assert link_for(cfg(), prospect, company) == "https://ours.example/freight/?ref=acme.test"
    assert link_for(SimpleNamespace(writer=SimpleNamespace(cta=CtaConfig())), prospect, company) == ""


# ── placing it ──

def test_the_placeholder_becomes_the_link():
    body = "Hi Jo,\n\nOne thing.\n\nShort overview here:\n{{cta_url}}\n\nWorth a look?\n\nSam"
    out = place_cta(body, "https://ours.example/?ref=acme.test")
    assert out.count("https://") == 1 and CTA_TOKEN not in out
    assert "here:\nhttps://ours.example/?ref=acme.test\n\nWorth" in out


def test_a_forgotten_placeholder_still_gets_the_link_before_the_sign_off():
    out = place_cta("Hi Jo,\n\nOne question?\n\nBest,\nSam", "https://ours.example/")
    assert out == "Hi Jo,\n\nOne question?\n\nhttps://ours.example/\n\nBest,\nSam"


def test_a_body_with_no_sign_off_gets_the_link_at_the_end():
    assert place_cta("One question for you, a long sentence here?", "https://o.example/").endswith(
        "\n\nhttps://o.example/")


def test_a_repeated_placeholder_leaves_one_link():
    out = place_cta("See {{cta_url}}\n\n{{ cta_url }}\n\nThanks", "https://o.example/")
    assert out.count("https://o.example/") == 1 and "cta_url" not in out


def test_no_link_configured_removes_the_placeholder():
    assert place_cta("Hi\n\n{{cta_url}}\n\nBye", "") == "Hi\n\nBye"
    assert drop_cta("subject {{cta_url}}") == "subject"


def test_one_link_passes_the_send_gate():
    body = place_cta("Hi Jo,\n\nOne question?\n\nSam", "https://ours.example/?ref=acme.test")
    assert pre_send_check("jo@acme.test", "a subject", body).ok


# ── config ──

@pytest.mark.parametrize("bad", ["ours.example/?ref={domain}", "https://", "https://x.example/{domain}/{domain}",
                                 "https://x.example/a b"])
def test_a_link_that_isnt_one_is_refused(bad):
    with pytest.raises(ValidationError):
        CtaConfig(default=bad)


def test_cta_is_off_by_default():
    assert not WriterConfig().cta.enabled
    assert not WriterConfig(cta=None).cta.enabled
    assert CtaConfig(default=HOME).enabled


def test_follow_up_modes():
    assert EmailChannelConfig().follow_ups == "auto"
    assert EmailChannelConfig(follow_ups=" Clicked ").follow_ups == "clicked"
    with pytest.raises(ValidationError):
        EmailChannelConfig(follow_ups="sometimes")
