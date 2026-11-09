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
# the usual written form 138 0013 8000 / 138-0013-8000 / +86-138-0013-8000: 3-4-4 with one kind of separator, not
# glued to a code (ORD-138-2026-0001 stays)
PHONE_CN_GROUPED_RE = re.compile(r"(?<![A-Za-z0-9_-])(?:(?:\+|00)?86[ \u00a0-]?|\(\+?86\)[ \u00a0]?)?"
                                 r"1[3-9][0-9]([ \u00a0-])[0-9]{4}\1[0-9]{4}(?![0-9A-Za-z_-])")
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

# A name right after a label (负责人：张伟, On-call: Zhang Wei, Reported by Alice Wang). Chinese: 2-4 characters
# starting with a common surname, ending at a non-Chinese character (or 和/及/与/等); Latin: 2-4 capitalised words.
# Roles, teams and common words are not names (产品经理, 支付组, Payments Team, 周一).
SURNAMES_2 = ("欧阳 歐陽 司马 司馬 上官 诸葛 諸葛 东方 東方 皇甫 尉迟 尉遲 公孙 公孫 慕容 令狐 司徒 夏侯 长孙 長孫 宇文 "
              "轩辕 軒轅 端木 西门 西門 南宫 南宮 独孤 獨孤 呼延 赫连 赫連").split()
SURNAMES_1 = set("王李张刘陈杨黄赵吴周徐孙马朱胡郭何高林罗郑梁谢宋唐许韩冯邓曹彭曾肖萧田董袁潘于蒋蔡余杜叶程苏魏吕丁任沈"
                 "姚卢姜崔钟谭陆汪范金石廖贾夏韦付傅方白邹孟熊秦邱江尹薛闫阎段雷侯龙史陶黎贺顾毛郝龚邵万钱严覃武戴莫孔汤"
                 "康易乔赖文常温庞樊兰殷施洪翁柯庄欧区麦关岑甘古冼邝霍伍祝耿倪鲁葛聂"
                 "張劉陳楊黃趙吳孫馬羅鄭謝許韓馮鄧蕭葉蘇呂盧鍾鐘譚陸賈韋鄒閆閻龍賀顧龔錢嚴湯賴龐蘭莊歐區麥關鄺聶魯於萬"
                 "喬溫")
# surname + one of these is a person (张经理, 王总, 李老师); on its own it is a role
TITLES = set(("经理 經理 总 總 总监 總監 工 老师 老師 主管 主任 先生 女士 小姐 老板 老闆 博士 教授 律师 律師 医生 醫生 姐 哥 "
              "总裁 總裁 董 部长 部長 组长 組長 科长 科長 处长 處長 局长 局長 行长 行長 院长 院長 校长 校長 厂长 廠長 队长 "
              "隊長 主席 生 太 同学 同學").split())
ROLE_SUFFIXES = tuple(("经理 經理 主管 总监 總監 工程师 工程師 架构师 架構師 设计师 設計師 分析师 分析師 专员 專員 同学 同學 "
                       "负责人 負責人 团队 團隊 小组 小組 组 組 部 部门 部門 中心 委员会 委員會 办公室 辦公室 公司 集团 集團 "
                       "银行 銀行 证券 證券 基金 保险 保險 平台 系统 系統 服务 服務 机器人 機器人").split())
ROLE_SUFFIXES_1 = tuple("方员員长長会會岗崗师師")       # one-character endings: only when no surname leads
NOT_NAMES = set(("产品 產品 运维 運維 运营 運營 开发 開發 测试 測試 客服 财务 財務 法务 法務 设计 設計 前端 后端 後端 研发 研發 "
                 "业务 業務 用户 用戶 客户 客戶 系统 系統 平台 待定 暂无 暫無 本人 自己 全员 全員 大家 双方 雙方 甲方 乙方 "
                 "我方 对方 對方 各方 管理员 管理員 机器人 機器人 值班 未定 未知 不限 任意 所有人 任何人 安全 安排 文本 文字 "
                 "文档 文檔 文件 金额 金額 金融 高级 高級 高层 高層 高管 高优 方案 方式 方法 方面 周期 週期 周末 週末 周报 "
                 "週報 周会 常规 常規 常量 程序 余额 餘額 万一 萬一 严重 嚴重 温度 溫度 白天 周一 周二 周三 周四 周五 周六 "
                 "周日 周天 週一 週二 週三 週四 週五 週六 週日 董事 董事会 董事會 董事长 董事長 理事会 理事會 股东 股東").split())
