// qoder-cosy.js — COSY protocol for Qoder international (api1.qoder.sh).
// Ported from RobbsLuo/Coding2API src/provider/qoder/{cosy,events}.py.
// Crypto: RSA PKCS#1 v1.5 (rsa-pkcs1.js, pure-JS), AES-128-CBC + MD5 (WebCrypto-free impls here).

import { rsaEncryptPkcs1, parseSpkiDer } from "./rsa-pkcs1.js";

// --- base64 ----------------------------------------------------------------
export function b64encode(bytes) {
  let bin = "";
  for (const b of bytes) bin += String.fromCharCode(b);
  return btoa(bin);
}
export function b64decode(text) {
  const bin = atob(text);
  const out = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
  return out;
}
export function b64urlEncode(bytes) {
  return b64encode(bytes).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

// --- custom base64 variant (Qoder wire format) ------------------------------
const STD = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
const CUSTOM = "_doRTgHZBKcGVjlvpC,@aFSx#DPuNJme&i*MzLOEn)sUrthbf%Y^w.(kIQyXqWA!";
const ENC = new Map([...(STD + "=")].map((c, i) => [c, [...(CUSTOM + "$")][i]]));
const DEC = new Map([...(CUSTOM + "$")].map((c, i) => [c, [...(STD + "=")][i]]));

export function qoderEncode(bytes) {
  const std = b64encode(bytes);
  const size = std.length;
  const head = Math.floor(size / 3);
  const rearranged = std.slice(size - head) + std.slice(head, size - head) + std.slice(0, head);
  return [...rearranged].map((c) => ENC.get(c) ?? c).join("");
}

// --- sorted compact JSON ----------------------------------------------------
export function jsonSortedCompact(mapping) {
  const parts = [];
  for (const key of Object.keys(mapping).sort()) {
    const value = mapping[key];
    parts.push(JSON.stringify(key) + ":" + JSON.stringify(value === null || value === undefined ? "" : value));
  }
  return "{" + parts.join(",") + "}";
}

// --- MD5 (protocol fingerprint + signature; upstream uses it, not security) --
export function md5Hex(text) {
  const msg = new TextEncoder().encode(text);
  const len = msg.length;
  const withPad = new Uint8Array((((len + 8) >> 6) + 1) * 64);
  withPad.set(msg);
  withPad[len] = 0x80;
  const bitLen = len * 8;
  const dv = new DataView(withPad.buffer);
  dv.setUint32(withPad.length - 8, bitLen >>> 0, true);
  dv.setUint32(withPad.length - 4, Math.floor(bitLen / 0x100000000), true);
  let a0 = 0x67452301, b0 = 0xefcdab89, c0 = 0x98badcfe, d0 = 0x10325476;
  const S = [7, 12, 17, 22, 5, 9, 14, 20, 4, 11, 16, 23, 6, 10, 15, 21];
  const K = new Uint32Array(64);
  for (let i = 0; i < 64; i++) K[i] = Math.floor(Math.abs(Math.sin(i + 1)) * 0x100000000);
  for (let off = 0; off < withPad.length; off += 64) {
    const M = new Uint32Array(16);
    for (let i = 0; i < 16; i++) M[i] = new DataView(withPad.buffer, off + i * 4).getUint32(0, true);
    let A = a0, B = b0, C = c0, D = d0;
    for (let i = 0; i < 64; i++) {
      let F, g;
      if (i < 16) { F = (B & C) | (~B & D); g = i; }
      else if (i < 32) { F = (D & B) | (~D & C); g = (5 * i + 1) % 16; }
      else if (i < 48) { F = B ^ C ^ D; g = (3 * i + 5) % 16; }
      else { F = C ^ (B | ~D); g = (7 * i) % 16; }
      F = (F + A + K[i] + M[g]) >>> 0;
      A = D; D = C; C = B;
      const s = S[(i >> 4) * 4 + (i % 4)];
      B = (B + ((F << s) | (F >>> (32 - s)))) >>> 0;
    }
    a0 = (a0 + A) >>> 0; b0 = (b0 + B) >>> 0; c0 = (c0 + C) >>> 0; d0 = (d0 + D) >>> 0;
  }
  return [a0, b0, c0, d0].map((w) => {
    let s = "";
    for (let i = 0; i < 4; i++) s += ((w >>> (i * 8)) & 0xff).toString(16).padStart(2, "0");
    return s;
  }).join("");
}

// --- stable device fingerprints --------------------------------------------
function deriveId(uid, salt) {
  return md5Hex(`${salt}:${uid || "anonymous"}`).slice(0, 36);
}
function deriveMachineType(uid) {
  return deriveId(uid, "machinetype").replace(/-/g, "").slice(0, 18);
}
async function deriveMachineToken(uid) {
  const raw = await crypto.subtle.digest("SHA-512", new TextEncoder().encode(`machinetoken:${uid}`));
  return b64urlEncode(new Uint8Array(raw)).slice(0, 43);
}

// --- AES-128-CBC (identity envelope; key reused as IV) ----------------------
async function aesCbcEncrypt(plain, keyBytes) {
  const key = await crypto.subtle.importKey("raw", keyBytes, "AES-CBC", false, ["encrypt"]);
  const pad = 16 - (plain.length % 16);
  const padded = new Uint8Array(plain.length + pad);
  padded.set(plain);
  padded.fill(pad, plain.length);
  const buf = await crypto.subtle.encrypt({ name: "AES-CBC", iv: keyBytes }, key, padded);
  return new Uint8Array(buf); // WebCrypto AES-CBC encrypt returns ciphertext only (no IV prefix)
}

// --- RSA PKCS#1 v1.5 (server key wraps the session AES key) ------------------
const SERVER_PUBLIC_KEY_PEM_BODY =
  "MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQDA8iMH5c02LilrsERw9t6Pv5Nc" +
  "4k6Pz1EaDicBMpdpxKduSZu5OANqUq8er4GM95omAGIOPOh+Nx0spthYA2BqGz+l" +
  "6HRkPJ7S236FZz73In/KVuLnwI8JJ2CbuJap8kvheCCZpmAWpb/cPx/3Vr/J6I17" +
  "XcW+ML9FoCI6AOvOzwIDAQAB";

export const SERVER_PUBLIC_KEY = parseSpkiDer(b64decode(SERVER_PUBLIC_KEY_PEM_BODY));

function modPow(base, exp, mod) {
  let result = 1n, b = base % mod, e = exp;
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
  return BigInt("0x" + (hex || "0"));
}
function bigIntToBytes(value, length) {
  const hex = value.toString(16).padStart(length * 2, "0");
  const out = new Uint8Array(length);
  for (let i = 0; i < length; i++) out[i] = parseInt(hex.slice(i * 2, i * 2 + 2), 16);
  return out;
}
function rsaWrapLocal(message) {
  const { n, e } = SERVER_PUBLIC_KEY;
  const nInt = bytesToBigInt(n), eInt = bytesToBigInt(e);
  const modLen = n.length;
  if (message.length > modLen - 11) throw new Error("rsa: message too long");
  const out = new Uint8Array(modLen);
  out[0] = 0x00; out[1] = 0x02;
  const psLen = modLen - message.length - 3;
  const ps = new Uint8Array(psLen);
  crypto.getRandomValues(ps);
  for (let i = 0; i < psLen; i++) ps[i] = (ps[i] % 0xfe) + 1;
  out.set(ps, 2);
  out[2 + psLen] = 0x00;
  out.set(message, 3 + psLen);
  return bigIntToBytes(modPow(bytesToBigInt(out), eInt, nInt), modLen);
}

// --- COSY constants ----------------------------------------------------------
export const COSY_VERSION = "1.1.64";
export const COSY_CLIENT_TYPE = "5";
export const COSY_CLIENT_IP = "169.254.198.161";
export const LOGIN_VERSION = "v2";
export const CLIENT_UA = "Go-http-client/2.0";

// --- session ------------------------------------------------------------------
export class CosySession {
  constructor({ uid, access_token, refresh_token = "", nickname = "", user_type = "personal_professional_trial", org_id = "", org_name = "", temp_key = null }) {
    if (!access_token) throw new Error("cosy: empty access token");
    this.uid = uid || "";
    this.access_token = access_token;
    this.machine_id = deriveId(this.uid, "machine");
    this.machine_type = deriveMachineType(this.uid);
    this._key = temp_key;
    this._meta = { refresh_token, nickname, user_type, org_id, org_name };
    this._material = null;
  }
  async init() {
    this.machine_token = await deriveMachineToken(this.uid);
    let key = this._key;
    if (!key || key.length !== 16) {
      const bytes = crypto.getRandomValues(new Uint8Array(16));
      key = [...bytes].map((b) => "0123456789abcdef"[b % 16]).join("");
    }
    const keyBytes = new TextEncoder().encode(key);
    const identity = {
      name: this._meta.nickname || "",
      aid: this.uid,
      uid: this.uid,
      yx_uid: "",
      organization_id: this._meta.org_id || "",
      organization_name: this._meta.org_name || "",
      user_type: this._meta.user_type || "personal_professional_trial",
      security_oauth_token: this.access_token,
      refresh_token: this._meta.refresh_token || "",
    };
    const identityJson = new TextEncoder().encode(jsonSortedCompact(identity));
    const info = b64encode(await aesCbcEncrypt(identityJson, keyBytes));
    const cosy_key = b64encode(rsaWrapLocal(keyBytes));
    this._material = { info, cosy_key };
    return this;
  }
  async headers({ body, rawUrl, modelKey = "", sse = true, accept = "text/event-stream" }) {
    const request_id = crypto.randomUUID();
    const payload_b64 = b64encode(new TextEncoder().encode(jsonSortedCompact({
      cosyVersion: COSY_VERSION, ideVersion: "", info: this._material.info,
      requestId: request_id, version: "v1",
    })));
    const stamp = String(Math.floor(Date.now() / 1000));
    let path = new URL(rawUrl).pathname;
    if (path.startsWith("/algo")) path = path.slice(5);
    const digest = md5Hex([payload_b64, this._material.cosy_key, stamp, body, path].join("\n"));
    const headers = {
      "cosy-data-policy": "AGREE",
      "content-type": "application/json",
      "cosy-machinetype": this.machine_type,
      "cosy-clienttype": COSY_CLIENT_TYPE,
      "cosy-date": stamp,
      "cosy-user": this.uid,
      "cosy-key": this._material.cosy_key,
      "accept": accept,
      "cosy-clientip": COSY_CLIENT_IP,
      "authorization": `Bearer COSY.${payload_b64}.${digest}`,
      "accept-encoding": "identity",
      "cosy-version": COSY_VERSION,
      "cosy-machineid": this.machine_id,
      "cosy-machinetoken": this.machine_token,
      "login-version": LOGIN_VERSION,
      "user-agent": CLIENT_UA,
    };
    if (sse) headers["cache-control"] = "no-cache";
    if (modelKey) {
      headers["x-model-key"] = modelKey;
      headers["x-model-source"] = "system";
    }
    return headers;
  }
}
