// rsa-pkcs1.js — raw RSAES-PKCS1-v1_5 encryption for CF Workers.
// WebCrypto omits PKCS#1 v1.5 encryption; the Qoder COSY protocol requires it.
// Pure-JS big-integer modexp (1024-bit, e=65537): ~100ms per operation, done
// once per session material — acceptable.

// BigInt-based modexp: modern Workers support BigInt natively.
export function modPow(base, exp, mod) {
  let result = 1n;
  let b = base % mod;
  let e = exp;
  while (e > 0n) {
    if (e & 1n) result = (result * b) % mod;
    b = (b * b) % mod;
    e >>= 1n;
  }
  return result;
}

function bytesToBigInt(bytes) {
  let hex = "";
  for (const b of bytes) hex += b.toString(16).padStart(2, "0");
  return BigInt("0x" + hex);
}

function bigIntToBytes(value, length) {
  const hex = value.toString(16).padStart(length * 2, "0");
  const out = new Uint8Array(length);
  for (let i = 0; i < length; i++) {
    out[i] = parseInt(hex.slice(i * 2, i * 2 + 2), 16);
  }
  return out;
}

// PKCS#1 v1.5 (type 2) pad: 0x00 0x02 PS 0x00 message — PS random, non-zero.
function pkcs1Pad(message, modulusLength) {
  if (message.length > modulusLength - 11) throw new Error("rsa: message too long");
  const out = new Uint8Array(modulusLength);
  out[0] = 0x00; out[1] = 0x02;
  const psLen = modulusLength - message.length - 3;
  const ps = new Uint8Array(psLen);
  crypto.getRandomValues(ps);
  for (let i = 0; i < psLen; i++) ps[i] = (ps[i] % 0xfe) + 1; // non-zero
  out.set(ps, 2);
  out[2 + psLen] = 0x00;
  out.set(message, 3 + psLen);
  return out;
}

export function rsaEncryptPkcs1(publicKey, message) {
  // publicKey: { n: Uint8Array (big-endian modulus, no leading zero), e: Uint8Array }
  const n = bytesToBigInt(publicKey.n);
  const e = bytesToBigInt(publicKey.e);
  const padded = pkcs1Pad(message, publicKey.n.length);
  const cipherInt = modPow(bytesToBigInt(padded), e, n);
  return bigIntToBytes(cipherInt, publicKey.n.length);
}

// Parse a SubjectPublicKeyInfo PEM body (DER) into {n, e} Uint8Arrays.
export function parseSpkiDer(der) {
  // SPKI: SEQUENCE { SEQUENCE{...alg...}, BIT STRING { 00, SEQUENCE{ INTEGER n, INTEGER e } } }
  function readLen(pos) {
    const first = der[pos];
    if (first < 0x80) return [first, pos + 1];
    const numBytes = first & 0x7f;
    let len = 0;
    for (let i = 0; i < numBytes; i++) len = len * 256 + der[pos + 1 + i];
    return [len, pos + 1 + numBytes];
  }
  function readInt(pos) {
    // expects der[pos] == 0x02; returns [value, positionAfterWholeTLV]
    const [len, next] = readLen(pos + 1);
    let value = der.slice(next, next + len);
    if (value[0] === 0) value = value.slice(1);
    return [value, next + len];
  }
  // outer SEQUENCE at 0
  // outer SEQUENCE at 0: 30 len, then alg SEQUENCE
  const [, bodyStart] = readLen(1);
  // alg SEQUENCE: tag(1)+lenBytes+payload
  const [algLen, algPayload] = readLen(bodyStart + 1);
  const bitStringTag = algPayload + algLen; // 0x03
  if (der[bitStringTag] !== 0x03) throw new Error("rsa: expected BIT STRING");
  const [bitsLen, bitsContent] = readLen(bitStringTag + 1);
  const unusedBits = der[bitsContent]; // 0x00
  void unusedBits;
  const innerSeq = bitsContent + 1; // inner SEQUENCE tag
  if (der[innerSeq] !== 0x30) throw new Error("rsa: expected inner SEQUENCE");
  const [, intsStart] = readLen(innerSeq + 1);
  const [n, eStart] = readInt(intsStart);
  const [e] = readInt(eStart);
  return { n, e };
}
