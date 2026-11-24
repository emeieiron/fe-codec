"""Independent reference encoders used only to compute test vectors for the Fe suite."""
import json

def head(major, n):
    if n < 24: return bytes([major << 5 | n])
    for width, add in ((1, 24), (2, 25), (4, 26), (8, 27)):
        if n < 1 << (8 * width):
            return bytes([major << 5 | add]) + n.to_bytes(width, 'big')
    raise ValueError(n)

def cbor_int(v):
    if v >= 0:
        if v < 1 << 64: return head(0, v)
        b = v.to_bytes((v.bit_length() + 7) // 8, 'big')
        return bytes([0xc2]) + head(2, len(b)) + b
    n = -1 - v
    if n < 1 << 64: return head(1, n)
    b = n.to_bytes((n.bit_length() + 7) // 8, 'big')
    return bytes([0xc3]) + head(2, len(b)) + b

def cbor(v):
    if v is True: return b'\xf5'
    if v is False: return b'\xf4'
    if v is None: return b'\xf6'
    if isinstance(v, int): return cbor_int(v)
    if isinstance(v, str):
        b = v.encode(); return head(3, len(b)) + b
    if isinstance(v, (bytes, bytearray)): return head(2, len(v)) + bytes(v)
    if isinstance(v, list): return head(4, len(v)) + b''.join(cbor(x) for x in v)
    if isinstance(v, dict):  # int keys, omit None, ascending
        items = sorted((k, x) for k, x in v.items() if x is not None)
        return head(5, len(items)) + b''.join(cbor_int(k) + cbor(x) for k, x in items)
    raise TypeError(v)

class Hex(bytes): pass

def js(v):
    if isinstance(v, Hex): return '"0x' + v.hex() + '"'
    if v is True: return 'true'
    if v is False: return 'false'
    if v is None: return 'null'
    if isinstance(v, int): return str(v)
    if isinstance(v, str): return json.dumps(v, ensure_ascii=False)
    if isinstance(v, list): return '[' + ','.join(js(x) for x in v) + ']'
    if isinstance(v, dict):
        return '{' + ','.join(json.dumps(k) + ':' + js(x) for k, x in v.items() if x is not None) + '}'
    raise TypeError(v)

def fe_bytes(b):
    return '[' + ', '.join(f'0x{x:02x}' for x in b) + ']'

def fe_str(s):
    b = s.encode()
    return len(b), json.dumps(s, ensure_ascii=False)

U256 = (1 << 256) - 1; I256 = -(1 << 255); U128 = (1 << 128) - 1; I128 = -(1 << 127)
ADDR = bytes.fromhex('d8da6bf26964af9d7eed9e03e53415d37aa96045')

cases = {
  'WIDE': ({'big': U256, 'signed': I256, 'mid': U128, 'small': I128}, {1: U256, 2: I256, 3: U128, 4: I128}),
  'WALLET': ({'owner': Hex(ADDR), 'hash': Hex(bytes.fromhex('deadbeef')), 'memo': Hex(bytes([1,2,3])), 'tag': Hex(bytes([0,1]))},
             {1: ADDR, 2: bytes.fromhex('deadbeef'), 3: bytes([1,2,3]), 4: bytes([0,1])}),
  'PAGE': ({'items': [1, 2, 65535], 'next': 7}, {1: [1, 2, 65535], 2: 7}),
  'SLOTS': ({'values': [1, None, 255]}, {1: [1, None, 255]}),
  'READING': ({'at': 5, 'temperature': '-12.5'}, {1: 5, 2: '-12.5'}),
  'PAIRS': ({'first': 2, 'second': 4}, {1: 2, 3: 4}),
  # A tuple is an array of its elements, at the root and as a field.
  'TUPLE': ([7, 'hi'], [7, 'hi']),
  'WAYPOINT': ({'at': [7, 'hi'], 'bounds': [[-1, 2], [3, -4]], 'legs': [[1, True], [2, False]],
                'next': [300, 18446744073709551615], 'tag': [5]},
               {1: [7, 'hi'], 2: [[-1, 2], [3, -4]], 3: [[1, True], [2, False]],
                4: [300, 18446744073709551615], 5: [5]}),
  'WAYPOINT_NO_NEXT': ({'at': [7, 'hi'], 'bounds': [[-1, 2], [3, -4]], 'legs': [[1, True], [2, False]],
                     'next': None, 'tag': [5]},
                    {1: [7, 'hi'], 2: [[-1, 2], [3, -4]], 3: [[1, True], [2, False]],
                     4: None, 5: [5]}),
  # A list is an array of its current length, in both formats.
  'TALLY': ({'counts': [1, 2]}, {1: [1, 2]}),
  'TALLY_EMPTY': ({'counts': []}, {1: []}),
  'TALLY_FULL': ({'counts': [1, 2, 3, 65535]}, {1: [1, 2, 3, 65535]}),
  'CREW': ({'members': [{'id': 1, 'name': 'hi'}, {'id': 2, 'name': 'yo'}], 'guests': [7]},
           {1: [{1: 1, 2: 'hi'}, {1: 2, 2: 'yo'}], 2: [7]}),
  'CREW_EMPTY': ({'members': [], 'guests': None}, {1: [], 2: None}),
  'CREW_NO_GUESTS': ({'members': [], 'guests': []}, {1: [], 2: []}),
  'ROUTES': ({'legs': [[1, True], [2, False]], 'grid': [[1, 2], [3]], 'slots': [1, None, 3], 'fixed': [[4], []]},
             {1: [[1, True], [2, False]], 2: [[1, 2], [3]], 3: [1, None, 3], 4: [[4], []]}),
  'ROUTES_EMPTY': ({'legs': [], 'grid': [], 'slots': [], 'fixed': [[], []]},
                   {1: [], 2: [], 3: [], 4: [[], []]}),
  'LIST_ROOT': ([1, 2], [1, 2]),
}
for name, (j, c) in cases.items():
    jt = js(j); cb = cbor(c)
    n, lit = fe_str(jt)
    print(f'const {name}_JSON: [u8; {n}] = {lit}')
    print(f'const {name}_CBOR: [u8; {len(cb)}] = {fe_bytes(cb)}')

# RFC 8949 Appendix A, checked against this reference.
rfc = [
 (0,'00'),(1,'01'),(10,'0a'),(23,'17'),(24,'1818'),(25,'1819'),(100,'1864'),(1000,'1903e8'),
 (1000000,'1a000f4240'),(1000000000000,'1b000000e8d4a51000'),(18446744073709551615,'1bffffffffffffffff'),
 (18446744073709551616,'c249010000000000000000'),(-18446744073709551616,'3bffffffffffffffff'),
 (-18446744073709551617,'c349010000000000000000'),(-1,'20'),(-10,'29'),(-100,'3863'),(-1000,'3903e7'),
 (False,'f4'),(True,'f5'),(None,'f6'),('','60'),('a','6161'),('IETF','6449455446'),('"\\','62225c'),
 ('ü','62c3bc'),('水','63e6b0b4'),('\U00010151','64f0908591'),(b'','40'),(bytes([1,2,3,4]),'4401020304'),
 ([],'80'),([1,2,3],'83010203'),({1:2,3:4},'a201020304'),
]
for v, h in rfc:
    assert cbor(v).hex() == h, (v, cbor(v).hex(), h)
print('# RFC 8949 Appendix A vectors agree with the reference encoder')
