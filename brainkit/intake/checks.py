"""The checks on converted text: personal data, secrets, business-sensitive items, links, instruction-like lines.

`scan(text)` finds hits; `apply(text, hits, policy)` returns masked text. Placeholders are numbered per package
and the same value gets the same placeholder; the original values are never written anywhere. A "review" hit looks
like a name or a password but may as well be a word (负责人：张三李四, Reported by Sam Example, 密码是 hunter): it is
neither masked nor refused; intake lists it for the submitter to check (review_notes).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile

# ---------- personal data ----------

HKID_RE = re.compile(r"(?<![A-Za-z0-9])([A-Z]{1,2})([0-9]{6})\(?([0-9A])\)?(?![A-Za-z0-9])")
CNID_RE = re.compile(r"(?<![0-9])([1-9][0-9]{16}[0-9Xx])(?![0-9])")
PHONE_CN_RE = re.compile(r"(?<![0-9])(?:\+?86[-\s]?)?1[3-9][0-9]{9}(?![0-9])")
# the usual written form 138 0013 8000 / 138-0013-8000 / +86-138-0013-8000: 3-4-4 with one kind of separator, not
# glued to a code (ORD-138-2026-0001 stays)
PHONE_CN_GROUPED_RE = re.compile(r"(?<![A-Za-z0-9_-])(?:(?:\+|00)?86[ \u00a0-]?|\(\+?86\)[ \u00a0]?)?"
                                 r"1[3-9][0-9]([ \u00a0-])[0-9]{4}\1[0-9]{4}(?![0-9A-Za-z_-])")
# +852 9123 4567, 00852-9123-4567, and the usual HK way with the code in brackets: (852) 9123 4567, (+852) 2123 4567
PHONE_HK_PREFIXED_RE = re.compile(r"(?:(?<![0-9])(?:\+|00)?852|\(\+?852\))[-\s]?[2-9][0-9]{3}[-\s]?[0-9]{4}(?![0-9])")
PHONE_HK_CONTEXT_RE = re.compile(r"(?i)(電話|电话|手機|手机|聯絡|联系|tel|phone|mobile|whatsapp)[^0-9\n]{0,12}"
                                 r"((?<![0-9])[2-9][0-9]{3}[-\s]?[0-9]{4}(?![0-9]))")
EMAIL_RE = re.compile(r"(?<![A-Za-z0-9._%+-])[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9-]{1,63}(?:\.[A-Za-z0-9-]{1,63})+")
CARD_RE = re.compile(r"(?<![0-9])(?:[0-9][ -]?){14,18}[0-9](?![0-9])")
MENTION_RE = re.compile(r"@([A-Z][a-zA-Z]+(?:[  ]+[A-Z][A-Za-z]+){0,3}|[一-鿿]{2,4})")
# a cell that looks like a person: 2-4 CJK chars, or 1-4 capitalised Latin words (optionally @-mentioned)
NAME_VALUE_RE = re.compile(r"^@?(?:[一-鿿]{2,4}|[A-Z][a-z]+(?:[ .-]+[A-Z][A-Za-z]+){0,3}|[A-Z][a-z]+ [A-Z]{2,})$")

# A name after a label (负责人：张伟, On-call: Zhang Wei, Reported by Alice Wang) or in a name column of a table.
# Precision first: a value is a person only when it is name-shaped. Chinese: a common surname, then one or two
# characters (王芳, 張三丰), or a surname and a title (张经理, 王工程师). Latin: 2-4 capitalised words; after "... by"
# one of them must be a known surname. Roles, teams, systems and common words are not names (产品经理, 支付组, 顾客,
# 白班, 常务副总, Payments Team, Zabbix Server, New Relic). What is name-shaped but may well be a word (4 characters
# after a one-character surname, "Reported by" without a known surname) is a "review" hit: neither masked nor refused,
# listed for the person to check.
SURNAMES_2 = ("欧阳 歐陽 司马 司馬 上官 诸葛 諸葛 东方 東方 皇甫 尉迟 尉遲 公孙 公孫 慕容 令狐 司徒 夏侯 长孙 長孫 宇文 "
              "轩辕 軒轅 端木 西门 西門 南宫 南宮 独孤 獨孤 呼延 赫连 赫連").split()
SURNAMES_1 = set("王李张刘陈杨黄赵吴周徐孙马朱胡郭何高林罗郑梁谢宋唐许韩冯邓曹彭曾肖萧田董袁潘于蒋蔡余杜叶程苏魏吕丁任沈"
                 "姚卢姜崔钟谭陆汪范金石廖贾夏韦付傅方白邹孟熊秦邱江尹薛闫阎段雷侯龙史陶黎贺顾毛郝龚邵万钱严覃武戴莫孔汤"
                 "康易乔赖文常温庞樊兰殷施洪翁柯庄欧区麦关岑甘古冼邝霍伍祝耿倪鲁葛聂"
                 "張劉陳楊黃趙吳孫馬羅鄭謝許韓馮鄧蕭葉蘇呂盧鍾鐘譚陸賈韋鄒閆閻龍賀顧龔錢嚴湯賴龐蘭莊歐區麥關鄺聶魯於萬"
                 "喬溫")
# surname + one of these is a person (张经理, 王总, 李老师, 王工程师); on its own it is a role
TITLES = set(("经理 經理 总 總 总监 總監 工 老师 老師 主管 主任 先生 女士 小姐 老板 老闆 博士 教授 律师 律師 医生 醫生 姐 哥 "
              "总裁 總裁 董 部长 部長 组长 組長 科长 科長 处长 處長 局长 局長 行长 行長 院长 院長 校长 校長 厂长 廠長 队长 "
              "隊長 主席 生 太 同学 同學 工程师 工程師 架构师 架構師 设计师 設計師 分析师 分析師 专员 專員 顾问 顧問 助理 "
              "秘书 秘書 会计 會計 研究员 研究員 会长 會長 师傅 師傅").split())
ROLE_SUFFIXES = tuple(("经理 經理 主管 总监 總監 工程师 工程師 架构师 架構師 设计师 設計師 分析师 分析師 专员 專員 同学 同學 "
                       "负责人 負責人 团队 團隊 小组 小組 组 組 部 部门 部門 中心 委员会 委員會 办公室 辦公室 公司 集团 集團 "
                       "银行 銀行 证券 證券 基金 保险 保險 平台 系统 系統 服务 服務 机器人 機器人").split())
ROLE_SUFFIXES_1 = tuple("方员員长長会會岗崗师師")       # one-character endings: only when no surname leads
# common words that start with a surname character (顾客 is a customer, 白班 the day shift, 任务 a task)
NOT_NAMES = set(("产品 產品 运维 運維 运营 運營 开发 開發 测试 測試 客服 财务 財務 法务 法務 设计 設計 前端 后端 後端 研发 研發 "
                 "业务 業務 用户 用戶 客户 客戶 系统 系統 平台 待定 暂无 暫無 本人 自己 全员 全員 大家 双方 雙方 甲方 乙方 "
                 "我方 对方 對方 各方 管理员 管理員 机器人 機器人 值班 未定 未知 不限 任意 所有人 任何人 安全 安排 文本 文字 "
                 "文档 文檔 文件 金额 金額 金融 高级 高級 高层 高層 高管 高优 方案 方式 方法 方面 周期 週期 周末 週末 周报 "
                 "週報 周会 常规 常規 常量 程序 余额 餘額 万一 萬一 严重 嚴重 温度 溫度 白天 周一 周二 周三 周四 周五 周六 "
                 "周日 周天 週一 週二 週三 週四 週五 週六 週日 董事 董事会 董事會 董事长 董事長 理事会 理事會 股东 股東 "
                 "顾客 顧客 顾问 顧問 任务 任務 任何 白班 常务 常務 常驻 常駐 常用 文员 文員 文案 钱包 錢包 钱款 錢款 关键 關鍵 "
                 "关联 關聯 关于 關於 关闭 關閉 区域 區域 区块 區塊 史上 马上 馬上 黄金 黃金 付款 付费 付費 万达 萬達 万科 "
                 "萬科 苏宁 蘇寧 高盛 金蝶 江湖 何时 何時 龙头 龍頭 雷达 雷達 康复 康復 庄家 莊家 欧洲 歐洲 欧元 歐元 曾经 曾經 "
                 "许可 許可 陈列 陳列 陈述 陳述 张贴 張貼 王牌 石油 林业 林業 夏季 高频 高頻 余量 餘量 方向 方便 周转 週轉 "
                 "金库 金庫 白色 白名单 白名單 宋体 宋體 黄色 黃色 于是 程序员 程序員 任务调度 任務調度 江南 武装 武裝 "
                 "易用 易于 易於 温馨 溫馨 汤底 湯底").split())
NOUN_ENDS = set("员員组組部类類表单單群号號方会會岗崗师師长長")   # 程序员, 任务单: a common word + one of these
CJK_LABELS = ("负责人|負責人|联系人|聯絡人|聯繫人|联络人|对接人|對接人|值班人员|值班人員|值班人|值班同学|值班同學|报告人|"
              "報告人|提出人|处理人|處理人|经办人|經辦人|跟进人|跟進人|执行人|執行人|申请人|申請人|审批人|審批人|审核人|"
              "審核人|修订人|修訂人|发起人|發起人|提交人|报修人|報修人|指派给|指派給|分配给|分配給")
_CJK_END = r"(?:(?![一-鿿])|(?=[和及与與等]))"
CJK_LABEL_NAME_RE = re.compile(r"(?:(?:%s)([ \t]*(?:[:：]|是|为|為)?[ \t]*)|值班[ \t]*[:：][ \t]*(?P<duty>))"
                               r"@?([一-鿿]{2,4})%s" % (CJK_LABELS, _CJK_END))
CJK_MORE_NAME_RE = re.compile(r"[ \t]*(?:[、/]|和|及|与|與)[ \t]*@?([一-鿿]{2,4})%s" % _CJK_END)
# Latin: a field label needs a separator (Owner: Alice Wang; "Contact Form Validation" is a feature), a "by" label
# does not (Reported by Alice Wang)
LATIN_FIELD_LABELS = (r"owner|assignee|reporter|on[- ]?call|contact(?:[ \t]+person)?|point[ \t]+of[ \t]+contact"
                      r"|approver|reviewer|author|requester")
LATIN_BY_LABELS = (r"assigned[ \t]+to|reported[ \t]+by|approved[ \t]+by|reviewed[ \t]+by|prepared[ \t]+by"
                   r"|written[ \t]+by|raised[ \t]+by|requested[ \t]+by|filed[ \t]+by|opened[ \t]+by|escalated[ \t]+to"
                   r"|handled[ \t]+by|acknowledged[ \t]+by|resolved[ \t]+by|fixed[ \t]+by")
# a table column of people: headed by one of the labels above (负责人, 处理人, 对接人, Assignee, Reporter, Reported by
# ...) or by a name column (姓名, 客户名称, Customer name). One list for both: a word that names a person after a
# label names the people in a column too.
NAME_COL_RE = re.compile(r"%s|姓名|客[戶户]名稱|客户名称|^\s*(?:%s|%s|contact[ \t]+name|customer[ \t]+name"
                         r"|client[ \t]+name)\s*$" % (CJK_LABELS, LATIN_FIELD_LABELS, LATIN_BY_LABELS), re.I)
_LATIN_WORD = r"[A-Z][a-z]+(?:['’-][A-Za-z]+)?"
LATIN_LABEL_NAME_RE = re.compile(
    r"(?i:\b(?:(?P<field>%s)\b[ \t]*[:：=]|(?P<by>%s)\b[ \t]*[:：=]?))[ \t]*@?"
    r"(?P<v>%s(?:[ \t]+(?:%s|[A-Z]{2,}(?![a-z]))){1,3})(?![A-Za-z])"
    % (LATIN_FIELD_LABELS, LATIN_BY_LABELS, _LATIN_WORD, _LATIN_WORD))
ROLE_WORDS = set((   # a Latin value with one of these is a role, a team or a system, not a person (Payments Team)
    "team teams squad group groups guild tribe chapter department dept org organisation organization division unit "
    "office board committee council center centre desk helpdesk platform product products engineering engineer "
    "engineers ops operations devops sre oncall on-call primary secondary backup backend frontend mobile web data infra "
    "infrastructure security support service services admin admins administrator bot bots robot system systems "
    "monitor monitoring alert alerts alerting alertmanager grafana prometheus pagerduty opsgenie sentry datadog "
    "gitlab github jira kirocrew agent agents ci cd pipeline owner owners lead leads "
    "manager managers director architect analyst designer developer developers reviewer reviewers approver "
    "approvers builder builders customer customers client clients user users vendor vendors partner partners legal "
    "finance compliance audit risk qa qe test testing tier level everyone nobody none tbd unassigned unknown me "
    "myself us we you them they "
    "server servers job jobs cron scheduler daemon worker workers script scripts scanner probe probes watchdog "
    "exporter webhook webhooks hook hooks api apis app apps tool tools check checks healthcheck health uptime alarm "
    "alarms zabbix nagios splunk kibana elastic elasticsearch cloudwatch pingdom statuspage newrelic relic dependabot "
    "renovate snyk sonar sonarqube corp corporation inc ltd llc company bank holdings gmbh plc tech hr sales "
    "marketing form forms view views button section panel dialog wizard flow process rotation schedule queue").split())
TOOL_WORDS = set("jenkins kiro brain bridge runner head".split())   # tools that are also surnames: Sarah Jenkins
STOP_WORDS = set((   # a Latin name ends before these (Reported by Alice Wang On Monday)
    "the a an our my your their this that all any no not same see above below on at in of for and or with from to "
    "by via monday tuesday wednesday thursday friday saturday sunday").split())   # not months: April, May, June
CELL_STOP_WORDS = STOP_WORDS - {"to", "or", "an", "on"}           # in a name cell: surnames (Peter To, Kenneth Or)
# after "Reported by" ... a person needs one of these (pinyin, Cantonese, common English surnames)
LATIN_SURNAMES = set((
    "wang li zhang liu chen yang huang zhao wu zhou xu sun ma zhu hu guo he gao lin luo zheng liang xie song tang han "
    "feng deng cao peng zeng xiao tian dong yuan pan yu jiang cai du ye cheng su wei lu ding ren shen yao cui zhong "
    "tan fan jin shi liao jia xia fu fang bai zou meng xiong qin qiu yin xue yan duan lei hou long tao gu mao hao gong "
    "shao wan qian dai mo kong kang yi qiao lai wen chang pang lan ou mai guan cen gan geng ni nie zhan "
    "chan wong leung cheung lau lee ho ng lam tsang chow yip tam kwok lo mak choi fung tse yeung hui siu kwan law chu "
    "poon yuen au chung ip ko so lai tong tsui chiu shum sze mok hung chau chui kwong fong kam chiang tsoi yau lui "
    "wai lok chik tsoi chui leong lim tan ong teo goh koh kho to or an on "     # to / or / an / on: stop words but in a name cell
    "smith johnson williams brown jones garcia miller davis rodriguez martinez hernandez lopez gonzalez wilson "
    "anderson thomas taylor moore jackson martin perez thompson white harris sanchez clark ramirez lewis robinson "
    "walker young allen king wright scott torres nguyen hill flores green adams nelson baker hall rivera campbell "
    "mitchell carter roberts gomez phillips evans turner diaz parker cruz edwards collins reyes stewart morris morales "
    "murphy cook rogers gutierrez ortiz morgan cooper peterson bailey reed kelly howard ramos kim cox ward richardson "
    "watson brooks chavez wood james bennett gray mendoza ruiz hughes price alvarez castillo sanders patel myers ross "
    "foster jimenez jenkins doe singh kumar shah khan park choi jung kang yoon tanaka suzuki sato watanabe").split())


def _surname_len(v) -> int:
    if v[:2] in SURNAMES_2 and len(v) > 2:
        return 2
    return 1 if v[:1] in SURNAMES_1 else 0


def cjk_person(v, need_surname=True):
    """'name', 'maybe' (name-shaped, but may be a word: a person should check) or None for a Chinese value. Without
    need_surname (a name column) a value that is no role or common word is a name."""
    if v in NOT_NAMES or (len(v) == 4 and v[:2] in NOT_NAMES) or \
            (len(v) == 3 and v[:2] in NOT_NAMES and v[2] in NOUN_ENDS):
        return None                                  # 顾客, 常务副总, 程序员
    n = _surname_len(v)
    rest = v[n:]
    if n and rest in TITLES:
        return "name"                                # 张经理, 王总, 王工程师
    if v.endswith(ROLE_SUFFIXES) and not (n and len(rest) == 1):
        return None                                  # 产品经理, 支付组 (but 王组 is a name)
    if not n and v.endswith(ROLE_SUFFIXES_1):
        return None                                  # 业务方, 程序员
    if not n:
        return None if need_surname else "name"
    if n == 1 and (len(v) == 4 or (len(v) == 3 and v.endswith(ROLE_SUFFIXES_1))):
        return "maybe"                               # 张三李四, 孙店长: as often a phrase or a role as a name
    return "name"


def cjk_name(v, need_surname=True) -> bool:
    """A Chinese value is a person (cjk_person says 'name')."""
    return cjk_person(v, need_surname) == "name"


def latin_person(v, cell=False) -> tuple:
    """(verdict, words) for a Latin value read up to the first stop word: verdict 'name', 'maybe' or None (a role,
    team or system word, or only tool names such as Jenkins); 'name' needs a known surname. In a name cell (cell=True)
    To / Or / An / On are surnames, not stop words."""
    stops = CELL_STOP_WORDS if cell else STOP_WORDS
    words = []
    for w in v.split():
        low = w.lower().strip("'’")
        if low in stops:
            break
        words.append(low)
    if not words or any(w in ROLE_WORDS for w in words) or all(w in TOOL_WORDS for w in words):
        return None, 0
    known = any(w in LATIN_SURNAMES or w.split("-")[-1] in LATIN_SURNAMES for w in words)
    return ("name" if known else "maybe"), len(words)


CELL_SPLIT_RE = re.compile(r"<br\s*/?>|[,，、/;；&()（）\[\]【】]|(?<=[一-鿿]{2})[和及与與](?=[一-鿿]{2})")


def _cell_person(x):
    """'name', 'maybe' or None for one part of a name-column cell. The column says a person may stand here, but only
    what the name check is sure of is a name (张伟, 王工程师, Alice Wang, Peter To); a value without a surname (风控,
    商家, Billing) is 'maybe', listed for the person to check."""
    if not NAME_VALUE_RE.match(x):
        return None
    core = x.lstrip("@")
    if re.match(r"^[一-鿿]+$", core):
        if cjk_person(core, need_surname=False) is None:
            return None
        return cjk_person(core) or "maybe"
    flat = re.sub(r"[.-]+", " ", core)
    verdict, n = latin_person(flat, cell=True)
    return verdict if n == len(flat.split()) else None


# Markdown emphasis on a label or a name (**负责人**：李娜, - **联系人：** 王芳, **Owner:** Alice Wang, Reported by **Bob
# Chen**): the label rules read the text with the markers blanked out (same length, so the offsets hold); an
# underscore inside a word (owner_id) stays
EMPHASIS_RE = re.compile(r"\*+|(?<![A-Za-z0-9])_+|_+(?![A-Za-z0-9])")


def _labelled_names(text) -> list:
    hits = []
    original = text
    text = EMPHASIS_RE.sub(lambda m: " " * len(m.group(0)), text)
    for m in CJK_LABEL_NAME_RE.finditer(text):
        name, start = m.group(3), m.start(3)
        bare = m.group("duty") is None and not (m.group(1) or "").strip()
        if bare and len(name) > 3:
            continue                                 # 负责人高度重视: without '：' only short names count
        verdict = cjk_person(name)
        if verdict is None:
            continue
        hits.append(Hit("pii" if verdict == "name" else "review", "person", start, start + len(name), name))
        pos = m.end(3)
        while True:                                  # 王芳、李娜 / 张伟和李娜
            more = CJK_MORE_NAME_RE.match(text, pos)
            verdict = cjk_person(more.group(1)) if more else None
            if verdict is None:
                break
            hits.append(Hit("pii" if verdict == "name" else "review", "person", more.start(1), more.end(1),
                            more.group(1)))
            pos = more.end(1)
    for m in LATIN_LABEL_NAME_RE.finditer(text):
        verdict, n = latin_person(m.group("v"))
        if n < 2:
            continue                                 # a single first name, or a team (Payments Team)
        if m.group("field"):
            verdict = "name"                         # Owner: / On-call: / Assignee: a field that names a person
        spans = [w.span() for w in re.finditer(r"\S+", m.group("v"))]
        start, end = m.start("v"), m.start("v") + spans[n - 1][1]
        hits.append(Hit("pii" if verdict == "name" else "review", "person", start, end, original[start:end]))
    return hits


# ---------- secrets ----------

# the armour line of a private key in any of its text forms: PEM (RSA, EC, PKCS#8, ENCRYPTED), OpenSSH, SSH2
# and PGP (its armour line ends in "PRIVATE KEY BLOCK"); the same shape as gitleaks' private-key rule
PRIVATE_KEY_RE = re.compile(r"(?i)-----BEGIN[ A-Z0-9_-]{0,100}PRIVATE KEY(?: BLOCK)?-----")
CLOUD_KEY_RE = re.compile(r"(?<![A-Z0-9])(AKIA|ASIA)[0-9A-Z]{16}(?![A-Z0-9])")
KV_SECRET_RE = re.compile(r"(?i)((?:passw(?:or)?d|pwd|secret|token|api[_-]?key|access[_-]?key|client[_-]?secret"
                          r"|密碼|密码|口令)[\"']?\s*[=:：]\s*)([^\s\"'<>|,;]{4,})")
# a password stated in a sentence: 密码是 Hunter2024 / 口令为 abcd1234 / password is Hunter2024 (after "is" the value
# needs a digit, so "password is required" stays; 密码为 8-16 位 and 密码是 ****** stay). A value of letters only
# after 是 / 为 (密码是 hunter) may be a password or a word: a review hit.
SAID_SECRET_CHARS = r"A-Za-z0-9!@#$%^&*_+=~./\\-"
SAID_SECRET_RE = re.compile(r"(?i)(?:passw(?:or)?d|密碼|密码|口令)[ \t]*(是|为|為|\bis\b)[ \t]*[:：]?[ \t]*"
                            r"([%s]{4,})(?![%s])" % (SAID_SECRET_CHARS, SAID_SECRET_CHARS))
SAID_SECRET_UNIT_RE = re.compile(r"(?i)[ \t]*(?:位|个|個|字|characters?\b|chars?\b|digits?\b)")
# words that describe a password or a token instead of being one: formats, algorithms, types, rules
# (密码为 SM4-CBC 加密存储, password is base64-encoded, 口令为 TOTP 动态口令, 密码为 null, token: string, 6-digit)
NOT_SECRET_WORDS = set((
    "base64 base32 base58 hex utf ascii md5 sha sha1 sha2 sha3 sha256 sha384 sha512 sm2 sm3 sm4 aes des 3des tdes rsa "
    "ecc ecdsa ed25519 x25519 dsa hmac cbc gcm ecb ctr cfb ofb pkcs pem der bcrypt scrypt argon argon2 argon2id pbkdf "
    "pbkdf2 crypt totp hotp otp pin jwt jws jwe oauth oauth2 saml ldap kerberos ntlm sso mfa 2fa plaintext plain "
    "cleartext text null nil none empty blank undefined required optional mandatory encrypted encrypt encoded encode "
    "hashed hash salted salt masked hidden random generated temporary dynamic expired digit digits char chars "
    "character characters letter letters numeric number numbers alphanumeric string str int integer long bool boolean "
    "varchar object array json xml yaml uuid true false yes no tbd todo").split())
SECRET_FAMILIES = ("sha", "md", "sm", "aes", "rsa", "des", "pkcs", "pbkdf", "argon", "base", "utf", "hmac", "crc")
BEARER_RE = re.compile(r"(?i)\bbearer\s+([A-Za-z0-9._~+/-]{16,}=*)")
TOKEN_RE = re.compile(r"\b(ghp_[A-Za-z0-9]{36}|github_pat_[A-Za-z0-9_]{40,}|xox[abpr]-[A-Za-z0-9-]{10,}"
                      r"|glpat-[A-Za-z0-9_-]{20}|eyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,})\b")
_NOT_ASCII_RE = re.compile(r"[\u2e80-\u9fff\u3000-\u303f\uff00-\uffef]")


def describes_a_secret(v) -> bool:
    """The value names a format, an algorithm, a type or a rule (base64-encoded, SM4-CBC, TOTP, null, 6-digit)."""
    words = [w for w in re.split(r"[^a-z0-9]+", v.lower()) if w and not w.isdigit()]
    return bool(words) and all(w in NOT_SECRET_WORDS or re.sub(r"[0-9]+$", "", w) in SECRET_FAMILIES for w in words)


def _blanked(v) -> bool:
    return not set(v) - set("*xX•.-_")               # ******, xxxx: already masked


def _kv_secrets(text) -> list:
    hits = []
    for m in KV_SECRET_RE.finditer(text):
        cut = _NOT_ASCII_RE.search(m.group(2))
        v = m.group(2)[:cut.start()] if cut else m.group(2)          # 密码：abc123（测试环境）
        if len(v) < 4 or _blanked(v) or v.startswith(("$", "%(", "{{")) or describes_a_secret(v) or \
                SAID_SECRET_UNIT_RE.match(text, m.start(2) + len(v)):
            continue                                 # 密码：8-16位 is a rule
        hits.append(Hit("secret", "kv_secret", m.start(2), m.start(2) + len(v), v))
    for m in SAID_SECRET_RE.finditer(text):
        v = m.group(2).rstrip(".")
        if len(v) < 4 or _blanked(v) or SAID_SECRET_UNIT_RE.match(text, m.end(2)) or describes_a_secret(v):
            continue
        digit = re.search(r"[0-9]", v)
        if m.group(1).lower() == "is" and not digit:
            continue                                 # password is required
        sure = digit or re.search(r"[^A-Za-z]", v)
        hits.append(Hit("secret" if sure else "review", "kv_secret" if sure else "secret", m.start(2),
                        m.start(2) + len(v), v))
    return hits


# ---------- business-sensitive ----------

# An amount carries a currency (a code, a sign or a name, before or after the number) or 元 (README, gate 3):
# HKD 50,000, EUR 300, 300 EUR, €300, 港幣 50,000, 人民币 5,000, 人民币 300 万, 100萬港元, 5 万元, 3,000 元. A number
# with 万 / 亿 alone is a count (峰值 QPS 10万, 日活 100万), the user's decision of 2026-11-10.
CUR_CODE = r"(?:HKD|USD|CNY|RMB|CNH|EUR|GBP|SGD|JPY|AUD)"
CUR_NAME = r"(?:港元|港幣|港币|美元|美金|人民幣|人民币|歐元|欧元|英鎊|英镑|日元)"
CUR = r"(?:%s|HK\$|US\$|S\$|¥|￥|\$|€|£|%s)" % (CUR_CODE, CUR_NAME)     # before the number (元 alone only after it)
MONEY_WORD = r"(?:元|%s)" % CUR_NAME                                       # after the number
CJK_SCALE = r"(?:[百千]?[億亿萬万]|[百千])"
SCALE = r"(?:%s|k|K|m|M|bn|million|billion)" % CJK_SCALE                    # only after a currency: 3M alone is a tenor
_NUM = r"[0-9][0-9,]*(?:\.[0-9]+)?"
_SP = r"[ \t\u00a0]?"
AMOUNT_RE = re.compile(r"(?<![A-Za-z0-9])(?:{cur}{sp}{num}(?:{sp}{scale})?(?:{sp}{word})?"
                       r"|{num}{sp}(?:{cjk}{sp})?{word}"
                       r"|{num}{sp}(?:{scale}{sp})?{code})(?![A-Za-z0-9])".format(
                           cur=CUR, sp=_SP, num=_NUM, scale=SCALE, word=MONEY_WORD, cjk=CJK_SCALE, code=CUR_CODE))
# A company name ends with its suffix. The suffixes are also the head of common words (银行卡, 银行账户, 证券账户, 证券代码,
# 资产管理计划): followed by one of these characters it is such a word, not a name. A Latin name stays on one line
# (a heading "Payment Methods" over "Bank cards ..." is no company).
COMPANY_WORD_NEXT = "卡账帳户戶号號转轉流代码碼间間业業系端侧側网網柜櫃存利汇匯信名回渠接交市投类類级級费費产產品计計规規"
COMPANY_RE = re.compile(r"[一-鿿A-Za-z0-9&·（）()]{2,30}?(?:股份有限公司|有限公司|有限責任公司|銀行|银行|集團|集团|證券|证券|"
                        r"基金管理|資產管理|资产管理)(?![%s])"
                        r"|\b[A-Z][A-Za-z0-9&.\-]*(?:[ \t]+[A-Z][A-Za-z0-9&.\-]*){0,5}[ \t]+(?:Limited|Ltd\.?|Bank|"
                        r"Securities|Inc\.?|LLC|Corp\.?|Corporation|Holdings|Group)\b" % COMPANY_WORD_NEXT)
# 合同编号：HT-2026-001, Contract No. C-12345: "No" is a word of its own and the number has a digit (a contract
# notification, the contract no longer applies)
CONTRACT_RE = re.compile(r"(?:合同|合約|合约|協議|协议|Contract)\s*(?:編號|编号|號|号|No(?![A-Za-z])\.?)\s*[:：#]?\s*"
                         r"((?=[A-Za-z-]*[0-9])[A-Za-z0-9-]{4,})", re.I)

# ---------- links and instructions ----------

URL_RE = re.compile(r"https?://([A-Za-z0-9.-]+)(?::\d+)?[^\s)\]>\"']*")
# A line that reads as an approval, a rejection or "allow test change": every form Lean takes as one (its APPROVE_RE,
# REJECT_RE, ALLOW_PROTECTED_RE: 批准DESIGN, 驳回PLAN, approveDESIGN, 同意：PLAN), and more (approve all ..., 批准所有 ...).
# A Chinese word needs no space after it, but must not run on into another one (拒绝服务, 批准人, 同意书 are words).
APPROVAL_KIND = r"IDEATION|REQUIREMENTS|DESIGN|INCEPTION|PLAN|RELEASE|CR-[A-Za-z0-9_]+-[0-9]+"
APPROVAL_LINE_RE = re.compile(r"(?im)^[ \t]*(?:[-*+][ \t]+|[0-9]{1,3}[.)、][ \t]*)?"
                              r"(?:(?:approve|approved|批准|同意|reject|驳回|拒绝)[ \t]*[:：]?[ \t]*(?:%s)"
                              r"|(?:approve|approved|reject|allow test change)\b"
                              r"|(?:批准|同意|驳回|拒绝|允许改测试|允许修改测试|允许修改验收测试)(?![一-鿿])"
                              r"|(?:批准|同意)(?:所有|全部)).*$" % APPROVAL_KIND)
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
    """The people in markdown-table columns whose header names a person (修訂人, 負責人, Owner ...)."""
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
                            # each part on its own: 张伟、支付组 masks 张伟 and keeps the team
                            cuts = [0] + [x for m in CELL_SPLIT_RE.finditer(cell) for x in m.span()] + [len(cell)]
                            for p0, p1 in zip(cuts[::2], cuts[1::2]):
                                part = cell[p0:p1].strip().strip("*").strip()
                                verdict = _cell_person(part) if part else None
                                if verdict:
                                    s = offsets[j] + a + cell.index(part, p0)
                                    hits.append(Hit("pii" if verdict == "name" else "review", "person", s,
                                                    s + len(part), part))
                j += 1
            i = j
        else:
            i += 1
    return hits


FENCE_RE = re.compile(r"(?m)^[ \t]*(?:`{3,}|~{3,})")
INLINE_CODE_RE = re.compile(r"`[^`\n]+`")


def _code_spans(text) -> list:
    """[(start, end)] of fenced code blocks and inline code."""
    fences = [m.start() for m in FENCE_RE.finditer(text)] + [len(text)]
    spans = list(zip(fences[0:-1:2], fences[1::2]))
    return spans + [m.span() for m in INLINE_CODE_RE.finditer(text)]


def scan(text) -> list:
    hits = []
    add = hits.append
    for m in PRIVATE_KEY_RE.finditer(text):
        add(Hit("secret", "private_key", m.start(), m.end(), m.group(0)))
    for m in CLOUD_KEY_RE.finditer(text):
        add(Hit("secret", "cloud_key", m.start(), m.end(), m.group(0)))
    hits += _kv_secrets(text)
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
    code = _code_spans(text)
    for m in MENTION_RE.finditer(text):
        v = m.group(1)
        if re.match(r"[一-鿿]", v):
            if cjk_person(v) is not None:            # not @所有人, @大家
                add(Hit("pii", "person", m.start(), m.end(), m.group(0)))
            continue
        if any(a <= m.start() < b for a, b in code):
            continue                                 # an annotation (@Service, @Autowired)
        verdict, n = latin_person(v)
        if verdict:                                  # not @Data, @Service
            end = m.start(1) + [w.end() for w in re.finditer(r"\S+", v)][n - 1]
            add(Hit("pii", "person", m.start(), end, text[m.start():end]))
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
    """Drop hits that overlap a stronger hit (secret > pii > business > review); links and instructions stay.

    One pass after the sort: the hits kept (but links and instructions) never overlap and come in order of start,
    so only the last of them can overlap the next hit (a long CSV export has tens of thousands of hits)."""
    rank = {"secret": 0, "pii": 1, "business": 2, "review": 3, "link": 4, "instruction": 5}
    hits.sort(key=lambda h: (h.start, rank[h.cat], -(h.end - h.start)))
    kept = []
    last = -1                                        # index in kept of the last hit that is no link or instruction
    for h in hits:
        if h.cat in ("link", "instruction"):
            kept.append(h)
            continue
        clash = kept[last] if last >= 0 and h.start < kept[last].end and kept[last].start < h.end else None
        if clash is None:
            last = len(kept)
            kept.append(h)
        elif _replaces(h, clash, rank):
            kept[last] = None                        # dropped; h goes to the end, as before
            last = len(kept)
            kept.append(h)
    return sorted((k for k in kept if k is not None), key=lambda h: h.start)


def _replaces(h, k, rank) -> bool:
    """h takes the place of k, which it overlaps: the stronger hit wins, but personal data that holds a secret stays
    whole (a phone 139 8888 1234 holding the PIN 8888), so no part of it shows around the secret's placeholder."""
    if {h.cat, k.cat} == {"pii", "secret"}:
        pii, secret = (h, k) if h.cat == "pii" else (k, h)
        if pii.start <= secret.start and secret.end <= pii.end and pii.end - pii.start > secret.end - secret.start:
            return h is pii
    return rank[h.cat] < rank[k.cat]