CJK_LABELS = ("负责人|負責人|联系人|聯絡人|聯繫人|联络人|对接人|對接人|值班人员|值班人員|值班人|值班同学|值班同學|报告人|"
              "報告人|提出人|处理人|處理人|经办人|經辦人|跟进人|跟進人|执行人|執行人|申请人|申請人|审批人|審批人|审核人|"
              "審核人|修订人|修訂人|发起人|發起人|提交人|报修人|報修人|指派给|指派給|分配给|分配給")
_CJK_END = r"(?:(?![一-鿿])|(?=[和及与與等]))"
CJK_LABEL_NAME_RE = re.compile(r"(?:(?:%s)([ \t]*(?:[:：]|是|为|為)?[ \t]*)|值班[ \t]*[:：][ \t]*(?P<duty>))"
                               r"@?([一-鿿]{2,4})%s" % (CJK_LABELS, _CJK_END))
CJK_MORE_NAME_RE = re.compile(r"[ \t]*(?:[、/]|和|及|与|與)[ \t]*@?([一-鿿]{2,4})%s" % _CJK_END)
LATIN_LABELS = (r"owner|assignee|assigned[ \t]+to|reported[ \t]+by|reporter|on[- ]?call|contact(?:[ \t]+person)?"
                r"|point[ \t]+of[ \t]+contact|approved[ \t]+by|approver|reviewed[ \t]+by|reviewer|prepared[ \t]+by"
                r"|author|written[ \t]+by|raised[ \t]+by|requested[ \t]+by|requester|filed[ \t]+by|opened[ \t]+by"
                r"|escalated[ \t]+to|handled[ \t]+by|acknowledged[ \t]+by|resolved[ \t]+by|fixed[ \t]+by")
_LATIN_WORD = r"[A-Z][a-z]+(?:['’-][A-Za-z]+)?"
LATIN_LABEL_NAME_RE = re.compile(r"(?i:\b(?:%s))[ \t]*[:：=]?[ \t]*@?(%s(?:[ \t]+(?:%s|[A-Z]{2,}(?![a-z]))){1,3})"
                                 r"(?![A-Za-z])" % (LATIN_LABELS, _LATIN_WORD, _LATIN_WORD))
ROLE_WORDS = set((   # a Latin value with one of these is a role or a team, not a person (Payments Team)
    "team teams squad group guild tribe chapter department dept org organisation organization division unit office "
    "board committee council center centre desk helpdesk platform product products engineering engineer engineers "
    "ops operations devops sre oncall on-call primary secondary backup backend frontend mobile web data infra "
    "infrastructure security support service services admin admins administrator bot bots robot system systems "
    "monitor monitoring alert alerts alerting alertmanager grafana prometheus pagerduty opsgenie sentry datadog "
    "jenkins gitlab github jira kiro kirocrew brain bridge runner agent agents ci cd pipeline owner owners lead leads "
    "manager managers director head architect analyst designer developer developers reviewer reviewers approver "
    "approvers builder builders customer customers client clients user users vendor vendors partner partners legal "
    "finance compliance audit risk qa qe test testing tier level everyone nobody none tbd unassigned unknown me "
    "myself us we you them they").split())
STOP_WORDS = set((   # a Latin name ends before these (Reported by Alice Wang On Monday)
    "the a an our my your their this that all any no not same see above below on at in of for and or with from to "
    "by via monday tuesday wednesday thursday friday saturday sunday").split())   # not months: April, May, June


def _surname_len(v) -> int:
    if v[:2] in SURNAMES_2 and len(v) > 2:
        return 2
    return 1 if v[:1] in SURNAMES_1 else 0


def cjk_name(v, need_surname=True) -> bool:
    """A Chinese value is a person: not a common word or role; a surname first (unless need_surname is False)."""
    if v in NOT_NAMES:
        return False
    n = _surname_len(v)
    rest = v[n:]
    if n and rest in TITLES:
        return True                                  # 张经理, 王总
    if v.endswith(ROLE_SUFFIXES) and not (n and len(rest) == 1):
        return False                                 # 产品经理, 支付组 (but 王组 is a name)
    if not n and v.endswith(ROLE_SUFFIXES_1):
        return False                                 # 业务方, 程序员
    return bool(n) or not need_surname


