"""
Spärren mot studsande adresser (integrations/email_guard.py).

Testerna mockar DNS — de ska gå att köra utan nät och aldrig bero på att en
viss domän råkar finnas kvar om ett år. Det viktigaste fallet är det SISTA:
går DNS inte att nå måste utskicket släppas igenom. En spärr som blockerar
David när uppkopplingen krånglar är värre än den studs den skulle förhindra.
"""

import unittest.mock as mock

import pytest

from integrations import email_guard as g


@pytest.fixture(autouse=True)
def _rensa_cache():
    # domain_can_receive_mail är lru_cache:ad — annars läcker svar mellan tester.
    g.domain_can_receive_mail.cache_clear()
    yield
    g.domain_can_receive_mail.cache_clear()


def _dns(mx=False, a=False):
    """Fejka en resolver: NoAnswer där posten saknas, en träff där den finns."""
    import dns.resolver

    def resolve(self, domain, typ, *args, **kwargs):
        if typ == "MX" and mx:
            return ["mx.exempel."]
        if typ in ("A", "AAAA") and a:
            return ["192.0.2.1"]
        raise dns.resolver.NoAnswer()

    return mock.patch("dns.resolver.Resolver.resolve", new=resolve)


def test_domän_med_mx_slapps_igenom():
    with _dns(mx=True):
        assert g.check_address("nagon@bolaget.se")[0] is True


def test_domän_utan_mx_men_med_a_slapps_igenom():
    """RFC 5321: saknas MX levereras posten till A-posten. Vi får inte döma ut den."""
    with _dns(mx=False, a=True):
        assert g.check_address("nagon@bolaget.se")[0] is True


def test_domän_utan_bade_mx_och_a_stoppas():
    """Det verkliga fallet: mn@metalcolor.com — domänen fanns inte alls."""
    with _dns(mx=False, a=False):
        ok, orsak = g.check_address("mn@metalcolor.com")
        assert ok is False
        assert "metalcolor.com" in orsak


@pytest.mark.parametrize("addr", ["", "utan-snabel-a", "två@snabel@a.se", "slutar@med."])
def test_trasig_syntax_stoppas(addr):
    assert g.check_address(addr)[0] is False


def test_dns_fel_blockerar_aldrig():
    """Nätverksfel = 'vet ej' = släpp igenom. Spärren får inte bli en felkälla."""
    with mock.patch("dns.resolver.Resolver.resolve", side_effect=OSError("nätet nere")):
        assert g.check_address("nagon@bolaget.se")[0] is True


def test_utskick_stoppas_innan_smtp():
    """
    send_email ska avvisa adressen UTAN att öppna en SMTP-anslutning — det är
    hela poängen: studsen ska aldrig hinna kosta oss något.
    """
    from integrations import email_sender
    with _dns(mx=False, a=False), mock.patch("smtplib.SMTP") as smtp:
        ok, orsak = email_sender.send_email("mn@metalcolor.com", "Ämne", "Text")
    assert ok is False
    assert "kan inte ta emot mejl" in orsak
    smtp.assert_not_called()