# A value judged sensitive once is masked wherever it occurs in the package as a whole word: other rows, other files,
# the hidden area and file names. That holds for values found by their own form (check digits, phone, card and
# address formats, key formats, amounts, company suffixes) and for names and passwords found next to a label (R1-48,
# the user's decision of 2026-11-10). Only a sure find spreads: a name the name check is not sure of (提出人：顾客,
# 负责人列的 风控) is never a value, and a name that is also a word (负责人：高峰, but 高峰期) is masked only where found. A password that is a common word of requirements (password: admin) or names its own
# field (token=refresh_token, api_key: X-Api-Key) is masked where it was found but does not spread, so the word
# elsewhere (a role, /api/admin, the refresh_token field) stays readable.
PROPAGATE_KINDS = {"hkid", "cnid", "phone", "email", "card", "private_key", "cloud_key", "bearer", "token",
                   "amount", "company", "contract", "person", "kv_secret"}
WORD_PASSWORDS = set("admin administrator root test demo guest user default monkey".split())
WORD_NAMES = set("高峰".split())         # a name that is also a word (高峰期): masked where found, not spread
FIELD_WORDS = set("password passwd pwd secret token key api apikey access refresh client auth session id app bearer"
                  .split())
FIELD_NAME_RE = re.compile(r"[A-Za-z]+(?:[-_.][A-Za-z]+)*")
_WORD_CHAR = re.compile(r"[A-Za-z0-9_]")


