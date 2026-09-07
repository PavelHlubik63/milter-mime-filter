from mail_filter.mime import decode_subject


def test_decode_q_encoded_subject():
    # "You have been hacked" as a Q-encoded (RFC 2047) UTF-8 word.
    raw = "=?UTF-8?Q?You_have_been_hacked?="
    assert decode_subject(raw) == "You have been hacked"


def test_decode_base64_encoded_subject():
    # "You have been hacked" as a Base64-encoded (RFC 2047) UTF-8 word.
    raw = "=?UTF-8?B?WW91IGhhdmUgYmVlbiBoYWNrZWQ=?="
    assert decode_subject(raw) == "You have been hacked"


def test_decode_plain_subject_passes_through():
    assert decode_subject("Ordinary subject") == "Ordinary subject"


def test_decode_mixed_encoded_and_plain_words():
    raw = "=?UTF-8?Q?Hacked=3A?= plain text"
    assert decode_subject(raw) == "Hacked: plain text"
