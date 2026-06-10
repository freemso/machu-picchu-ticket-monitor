from machu_picchu_monitor.config import Settings
from machu_picchu_monitor.providers import OfficialApiProvider

SAMPLE_ENCRYPTED_HORARIOS = (
    "SfPgKwzfqa8n7/aZGrq87amAie/2caN6dcyN1BpPQKJVmFAkuchenztgxCG7DwEvESj3KnVArGI14"
    "VLFl8bzDyt3HqybslO8ie1faQZ+T8ReZatdQCiknEt3TzI93HpVO4a1Xvac0OKA/jd+yF3xZDuGL"
    "0QRiceM5PEMZwhHoX8AhyUTXc+6bwD6+et3LLMW+ALeuuHMYPnWkb+PT9XoaHPiAoNETEpfr5Tc"
    "kPQ9t9p07FhS8xVVrASspskgkFrBOxJdYMPOBe0RUMs2WLzPFxFvJ2fy1f5lvkLdeRBuuCGkmc/"
    "3cQ38hqk8owGt3fOFCtHHN4gACIOZXY27piBlplEs3HVoaYMEragMpv4pjzDmAlJZqazv+AoT1i"
    "iUyavMlin3peFUH/I63TyCTRdBqiKBrNXmFZ+bzonssYGsdQm03CMhjy0xENI3fj2j0yxdqKvXQ3"
    "cVwiEAflWMqbhYb/RfkCCq2HjEyhBOareVVDiMj8elC8bdsukgp5pWihSRTFFS0FDBnwRLt4Wbb"
    "stLImiC3BT2R+nNenxEWF91LuxBs7WS3VubfFpHFE4J62SlsOC2dXVguhk9uWPdeA1vJ5+ah0rS"
    "6mk0okn5avEaIjfoCpxW7pFL+ltJIkXpDsE3wxIjKZiMhmFz9jMbVR/1W1oK7sYXafwhv48Tj7"
    "AwwPmOG9dgmyY3eZ97L2iL6zg2qDfrt7ieF+MOZEPTUTfuWvk3195bvJ/m6KJDPRmqM/rEjXJY9"
    "TPgcdftB5uN/FQSvs87lpgrgGbiYL0w4UXak73DL3er9z0R+VnahFj73pnmuxS015MWfwAGJnPa"
    "5dkCeSRDSkhBaQvLkuBLGZGWvg=="
)


def test_official_api_signature_matches_frontend_algorithm() -> None:
    provider = OfficialApiProvider(Settings())
    assert provider._sign("1780991381984") == "bUwegjGpgkAksaD/9bv1eHLcrs3ZxDJ1EHiWgAJhhQw="


def test_decrypts_official_encrypted_horarios_payload() -> None:
    provider = OfficialApiProvider(Settings())
    rows = provider._decrypt_data(SAMPLE_ENCRYPTED_HORARIOS)
    assert isinstance(rows, list)
    assert rows[0]["dhora_ini"] == "07:00:00"
    assert rows[0]["ncupo_actual"] == 31