def _spreads(kind, v) -> bool:
    if len(v) < 2:
        return False
    if kind == "person":
        return v not in WORD_NAMES
    if kind == "kv_secret":
        return v.lower() not in WORD_PASSWORDS and not _field_name(v)
    return True


def _field_name(v) -> bool:
    """The value is itself a field name (refresh_token, X-Api-Key): letters only, each part a field word or a letter.
    Prod_Secret_2026 or live-api-key-7f3k is a password that only holds such a word."""
    return bool(FIELD_NAME_RE.fullmatch(v)) and all(len(w) == 1 or w.lower() in FIELD_WORDS
                                                    for w in re.split(r"[-_.]", v))


def _whole_word(text, start, end) -> bool:
    """A Latin or digit edge of the value is no part of a longer word or number (Service in RefundService, 5万 in 15万,
    8888 in 13988881234). Chinese has no spaces between words, so a Chinese edge always counts."""
    def glued(a, b):
        return 0 <= a and b < len(text) and _WORD_CHAR.match(text[a]) and _WORD_CHAR.match(text[b])
    return not glued(start - 1, start) and not glued(end - 1, end)


def propagate(text, hits, values) -> list:
    """Add a hit for every occurrence of a value already judged sensitive anywhere in the submission (the kinds in
    PROPAGATE_KINDS, see _spreads, and what gitleaks found: a key by its form)."""
    extra = []
    for (cat, kind), vals in values.items():
        if kind not in PROPAGATE_KINDS and not kind.startswith("gitleaks:"):
            continue
        for v in vals:
            if kind == "person":
                v = v.lstrip("@")                    # an @mention spreads as the name (@李娜 -> 李娜 elsewhere)
            if not _spreads(kind, v):
                continue
            start = text.find(v)
            while start >= 0:
                if _whole_word(text, start, start + len(v)):
                    extra.append(Hit(cat, kind, start, start + len(v), v))
                    start += len(v)
                else:
                    start += 1
                start = text.find(v, start)
    return _dedupe(list(hits) + extra) if extra else hits


