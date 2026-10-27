"""Byte-level BPE readout strings -> text ("çļĦç»ĵæŀľ" -> 的结果); ``wsbench.readouts`` decodes
every token cell once, at load time (docs/producing_readouts.md)."""


def _bytes_to_unicode() -> dict[int, str]:
    """Returns the GPT-2 byte-level BPE table (Qwen's tokenizer uses the same): byte -> char."""
    bs = list(range(ord("!"), ord("~") + 1)) + list(range(ord("¡"), ord("¬") + 1))
    bs += list(range(ord("®"), ord("ÿ") + 1))
    cs = bs[:]
    n = 0
    for b in range(256):
        if b not in bs:
            bs.append(b)
            cs.append(256 + n)
            n += 1
    return {b: chr(c) for b, c in zip(bs, cs, strict=True)}


_U2B = {c: b for b, c in _bytes_to_unicode().items()}


def decode_token(tok: str) -> str:
    """Decodes a byte-level BPE vocabulary string to text, char by char: byte-table chars back
    to their byte, anything else (e.g. the space that replaced ``Ġ``) to its own UTF-8 bytes.
    Bytes that are not valid UTF-8 on their own (a token holding part of a character) become
    U+FFFD. Decoding text that is already decoded mangles Latin-1 letters, so decode once."""
    raw = b"".join(bytes([_U2B[ch]]) if ch in _U2B else ch.encode("utf-8") for ch in tok)
    return raw.decode("utf-8", errors="replace")