def latin_words(v) -> list:
    """The words of a Latin value up to the first stop word; none when a role or team word comes first."""
    out = []
    for w in v.split():
        low = w.lower().strip("'’")
        if low in ROLE_WORDS:
            return []
        if low in STOP_WORDS:
            break
        out.append(w)
    return out


def _cell_is_name(x) -> bool:
    x = x.strip("*")
    if not NAME_VALUE_RE.match(x):
        return False
    core = x.lstrip("@")
    if re.match(r"^[一-鿿]+$", core):
        return cjk_name(core, need_surname=False)
    return len(latin_words(re.sub(r"[.-]+", " ", core))) == len(re.sub(r"[.-]+", " ", core).split())


def _labelled_names(text) -> list:
    hits = []
    for m in CJK_LABEL_NAME_RE.finditer(text):
        name, start = m.group(3), m.start(3)
        bare = m.group("duty") is None and not (m.group(1) or "").strip()
        if bare and len(name) > 3:
            continue                                 # 负责人高度重视: without '：' only short names count
        if not cjk_name(name):
            continue
        hits.append(Hit("pii", "person", start, start + len(name), name))
        pos = m.end(3)
        while True:                                  # 王芳、李娜 / 张伟和李娜
            more = CJK_MORE_NAME_RE.match(text, pos)
            if not more or not cjk_name(more.group(1)):
                break
            hits.append(Hit("pii", "person", more.start(1), more.end(1), more.group(1)))
            pos = more.end(1)
    for m in LATIN_LABEL_NAME_RE.finditer(text):
        words = latin_words(m.group(1))
        if len(words) < 2:
            continue                                 # a single first name, or a team (Payments Team)
        spans = [w.span() for w in re.finditer(r"\S+", m.group(1))]
        start, end = m.start(1), m.start(1) + spans[len(words) - 1][1]
        hits.append(Hit("pii", "person", start, end, text[start:end]))
    return hits


# ---------- secrets ----------

PRIVATE_KEY_RE = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")
CLOUD_KEY_RE = re.compile(r"(?<![A-Z0-9])(AKIA|ASIA)[0-9A-Z]{16}(?![A-Z0-9])")
KV_SECRET_RE = re.compile(r"(?i)((?:passw(?:or)?d|pwd|secret|token|api[_-]?key|access[_-]?key|client[_-]?secret"
                          r"|密碼|密码|口令)[\"']?\s*[=:：]\s*)([^\s\"'<>|,;]{4,})")
# a password stated in a sentence: 密码是 Hunter2024 / 口令为 abcd1234 / password is Hunter2024 ("is": the value
# needs a digit, so "password is required" stays; 密码为 8-16 位 and 密码是 ****** stay)
SAID_SECRET_CHARS = r"A-Za-z0-9!@#$%^&*_+=~./\\-"
SAID_SECRET_RE = re.compile(r"(?i)(?:passw(?:or)?d|密碼|密码|口令)[ \t]*(是|为|為|\bis\b)[ \t]*[:：]?[ \t]*"
                            r"([%s]{4,})(?![%s])" % (SAID_SECRET_CHARS, SAID_SECRET_CHARS))
SAID_SECRET_ALGO_RE = re.compile(r"(?i)^(?:sha|md[245]|bcrypt|argon|pbkdf|scrypt|hmac|aes|rsa)")
SAID_SECRET_UNIT_RE = re.compile(r"(?i)[ \t]*(?:位|个|個|字|characters?\b|chars?\b|digits?\b)")
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
                            if names and all(_cell_is_name(x) for x in names):
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
    for m in SAID_SECRET_RE.finditer(text):
        v = m.group(2)
        if (m.group(1).lower() == "is" and not re.search(r"[0-9]", v)) or not set(v) - set("*xX•.-_") \
                or SAID_SECRET_UNIT_RE.match(text, m.end(2)) or SAID_SECRET_ALGO_RE.match(v):
            continue
        add(Hit("secret", "kv_secret", m.start(2), m.end(2), v))
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
    for rx in (PHONE_CN_RE, PHONE_CN_GROUPED_RE, PHONE_HK_PREFIXED_RE):
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
    hits += _labelled_names(text)
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