_GITLEAKS_SEEN = {}          # (tool, real path, size, content sha256) -> findings: a dry run and the real run, or
_GITLEAKS_SEEN_MAX = 512     # a long-running process, do not run gitleaks again on the same file


def gitleaks_hits(path) -> list:
    """Extra secret findings from gitleaks when it is installed: [(rule, line, secret)]. Empty when not installed. The
    result for the same file with the same content is reused (gitleaks takes about half a second per call)."""
    tool = shutil.which("gitleaks")
    if not tool:
        return []
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    key = (tool, os.path.realpath(path), os.path.getsize(path), h.hexdigest())
    if key not in _GITLEAKS_SEEN:
        if len(_GITLEAKS_SEEN) >= _GITLEAKS_SEEN_MAX:
            _GITLEAKS_SEEN.clear()
        _GITLEAKS_SEEN[key] = _run_gitleaks(tool, path)
    return list(_GITLEAKS_SEEN[key])


def _run_gitleaks(tool, path) -> list:
    with tempfile.TemporaryDirectory() as d:
        report = os.path.join(d, "r.json")
        for args in (["dir", path], ["detect", "--no-git", "--source", path]):
            p = subprocess.run([tool] + args + ["--report-format", "json", "--report-path", report, "--no-banner",
                                                "--exit-code", "0"], capture_output=True, text=True, timeout=120)
            if p.returncode == 0 and os.path.exists(report):
                try:
                    with open(report) as f:
                        return [(x.get("RuleID", ""), x.get("StartLine", 0), x.get("Secret") or "")
                                for x in json.load(f) or []]
                except ValueError:
                    return []
    return []


