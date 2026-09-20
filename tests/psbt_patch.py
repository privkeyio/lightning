import base64


def _cs(b, i):
    n = b[i]
    if n < 0xfd:
        return n, i + 1
    if n == 0xfd:
        return int.from_bytes(b[i + 1:i + 3], 'little'), i + 3
    if n == 0xfe:
        return int.from_bytes(b[i + 1:i + 5], 'little'), i + 5
    return int.from_bytes(b[i + 1:i + 9], 'little'), i + 9


def set_input0_sighash(psbt_b64, sighash):
    """Insert PSBT_IN_SIGHASH_TYPE (0x03) into the first input's map."""
    raw = base64.b64decode(psbt_b64)
    assert raw[:5] == b'psbt\xff', raw[:5]
    i = 5
    # walk the global map to its 0x00 terminator
    while raw[i] != 0x00:
        klen, i = _cs(raw, i)
        i += klen
        vlen, i = _cs(raw, i)
        i += vlen
    i += 1  # consume terminator; input 0's map starts here
    rec = b'\x01\x03\x04' + sighash.to_bytes(4, 'little')
    return base64.b64encode(raw[:i] + rec + raw[i:]).decode()


def _cs_write(n):
    if n < 0xfd:
        return bytes([n])
    if n <= 0xffff:
        return b'\xfd' + n.to_bytes(2, 'little')
    return b'\xfe' + n.to_bytes(4, 'little')


def set_input0_nonwitness_utxo(psbt_b64, prev_tx_hex):
    """Insert PSBT_IN_NON_WITNESS_UTXO (0x00) into an empty first input map."""
    raw = base64.b64decode(psbt_b64)
    assert raw[:5] == b'psbt\xff', raw[:5]
    i = 5
    while raw[i] != 0x00:
        klen, i = _cs(raw, i)
        i += klen
        vlen, i = _cs(raw, i)
        i += vlen
    i += 1  # consume terminator; input 0's map starts here
    assert raw[i] == 0x00, 'input 0 map is not empty'
    prev = bytes.fromhex(prev_tx_hex)
    rec = _cs_write(1) + b'\x00' + _cs_write(len(prev)) + prev
    return base64.b64encode(raw[:i] + rec + raw[i:]).decode()
