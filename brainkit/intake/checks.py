"""The checks on converted text: personal data, secrets, business-sensitive items, links, instruction-like lines.

`scan(text)` finds hits; `apply(text, hits, policy)` returns masked text. Placeholders are numbered per package
and the same value gets the same placeholder; the original values are never written anywhere.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
import json
import os

# ---------- personal data ----------

HKID_RE = re.compile(r"(?<![A-Za-z0-9])([A-Z]{1,2})([0-9]{6})\(?([0-9A])\)?(?![A-Za-z0-9])")
CNID_RE = re.compile(r"(?<![0-9])([1-9][0-9]{16}[0-9Xx])(?![0-9])")
PHONE_CN_RE = re.compile(r"(?<![0-9])(?:\+?86[-\s]?)?1[3-9][0-9]{9}(?![0-9])")
PHONE_HK_PREFIXED_RE = re.compile(r"(?<![0-9])\+?852[-\s]?[2-9][0-9]{3}[-\s]?[0-9]{4}(?![0-9])")
PHONE_HK_CONTEXT_RE = re.compile(r"(?i)(電話|电话|手機|手机|聯絡|联系|tel|phone|mobile|whatsapp)[^0-9\n]{0,12}"
                                 r"((?<![0-9])[2-9][0-9]{3}[-\s]?[0-9]{4}(?![0-9]))")
EMAIL_RE = re.compile(r"(?<![A-Za-z0-9._%+-])[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9-]{1,63}(?:\.[A-Za-z0-9-]{1,63})+")
CARD_RE = re.compile(r"(?<![0-9])(?:[0-9][ -]?){14,18}[0-9](?![0-9])")
MENTION_RE = re.compile(r"@([A-Z][a-zA-Z]+(?:[  ]+[A-Z][A-Za-z]+){0,3}|[一-鿿]{2,4})")
NAME_COL_RE = re.compile(r"修[訂订]人|負責人|负责人|聯絡人|联系人|聯繫人|姓名|客[戶户]名稱|客户名称|審批人|审批人|申請人|申请人"
                         r"|^\s*(owner|contact|contact name|customer name|client name|approver|reviewer|author"
                         r"|prepared by|approved by)\s*$", re.I)
# a cell that looks like a person: 2-4 CJK chars, or 1-4 capitalised Latin words (optionally @-mentioned)
NAME_VALUE_RE = re.compile(r"^@?(?:[一-鿿]{2,4}|[A-Z][a-z]+(?:[ .-]+[A-Z][A-Za-z]+){0,3}|[A-Z][a-z]+ [A-Z]{2,})$")

# ---------- secrets ----------

PRIVATE_KEY_RE = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")
CLOUD_KEY_RE = re.compile(r"(?<![A-Z0-9])(AKIA|ASIA)[0-9A-Z]{16}(?![A-Z0-9])")
KV_SECRET_RE = re.compile(r"(?i)((?:passw(?:or)?d|pwd|secret|token|api[_-]?key|access[_-]?key|client[_-]?secret"
                          r"|密碼|密码|口令)[\"']?\s*[=:：]\s*)([^\s\"'<>|,;]{4,})")
BEARER_RE = re.compile(r"(?i)\bbearer\s+([A-Za-z0-9._~+/-]{16,}=*)")
TOKEN_RE = re.compile(r"\b(ghp_[A-Za-z0-9]{36}|github_pat_[A-Za-z0-9_]{40,}|xox[abpr]-[A-Za-z0-9-]{10,}"
                      r"|glpat-[A-Za-z0-9_-]{20}|eyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,})\b")

# ---------- business-sensitive ----------

CUR = r"(?:HKD|USD|CNY|RMB|HK\$|US\$|S\$|¥|￥|\$|€|£)"
SCALE = r"(?:億|亿|萬|万|千|k|K|m|M|bn|million|billion)"          # only after a currency: 3M alone is a tenor
MONEY_WORD = r"(?:元|港元|港幣|港币|美元|美金|人民幣|人民币|歐元|欧元|英鎊|英镑|日元)"
CUR_CODE = r"(?:HKD|USD|CNY|RMB|CNH|EUR|GBP|SGD|JPY|AUD)"
AMOUNT_RE = re.compile(r"(?<![A-Za-z0-9])(?:%s\s?[0-9][0-9,]*(?:\.[0-9]+)?(?:\s?%s)?(?:\s?%s)?"
                       r"|[0-9][0-9,]*(?:\.[0-9]+)?\s?(?:億|亿|萬|万)?\s?%s"
                       r"|[0-9][0-9,]*(?:\.[0-9]+)?\s?%s?\s?%s)(?![A-Za-z0-9])"
                       % (CUR, SCALE, MONEY_WORD, MONEY_WORD, SCALE, CUR_CODE))
COMPANY_RE = re.compile(r"[一-鿿A-Za-z0-9&·（）()]{2,30}?(?:股份有限公司|有限公司|有限責任公司|銀行|银行|集團|集团|證券|证券|"
                        r"基金管理|資產管理|资产管理)"
                        r"|\b[A-Z][A-Za-z0-9&.\-]*(?:\s+[A-Z][A-Za-z0-9&.\-]*){0,5}\s+(?:Limited|Ltd\.?|Bank|Securities|"
                        r"Inc\.?|LLC|Corp\.?|Corporation|Holdings|Group)\b")
CONTRACT_RE = re.compile(r"(?:合同|合約|合约|協議|协议|Contract)\s*(?:編號|编号|號|号|No\.?)\s*[:：#]?\s*([A-Za-z0-9-]{4,})", re.I)

# ---------- links and instructions ----------

URL_RE = re.compile(r"https?://([A-Za-z0-9.-]+)(?::\d+)?[^\s)\]>\"']*")
APPROVAL_LINE_RE = re.compile(r"(?im)^[ \t]*(?:[-*+][ \t]+|[0-9]{1,3}[.)、][ \t]*)?"
                              r"(?:approve|approved|批准|同意|reject|驳回|拒绝|allow test change|允许改测试"
                              r"|允许修改测试|允许修改验收测试)\b.*$")
INJECTION_RE = re.compile(r"(?i)(ignore|disregard|forget)\s+(all\s+|the\s+)?(previous|prior|above|earlier)\s+"
                          r"(instructions|rules|prompts?)|忽略(以上|之前|前面|上面)(的)?(所有)?(指令|指示|规则|規則|要求)"
                          r"|you are now\b|你现在是|你現在是|system prompt|系統提示|系统提示|developer mode")

PII_KINDS = ("hkid", "cnid", "phone", "email", "card", "person")


class Hit:
    __slots__ = ("cat", "kind", "start", "end", "value")

    def __init__(self, cat, kind, start, end, value):
        self.cat, self.kind, self.start, self.end, self.value = cat, kind, start, end, value


def hkid_ok(prefix, digits, check) -> bool:
    vals = ([36] if len(prefix) == 1 else []) + [ord(c) - 55 for c in prefix] + [int(c) for c in digits]
    total = sum(v * w for v, w in zip(vals, range(9, 1, -1)))
    expect = (11 - total % 11) % 11
    return check == ("A" if expect == 10 else str(expect))


def cnid_ok(s) -> bool:
    w = [7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2]
    total = sum(int(c) * k for c, k in zip(s[:17], w))
    return "10X98765432"[total % 11] == s[17].upper()


def luhn_ok(digits) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        n = int(ch)
        if i % 2:
            n *= 2
            if n > 9:
                n -= 9
        total += n
    return total % 10 == 0 and len(set(digits)) > 1


def _table_name_cells(text):
    """Cells of markdown-table columns whose header names a person (修訂人, 負責人, Name ...)."""
    hits = []
    lines = text.split("\n")
    pos = 0
    offsets = []
    for line in lines:
        offsets.append(pos)
        pos += len(line) + 1
    i = 0
    while i < len(lines) - 1:
        head, sep = lines[i], lines[i + 1]
        if head.strip().startswith("|") and re.match(r"^\s*\|?\s*:?-{2,}", sep):
            cols = [c.strip().strip("*") for c in head.strip().strip("|").split("|")]
            name_cols = {k for k, c in enumerate(cols) if NAME_COL_RE.search(c)}
            j = i + 2
            while j < len(lines) and lines[j].strip().startswith("|"):
                if name_cols:
                    line = lines[j]
                    # walk cells with their offsets
                    starts = [m.start() for m in re.finditer(r"(?<!\\)\|", line)]
                    for k in range(len(starts) - 1):
                        if k in name_cols:
                            a, b = starts[k] + 1, starts[k + 1]
                            cell = line[a:b]
                            core = cell.strip()
                            names = [x.strip() for x in re.split(r"<br\s*/?>|[,，、/;；]", core) if x.strip()]
                            if names and all(NAME_VALUE_RE.match(x.strip("*")) for x in names):
                                lead = len(cell) - len(cell.lstrip())
                                s = offsets[j] + a + lead
                                hits.append(Hit("pii", "person", s, s + len(core), core))
                j += 1
            i = j
        else:
            i += 1
    return hits


def scan(text) -> list:
    hits = []
    add = hits.append
    for m in PRIVATE_KEY_RE.finditer(text):
        add(Hit("secret", "private_key", m.start(), m.end(), m.group(0)))
    for m in CLOUD_KEY_RE.finditer(text):
        add(Hit("secret", "cloud_key", m.start(), m.end(), m.group(0)))
    for m in KV_SECRET_RE.finditer(text):
        add(Hit("secret", "kv_secret", m.start(2), m.end(2), m.group(2)))
    for m in BEARER_RE.finditer(text):
        add(Hit("secret", "bearer", m.start(1), m.end(1), m.group(1)))
    for m in TOKEN_RE.finditer(text):
        add(Hit("secret", "token", m.start(1), m.end(1), m.group(1)))
    for m in HKID_RE.finditer(text):
        if hkid_ok(m.group(1), m.group(2), m.group(3)):
            add(Hit("pii", "hkid", m.start(), m.end(), m.group(0)))
    for m in CNID_RE.finditer(text):
        if cnid_ok(m.group(1)):
            add(Hit("pii", "cnid", m.start(1), m.end(1), m.group(1)))
    for rx in (PHONE_CN_RE, PHONE_HK_PREFIXED_RE):
        for m in rx.finditer(text):
            add(Hit("pii", "phone", m.start(), m.end(), m.group(0)))
    for m in PHONE_HK_CONTEXT_RE.finditer(text):
        add(Hit("pii", "phone", m.start(2), m.end(2), m.group(2)))
    for m in EMAIL_RE.finditer(text):
        add(Hit("pii", "email", m.start(), m.end(), m.group(0)))
    for m in CARD_RE.finditer(text):
        digits = re.sub(r"[ -]", "", m.group(0))
        if 15 <= len(digits) <= 19 and luhn_ok(digits):
            add(Hit("pii", "card", m.start(), m.end(), m.group(0)))
    for m in MENTION_RE.finditer(text):
        add(Hit("pii", "person", m.start(), m.end(), m.group(0)))
    hits += _table_name_cells(text)
    for m in AMOUNT_RE.finditer(text):
        if re.search(r"[0-9]", m.group(0)):
            add(Hit("business", "amount", m.start(), m.end(), m.group(0)))
    for m in COMPANY_RE.finditer(text):
        add(Hit("business", "company", m.start(), m.end(), m.group(0).strip()))
    for m in CONTRACT_RE.finditer(text):
        add(Hit("business", "contract", m.start(1), m.end(1), m.group(1)))
    for m in URL_RE.finditer(text):
        add(Hit("link", "url", m.start(), m.end(), m.group(1).lower()))
    for m in APPROVAL_LINE_RE.finditer(text):
        add(Hit("instruction", "approval_line", m.start(), m.end(), m.group(0)))
    for m in INJECTION_RE.finditer(text):
        add(Hit("instruction", "injection", m.start(), m.end(), m.group(0)))
    return _dedupe(hits)


def _dedupe(hits):
    """Drop hits fully inside a stronger overlapping hit (secret > pii > business > link > instruction)."""
    rank = {"secret": 0, "pii": 1, "business": 2, "link": 3, "instruction": 4}
    hits.sort(key=lambda h: (h.start, rank[h.cat], -(h.end - h.start)))
    kept = []
    for h in hits:
        if h.cat in ("link", "instruction"):
            kept.append(h)
            continue
        clash = next((k for k in kept if k.cat not in ("link", "instruction") and h.start < k.end and k.start < h.end),
                     None)
        if clash is None:
            kept.append(h)
        elif rank[h.cat] < rank[clash.cat]:
            kept.remove(clash)
            kept.append(h)
    return sorted(kept, key=lambda h: h.start)


def propagate(text, hits, values) -> list:
    """Add a hit for every occurrence of a value already judged sensitive anywhere in the submission."""
    extra = []
    for (cat, kind), vals in values.items():
        for v in vals:
            if len(v) < 2:
                continue
            start = text.find(v)
            while start >= 0:
                extra.append(Hit(cat, kind, start, start + len(v), v))
                start = text.find(v, start + len(v))
    return _dedupe(list(hits) + extra) if extra else hits


def gitleaks_hits(path) -> list:
    """Extra secret findings from gitleaks when it is installed: [(rule, line)]. Empty when not installed."""
    tool = shutil.which("gitleaks")
    if not tool:
        return []
    with tempfile.TemporaryDirectory() as d:
        report = os.path.join(d, "r.json")
        for args in (["dir", path], ["detect", "--no-git", "--source", path]):
            p = subprocess.run([tool] + args + ["--report-format", "json", "--report-path", report, "--no-banner",
                                                "--exit-code", "0"], capture_output=True, text=True, timeout=120)
            if p.returncode == 0 and os.path.exists(report):
                try:
                    with open(report) as f:
                        return [(x.get("RuleID", ""), x.get("StartLine", 0)) for x in json.load(f) or []]
                except ValueError:
                    return []
    return []


class Masker:
    """Same value -> same placeholder within one package."""

    LABEL = {"hkid": "身份證", "cnid": "身份证", "phone": "电话", "email": "邮箱", "card": "卡号", "person": "人名",
             "kv_secret": "密钥", "bearer": "令牌", "token": "令牌", "amount": "金额", "company": "公司",
             "contract": "合同号"}

    def __init__(self):
        self.seen = {}
        self.count = {}

    def placeholder(self, kind, value):
        key = (kind, value)
        if key not in self.seen:
            self.count[kind] = self.count.get(kind, 0) + 1
            self.seen[key] = "[%s-%d]" % (self.LABEL.get(kind, kind), self.count[kind])
        return self.seen[key]


def apply(text, hits, mask_business, masker) -> str:
    """Mask personal data and secrets (always), business items (if mask_business); neutralize instruction lines."""
    out = []
    pos = 0
    for h in hits:
        if h.cat == "pii" or h.cat == "secret" or (h.cat == "business" and mask_business):
            if h.start < pos:
                continue
            out.append(text[pos:h.start])
            out.append(masker.placeholder(h.kind, h.value))
            pos = h.end
    out.append(text[pos:])
    masked = "".join(out)
    # instruction-like lines become quoted data, so no reader takes them as a command
    lines = masked.split("\n")
    for i, line in enumerate(lines):
        if APPROVAL_LINE_RE.match(line) or INJECTION_RE.search(line):
            lines[i] = "> " + line
    return "\n".join(lines)


def summarize(hits) -> dict:
    s = {}
    for h in hits:
        s.setdefault(h.cat, {}).setdefault(h.kind, 0)
        s[h.cat][h.kind] += 1
    return s