def with_leaks(text, hits, leaks) -> list:
    """`hits` plus a secret hit (kind gitleaks:<rule>) for every place in `text` where a value gitleaks found
    (gitleaks_hits) stands, so it is masked and listed like the built-in finds."""
    extra = []
    for rule, _, secret in leaks:
        start = text.find(secret) if len(secret) >= 4 else -1
        while start >= 0:
            extra.append(Hit("secret", "gitleaks:" + rule, start, start + len(secret), secret))
            start = text.find(secret, start + len(secret))
    return _dedupe(list(hits) + extra) if extra else hits


class Masker:
    """Same value -> same placeholder within one package; numbered per label (令牌 is a bearer or a token)."""

    LABEL = {"hkid": "身份證", "cnid": "身份证", "phone": "电话", "email": "邮箱", "card": "卡号", "person": "人名",
             "kv_secret": "密钥", "bearer": "令牌", "token": "令牌", "amount": "金额", "company": "公司",
             "contract": "合同号"}

    def __init__(self):
        self.seen = {}
        self.count = {}

    def placeholder(self, kind, value):
        key = (kind, value.lstrip("@") if kind == "person" else value)     # @李娜 and 李娜 are one person
        if key not in self.seen:
            label = "密钥" if kind.startswith("gitleaks:") else self.LABEL.get(kind, kind)
            self.count[label] = self.count.get(label, 0) + 1
            self.seen[key] = "[%s-%d]" % (label, self.count[label])
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


def review_notes(text, hits, limit=5) -> list:
    """Short quotes of the review hits for the submitter: up to 12 characters before it on the same line (never
    reaching into another hit) and the possible name; a possible password is not repeated ("密码是 …")."""
    out = []
    for h in hits:
        if h.cat != "review":
            continue
        start = max(text.rfind("\n", 0, h.start) + 1, h.start - 12)
        for k in hits:
            if k is not h and k.cat != "review" and start < k.end <= h.start:
                start = k.end
        quote = (text[start:h.start].lstrip() + (h.value if h.kind == "person" else "…")).strip()
        if quote not in out:
            out.append(quote)
        if len(out) >= limit:
            break
    return out
