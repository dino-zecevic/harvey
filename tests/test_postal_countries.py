"""compliance.postal_address_countries: the postal address goes only to
recipients in the listed countries (CAN-SPAM and CASL ask for it, UK and EU
rules don't), and to anyone whose country Harvey can't be sure of."""

from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from harvey.config import ComplianceConfig, outgoing_footer, postal_address_for
from harvey.models.company import Company
from harvey.models.prospect import Prospect
from tests.test_click_follow_ups import make_sender
from tests.test_outbox_native import state  # noqa: F401  (fixture)

ADDRESS = "1 Main St, Springfield"
OPT_OUT = 'Not interested? Reply "no thanks".'


def compliance(countries=("US", "CA")):
    return ComplianceConfig(postal_address=ADDRESS, opt_out_line=OPT_OUT,
                            postal_address_countries=list(countries))


class Cfg:
    compliance = compliance()


# ── config ──

def test_codes_are_normalised():
    assert ComplianceConfig(postal_address_countries=["us", " ca ", "USA", "uk"]
                            ).postal_address_countries == ["US", "CA", "GB"]
    assert ComplianceConfig(postal_address_countries="us").postal_address_countries == ["US"]
    assert ComplianceConfig().postal_address_countries == []


def test_a_country_name_is_refused():
    with pytest.raises(ValidationError):
        ComplianceConfig(postal_address_countries=["Canada"])


def test_who_gets_the_address():
    assert postal_address_for(Cfg, "US") and postal_address_for(Cfg, "ca")
    assert not postal_address_for(Cfg, "GB") and not postal_address_for(Cfg, "NO")
    assert postal_address_for(Cfg, "")                       # not sure: it goes

    class Everyone:
        compliance = compliance(())
    assert postal_address_for(Everyone, "GB")


def test_the_footer_without_the_address():
    assert outgoing_footer(Cfg, postal=False) == OPT_OUT
    assert outgoing_footer(Cfg) == f"{ADDRESS}\n{OPT_OUT}"


# ── what leaves ──

def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


async def queued(state, location, email, tz=""):
    cid = await state.add_company(Company(name="Dock Co", domain=email.split("@")[1],
                                          location=location))
    pid = await state.add_prospect(Prospect(
        first_name="Ana", last_name="Lee", title="Owner", company="Dock Co", company_id=cid,
        email=email, email_status="verified", email_verified=True, status="queued"))
    if tz:
        await state.set_prospect_timezone(pid, tz)
    await state.add_outbox_item(
        prospect_id=pid, to_email=email, subject="a subject", body="Hi Ana,\n\nA question?",
        send_at=(_now() - timedelta(minutes=5)).isoformat(), status="approved", step=1)
    return pid


@pytest.mark.asyncio
@pytest.mark.parametrize("location,tz,gets_address", [
    ("Austin, TX", "", True),
    ("Toronto, Ontario, Canada", "", True),
    ("Leeds, United Kingdom", "", False),
    ("Bergen, Norway", "", False),
    ("", "Europe/Dublin", False),        # no location, but a stored zone in Ireland
    ("", "", True),                      # nothing known: the address goes
])
async def test_the_address_goes_only_where_it_is_asked_for(state, location, tz, gets_address):
    await queued(state, location, "ana@dock.test", tz)
    sender = make_sender(state)
    sender.config.compliance = compliance()
    await sender._drain_due()
    [sent] = sender.provider.sent
    assert (ADDRESS in sent["body"]) is gets_address
    assert sent["body"].rstrip().endswith(OPT_OUT)


@pytest.mark.asyncio
async def test_without_a_country_list_everyone_gets_it(state):
    await queued(state, "Leeds, United Kingdom", "ana@dock.test")
    sender = make_sender(state)
    sender.config.compliance = compliance(())
    await sender._drain_due()
    assert ADDRESS in sender.provider.sent[0]["body"]
